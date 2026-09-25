#!/usr/bin/env bash
#############################################################################
# 05_deploy_payload.sh — deploy the headless transcription payload and VERIFY it
#
# Uploads to @PAYLOAD_STAGE:
#     transcribe_job.py          the headless payload
#     transcribe_functions.py    imported by it (`import transcribe_functions as tf`)
#     transcribe_job_spec.yaml   rendered from .template using V_PROJECT_CONFIG
#
# then downloads all three back and compares them byte-for-byte against what was sent.
#
#############################################################################
# WHY THE VERIFY STEP EXISTS
#
# On 2026-09-24 a Tier 3 validation run produced a perfect-looking result with one
# column silently empty. Cause: the payload was uploaded, then edited locally, and
# never re-uploaded. The run used the STALE staged copy - 35,840 bytes against 36,958
# local - so the fix under test was never actually exercised, while every dashboard
# signal and every other column said the run had succeeded.
#
# A successful PUT is NOT proof of deployment. This is the same lesson
# 04_deploy_notebook.sh already encodes for the notebook; the payload had no equivalent.
#
# DO NOT compare stage `size` to local size. Internal stages pad on encryption, so a
# correct upload reports 36,958 local -> 36,960 staged. A size comparison produces
# false failures, and a check that cries wolf gets ignored. Compare CONTENT.
#############################################################################
# STALE BYTECODE
#
# The stage is mounted as a volume, so a __pycache__ directory left on it can shadow
# edited source at import time. This script removes it every run.
#############################################################################

set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
CONFIG_STAGE_PATH="${CONFIG_STAGE_PATH:-@TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

for arg in "$@"; do
    case $arg in
        --help|-h)
            echo "Deploy the transcription payload to Snowflake and verify the upload."
            echo ""
            echo "Usage: $0"
            echo ""
            echo "Environment variables:"
            echo "  SNOW_CONNECTION     Snowflake connection name (default: DEMO)"
            echo "  CONFIG_STAGE_PATH   Config include path"
            echo "  PYTHON_BIN          Interpreter for rendering and verification"
            echo ""
            echo "Object names are read from the config store, never hard-coded here."
            exit 0
            ;;
    esac
done

cd "$(dirname "$0")"
CONFIG_INCLUDE="EXECUTE IMMEDIATE FROM ${CONFIG_STAGE_PATH};"

PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 not found; needed to render the spec and verify.${NC}"
    exit 1
fi

PAYLOAD_DIR="payload"
SPEC_TEMPLATE="${PAYLOAD_DIR}/transcribe_job_spec.yaml.template"
PY_FILES=("transcribe_job.py" "transcribe_functions.py")

for f in "${PY_FILES[@]}"; do
    if [ ! -f "${PAYLOAD_DIR}/${f}" ]; then
        echo -e "${RED}Error: missing ${PAYLOAD_DIR}/${f}${NC}"
        exit 1
    fi
done
if [ ! -f "$SPEC_TEMPLATE" ]; then
    echo -e "${RED}Error: missing ${SPEC_TEMPLATE}${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Payload Deployment${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Connection:  $CONNECTION"
echo "Config:      $CONFIG_STAGE_PATH"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Read object names from the config store
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Loading configuration from the config store...${NC}"
CFG_JSON="$(snow sql -q "${CONFIG_INCLUDE}
SELECT \$CONFIG_REVISION          AS CONFIG_REVISION,
       \$FQ_STAGE_PAYLOAD         AS FQ_STAGE_PAYLOAD,
       \$FQ_STAGE_AV              AS FQ_STAGE_AV,
       \$PROJECT_DB               AS PROJECT_DB,
       \$PROJECT_SCHEMA           AS PROJECT_SCHEMA,
       \$PROJECT_WH               AS PROJECT_WH,
       \$PROJECT_RESULTS_TABLE    AS PROJECT_RESULTS_TABLE,
       \$PROJECT_RUN_EVENTS_TABLE AS PROJECT_RUN_EVENTS_TABLE,
       \$PROJECT_JOB_IMAGE        AS PROJECT_JOB_IMAGE,
       \$PROJECT_JOB_SPEC_FILE    AS PROJECT_JOB_SPEC_FILE,
       \$PROJECT_LAUNCH_MODE      AS PROJECT_LAUNCH_MODE;" \
    --connection "$CONNECTION" --enable-templating NONE --format json)"

eval "$("$PYTHON_BIN" - "$CFG_JSON" <<'PYEOF'
import json, sys, shlex
try:
    parsed = json.loads(sys.argv[1])
except json.JSONDecodeError:
    sys.stderr.write("Could not parse config JSON from snow sql.\n")
    sys.exit(1)

def rows(obj):
    if isinstance(obj, dict):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from rows(item)

wanted = ["CONFIG_REVISION", "FQ_STAGE_PAYLOAD", "FQ_STAGE_AV", "PROJECT_DB",
          "PROJECT_SCHEMA", "PROJECT_WH", "PROJECT_RESULTS_TABLE",
          "PROJECT_RUN_EVENTS_TABLE", "PROJECT_JOB_IMAGE", "PROJECT_JOB_SPEC_FILE",
          "PROJECT_LAUNCH_MODE"]
hit = None
for row in rows(parsed):
    if row.get("FQ_STAGE_PAYLOAD"):
        hit = row
if hit is None:
    sys.stderr.write("Config include did not return the expected columns.\n")
    sys.exit(1)
for key in wanted:
    print("CFG_%s=%s" % (key, shlex.quote(str(hit.get(key) or ""))))
PYEOF
)"

if [ -z "${CFG_FQ_STAGE_PAYLOAD:-}" ]; then
    echo -e "${RED}Error: failed to resolve configuration.${NC}"
    exit 1
fi

echo -e "${GREEN}Config revision: ${CFG_CONFIG_REVISION}${NC}"
echo "  Payload stage: @$CFG_FQ_STAGE_PAYLOAD"
echo "  Spec file:     $CFG_PROJECT_JOB_SPEC_FILE"
echo "  Launch mode:   $CFG_PROJECT_LAUNCH_MODE"
echo ""

# A payload deploy is pointless if the gate is still wired to the notebook. Warn
# rather than fail: staging the payload before flipping the switch is a legitimate
# order of operations, and rollback deliberately leaves the payload in place.
if [ "$CFG_PROJECT_LAUNCH_MODE" != "JOB_SERVICE" ]; then
    echo -e "${YELLOW}Note: PROJECT_LAUNCH_MODE is '${CFG_PROJECT_LAUNCH_MODE}', so the gate"
    echo -e "      procedure will NOT run this payload. Deploying it anyway.${NC}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 1b: refuse to deploy against a stale staged config
# ---------------------------------------------------------------------------
# Already cd'd into scripts/ above, so resolve the helper from the current directory.
# Do NOT re-derive it from $0 here: after the cd, a relative invocation such as
# ./scripts/05_deploy_payload.sh makes dirname "$0" point at a path that no longer
# exists from the new working directory.
FRESH_CHECK="$(pwd)/check_config_fresh.sh"
if [ -x "$FRESH_CHECK" ]; then
    "$FRESH_CHECK" "$CFG_CONFIG_REVISION" "$CONFIG_STAGE_PATH"
    echo ""
else
    echo -e "${YELLOW}Note: check_config_fresh.sh not found; skipping staleness check.${NC}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 2: Render the spec from the template
# ---------------------------------------------------------------------------
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "$BUILD_DIR"' EXIT
RENDERED="${BUILD_DIR}/${CFG_PROJECT_JOB_SPEC_FILE}"

echo -e "${YELLOW}Rendering ${CFG_PROJECT_JOB_SPEC_FILE} from template...${NC}"
"$PYTHON_BIN" - "$SPEC_TEMPLATE" "$RENDERED" <<PYEOF
import sys
src, dst = sys.argv[1], sys.argv[2]
subs = {
    "__JOB_IMAGE__":               "${CFG_PROJECT_JOB_IMAGE}",
    "__PROJECT_DB__":              "${CFG_PROJECT_DB}",
    "__PROJECT_SCHEMA__":          "${CFG_PROJECT_SCHEMA}",
    "__PROJECT_WH__":              "${CFG_PROJECT_WH}",
    "__FQ_STAGE_AV__":             "${CFG_FQ_STAGE_AV}",
    "__PROJECT_RESULTS_TABLE__":   "${CFG_PROJECT_RESULTS_TABLE}",
    "__PROJECT_RUN_EVENTS_TABLE__":"${CFG_PROJECT_RUN_EVENTS_TABLE}",
    "__FQ_STAGE_PAYLOAD__":        "${CFG_FQ_STAGE_PAYLOAD}",
}
text = open(src).read()
for k, v in subs.items():
    if not v:
        sys.stderr.write("Refusing to render: %s resolved to an empty value.\n" % k)
        sys.exit(1)
    text = text.replace(k, v)

# An unsubstituted placeholder would be uploaded as a literal and fail at launch
# time, which is the worst moment to discover it.
import re
left = re.findall(r"__[A-Z_]+__", text)
if left:
    sys.stderr.write("Unsubstituted placeholders remain: %s\n" % sorted(set(left)))
    sys.exit(1)

open(dst, "w").write(text)
print("  rendered %d bytes" % len(text))
PYEOF
echo -e "${GREEN}Spec rendered${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 3: Clear stale bytecode, then upload
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Removing stale bytecode from @${CFG_FQ_STAGE_PAYLOAD}...${NC}"
snow sql -q "REMOVE @${CFG_FQ_STAGE_PAYLOAD}/__pycache__/;" \
    --connection "$CONNECTION" --enable-templating NONE >/dev/null 2>&1 || true

echo -e "${YELLOW}Uploading payload to @${CFG_FQ_STAGE_PAYLOAD}...${NC}"
for f in "${PY_FILES[@]}"; do
    snow sql -q "PUT file://$(pwd)/${PAYLOAD_DIR}/${f} @${CFG_FQ_STAGE_PAYLOAD}
        AUTO_COMPRESS = FALSE OVERWRITE = TRUE;" \
        --connection "$CONNECTION" --enable-templating NONE >/dev/null
    echo "  uploaded $f"
done
snow sql -q "PUT file://${RENDERED} @${CFG_FQ_STAGE_PAYLOAD}
    AUTO_COMPRESS = FALSE OVERWRITE = TRUE;" \
    --connection "$CONNECTION" --enable-templating NONE >/dev/null
echo "  uploaded ${CFG_PROJECT_JOB_SPEC_FILE}"
echo -e "${GREEN}Upload complete${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 4: VERIFY -- download what is actually on the stage and compare CONTENT.
# The step whose absence let a stale payload pass a full validation run.
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Verifying staged files against local...${NC}"
VERIFY_DIR="${BUILD_DIR}/verify"
mkdir -p "$VERIFY_DIR"
snow sql -q "GET @${CFG_FQ_STAGE_PAYLOAD} file://${VERIFY_DIR}/;" \
    --connection "$CONNECTION" --enable-templating NONE >/dev/null

if ! "$PYTHON_BIN" - "$VERIFY_DIR" "$PAYLOAD_DIR" "$RENDERED" "$CFG_PROJECT_JOB_SPEC_FILE" <<'PYEOF'
import hashlib, os, sys

verify_dir, payload_dir, rendered, spec_name = sys.argv[1:5]

def digest(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()

expected = {
    "transcribe_job.py":       os.path.join(payload_dir, "transcribe_job.py"),
    "transcribe_functions.py": os.path.join(payload_dir, "transcribe_functions.py"),
    spec_name:                 rendered,
}

downloaded = {}
for root, _dirs, files in os.walk(verify_dir):
    for name in files:
        downloaded[name] = os.path.join(root, name)

ok = True
for name, local_path in sorted(expected.items()):
    staged_path = downloaded.get(name)
    if staged_path is None:
        print("  FAIL %-26s not present on the stage" % name)
        ok = False
        continue
    lo, st = digest(local_path), digest(staged_path)
    if lo == st:
        print("  OK   %-26s %s" % (name, lo[:12]))
    else:
        print("  FAIL %-26s local %s != staged %s" % (name, lo[:12], st[:12]))
        print("       the stage holds a DIFFERENT file than the one you just built")
        ok = False

stale = sorted(n for n in downloaded if n.endswith(".pyc"))
if stale:
    print("  WARN bytecode still on the stage, may shadow source: %s" % stale)

sys.exit(0 if ok else 1)
PYEOF
then
    echo ""
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  DEPLOY VERIFICATION FAILED${NC}"
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}The stage does not hold what you just built. Do NOT run the"
    echo -e "pipeline: it would transcribe using the wrong code.${NC}"
    exit 1
fi

echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Payload deployed and verified${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Next:"
echo "  - If you changed 00_config.sql, re-run scripts/03_automate.sql so the gate"
echo "    procedure is rebuilt with the current values."
echo "  - Trigger a run:  EXECUTE TASK <task>;  (or use the dashboard)"
