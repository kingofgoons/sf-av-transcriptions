#!/bin/bash
#
# Deploy the Streamlit dashboard to Snowflake, and VERIFY that every deployed file
# actually matches local.
#
# Usage:
#   ./09_deploy_dashboard.sh
#   SNOW_CONNECTION=myconn ./09_deploy_dashboard.sh
#
# Sibling of 04_deploy_notebook.sh. Named 09_ rather than 05_ only to avoid renumbering
# the existing 05..08 scripts.
#
# ----------------------------------------------------------------------------
# WHY THIS SCRIPT EXISTS INSTEAD OF `snow streamlit deploy`  (2026-08-19)
# ----------------------------------------------------------------------------
#   1. OWNERSHIP. Warehouse-runtime Streamlit apps run with OWNER'S RIGHTS, so every
#      viewer executes queries with the app owner's privileges. The app is deliberately
#      owned by TRANSCRIPTION_APP_ROLE (least privilege), NOT ACCOUNTADMIN.
#      `snow streamlit deploy` creates the app as the CONNECTION's role, so running it
#      normally would silently re-own the app and restore the privilege escalation.
#
#      `GRANT OWNERSHIP ON STREAMLIT` cannot fix this after the fact - Snowflake rejects
#      it outright: "Unsupported feature GRANT/REVOKE OWNERSHIP ON STREAMLIT". An app runs
#      as the role that CREATED it, permanently. The only way to set the owner is to create
#      the object while using the target role.
#
#   2. `snow streamlit deploy --role X` fails anyway: the CLI unconditionally issues
#      "CREATE STAGE IF NOT EXISTS" even when the stage exists, so it demands CREATE STAGE
#      on the schema. Granting that would widen what a buggy app can do purely for CLI
#      convenience.
#
# ----------------------------------------------------------------------------
# WHY STALE FILES ARE REMOVED FIRST
# ----------------------------------------------------------------------------
# `CREATE OR REPLACE STREAMLIT ... FROM '@stage/dir'` COPIES whatever is in that stage
# directory into the app's embedded stage. It does not diff. So a module deleted locally
# would linger on the stage forever and keep being copied into every deploy - and a
# leftover module can shadow a real one or a stdlib name. The stage directory is therefore
# cleared before upload.
#
# REMOVE is run by the DEPLOYING role, not the app role: REMOVE is blocked inside
# owner's-rights contexts ("Unsupported statement type 'REMOVE_FILES'"), but the deploying
# connection is not owner-restricted.
#
set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_STAGE="${CONFIG_STAGE:-@TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql}"

APP_DIR="${PROJECT_DIR}/streamlit"
ENTRYPOINT="transcription_dashboard.py"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'
fail() { echo -e "${RED}$1${NC}"; exit 1; }

echo "========================================"
echo "  Deploy Streamlit dashboard"
echo "========================================"
echo "Connection: ${CONNECTION}"
echo "Source dir: ${APP_DIR}"
echo ""

[[ -d "${APP_DIR}" ]] || fail "App directory not found: ${APP_DIR}"
[[ -f "${APP_DIR}/${ENTRYPOINT}" ]] || fail "Entrypoint not found: ${APP_DIR}/${ENTRYPOINT}"

# Warehouse-runtime apps REQUIRE the entrypoint in the root of the source directory.
# Guard against someone moving it into a subdirectory.
if [[ ! -f "${APP_DIR}/${ENTRYPOINT}" ]]; then
    fail "Entrypoint must live in the ROOT of ${APP_DIR} (warehouse runtime requirement)."
fi

# `pages/` has special meaning in Streamlit (automatic multipage nav) and would silently
# restructure the app. Tab modules are named tab_*.py deliberately.
if [[ -d "${APP_DIR}/pages" ]]; then
    fail "${APP_DIR}/pages exists. Streamlit treats pages/ as automatic multipage
navigation, which restructures the app. Use flat tab_*.py modules instead."
fi

# ---------------------------------------------------------------------------
# 0. PRE-FLIGHT. Catch NAME errors before they reach the browser.
#
# `python -m compileall` proves a module PARSES; it does not prove the names it
# references exist. On 2026-08-19 the first modular deploy shipped two runtime-fatal
# bugs that compiled fine - a missing `from datetime import datetime` in sf_exports,
# and tab_browse using `session` without accepting it as a parameter. Both surfaced
# only as "Application error: name 'datetime' is not defined" in the app. This gate
# exists so that cannot happen again.
# ---------------------------------------------------------------------------
echo "Running pre-flight checks..."
PY_BIN="$(command -v python3 || command -v python)"
"${PY_BIN}" "${SCRIPT_DIR}/lint_dashboard.py" "${APP_DIR}" \
    || fail "Pre-flight failed. Fix the problems above before deploying."
echo ""

# ---------------------------------------------------------------------------
# Resolve object names from the config store. No names are hard-coded here.
# ---------------------------------------------------------------------------
echo "Reading config from ${CONFIG_STAGE}..."
CONFIG_OUT="$(snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
EXECUTE IMMEDIATE FROM ${CONFIG_STAGE};
SELECT \$FQ_SCHEMA || '|' || \$PROJECT_STREAMLIT || '|' || \$PROJECT_APP_ROLE || '|' || \$PROJECT_WH || '|' || \$PROJECT_STREAMLIT_TITLE AS CFG;
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
APP_TITLE="$(cut -d'|' -f5 <<<"${CFG}")"
FQ_APP="${FQ_SCHEMA}.${APP_NAME}"
APP_STAGE="${FQ_SCHEMA}.STREAMLIT_STAGE"
STAGE_DIR="${APP_NAME}"

MODULE_COUNT=$(find "${APP_DIR}" -maxdepth 1 -name '*.py' | wc -l | tr -d ' ')
echo "  schema:   ${FQ_SCHEMA}"
echo "  app:      ${FQ_APP}"
echo "  title:    ${APP_TITLE}"
echo "  owner:    ${APP_ROLE}"
echo "  wh:       ${APP_WH}"
echo "  modules:  ${MODULE_COUNT} .py files + .streamlit/config.toml + environment.yml"
echo ""

# ---------------------------------------------------------------------------
# 1. Clear stale staged files, then upload. AUTO_COMPRESS=FALSE keeps sources readable.
# ---------------------------------------------------------------------------
echo "Clearing stale staged files..."
snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
REMOVE '@${APP_STAGE}/${STAGE_DIR}/';
" >/dev/null 2>&1 || true   # empty directory is not an error

echo "Uploading ${MODULE_COUNT} module(s)..."
snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
PUT 'file://${APP_DIR}/*.py' '@${APP_STAGE}/${STAGE_DIR}/'
    AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
" >/dev/null

if [[ -f "${APP_DIR}/.streamlit/config.toml" ]]; then
    echo "Uploading .streamlit/config.toml..."
    snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
    PUT 'file://${APP_DIR}/.streamlit/config.toml' '@${APP_STAGE}/${STAGE_DIR}/.streamlit/'
        AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
    " >/dev/null
else
    echo -e "${YELLOW}  no .streamlit/config.toml - app will use default theme${NC}"
fi

# environment.yml pins the Python environment. It must sit in the ROOT of the source
# directory, alongside the entrypoint. WITHOUT IT SNOWFLAKE RESOLVES STREAMLIT 1.22.0 -
# the OLDEST supported version, not the newest - which breaks st.file_uploader,
# st.download_button, st.fragment and hide_index. This is a hard failure, not a warning:
# the app still loads without it, so a silent omission would ship a 2023 Streamlit.
if [[ -f "${APP_DIR}/environment.yml" ]]; then
    echo "Uploading environment.yml..."
    snow sql --connection "${CONNECTION}" --enable-templating NONE -q "
    PUT 'file://${APP_DIR}/environment.yml' '@${APP_STAGE}/${STAGE_DIR}/'
        AUTO_COMPRESS = FALSE OVERWRITE = TRUE;
    " >/dev/null
else
    echo -e "${RED}ERROR: ${APP_DIR}/environment.yml is missing.${NC}"
    echo "Without it the app silently runs Streamlit 1.22.0 and several components break."
    exit 1
fi

# ---------------------------------------------------------------------------
# 2. Recreate the app AS THE APP ROLE so ownership lands correctly.
#
#    Uses the CLI's --role flag, NOT a `USE ROLE` statement. The PAT-authenticated
#    connection rejects the statement form:
#        003107 (42501): Current session is restricted. USE ROLE not allowed.
#    Setting the role at connection time is permitted. (Confusingly, `USE ROLE` DOES work
#    through other client paths against this same account, so do not "simplify" this back
#    to a USE ROLE statement because it worked in a worksheet.)
# ---------------------------------------------------------------------------
echo "Creating app as ${APP_ROLE}..."
snow sql --connection "${CONNECTION}" --role "${APP_ROLE}" --enable-templating NONE -q "
CREATE OR REPLACE STREAMLIT ${FQ_APP}
    FROM '@${APP_STAGE}/${STAGE_DIR}'
    MAIN_FILE = '${ENTRYPOINT}'
    QUERY_WAREHOUSE = ${APP_WH}
    TITLE = '${APP_TITLE}';
" >/dev/null

# ---------------------------------------------------------------------------
# 3. VERIFY. Download EVERY file back and diff against local, and confirm the owner.
#    A silent no-op deploy must be a hard failure.
# ---------------------------------------------------------------------------
echo "Verifying deployment..."
TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

# NOTE: --database/--schema are REQUIRED here even though the stage path is fully
# qualified. Without them a recursive GET fails with:
#     090105 (22000): Cannot perform STAGE GET. This session does not have a current
#     database. Call 'USE DATABASE', or use a qualified name.
# The DEMO connection carries no default database, and a qualified stage path alone does
# not satisfy the check.
snow stage copy "@${APP_STAGE}/${STAGE_DIR}/" "${TMP}" --recursive \
    --connection "${CONNECTION}" \
    --database "$(cut -d'.' -f1 <<<"${FQ_SCHEMA}")" \
    --schema "$(cut -d'.' -f2 <<<"${FQ_SCHEMA}")" >/dev/null 2>&1 \
    || fail "VERIFY FAILED: could not download the deployed sources."

MISMATCH=0
CHECKED=0
for LOCAL_FILE in "${APP_DIR}"/*.py; do
    BASE="$(basename "${LOCAL_FILE}")"
    # snow stage copy --recursive may nest under the stage dir name
    REMOTE="$(find "${TMP}" -name "${BASE}" -type f | head -1)"
    if [[ -z "${REMOTE}" ]]; then
        echo -e "  ${RED}MISSING on stage: ${BASE}${NC}"; MISMATCH=1; continue
    fi
    if ! diff -q "${REMOTE}" "${LOCAL_FILE}" >/dev/null; then
        echo -e "  ${RED}DIFFERS: ${BASE}${NC}"
        diff "${REMOTE}" "${LOCAL_FILE}" | head -10
        MISMATCH=1
    fi
    CHECKED=$((CHECKED+1))
done

if [[ -f "${APP_DIR}/.streamlit/config.toml" ]]; then
    REMOTE_CFG="$(find "${TMP}" -name 'config.toml' -type f | head -1)"
    if [[ -z "${REMOTE_CFG}" ]]; then
        echo -e "  ${RED}MISSING on stage: .streamlit/config.toml${NC}"; MISMATCH=1
    elif ! diff -q "${REMOTE_CFG}" "${APP_DIR}/.streamlit/config.toml" >/dev/null; then
        echo -e "  ${RED}DIFFERS: .streamlit/config.toml${NC}"; MISMATCH=1
    else
        CHECKED=$((CHECKED+1))
    fi
fi

if [[ -f "${APP_DIR}/environment.yml" ]]; then
    REMOTE_ENV="$(find "${TMP}" -name 'environment.yml' -type f | head -1)"
    if [[ -z "${REMOTE_ENV}" ]]; then
        echo -e "  ${RED}MISSING on stage: environment.yml${NC}"; MISMATCH=1
    elif ! diff -q "${REMOTE_ENV}" "${APP_DIR}/environment.yml" >/dev/null; then
        echo -e "  ${RED}DIFFERS: environment.yml${NC}"; MISMATCH=1
    else
        CHECKED=$((CHECKED+1))
    fi
fi

[[ ${MISMATCH} -eq 0 ]] || fail "VERIFY FAILED: deployed sources do not match local."
echo -e "  ${GREEN}${CHECKED} file(s) match local${NC}"

# Confirm no stale extras survived the REMOVE
STAGED_PY=$(find "${TMP}" -name '*.py' -type f | wc -l | tr -d ' ')
if [[ "${STAGED_PY}" -ne "${MODULE_COUNT}" ]]; then
    echo -e "  ${YELLOW}note: stage has ${STAGED_PY} .py files, local has ${MODULE_COUNT}${NC}"
    echo -e "  ${YELLOW}      a stale module may still be present - inspect @${APP_STAGE}/${STAGE_DIR}/${NC}"
fi

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

# ---------------------------------------------------------------------------
# 4. Assert the Streamlit version pin actually took effect.
#
#    Staging environment.yml is not enough - Snowflake only picks it up when it sits in
#    the source root. If it is missed, the app silently resolves Streamlit 1.22.0 from
#    2023. The app still loads, so this failure mode is invisible without an explicit
#    check. It cost a debugging session once already; do not downgrade it to a warning.
#
#    Read USER_PACKAGES, not DEFAULT_PACKAGES. default_packages always lists a bare,
#    unpinned `streamlit` because that is the base environment - it says nothing about
#    what environment.yml asked for, and matching against it is a guaranteed false
#    positive (which is exactly what the first version of this check did).
# ---------------------------------------------------------------------------
PKG_OUT="$(snow sql --connection "${CONNECTION}" --enable-templating NONE --format json -q "
DESCRIBE STREAMLIT ${FQ_APP};
")"
USER_PKGS="$(printf '%s' "${PKG_OUT}" | /usr/bin/python3 -c "
import json,sys
data=json.load(sys.stdin)
rows=[r for blk in (data if isinstance(data,list) else [data]) for r in (blk if isinstance(blk,list) else [blk]) if isinstance(r,dict)]
for r in rows:
    if 'user_packages' in r: print(r.get('user_packages') or ''); break
")"

if [[ -z "${USER_PKGS}" ]]; then
    fail "VERIFY FAILED: the app has NO user packages, so environment.yml was not applied.
Snowflake falls back to Streamlit 1.22.0 (the OLDEST supported version), which breaks
st.file_uploader, st.download_button, st.fragment and hide_index. Confirm environment.yml
landed in the ROOT of @${APP_STAGE}/${STAGE_DIR}/ - a subdirectory is ignored silently."
fi
if ! grep -qE 'streamlit==[0-9]+\.[0-9]+' <<<"${USER_PKGS}"; then
    fail "VERIFY FAILED: streamlit is not pinned to a specific version.
  user_packages: ${USER_PKGS}
Pin it in environment.yml with '=' (conda), e.g. 'streamlit=1.52.2', and use only a version
that Streamlit-in-Snowflake supports on warehouse runtime."
fi
PINNED_VER="$(grep -oE 'streamlit==[0-9.]+' <<<"${USER_PKGS}")"
echo -e "  ${GREEN}${PINNED_VER} (was unpinned -> 1.22.0)${NC}"

echo ""
echo -e "${GREEN}Dashboard deployed and verified.${NC}"
echo ""
echo "Open it at:"
echo "  https://app.snowflake.com/SFSENORTHAMERICA/VA_DEMO149/#/streamlit-apps/${FQ_APP}"
echo ""
echo -e "${YELLOW}Note:${NC} CREATE OR REPLACE assigns a new url_id, so any previously"
echo "bookmarked direct link will 404. Navigate via Projects > Streamlit instead."
