#!/usr/bin/env bash
#
# check_config_fresh.sh — refuse to deploy against a stale staged config
#
# THE PROBLEM THIS SOLVES
#
#   Scripts read the STAGED config, not your local file. So editing 00_config.sql
#   and forgetting ./publish_config.sh means every script silently keeps using the
#   PREVIOUS values. Silent, wrong, and only detectable by a human noticing a
#   revision stamp.
#
#   That is the same failure mode the config store was created to fix. On
#   2026-08-18 the config block was copy-pasted across 7 files and drifted, leaving
#   the service role with grants on BOTH V1 and V2 objects. Centralising the values
#   fixed the copies but moved the drift to "local versus staged".
#
#   Because CONFIG_REVISION is now a hash of the SET values rather than a
#   hand-typed stamp, the two can be compared mechanically.
#
# WHEN THIS DOES NOT FAIL
#
#   If there is no local config file, the check is SKIPPED with a warning rather
#   than failed. Deploying from a fresh clone against a config someone else
#   published is legitimate: the staged copy is authoritative, and the operator may
#   have no local file at all. Only a local file that DISAGREES is an error.
#
# Usage:
#   ./check_config_fresh.sh <staged_revision> <staged_config_path>
#
# Example:
#   ./check_config_fresh.sh ead5d35d4dbc @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REVISION_TOOL="${SCRIPT_DIR}/config_revision.sh"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

if [[ $# -lt 2 ]]; then
    echo "Usage: $(basename "$0") <staged_revision> <staged_config_path>" >&2
    exit 2
fi

STAGED_REVISION="$1"
STAGED_PATH="$2"

# Derive which local file to compare against from the staged path, so a parallel
# deployment (CONFIG_STAGE_PATH=.../00_config_dev.sql) is checked against
# 00_config_dev.sql rather than always against 00_config.sql.
LOCAL_CONFIG="${SCRIPT_DIR}/$(basename "${STAGED_PATH}")"

if [[ "${STAGED_REVISION}" == "INJECTED_AT_PUBLISH" ]]; then
    echo -e "${RED}Error: the staged config carries the un-substituted sentinel.${NC}" >&2
    echo "       Its CONFIG_REVISION is literally 'INJECTED_AT_PUBLISH', which means the" >&2
    echo "       file reached the stage WITHOUT going through publish_config.sh." >&2
    echo "       Re-publish it:  ./publish_config.sh" >&2
    exit 1
fi

if [[ ! -f "${LOCAL_CONFIG}" ]]; then
    echo -e "${YELLOW}Note: no local $(basename "${LOCAL_CONFIG}") to compare against;${NC}"
    echo -e "${YELLOW}      cannot verify the staged config is current. Proceeding.${NC}"
    echo "      (Staged revision: ${STAGED_REVISION})"
    exit 0
fi

if [[ ! -x "${REVISION_TOOL}" ]]; then
    echo -e "${YELLOW}Note: ${REVISION_TOOL} missing; skipping the staleness check.${NC}"
    exit 0
fi

LOCAL_REVISION="$("${REVISION_TOOL}" "${LOCAL_CONFIG}")"

if [[ "${LOCAL_REVISION}" != "${STAGED_REVISION}" ]]; then
    echo -e "${RED}Error: the staged config is STALE.${NC}" >&2
    echo "" >&2
    echo "  Your local $(basename "${LOCAL_CONFIG}")  ->  ${LOCAL_REVISION}" >&2
    echo "  What is staged                     ->  ${STAGED_REVISION}" >&2
    echo "" >&2
    echo "  You edited your config but did not publish it. Every script reads the" >&2
    echo "  STAGED copy, so deploying now would use the OLD values while you believe" >&2
    echo "  the new ones are in effect." >&2
    echo "" >&2
    echo "  Fix:  ./publish_config.sh" >&2
    echo "" >&2
    echo "  If you intended to deploy against the staged config and not your local" >&2
    echo "  edits, move your local file aside first." >&2
    exit 1
fi

echo -e "${GREEN}Staged config is current (revision ${STAGED_REVISION}).${NC}"
