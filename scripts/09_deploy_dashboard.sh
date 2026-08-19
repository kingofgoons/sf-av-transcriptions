#!/bin/bash
#
# Deploy the Streamlit dashboard to Snowflake, and VERIFY that the deployed app
# actually contains the local code.
#
# Usage:
#   ./09_deploy_dashboard.sh
#   SNOW_CONNECTION=myconn ./09_deploy_dashboard.sh
#
# Sibling of 04_deploy_notebook.sh. Named 09_ rather than 05_ only to avoid
# renumbering the existing 05..08 scripts.
#
# ----------------------------------------------------------------------------
# WHY THIS SCRIPT EXISTS INSTEAD OF `snow streamlit deploy`  (2026-08-19)
# ----------------------------------------------------------------------------
# Two problems with the CLI path:
#
#   1. OWNERSHIP. Warehouse-runtime Streamlit apps run with OWNER'S RIGHTS, so
#      every viewer executes queries with the app owner's privileges. The app is
#      deliberately owned by TRANSCRIPTION_APP_ROLE (least privilege), NOT by
#      ACCOUNTADMIN. `snow streamlit deploy` creates the app as the CONNECTION's
#      role, so running it normally would silently re-own the app back to
#      ACCOUNTADMIN and quietly restore the privilege escalation we just removed.
#
#      `GRANT OWNERSHIP ON STREAMLIT` cannot fix this after the fact - Snowflake
#      rejects it outright:
#          "Unsupported feature GRANT/REVOKE OWNERSHIP ON STREAMLIT"
#      An app runs as the role that CREATED it, permanently. The ONLY way to set
#      the owner is to create the object while using the target role.
#
#   2. `snow streamlit deploy --role TRANSCRIPTION_APP_ROLE` fails anyway. The
#      CLI unconditionally issues "CREATE STAGE IF NOT EXISTS" even when the
#      stage already exists, so it demands CREATE STAGE on the schema:
#          003001 (42501): ... must have CREATE STAGE granted on SCHEMA ...
#      Granting CREATE STAGE to the app role would widen what a buggy or
#      compromised app can do, purely to satisfy a CLI convenience. Not worth it.
#
# So: PUT as the deploying role (which has stage WRITE), then CREATE OR REPLACE
# the app as the APP role, then verify by downloading the deployed file back and
# diffing it against local. Never trust a deploy you have not verified.
#
set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_STAGE="${CONFIG_STAGE:-@TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql}"

LOCAL_APP="${PROJECT_DIR}/transcription_dashboard.py"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
fail() { echo -e "${RED}$1${NC}"; exit 1; }

echo "========================================"
echo "  Deploy Streamlit dashboard"
echo "========================================"
echo "Connection: ${CONNECTION}"
echo "Local file: ${LOCAL_APP}"
echo ""

[[ -f "${LOCAL_APP}" ]] || fail "Local app not found: ${LOCAL_APP}"

# ---------------------------------------------------------------------------
# Resolve object names from the config store. No names are hard-coded here.
# ---------------------------------------------------------------------------
echo "Reading config from ${CONFIG_STAGE}..."
CONFIG_OUT="$(snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
EXECUTE IMMEDIATE FROM ${CONFIG_STAGE};
SELECT \$FQ_SCHEMA || '|' || \$PROJECT_STREAMLIT || '|' || \$PROJECT_APP_ROLE || '|' || \$PROJECT_WH AS CFG;
" --format json)"

CFG="$(printf '%s' "${CONFIG_OUT}" | /usr/bin/python3 -c "
import json,sys
data=json.load(sys.stdin)
rows=[r for blk in (data if isinstance(data,list) else [data]) for r in (blk if isinstance(blk,list) else [blk]) if isinstance(r,dict) and 'CFG' in r]
print(rows[-1]['CFG'] if rows else '')
")"
[[ -n "${CFG}" ]] || fail "Could not read config from ${CONFIG_STAGE}. Run ./publish_config.sh first."

FQ_SCHEMA="$(cut -d'|' -f1 <<<"${CFG}")"
APP_NAME="$(cut -d'|' -f2 <<<"${CFG}")"
APP_ROLE="$(cut -d'|' -f3 <<<"${CFG}")"
APP_WH="$(cut -d'|' -f4 <<<"${CFG}")"
FQ_APP="${FQ_SCHEMA}.${APP_NAME}"
APP_STAGE="${FQ_SCHEMA}.STREAMLIT_STAGE"
STAGE_DIR="${APP_NAME}"

echo "  schema:   ${FQ_SCHEMA}"
echo "  app:      ${FQ_APP}"
echo "  owner:    ${APP_ROLE}"
echo "  wh:       ${APP_WH}"
echo ""

# ---------------------------------------------------------------------------
# 1. Upload. Done as the deploying role, which has WRITE on the stage.
#    AUTO_COMPRESS=FALSE: the app source must stay a readable .py on the stage.
# ---------------------------------------------------------------------------
echo "Uploading app source..."
snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
PUT 'file://${LOCAL_APP}' '@${APP_STAGE}/${STAGE_DIR}/'
    AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
" >/dev/null

# ---------------------------------------------------------------------------
# 2. Recreate the app AS THE APP ROLE so ownership lands correctly.
#
#    NOTE: this uses the CLI's --role flag, NOT a `USE ROLE` statement. The
#    PAT-authenticated connection rejects the statement form:
#        003107 (42501): Current session is restricted. USE ROLE not allowed.
#    Setting the role at connection time is permitted. (Confusingly, `USE ROLE`
#    DOES work through some other client paths against this same account, so do
#    not "simplify" this back to a USE ROLE statement because it worked in a
#    worksheet.)
# ---------------------------------------------------------------------------
echo "Creating app as ${APP_ROLE}..."
snow sql --connection "${CONNECTION}" --role "${APP_ROLE}" --enable-templating NONE -q "
CREATE OR REPLACE STREAMLIT ${FQ_APP}
    FROM '@${APP_STAGE}/${STAGE_DIR}'
    MAIN_FILE = '$(basename "${LOCAL_APP}")'
    QUERY_WAREHOUSE = ${APP_WH}
    TITLE = 'transcription_dashboard_v2';
" >/dev/null

# ---------------------------------------------------------------------------
# 3. VERIFY. Download the deployed file back and diff against local, and confirm
#    the owner is the app role. A silent no-op deploy must be a hard failure.
# ---------------------------------------------------------------------------
echo "Verifying deployment..."
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

snow stage copy "@${APP_STAGE}/${STAGE_DIR}/$(basename "${LOCAL_APP}")" "${TMP}" \
    --connection "${CONNECTION}" >/dev/null 2>&1 \
    || fail "VERIFY FAILED: could not download the deployed app source."

DOWNLOADED="${TMP}/$(basename "${LOCAL_APP}")"
[[ -f "${DOWNLOADED}" ]] || fail "VERIFY FAILED: download produced no file."

if ! diff -q "${DOWNLOADED}" "${LOCAL_APP}" >/dev/null; then
    echo -e "${RED}VERIFY FAILED: deployed source differs from local.${NC}"
    diff "${DOWNLOADED}" "${LOCAL_APP}" | head -20
    exit 1
fi
echo -e "  ${GREEN}content matches local ($(wc -c < "${LOCAL_APP}") bytes)${NC}"

OWNER_OUT="$(snow sql --connection "${CONNECTION}" --enable-templating NONE --format json -q "
SHOW STREAMLITS LIKE '${APP_NAME}' IN SCHEMA ${FQ_SCHEMA};
")"
ACTUAL_OWNER="$(printf '%s' "${OWNER_OUT}" | /usr/bin/python3 -c "
import json,sys
data=json.load(sys.stdin)
rows=[r for blk in (data if isinstance(data,list) else [data]) for r in (blk if isinstance(blk,list) else [blk]) if isinstance(r,dict) and 'owner' in r]
print(rows[-1]['owner'] if rows else '')
")"

if [[ "${ACTUAL_OWNER}" != "${APP_ROLE}" ]]; then
    fail "VERIFY FAILED: app owner is '${ACTUAL_OWNER}', expected '${APP_ROLE}'.
Warehouse-runtime apps run with OWNER'S RIGHTS, so the wrong owner is a privilege
escalation, not a cosmetic problem. Ownership cannot be transferred after the fact -
the app must be recreated while using ${APP_ROLE}."
fi
echo -e "  ${GREEN}owner is ${ACTUAL_OWNER}${NC}"

echo ""
echo -e "${GREEN}Dashboard deployed and verified.${NC}"
echo ""
echo "Open it at:"
echo "  https://app.snowflake.com/SFSENORTHAMERICA/VA_DEMO149/#/streamlit-apps/${FQ_APP}"
echo ""
echo -e "${YELLOW}Note:${NC} CREATE OR REPLACE assigns a new url_id, so any previously"
echo "bookmarked direct link will 404. Navigate via Projects > Streamlit instead."
