#!/usr/bin/env bash
#
# init_config.sh — create your local deployment config from the tracked template
#
# scripts/00_config.sql.template is version controlled and holds the documented
# defaults. scripts/00_config.sql is your working copy and is GITIGNORED, so each
# installation can point at its own objects without committing local values or
# generating merge conflicts on a shared file.
#
# This mirrors the split this project already uses for
# av.uploader/config.template.json -> av.uploader/config.json.
#
# Usage:
#   ./init_config.sh                 # create scripts/00_config.sql
#   ./init_config.sh --force         # overwrite an existing copy (destructive)
#   ./init_config.sh -o 00_config_dev.sql   # a second, parallel deployment copy
#
# After this, edit your copy and run ./publish_config.sh. Nothing takes effect
# until it is published, because every script reads the STAGED copy.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="${SCRIPT_DIR}/00_config.sql.template"
OUTPUT_NAME="00_config.sql"
FORCE=false

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

while [[ $# -gt 0 ]]; do
    case "$1" in
        --force|-f) FORCE=true; shift ;;
        --output|-o) OUTPUT_NAME="$2"; shift 2 ;;
        --help|-h)
            sed -n '2,20p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
            exit 0 ;;
        *)
            echo -e "${RED}Unknown argument: $1${NC}" >&2
            echo "Try --help." >&2
            exit 2 ;;
    esac
done

TARGET="${SCRIPT_DIR}/${OUTPUT_NAME}"

echo "========================================"
echo "  Initialize deployment config"
echo "========================================"
echo ""
echo "Template: ${TEMPLATE}"
echo "Target:   ${TARGET}"
echo ""

if [[ ! -f "${TEMPLATE}" ]]; then
    echo -e "${RED}Error: template not found at ${TEMPLATE}${NC}" >&2
    echo "This file is version controlled; if it is missing, your checkout is incomplete." >&2
    exit 1
fi

# REFUSE to clobber by default. An edited config represents an operator's real
# deployment; silently replacing it with defaults would point every subsequent
# script at the wrong objects, which is worse than the inconvenience of a refusal.
if [[ -f "${TARGET}" ]] && [[ "${FORCE}" != "true" ]]; then
    echo -e "${YELLOW}${OUTPUT_NAME} already exists. Not overwriting.${NC}"
    echo ""
    echo "Current values in your copy:"
    grep -E "^SET (PROJECT_DB|PROJECT_SCHEMA|PROJECT_WH|PROJECT_COMPUTE_POOL)\s*=" \
        "${TARGET}" | sed 's/^/  /' || true
    echo ""
    echo "To edit it:            \$EDITOR ${TARGET}"
    echo "To publish changes:    ./publish_config.sh"
    echo "To start over:         ./init_config.sh --force   (discards your edits)"
    exit 1
fi

if [[ -f "${TARGET}" ]]; then
    echo -e "${YELLOW}--force given: overwriting your existing ${OUTPUT_NAME}.${NC}"
fi

cp "${TEMPLATE}" "${TARGET}"

echo -e "${GREEN}Created ${OUTPUT_NAME} from the template.${NC}"
echo ""
echo "Deployment it currently targets:"
grep -E "^SET (PROJECT_DB|PROJECT_SCHEMA|PROJECT_WH|PROJECT_COMPUTE_POOL)\s*=" \
    "${TARGET}" | sed 's/^/  /' || true
echo ""
echo "Next steps:"
echo "  1. Edit ${TARGET} for your deployment."
echo "     You do NOT need to touch CONFIG_REVISION — publish_config.sh derives it."
echo "  2. Run ./publish_config.sh to validate and stage it."
echo ""
echo "Until step 2 runs, no script sees these values: they all read the STAGED copy."
