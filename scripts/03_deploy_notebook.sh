#!/bin/bash
#
# Deploy notebook to Snowflake
# Uploads the notebook file to stage and updates the live version
#
# Usage:
#   ./03_deploy_notebook.sh              # Deploy using default connection (DEMO)
#   ./03_deploy_notebook.sh --safe       # Suspend tasks during deployment
#   SNOW_CONNECTION=myconn ./03_deploy_notebook.sh  # Use specific connection
#
# Environment variables for parallel deployments:
#   PROJECT_DB           Database name (default: TRANSCRIPTION_DB)
#   PROJECT_SCHEMA       Schema name (default: TRANSCRIPTION_SCHEMA)
#   PROJECT_NOTEBOOK     Notebook name (default: TRANSCRIBE_AV_FILES)
#   PROJECT_STAGE_NB     Notebook stage name (default: NOTEBOOK_STAGE)
#   PROJECT_TASK_TRANSCRIBE  Transcription task (default: TRANSCRIBE_NEW_FILES_TASK)
#   PROJECT_TASK_REFRESH     Stage refresh task (default: REFRESH_STAGE_DIRECTORY_TASK)
#

set -e

# Connection configuration
CONNECTION="${SNOW_CONNECTION:-DEMO}"

# Project configuration - override these for parallel deployments
PROJECT_DB="${PROJECT_DB:-TRANSCRIPTION_DB}"
PROJECT_SCHEMA="${PROJECT_SCHEMA:-TRANSCRIPTION_SCHEMA}"
PROJECT_NOTEBOOK="${PROJECT_NOTEBOOK:-TRANSCRIBE_AV_FILES}"
PROJECT_STAGE_NB="${PROJECT_STAGE_NB:-NOTEBOOK_STAGE}"
PROJECT_TASK_TRANSCRIBE="${PROJECT_TASK_TRANSCRIBE:-TRANSCRIBE_NEW_FILES_TASK}"
PROJECT_TASK_REFRESH="${PROJECT_TASK_REFRESH:-REFRESH_STAGE_DIRECTORY_TASK}"

# Derived paths
NOTEBOOK_FILE="../notebooks/audio_video_transcription.ipynb"
STAGE="@${PROJECT_DB}.${PROJECT_SCHEMA}.${PROJECT_STAGE_NB}"
NOTEBOOK_FQN="${PROJECT_DB}.${PROJECT_SCHEMA}.${PROJECT_NOTEBOOK}"
TASK_TRANSCRIBE_FQN="${PROJECT_DB}.${PROJECT_SCHEMA}.${PROJECT_TASK_TRANSCRIBE}"
TASK_REFRESH_FQN="${PROJECT_DB}.${PROJECT_SCHEMA}.${PROJECT_TASK_REFRESH}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Parse arguments
SAFE_MODE=false
for arg in "$@"; do
    case $arg in
        --safe)
            SAFE_MODE=true
            shift
            ;;
        --help|-h)
            echo "Deploy notebook to Snowflake"
            echo ""
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  --safe    Suspend tasks during deployment, resume after"
            echo "  --help    Show this help message"
            echo ""
            echo "Environment variables:"
            echo "  SNOW_CONNECTION         Snowflake connection name (default: DEMO)"
            echo "  PROJECT_DB              Database name (default: TRANSCRIPTION_DB)"
            echo "  PROJECT_SCHEMA          Schema name (default: TRANSCRIPTION_SCHEMA)"
            echo "  PROJECT_NOTEBOOK        Notebook name (default: TRANSCRIBE_AV_FILES)"
            echo "  PROJECT_STAGE_NB        Notebook stage (default: NOTEBOOK_STAGE)"
            echo "  PROJECT_TASK_TRANSCRIBE Transcription task (default: TRANSCRIBE_NEW_FILES_TASK)"
            echo "  PROJECT_TASK_REFRESH    Stage refresh task (default: REFRESH_STAGE_DIRECTORY_TASK)"
            echo ""
            echo "Example for parallel deployment:"
            echo "  PROJECT_DB=TRANSCRIPTION_DEV ./03_deploy_notebook.sh"
            exit 0
            ;;
    esac
done

# Change to script directory
cd "$(dirname "$0")"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Notebook Deployment${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "Connection: $CONNECTION"
echo "Database:   $PROJECT_DB"
echo "Schema:     $PROJECT_SCHEMA"
echo "Notebook:   $PROJECT_NOTEBOOK"
echo "Stage:      $STAGE"
echo "Safe mode:  $SAFE_MODE"
echo ""

# Check if notebook file exists
if [ ! -f "$NOTEBOOK_FILE" ]; then
    echo -e "${RED}Error: Notebook file not found: $NOTEBOOK_FILE${NC}"
    exit 1
fi

# Safe mode: suspend tasks
if [ "$SAFE_MODE" = true ]; then
    echo -e "${YELLOW}Suspending tasks...${NC}"
    snow sql -q "ALTER TASK $TASK_TRANSCRIBE_FQN SUSPEND;" --connection "$CONNECTION" 2>/dev/null || true
    snow sql -q "ALTER TASK $TASK_REFRESH_FQN SUSPEND;" --connection "$CONNECTION" 2>/dev/null || true
    echo -e "${GREEN}Tasks suspended${NC}"
    echo ""
fi

# Step 1: Upload notebook to stage
echo -e "${YELLOW}Uploading notebook to stage...${NC}"
snow stage copy "$NOTEBOOK_FILE" "$STAGE" --overwrite --connection "$CONNECTION"
echo -e "${GREEN}Upload complete${NC}"
echo ""

# Step 2: Update notebook live version
echo -e "${YELLOW}Updating notebook live version...${NC}"
snow sql -q "ALTER NOTEBOOK $NOTEBOOK_FQN ADD LIVE VERSION FROM LAST;" --connection "$CONNECTION"
echo -e "${GREEN}Notebook version updated${NC}"
echo ""

# Safe mode: resume tasks
if [ "$SAFE_MODE" = true ]; then
    echo -e "${YELLOW}Resuming tasks...${NC}"
    snow sql -q "ALTER TASK $TASK_REFRESH_FQN RESUME;" --connection "$CONNECTION"
    snow sql -q "ALTER TASK $TASK_TRANSCRIBE_FQN RESUME;" --connection "$CONNECTION"
    echo -e "${GREEN}Tasks resumed${NC}"
    echo ""
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Deployment complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "View notebook in Snowsight:"
echo "  Projects > Notebooks > $PROJECT_NOTEBOOK"
