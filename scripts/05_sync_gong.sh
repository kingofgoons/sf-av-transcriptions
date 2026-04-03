#!/bin/bash
#
# Sync Bo Landsman's Gong calls from Snowhouse into DEMO.
#
# Usage:
#   ./05_sync_gong.sh               # Sync using default connections
#   ./05_sync_gong.sh --dry-run     # Preview MERGE SQL, no writes to DEMO
#   SNOW_CONNECTION=myconn ./05_sync_gong.sh          # Override DEMO connection
#   SNOWHOUSE_CONNECTION=mysh ./05_sync_gong.sh       # Override Snowhouse connection
#
# Environment variables:
#   SNOW_CONNECTION        DEMO account connection name (default: DEMO)
#   SNOWHOUSE_CONNECTION   Snowhouse connection name (default: snowhouse)
#
# Flow:
#   1. Run 05_sync_gong.sql against Snowhouse → JSON output to temp file
#   2. Python generates a MERGE statement from the exported rows
#   3. MERGE runs against DEMO — idempotent, safe to re-run

set -e

DEMO_CONNECTION="${SNOW_CONNECTION:-DEMO}"
SNOWHOUSE_CONNECTION="${SNOWHOUSE_CONNECTION:-snowhouse}"
TARGET="TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.GONG_CALLS_MIRROR"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EXPORT_FILE="/tmp/gong_export_$$.json"
MERGE_FILE="/tmp/gong_merge_$$.sql"

DRY_RUN=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Remove temp files on exit
cleanup() {
    rm -f "$EXPORT_FILE" "$MERGE_FILE"
}
trap cleanup EXIT

# Parse arguments
for arg in "$@"; do
    case $arg in
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --help|-h)
            echo "Sync Bo Landsman's Gong calls from Snowhouse to DEMO"
            echo ""
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --dry-run   Preview the generated MERGE SQL without writing to DEMO"
            echo "  --help      Show this message"
            echo ""
            echo "Environment variables:"
            echo "  SNOW_CONNECTION        DEMO connection name (default: DEMO)"
            echo "  SNOWHOUSE_CONNECTION   Snowhouse connection name (default: snowhouse)"
            exit 0
            ;;
    esac
done

cd "$SCRIPT_DIR"

echo -e "${GREEN}==========================================${NC}"
echo -e "${GREEN}  Gong Calls Sync: Snowhouse → DEMO${NC}"
echo -e "${GREEN}==========================================${NC}"
echo ""
echo "Snowhouse:  $SNOWHOUSE_CONNECTION"
echo "DEMO:       $DEMO_CONNECTION"
echo "Target:     $TARGET"
echo "Dry run:    $DRY_RUN"
echo ""

# --------------------------------------------------------------------------
# Step 1: Export from Snowhouse
# --------------------------------------------------------------------------
echo -e "${YELLOW}Step 1: Exporting from Snowhouse...${NC}"

snow sql -f "05_sync_gong.sql" \
    --connection "$SNOWHOUSE_CONNECTION" \
    --format json \
    --enable-templating NONE \
    > "$EXPORT_FILE"

ROW_COUNT=$(python3 -c "import json; print(len(json.load(open('$EXPORT_FILE'))))" 2>/dev/null || echo "0")

echo -e "${GREEN}Exported ${ROW_COUNT} rows${NC}"
echo ""

if [ "$ROW_COUNT" -eq 0 ]; then
    echo "Nothing to sync."
    exit 0
fi

# --------------------------------------------------------------------------
# Step 2: Generate MERGE SQL
# --------------------------------------------------------------------------
echo -e "${YELLOW}Step 2: Generating MERGE SQL...${NC}"

export EXPORT_FILE MERGE_FILE TARGET

python3 << 'PYTHON_EOF'
import json, sys, os

EXPORT_FILE = os.environ['EXPORT_FILE']
MERGE_FILE  = os.environ['MERGE_FILE']
TARGET      = os.environ['TARGET']

VARIANT_COLS = {
    'PARTICIPANTS_JSON', 'TOPICS_JSON', 'STATS_JSON',
    'RELATED_ACCOUNTS_JSON', 'RELATED_OPPORTUNITIES_JSON', 'RELATED_CONTACTS_JSON',
}
NUM_COLS = {'DURATION_SECONDS', 'CALL_SCORE', 'TALK_TIME_US_SECONDS', 'TALK_TIME_THEM_SECONDS'}
DATE_COLS = {'MEETING_DATE'}
TS_COLS   = {'CALL_START_TS'}


def sql_val(col, val):
    """Render a Python value as a Snowflake SQL literal for the given column."""
    if val is None:
        return 'NULL'

    if col in VARIANT_COLS:
        # snow CLI exports VARIANT as a plain Python string (serialised JSON).
        # Pass it straight to TRY_PARSE_JSON — do NOT json.dumps() it again,
        # or we would double-encode the string and TRY_PARSE_JSON would return NULL.
        if isinstance(val, str):
            raw = val
        else:
            # Shouldn't happen, but handle dict/list safely
            raw = json.dumps(val, ensure_ascii=False)
        return "TRY_PARSE_JSON('" + raw.replace("'", "''") + "')"

    if col in NUM_COLS:
        try:
            n = float(val)
            return str(int(n)) if n == int(n) else str(n)
        except (TypeError, ValueError):
            return 'NULL'

    if col in DATE_COLS:
        return "'" + str(val).replace("'", "''") + "'::DATE"

    if col in TS_COLS:
        return "'" + str(val).replace("'", "''") + "'::TIMESTAMP_TZ"

    # Default: VARCHAR
    return "'" + str(val).replace("'", "''") + "'"


with open(EXPORT_FILE) as f:
    raw = json.load(f)

if not isinstance(raw, list) or not raw:
    print("Error: no rows in export file", file=sys.stderr)
    sys.exit(1)

# Normalize column names to uppercase (snow CLI may vary)
rows = [{k.upper(): v for k, v in row.items()} for row in raw]
cols = list(rows[0].keys())

# Build USING clause: one SELECT per row, UNION ALL'd
source_selects = []
for row in rows:
    vals = ', '.join(sql_val(c, row.get(c)) for c in cols)
    source_selects.append('    SELECT ' + vals)

source_sql  = '\n    UNION ALL\n'.join(source_selects)
col_aliases = ', '.join(cols)

# WHEN MATCHED: update all columns except the key, then stamp SYNCED_AT
update_cols = [c for c in cols if c != 'GONG_ID']
update_set  = ',\n        '.join('target.' + c + ' = source.' + c for c in update_cols)
update_set += ',\n        target.SYNCED_AT = CURRENT_TIMESTAMP()'

# WHEN NOT MATCHED: insert all source columns + SYNCED_AT
insert_cols = ', '.join(cols) + ', SYNCED_AT'
insert_vals = ', '.join('source.' + c for c in cols) + ', CURRENT_TIMESTAMP()'

merge_sql = (
    "-- Generated by 05_sync_gong.sh — do not edit manually\n"
    "-- Rows: " + str(len(rows)) + "\n"
    "MERGE INTO " + TARGET + " AS target\n"
    "USING (\n" + source_sql + "\n"
    ") AS source (" + col_aliases + ")\n"
    "ON target.GONG_ID = source.GONG_ID\n"
    "WHEN MATCHED THEN UPDATE SET\n"
    "    " + update_set + "\n"
    "WHEN NOT MATCHED THEN INSERT (" + insert_cols + ")\n"
    "    VALUES (" + insert_vals + ");\n"
)

with open(MERGE_FILE, 'w') as f:
    f.write(merge_sql)

print("Generated MERGE SQL for " + str(len(rows)) + " rows")
PYTHON_EOF

echo ""

# --------------------------------------------------------------------------
# Step 3: Apply MERGE to DEMO (or preview on --dry-run)
# --------------------------------------------------------------------------
if [ "$DRY_RUN" = true ]; then
    echo -e "${YELLOW}Dry run — MERGE SQL preview (first 60 lines):${NC}"
    echo ""
    head -60 "$MERGE_FILE"
    echo ""
    echo -e "${YELLOW}(skipping write to DEMO)${NC}"
else
    echo -e "${YELLOW}Step 3: Merging into DEMO ($TARGET)...${NC}"
    snow sql -f "$MERGE_FILE" --connection "$DEMO_CONNECTION"
    echo -e "${GREEN}Merge complete${NC}"
fi

echo ""
echo -e "${GREEN}==========================================${NC}"
if [ "$DRY_RUN" = true ]; then
    echo -e "${GREEN}  Dry run complete — ${ROW_COUNT} rows previewed${NC}"
else
    echo -e "${GREEN}  Sync complete — ${ROW_COUNT} rows synced${NC}"
fi
echo -e "${GREEN}==========================================${NC}"
echo ""
