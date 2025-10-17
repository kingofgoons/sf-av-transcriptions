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
GRANT OWNERSHIP ON NETWORK RULE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.allow_all_rule TO ROLE SYSADMIN;
GRANT OWNERSHIP ON NETWORK RULE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.pypi_network_rule TO ROLE SYSADMIN;

-- Grant USAGE on external access integrations to SYSADMIN
GRANT USAGE ON INTEGRATION transcription_pypi_access_integration TO ROLE SYSADMIN;
GRANT USAGE ON INTEGRATION transcription_allow_all_integration TO ROLE SYSADMIN;

-- Grant USAGE on compute pool to SYSADMIN (if not already granted)
GRANT USAGE ON COMPUTE POOL TRANSCRIPTION_GPU_POOL TO ROLE SYSADMIN;
GRANT OPERATE ON COMPUTE POOL TRANSCRIPTION_GPU_POOL TO ROLE SYSADMIN;

-- Switch to SYSADMIN for creating automation objects
USE ROLE SYSADMIN;
USE DATABASE TRANSCRIPTION_DB;
USE SCHEMA TRANSCRIPTION_SCHEMA;
USE WAREHOUSE TRANSCRIPTION_WH;

-- Step 1: Create a stream directly on the stage
-- This captures changes (new files added) to the stage
-- Note: The stage already has a directory table enabled (DIRECTORY = ENABLE = TRUE in setup.sql)
CREATE OR REPLACE STREAM AV_STAGE_STREAM
    ON STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE;

-- Step 2: Refresh the stage directory table to capture current files
-- This populates the directory table metadata with existing files
ALTER STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE REFRESH;

-- Step 3: Create a stored procedure to execute the notebook
-- Using a stored procedure with EXECUTE AS OWNER ensures proper privileges
-- Note: EXECUTE NOTEBOOK is asynchronous - it starts the notebook but doesn't wait for completion
CREATE OR REPLACE PROCEDURE RUN_TRANSCRIPTION_NOTEBOOK()
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
DECLARE
    result STRING;
BEGIN
    -- Execute the transcription notebook (asynchronous operation)
    EXECUTE NOTEBOOK TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIBE_AV_FILES();
    result := 'Notebook execution initiated at ' || CURRENT_TIMESTAMP()::STRING;
    RETURN result;
EXCEPTION
    WHEN OTHER THEN
        RETURN 'Error executing notebook: ' || SQLERRM;
END;

-- Step 4: Create a task to execute the notebook when new files are detected
CREATE OR REPLACE TASK TRANSCRIBE_NEW_FILES_TASK
    WAREHOUSE = TRANSCRIPTION_WH
    SCHEDULE = '5 MINUTE'  -- Check every 5 minutes
    WHEN SYSTEM$STREAM_HAS_DATA('TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AV_STAGE_STREAM')
AS
    CALL TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.RUN_TRANSCRIPTION_NOTEBOOK();

-- Step 5: Create a task to refresh the stage directory periodically
-- This ensures new files uploaded to the stage are detected
CREATE OR REPLACE TASK REFRESH_STAGE_DIRECTORY_TASK
    WAREHOUSE = TRANSCRIPTION_WH
    SCHEDULE = '5 MINUTE'  -- Refresh every 5 minutes
AS
    ALTER STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE REFRESH;

-- Step 6: Resume the tasks to activate them (run these manually after granting privileges)
ALTER TASK REFRESH_STAGE_DIRECTORY_TASK RESUME;
ALTER TASK TRANSCRIBE_NEW_FILES_TASK RESUME;

----------------------------------
-- Verification and Management Queries
----------------------------------

-- Check if stream has data (new files detected)
SELECT SYSTEM$STREAM_HAS_DATA('TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AV_STAGE_STREAM');

-- View current files in the stage (query the stage's built-in directory table)
SELECT * FROM DIRECTORY(@TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE) ORDER BY LAST_MODIFIED DESC;

-- View stream metadata to see what files are pending processing
SELECT * FROM TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AV_STAGE_STREAM;

-- Check task execution history (with detailed error messages and return values)
SELECT NAME, STATE, ERROR_CODE, ERROR_MESSAGE, SCHEDULED_TIME, COMPLETED_TIME, RETURN_VALUE 
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => 'TRANSCRIBE_NEW_FILES_TASK',
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
WHERE NOTEBOOK_NAME = 'TRANSCRIBE_AV_FILES'
    AND START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY START_TIME DESC;

----------------------------------
-- Task Management Commands
----------------------------------

-- Suspend tasks (run this before recreating them or when troubleshooting)
-- ALTER TASK TRANSCRIBE_NEW_FILES_TASK SUSPEND;
-- ALTER TASK REFRESH_STAGE_DIRECTORY_TASK SUSPEND;

-- Resume tasks (run this after all privileges are granted and tasks are ready)
-- ALTER TASK REFRESH_STAGE_DIRECTORY_TASK RESUME;
-- ALTER TASK TRANSCRIBE_NEW_FILES_TASK RESUME;

-- Drop stream and tasks if needed to reset
-- DROP TASK IF EXISTS TRANSCRIBE_NEW_FILES_TASK;
-- DROP TASK IF EXISTS REFRESH_STAGE_DIRECTORY_TASK;
-- DROP STREAM IF EXISTS AV_STAGE_STREAM;

