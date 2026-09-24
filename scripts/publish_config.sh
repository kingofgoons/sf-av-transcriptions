#!/usr/bin/env bash
#
# publish_config.sh — validate, stamp and publish your deployment config
#
# Every setup script reads config with:
#     EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;
#
# so the STAGED copy is what actually takes effect. RUN THIS AFTER EVERY EDIT TO
# your local 00_config.sql, or scripts will silently keep using the previous values.
#
# WHAT THIS DOES, IN ORDER
#   1. Drift check   — every SET in the template must exist in your working copy.
#   2. Stamp         — CONFIG_REVISION is replaced with a hash of your SET values.
#   3. Publish       — the stamped copy is PUT to the shared deployment stage.
#   4. Verify        — the staged file is executed, echoing what scripts will load.
#
# Your local file is never modified: stamping happens in a temp copy. That is why
# the local file keeps the INJECTED_AT_PUBLISH sentinel.
#
# Prerequisites:
#   ./init_config.sh has been run (creates scripts/00_config.sql)
#   scripts/01_bootstrap.sql has been run once for this account
#
# Usage:
#   ./publish_config.sh
#   SNOW_CONNECTION=OTHER ./publish_config.sh
#   CONFIG_FILE=00_config_dev.sql ./publish_config.sh    # parallel deployment
#
set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
DEPLOY_STAGE="${DEPLOY_STAGE:-TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS}"
CONFIG_FILE="${CONFIG_FILE:-00_config.sql}"
TEMPLATE_FILE="00_config.sql.template"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${SCRIPT_DIR}/${CONFIG_FILE}"
TEMPLATE_PATH="${SCRIPT_DIR}/${TEMPLATE_FILE}"
REVISION_TOOL="${SCRIPT_DIR}/config_revision.sh"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

# Stamped temp copy, cleaned up on every exit path.
STAGED_DIR=""
cleanup() { [[ -n "${STAGED_DIR}" && -d "${STAGED_DIR}" ]] && rm -rf "${STAGED_DIR}" || true; }
trap cleanup EXIT

echo "========================================"
echo "  Publish deployment config"
echo "========================================"
echo ""
echo "Connection: ${CONNECTION}"
echo "Stage:      @${DEPLOY_STAGE}"
echo "File:       ${CONFIG_PATH}"
echo ""

if [[ ! -f "${CONFIG_PATH}" ]]; then
    echo -e "${RED}Error: ${CONFIG_PATH} not found${NC}"
    echo ""
    echo "Your working config is gitignored, so a fresh clone does not have one yet."
    echo "Create it from the tracked template:"
    echo ""
    echo "    ./init_config.sh"
    echo ""
    exit 1
fi

if [[ ! -x "${REVISION_TOOL}" ]]; then
    echo -e "${RED}Error: ${REVISION_TOOL} missing or not executable${NC}"
    exit 1
fi

#############################################################################
# 1. DRIFT CHECK
#
# The one real weakness of the template-and-copy model: if the template gains a
# variable later, an existing working copy will not have it, and a script that
# references $PROJECT_NEWTHING fails at run time with an unhelpful error. Catch it
# here, by name, before anything is published.
#############################################################################
echo "Checking for template drift..."

if [[ ! -f "${TEMPLATE_PATH}" ]]; then
    echo -e "${YELLOW}Warning: ${TEMPLATE_FILE} not found; skipping drift check.${NC}"
else
    set_names() {
        { grep -oE '^SET[[:space:]]+[A-Za-z_][A-Za-z0-9_]*' "$1" || true; } \
            | awk '{print $2}' | sort -u
    }
    MISSING="$(comm -23 <(set_names "${TEMPLATE_PATH}") <(set_names "${CONFIG_PATH}") || true)"
    EXTRA="$(comm -13 <(set_names "${TEMPLATE_PATH}") <(set_names "${CONFIG_PATH}") || true)"

    if [[ -n "${MISSING}" ]]; then
        echo -e "${RED}Error: your ${CONFIG_FILE} is missing variables that ${TEMPLATE_FILE} defines:${NC}"
        echo "${MISSING}" | sed 's/^/  - /'
        echo ""
        echo "The template gained these after your copy was created. Add them to"
        echo "${CONFIG_FILE} (copy the lines and their comments across from the template),"
        echo "then re-run this script. Publishing without them would leave scripts"
        echo "referencing unset session variables."
        exit 1
    fi

    # Extras are tolerated: they may be a deliberate local addition, and refusing
    # to publish over them would be hostile. Surfaced so they are not a surprise.
    if [[ -n "${EXTRA}" ]]; then
        echo -e "${YELLOW}Note: your ${CONFIG_FILE} defines variables the template does not:${NC}"
        echo "${EXTRA}" | sed 's/^/  - /'
        echo "      Publishing anyway. Consider adding them to the template if they"
        echo "      are meant for every deployment."
    fi
    echo -e "${GREEN}No drift: every template variable is present.${NC}"
fi
echo ""

#############################################################################
# 2. STAMP THE REVISION
#
# Derived from a hash of the SET values, NOT hand-maintained. This is what makes a
# stale staged copy mechanically detectable: 04_deploy_notebook.sh and
# 09_deploy_dashboard.sh recompute it from the local file and refuse to deploy if
# it disagrees with what is staged.
#############################################################################
REVISION="$("${REVISION_TOOL}" "${CONFIG_PATH}")"

if [[ -z "${REVISION}" ]]; then
    echo -e "${RED}Error: computed an empty revision. Refusing to publish.${NC}"
    echo "04_deploy_notebook.sh requires CONFIG_REVISION to be present."
    exit 1
fi

echo "Revision:   ${REVISION}  (derived from your SET values)"
echo ""
echo "Active deployment in this config:"
grep -E "^SET (PROJECT_DB|PROJECT_SCHEMA|PROJECT_WH|PROJECT_COMPUTE_POOL)\s*=" "${CONFIG_PATH}" \
    | sed 's/^/  /' || true
echo ""

# Substitute the sentinel in a TEMP copy. The local file is left untouched so it
# stays a clean source, and so the sentinel remains as the tell-tale that a file
# reaching the stage some other way was never stamped.
#
# A temp DIRECTORY, not a temp file: the staged object must be named exactly
# ${CONFIG_FILE}, and writing that name into a private directory avoids colliding
# with a concurrent run in a shared $TMPDIR.
STAGED_DIR="$(mktemp -d -t publishcfg.XXXXXX)"
STAGED_TMP="${STAGED_DIR}/${CONFIG_FILE}"
sed "s/^SET CONFIG_REVISION = 'INJECTED_AT_PUBLISH';/SET CONFIG_REVISION = '${REVISION}';/" \
    "${CONFIG_PATH}" > "${STAGED_TMP}"

# Anchored to the SET statement on purpose. A bare search for the sentinel string
# also matches the template's own comment explaining what the sentinel is for,
# which made this guard fire on a correctly-stamped file.
if grep -qE "^SET CONFIG_REVISION = 'INJECTED_AT_PUBLISH';" "${STAGED_TMP}"; then
    echo -e "${RED}Error: the CONFIG_REVISION sentinel was not substituted.${NC}"
    echo "Expected this exact line in ${CONFIG_FILE}:"
    echo "    SET CONFIG_REVISION = 'INJECTED_AT_PUBLISH';"
    echo "Found:"
    grep -n "CONFIG_REVISION" "${CONFIG_PATH}" | sed 's/^/    /' || true
    exit 1
fi

if ! grep -q "^SET CONFIG_REVISION = '${REVISION}';" "${STAGED_TMP}"; then
    echo -e "${RED}Error: stamped file does not carry the expected revision.${NC}"
    exit 1
fi

#############################################################################
# 3. PUBLISH
#############################################################################
echo "Uploading..."
snow sql \
    -q "PUT 'file://${STAGED_TMP}' @${DEPLOY_STAGE} AUTO_COMPRESS=FALSE OVERWRITE=TRUE" \
    --connection "${CONNECTION}" \
    --enable-templating NONE

echo ""
echo "Verifying staged copy..."
snow sql \
    -q "EXECUTE IMMEDIATE FROM @${DEPLOY_STAGE}/${CONFIG_FILE}" \
    --connection "${CONNECTION}" \
    --enable-templating NONE

echo ""
echo -e "${GREEN}Config published and verified.${NC}"
echo ""
echo "The values echoed above are what every setup script will now load."
echo "Revision ${REVISION} is what 04/09 will check your local file against."
echo "If they are not what you expected, re-check ${CONFIG_FILE} and re-run this script."
