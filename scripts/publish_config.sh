#!/usr/bin/env bash
#
# publish_config.sh — publish 00_config.sql to the shared deployment stage
#
# Every setup script reads config with:
#     EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;
#
# so the staged copy is what actually takes effect. RUN THIS AFTER EVERY EDIT TO
# 00_config.sql, or scripts will silently keep using the previous values.
#
# Prerequisite: scripts/01_bootstrap.sql has been run once for this account.
#
# Usage:
#   ./publish_config.sh
#   SNOW_CONNECTION=OTHER ./publish_config.sh
#
set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
DEPLOY_STAGE="${DEPLOY_STAGE:-TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS}"
CONFIG_FILE="00_config.sql"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_PATH="${SCRIPT_DIR}/${CONFIG_FILE}"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

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
    exit 1
fi

# Surface the revision stamp so it is obvious what is being published. Scripts echo
# this same value at load time, making a stale staged copy easy to spot.
REVISION="$(grep -E "^SET CONFIG_REVISION" "${CONFIG_PATH}" | head -1 | sed "s/.*=[[:space:]]*'\([^']*\)'.*/\1/" || true)"
if [[ -z "${REVISION}" ]]; then
    echo -e "${YELLOW}Warning: no SET CONFIG_REVISION found in ${CONFIG_FILE}.${NC}"
    echo -e "${YELLOW}         Without it you cannot tell a stale staged copy from a current one.${NC}"
else
    echo "Revision:   ${REVISION}"
fi

echo ""
echo "Active deployment in this config:"
grep -E "^SET (PROJECT_DB|PROJECT_SCHEMA|PROJECT_WH|PROJECT_COMPUTE_POOL)\s*=" "${CONFIG_PATH}" \
    | sed 's/^/  /' || true
echo ""

echo "Uploading..."
snow sql \
    -q "PUT 'file://${CONFIG_PATH}' @${DEPLOY_STAGE} AUTO_COMPRESS=FALSE OVERWRITE=TRUE" \
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
echo "If they are not what you expected, re-check ${CONFIG_FILE} and re-run this script."
