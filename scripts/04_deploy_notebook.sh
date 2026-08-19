#!/bin/bash
#
# Deploy the notebook to Snowflake, and VERIFY that the deployed live version
# actually contains the local code.
#
# Usage:
#   ./04_deploy_notebook.sh              # Deploy using default connection (DEMO)
#   ./04_deploy_notebook.sh --safe       # Suspend the transcription task during deployment
#   SNOW_CONNECTION=myconn ./04_deploy_notebook.sh
#
# All object names come from the config store (scripts/00_config.sql, published to
# @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS by scripts/publish_config.sh). This script
# deliberately hard-codes NO object names.
#
# ---------------------------------------------------------------------------
# WHY THIS SCRIPT LOOKS THE WAY IT DOES  (learned the hard way, 2026-08-18)
# ---------------------------------------------------------------------------
# The previous version of this script did:
#     PUT notebook.ipynb -> @NOTEBOOK_STAGE
#     ALTER NOTEBOOK ... ADD LIVE VERSION FROM LAST
# and reported success. It silently deployed NOTHING.
#
# Four Snowflake behaviours make the naive sequence wrong:
#
#   1. "ADD LIVE VERSION FROM LAST" restores the live version from the last
#      COMMITTED VERSION -- not from whatever file you just PUT on the stage.
#      Once any committed version exists, a PUT followed by ADD LIVE VERSION is
#      a no-op that still prints "Live version successfully created."
#      => To make staged bytes authoritative you must re-point the notebook at
#         the stage with CREATE OR REPLACE NOTEBOOK ... FROM '@stage'.
#
#   2. "COMMIT" CONSUMES the live version. After COMMIT, SHOW VERSIONS reports
#      is_live=false for every row, and EXECUTE NOTEBOOK fails with
#      "Live version is not found." You must ADD LIVE VERSION FROM LAST *again*
#      after committing to leave the notebook in a runnable state.
#
#   3. CREATE OR REPLACE NOTEBOOK drops EXTERNAL_ACCESS_INTEGRATIONS. Without
#      them the notebook cannot reach PyPI and package installs fail at runtime.
#      => They must be re-applied on every deploy.
#
#   4. CREATE OR REPLACE NOTEBOOK resets Snowflake-side version history
#      (VERSION$1..N are lost). This is an accepted trade-off: git is the real
#      source of truth for notebook history. The COMMIT below creates a fresh
#      rollback point immediately after each deploy.
#
# Because a deploy can fail silently, this script ALWAYS downloads the live
# version afterwards and compares its cell sources against the local file.
# A mismatch is a hard failure. Never trust a deploy you have not verified --
# matching the PUT byte count only proves the upload, not the live version.
#

set -euo pipefail

CONNECTION="${SNOW_CONNECTION:-DEMO}"
CONFIG_STAGE_PATH="${CONFIG_STAGE_PATH:-@TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql}"
NOTEBOOK_MAIN_FILE="audio_video_transcription.ipynb"

# The notebook MUST end up owned by the same role that owns the transcription task
# and gate procedure (SYSADMIN). CREATE OR REPLACE NOTEBOOK makes the *executing*
# role the owner, so deploying with the connection's default role (often
# ACCOUNTADMIN) silently re-owns the notebook, and the SYSADMIN-owned gate
# procedure then fails at run time with:
#   SQL compilation error: Notebook '...' does not exist or not authorized.
#
# We cannot fix this with USE ROLE: connections authenticating with a
# programmatic access token (PAT) are role-restricted and reject USE ROLE with
# "Current session is restricted. USE ROLE not allowed." So instead we transfer
# ownership after the DDL and then VERIFY the resulting owner.
OWNER_ROLE="${OWNER_ROLE:-SYSADMIN}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

SAFE_MODE=false
for arg in "$@"; do
    case $arg in
        --safe)
            SAFE_MODE=true
            ;;
        --help|-h)
            echo "Deploy the notebook to Snowflake and verify the live version."
            echo ""
            echo "Usage: $0 [--safe]"
            echo ""
            echo "Options:"
            echo "  --safe    Suspend the transcription task during deployment, resume after"
            echo "  --help    Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  SNOW_CONNECTION     Snowflake connection name (default: DEMO)"
            echo "  OWNER_ROLE          Role that must own the notebook (default: SYSADMIN)"
            echo "  CONFIG_STAGE_PATH   Config include path"
            echo "                      (default: @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql)"
            echo ""
            echo "Object names are read from the config store, never hard-coded here."
            exit 0
            ;;
    esac
done

cd "$(dirname "$0")"
NOTEBOOK_FILE="../notebooks/${NOTEBOOK_MAIN_FILE}"
CONFIG_INCLUDE="EXECUTE IMMEDIATE FROM ${CONFIG_STAGE_PATH};"

if [ ! -f "$NOTEBOOK_FILE" ]; then
    echo -e "${RED}Error: Notebook file not found: $NOTEBOOK_FILE${NC}"
    exit 1
fi

# Resolve a python interpreter for the verification step.
# NOTE: must be Python 3.9+ so that __file__ / json handling behave as expected.
PYTHON_BIN="${PYTHON_BIN:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
    echo -e "${RED}Error: python3 not found; needed to verify the deploy.${NC}"
    exit 1
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Notebook Deployment${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Connection:  $CONNECTION"
echo "Owner role:  $OWNER_ROLE"
echo "Config:      $CONFIG_STAGE_PATH"
echo "Notebook:    $NOTEBOOK_FILE"
echo "Safe mode:   $SAFE_MODE"
echo ""

# ---------------------------------------------------------------------------
# Step 1: Read object names from the config store
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Loading configuration from the config store...${NC}"
CFG_JSON="$(snow sql -q "${CONFIG_INCLUDE}
SELECT \$CONFIG_REVISION      AS CONFIG_REVISION,
       \$FQ_NOTEBOOK          AS FQ_NOTEBOOK,
       \$FQ_STAGE_NB          AS FQ_STAGE_NB,
       \$FQ_TASK              AS FQ_TASK,
       \$PROJECT_WH           AS PROJECT_WH,
       \$PROJECT_COMPUTE_POOL AS PROJECT_COMPUTE_POOL,
       \$PROJECT_PYPI_INTEGRATION      AS PYPI_INTEGRATION,
       \$PROJECT_ALLOW_ALL_INTEGRATION AS ALLOW_ALL_INTEGRATION;" \
    --connection "$CONNECTION" --enable-templating NONE --format json)"

# The include emits its own result sets; take the last row that carries our keys.
eval "$("$PYTHON_BIN" - "$CFG_JSON" <<'PYEOF'
import json, sys, shlex
raw = sys.argv[1]
try:
    parsed = json.loads(raw)
except json.JSONDecodeError:
    sys.stderr.write("Could not parse config JSON from snow sql.\n")
    sys.exit(1)

# snow sql may return a list of result sets, or a flat list of rows.
def rows(obj):
    if isinstance(obj, dict):
        yield obj
    elif isinstance(obj, list):
        for item in obj:
            yield from rows(item)

wanted = ["CONFIG_REVISION", "FQ_NOTEBOOK", "FQ_STAGE_NB", "FQ_TASK",
          "PROJECT_WH", "PROJECT_COMPUTE_POOL", "PYPI_INTEGRATION",
          "ALLOW_ALL_INTEGRATION"]
hit = None
for row in rows(parsed):
    if "FQ_NOTEBOOK" in row and row.get("FQ_NOTEBOOK"):
        hit = row
if hit is None:
    sys.stderr.write("Config include did not return the expected columns.\n")
    sys.exit(1)
for key in wanted:
    print("CFG_%s=%s" % (key, shlex.quote(str(hit.get(key) or ""))))
PYEOF
)"

if [ -z "${CFG_FQ_NOTEBOOK:-}" ]; then
    echo -e "${RED}Error: failed to resolve configuration.${NC}"
    exit 1
fi

echo -e "${GREEN}Config revision: ${CFG_CONFIG_REVISION}${NC}"
echo "  Notebook:     $CFG_FQ_NOTEBOOK"
echo "  Stage:        @$CFG_FQ_STAGE_NB"
echo "  Warehouse:    $CFG_PROJECT_WH"
echo "  Compute pool: $CFG_PROJECT_COMPUTE_POOL"
echo ""

# ---------------------------------------------------------------------------
# Step 2 (optional): suspend the task so a deploy cannot race a running job
# ---------------------------------------------------------------------------
if [ "$SAFE_MODE" = true ]; then
    echo -e "${YELLOW}Suspending task $CFG_FQ_TASK...${NC}"
    snow sql -q "ALTER TASK ${CFG_FQ_TASK} SUSPEND;" \
        --connection "$CONNECTION" --enable-templating NONE >/dev/null 2>&1 || true
    echo -e "${GREEN}Task suspended${NC}"
    echo ""
fi

# ---------------------------------------------------------------------------
# Step 3: Upload the notebook to the stage
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Uploading notebook to @${CFG_FQ_STAGE_NB}...${NC}"
snow sql -q "PUT file://$(pwd)/${NOTEBOOK_FILE#./} @${CFG_FQ_STAGE_NB}
    AUTO_COMPRESS = FALSE OVERWRITE = TRUE;" \
    --connection "$CONNECTION" --enable-templating NONE
echo -e "${GREEN}Upload complete${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 4: Make the staged bytes authoritative, then leave a live version present
#
# Sequence matters -- see the header notes. CREATE OR REPLACE re-points the
# notebook at the stage; ADD LIVE VERSION then builds live from those files;
# COMMIT snapshots it as a rollback point but REMOVES live, so we add live once
# more to leave the notebook runnable by EXECUTE NOTEBOOK.
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Deploying staged file as the live version...${NC}"
snow sql -q "${CONFIG_INCLUDE}
EXECUTE IMMEDIATE \$\$
DECLARE
    sql_cmd VARCHAR;
BEGIN
    sql_cmd := 'CREATE OR REPLACE NOTEBOOK ' || \$FQ_NOTEBOOK ||
               ' FROM ''@' || \$FQ_STAGE_NB || '''' ||
               ' MAIN_FILE = ''${NOTEBOOK_MAIN_FILE}''' ||
               ' QUERY_WAREHOUSE = ''' || \$PROJECT_WH || '''' ||
               ' COMPUTE_POOL = ''' || \$PROJECT_COMPUTE_POOL || '''' ||
               ' RUNTIME_NAME = ''SYSTEM\$GPU_RUNTIME''';
    EXECUTE IMMEDIATE sql_cmd;

    -- CREATE OR REPLACE drops these; without them PyPI installs fail at runtime.
    sql_cmd := 'ALTER NOTEBOOK ' || \$FQ_NOTEBOOK ||
               ' SET EXTERNAL_ACCESS_INTEGRATIONS = (''' ||
               \$PROJECT_PYPI_INTEGRATION || ''', ''' ||
               \$PROJECT_ALLOW_ALL_INTEGRATION || ''')';
    EXECUTE IMMEDIATE sql_cmd;

    -- Build live from the staged files.
    EXECUTE IMMEDIATE 'ALTER NOTEBOOK ' || \$FQ_NOTEBOOK || ' ADD LIVE VERSION FROM LAST';

    -- Snapshot a rollback point. NOTE: this REMOVES the live version.
    EXECUTE IMMEDIATE 'ALTER NOTEBOOK ' || \$FQ_NOTEBOOK || ' COMMIT';

    -- Re-establish live from the commit we just made, or EXECUTE NOTEBOOK will
    -- fail with 'Live version is not found.'
    EXECUTE IMMEDIATE 'ALTER NOTEBOOK ' || \$FQ_NOTEBOOK || ' ADD LIVE VERSION FROM LAST';

    -- Hand the notebook back to the role that owns the task and gate procedure.
    -- Without this the gate procedure cannot see the notebook at all.
    BEGIN
        EXECUTE IMMEDIATE 'GRANT OWNERSHIP ON NOTEBOOK ' || \$FQ_NOTEBOOK ||
                          ' TO ROLE ${OWNER_ROLE} COPY CURRENT GRANTS';
    EXCEPTION
        WHEN OTHER THEN
            RETURN 'deployed, but OWNERSHIP TRANSFER TO ${OWNER_ROLE} FAILED';
    END;

    RETURN 'deployed and committed';
END;
\$\$;" \
    --connection "$CONNECTION" --enable-templating NONE
echo -e "${GREEN}Live version created and committed${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 5: VERIFY -- download the live version and compare cell sources.
# This is the step whose absence let a broken deploy look successful.
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Verifying deployed live version...${NC}"
VERIFY_DIR="$(mktemp -d)"
trap 'rm -rf "$VERIFY_DIR"' EXIT

snow sql -q "GET 'snow://notebook/${CFG_FQ_NOTEBOOK}/versions/live/${NOTEBOOK_MAIN_FILE}' 'file://${VERIFY_DIR}/';" \
    --connection "$CONNECTION" --enable-templating NONE >/dev/null

if ! "$PYTHON_BIN" - "$VERIFY_DIR" "$NOTEBOOK_FILE" <<'PYEOF'
import glob, json, os, sys

verify_dir, local_path = sys.argv[1], sys.argv[2]
found = glob.glob(os.path.join(verify_dir, "**", "*.ipynb"), recursive=True)
if not found:
    print("FAIL: could not download the live version for verification.")
    sys.exit(1)

def sources(path):
    with open(path) as fh:
        nb = json.load(fh)
    return [("".join(c.get("source", ""))) for c in nb["cells"]]

live, local = sources(found[0]), sources(local_path)
if live == local:
    print("OK: live version matches local file (%d cells)." % len(live))
    sys.exit(0)

print("FAIL: deployed live version does NOT match the local notebook.")
print("  local cells: %d" % len(local))
print("  live  cells: %d" % len(live))
if len(live) == len(local):
    for i, (a, b) in enumerate(zip(local, live)):
        if a != b:
            print("  first differing cell index: %d" % i)
            break
sys.exit(1)
PYEOF
then
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  DEPLOY VERIFICATION FAILED${NC}"
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}The notebook in Snowflake does not match your local file.${NC}"
    echo -e "${RED}Do NOT assume the deploy worked. Investigate before running.${NC}"
    exit 1
fi
echo -e "${GREEN}Verification passed${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 5b: VERIFY OWNERSHIP.
# A notebook owned by the wrong role compiles fine but fails at run time with
# "Notebook '...' does not exist or not authorized" from the gate procedure.
# ---------------------------------------------------------------------------
echo -e "${YELLOW}Verifying notebook ownership...${NC}"
NB_SHORT="${CFG_FQ_NOTEBOOK##*.}"
NB_SCHEMA="${CFG_FQ_NOTEBOOK%.*}"
ACTUAL_OWNER="$(snow sql -q "SHOW NOTEBOOKS LIKE '${NB_SHORT}' IN SCHEMA ${NB_SCHEMA};" \
    --connection "$CONNECTION" --enable-templating NONE --format json 2>/dev/null \
    | "$PYTHON_BIN" -c "
import json,sys
def rows(o):
    if isinstance(o, dict): yield o
    elif isinstance(o, list):
        for i in o: yield from rows(i)
try:
    data = json.load(sys.stdin)
except Exception:
    print(''); sys.exit(0)
for r in rows(data):
    if r.get('name'): print(r.get('owner') or ''); break
")"

if [ "$ACTUAL_OWNER" != "$OWNER_ROLE" ]; then
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}  OWNERSHIP VERIFICATION FAILED${NC}"
    echo -e "${RED}========================================${NC}"
    echo -e "${RED}Notebook owner is '${ACTUAL_OWNER:-unknown}', expected '${OWNER_ROLE}'.${NC}"
    echo -e "${RED}The transcription task will fail with${NC}"
    echo -e "${RED}  \"Notebook '...' does not exist or not authorized\".${NC}"
    echo -e "${RED}Fix with:${NC}"
    echo -e "${RED}  GRANT OWNERSHIP ON NOTEBOOK ${CFG_FQ_NOTEBOOK} TO ROLE ${OWNER_ROLE} COPY CURRENT GRANTS;${NC}"
    exit 1
fi
echo -e "${GREEN}Owner is ${ACTUAL_OWNER}${NC}"
echo ""

# ---------------------------------------------------------------------------
# Step 6 (optional): resume the task
# ---------------------------------------------------------------------------
if [ "$SAFE_MODE" = true ]; then
    echo -e "${YELLOW}Resuming task $CFG_FQ_TASK...${NC}"
    # The transcription task is intentionally schedule-less; RESUME is a no-op
    # for scheduling but restores it to a non-suspended state.
    snow sql -q "ALTER TASK ${CFG_FQ_TASK} RESUME;" \
        --connection "$CONNECTION" --enable-templating NONE >/dev/null 2>&1 || true
    echo -e "${GREEN}Task resumed${NC}"
    echo ""
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Deployment complete and verified!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "View notebook in Snowsight:"
echo "  Projects > Notebooks > ${CFG_FQ_NOTEBOOK##*.}"
echo ""
echo "Trigger a run:"
echo "  EXECUTE TASK ${CFG_FQ_TASK};"
