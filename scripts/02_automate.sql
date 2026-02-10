--#############################################################################
-- IMPORTANT: Copy and paste the configuration block from 00_config.sql here
-- before running this script. This allows you to deploy parallel instances.
--#############################################################################

-- Core naming - change these to create a parallel deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH';              -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL';  -- GPU compute pool name

-- Derived names (automatically built from above)
SET PROJECT_NOTEBOOK = 'TRANSCRIBE_AV_FILES';     -- Notebook name
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STAGE_NB = 'NOTEBOOK_STAGE';          -- Stage for notebook assets -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STREAM = 'AV_STAGE_STREAM';           -- Stream for file detection
SET PROJECT_TASK_TRANSCRIBE = 'TRANSCRIBE_NEW_FILES_TASK';  -- Transcription task
SET PROJECT_TASK_REFRESH = 'REFRESH_STAGE_DIRECTORY_TASK';  -- Stage refresh task

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule';
SET PROJECT_PYPI_RULE = 'pypi_network_rule';

--#############################################################################
-- END CONFIGURATION
--#############################################################################

----------------------------------
----------------------------------
/*   AUTOMATION SETUP           */
----------------------------------
----------------------------------

-- First, ensure SYSADMIN has necessary privileges on integrations
USE ROLE ACCOUNTADMIN;

-- Grant EXECUTE TASK privilege at the account level (required for task owners)
GRANT EXECUTE TASK ON ACCOUNT TO ROLE SYSADMIN;

-- Grant ownership of network rules to SYSADMIN (if not already owned)
GRANT OWNERSHIP ON NETWORK RULE IDENTIFIER($PROJECT_ALLOW_ALL_RULE) TO ROLE SYSADMIN;
GRANT OWNERSHIP ON NETWORK RULE IDENTIFIER($PROJECT_PYPI_RULE) TO ROLE SYSADMIN;

-- Grant USAGE on external access integrations to SYSADMIN
GRANT USAGE ON INTEGRATION IDENTIFIER($PROJECT_PYPI_INTEGRATION) TO ROLE SYSADMIN;
GRANT USAGE ON INTEGRATION IDENTIFIER($PROJECT_ALLOW_ALL_INTEGRATION) TO ROLE SYSADMIN;

-- Grant USAGE on compute pool to SYSADMIN (if not already granted)
GRANT USAGE ON COMPUTE POOL IDENTIFIER($PROJECT_COMPUTE_POOL) TO ROLE SYSADMIN;
GRANT OPERATE ON COMPUTE POOL IDENTIFIER($PROJECT_COMPUTE_POOL) TO ROLE SYSADMIN;

-- Switch to SYSADMIN for creating automation objects
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);
USE WAREHOUSE IDENTIFIER($PROJECT_WH);

-- Build fully qualified names for use in statements that don't support IDENTIFIER()
SET FQ_STAGE_AV = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STAGE_AV;
SET FQ_STREAM = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STREAM;
SET FQ_NOTEBOOK = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_NOTEBOOK;

-- Step 1: Create a stream directly on the stage
-- This captures changes (new files added) to the stage
-- Note: The stage already has a directory table enabled (DIRECTORY = ENABLE = TRUE in 01_setup.sql)
SET SQL_CMD = 'CREATE OR REPLACE STREAM ' || $PROJECT_STREAM || ' ON STAGE ' || $FQ_STAGE_AV;
EXECUTE IMMEDIATE $SQL_CMD;

-- Step 2: Refresh the stage directory table to capture current files
-- This populates the directory table metadata with existing files
SET SQL_CMD = 'ALTER STAGE ' || $FQ_STAGE_AV || ' REFRESH';
EXECUTE IMMEDIATE $SQL_CMD;

-- Step 3: Create a stored procedure to execute the notebook
-- Using a stored procedure with EXECUTE AS OWNER ensures proper privileges
-- Note: EXECUTE NOTEBOOK is asynchronous - it starts the notebook but doesn't wait for completion
-- Using anonymous block because SQL exceeds 256-byte session variable limit
DECLARE
    sql_cmd VARCHAR;
BEGIN
    sql_cmd := 'CREATE OR REPLACE PROCEDURE RUN_TRANSCRIPTION_NOTEBOOK()
        RETURNS STRING
        LANGUAGE SQL
        EXECUTE AS OWNER
    AS
    DECLARE
        result STRING;
    BEGIN
        EXECUTE NOTEBOOK ' || $FQ_NOTEBOOK || '();
        result := ''Notebook execution initiated at '' || CURRENT_TIMESTAMP()::STRING;
        RETURN result;
    EXCEPTION
        WHEN OTHER THEN
            RETURN ''Error executing notebook: '' || SQLERRM;
    END';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- Step 4: Create a task to execute the notebook when new files are detected
-- Using anonymous block because SQL exceeds 256-byte session variable limit
DECLARE
    sql_cmd VARCHAR;
    fq_proc VARCHAR;
BEGIN
    fq_proc := $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.RUN_TRANSCRIPTION_NOTEBOOK';
    sql_cmd := 'CREATE OR REPLACE TASK ' || $PROJECT_TASK_TRANSCRIBE || '
        WAREHOUSE = ' || $PROJECT_WH || '
        SCHEDULE = ''5 MINUTE''
        WHEN SYSTEM$STREAM_HAS_DATA(''' || $FQ_STREAM || ''')
    AS
        CALL ' || fq_proc || '()';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- Step 5: Create a task to refresh the stage directory periodically
-- This ensures new files uploaded to the stage are detected
DECLARE
    sql_cmd VARCHAR;
BEGIN
    sql_cmd := 'CREATE OR REPLACE TASK ' || $PROJECT_TASK_REFRESH || '
        WAREHOUSE = ' || $PROJECT_WH || '
        SCHEDULE = ''5 MINUTE''
    AS
        ALTER STAGE ' || $FQ_STAGE_AV || ' REFRESH';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- Step 6: Resume the tasks to activate them (run these manually after granting privileges)
ALTER TASK IDENTIFIER($PROJECT_TASK_REFRESH) RESUME;
ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) RESUME;

----------------------------------
-- Verification and Management Queries
----------------------------------

-- Check if stream has data (new files detected)
SELECT SYSTEM$STREAM_HAS_DATA($FQ_STREAM);

-- View current files in the stage (query the stage's built-in directory table)
-- Note: Dynamic SQL needed for DIRECTORY() function
SET SQL_CMD = 'SELECT * FROM DIRECTORY(@' || $FQ_STAGE_AV || ') ORDER BY LAST_MODIFIED DESC';
EXECUTE IMMEDIATE $SQL_CMD;

-- View stream metadata to see what files are pending processing
SELECT * FROM IDENTIFIER($PROJECT_STREAM);

-- Check task execution history (with detailed error messages and return values)
SELECT NAME, STATE, ERROR_CODE, ERROR_MESSAGE, SCHEDULED_TIME, COMPLETED_TIME, RETURN_VALUE 
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => $PROJECT_TASK_TRANSCRIBE,
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())
)) ORDER BY SCHEDULED_TIME DESC;

-- Check notebook execution history to verify notebooks are actually running
-- Note: EXECUTE NOTEBOOK is asynchronous, so the task completes immediately but the notebook may still be running
-- This query shows the actual notebook execution status and completion (requires ACCOUNTADMIN or appropriate grants)
SELECT 
    --*
    NOTEBOOK_NAME,
    START_TIME,
    END_TIME,
    DATEDIFF('second', START_TIME, END_TIME) as DURATION_SECONDS,
    NOTEBOOK_EXECUTION_TIME_SECS AS SECS_RUN_THIS_HOUR,
    CREDITS AS CREDITS_USED_IN_THE_HOUR,
    COMPUTE_POOL_NAME
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE NOTEBOOK_NAME = $PROJECT_NOTEBOOK
    AND START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY START_TIME DESC;

----------------------------------
-- Task Management Commands
----------------------------------

-- Suspend tasks (run this before recreating them or when troubleshooting)
-- ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) SUSPEND;
-- ALTER TASK IDENTIFIER($PROJECT_TASK_REFRESH) SUSPEND;

-- Resume tasks (run this after all privileges are granted and tasks are ready)
-- ALTER TASK IDENTIFIER($PROJECT_TASK_REFRESH) RESUME;
-- ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) RESUME;

-- Drop stream and tasks if needed to reset
-- DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_TRANSCRIBE);
-- DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH);
-- DROP STREAM IF EXISTS IDENTIFIER($PROJECT_STREAM);
