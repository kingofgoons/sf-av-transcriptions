--#############################################################################
-- 07_reset.sql — Reset the stage stream and re-arm the pipeline
--#############################################################################
--
-- WHAT THIS DOES
--   Recreates AV_STAGE_STREAM_V2 at the current stage offset, refreshes the
--   stage directory table, and resumes the pipeline tasks.
--
-- WHEN TO RUN IT
--   1. The stream has gone stale (or is about to — see STALE_AFTER below).
--   2. The stream is permanently armed and TRANSCRIBE_NEW_FILES_TASK_V2 is
--      launching the GPU notebook every 5 minutes with no work to do.
--
-- WHY IT IS NEEDED
--   Nothing in this pipeline consumes AV_STAGE_STREAM_V2. The notebook dedupes
--   by diffing LIST @AUDIO_VIDEO_STAGE against TRANSCRIPTION_RESULTS; it never
--   SELECTs from the stream. Two consequences follow:
--
--     a) The stream offset never advances, so SYSTEM$STREAM_HAS_DATA stays TRUE
--        forever after the first upload — the task's WHEN gate always passes.
--     b) STALE_AFTER is pinned at (stream creation + DATA_RETENTION_TIME_IN_DAYS)
--        and never moves, so the stream goes stale on a fixed 14-day timer
--        whether or not files are uploaded.
--
--   Recreating the stream resets both. This is a STOPGAP, not a fix — see the
--   note at the bottom of this file.
--
-- SAFETY
--   This script does NOT read, write, truncate, or drop TRANSCRIPTION_RESULTS.
--   Existing transcripts are untouched. CREATE OR REPLACE STREAM only resets
--   change-tracking metadata on the stage.
--
--#############################################################################
-- CONFIGURATION IS LOADED, NOT PASTED.
--   All PROJECT_* / FQ_* session variables come from scripts/00_config.sql, staged
--   at @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS.
--
--   Prerequisite (once per account):  scripts/01_bootstrap.sql
--   After editing config:             scripts/publish_config.sh
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

USE ROLE SYSADMIN;
USE WAREHOUSE IDENTIFIER($PROJECT_WH);
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);

-- Fully qualified names (FQ_STAGE_AV, FQ_STREAM, FQ_NOTEBOOK, FQ_RESULTS, ...) are
-- already built by 00_config.sql, loaded above. Do not rebuild them here.


----------------------------------
-- Step 0: Pre-flight — confirm nothing is mid-transcription
----------------------------------
-- Do NOT proceed if a notebook is actively running. NUM_JOBS > 0 or
-- ACTIVE_NODES > 0 means a transcription may be in flight; suspending the task
-- and recreating the stream underneath it risks a half-processed batch.
SHOW COMPUTE POOLS LIKE $PROJECT_COMPUTE_POOL;

-- Current state before we change anything (for the record)
SELECT
    SYSTEM$STREAM_HAS_DATA($FQ_STREAM) AS STREAM_ARMED_BEFORE,
    CURRENT_TIMESTAMP() AS RESET_STARTED_AT;

SHOW STREAMS LIKE $PROJECT_STREAM IN SCHEMA IDENTIFIER($PROJECT_SCHEMA);
-- Check the STALE and STALE_AFTER columns in the output above.


----------------------------------
-- Step 1: Suspend both tasks
----------------------------------
-- Recreating the stream while TRANSCRIBE_NEW_FILES_TASK_V2 is armed can fire a
-- notebook run mid-reset. Suspend first, resume at the end.
ALTER TASK IF EXISTS IDENTIFIER($PROJECT_TASK_TRANSCRIBE) SUSPEND;
ALTER TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH) SUSPEND;


----------------------------------
-- Step 2: Recreate the stream at the current stage offset
----------------------------------
-- This clears the permanently-armed change rows and resets the 14-day
-- staleness clock. Change history is discarded — that is the intent here,
-- since dedup is handled by TRANSCRIPTION_RESULTS, not by the stream.
SET SQL_CMD = 'CREATE OR REPLACE STREAM ' || $FQ_STREAM || ' ON STAGE ' || $FQ_STAGE_AV;
EXECUTE IMMEDIATE $SQL_CMD;


----------------------------------
-- Step 3: Refresh the stage directory table
----------------------------------
-- Re-registers current stage contents in the directory table metadata so that
-- any file uploaded while the tasks were suspended is visible.
SET SQL_CMD = 'ALTER STAGE ' || $FQ_STAGE_AV || ' REFRESH';
EXECUTE IMMEDIATE $SQL_CMD;


----------------------------------
-- Step 4: Verify the reset
----------------------------------
-- STREAM_ARMED_AFTER should be FALSE. If it is TRUE, the stage refresh in
-- Step 3 registered genuinely new files — check Step 5 to see what they are.
SELECT
    SYSTEM$STREAM_HAS_DATA($FQ_STREAM) AS STREAM_ARMED_AFTER,
    CURRENT_TIMESTAMP() AS RESET_COMPLETED_AT;

-- Confirm the new staleness deadline (should be ~14 days out)
SHOW STREAMS LIKE $PROJECT_STREAM IN SCHEMA IDENTIFIER($PROJECT_SCHEMA);


----------------------------------
-- Step 5: Is there actually any work to do?
----------------------------------
-- Authoritative check — this is the same diff the notebook performs internally.
-- If this returns zero rows, there is nothing to transcribe and you should NOT
-- run the notebook manually in Step 7.
SET SQL_CMD = '
SELECT
    REGEXP_SUBSTR(d.RELATIVE_PATH, ''[^/]+$'') AS FILE_NAME,
    ROUND(d.SIZE / 1024 / 1024, 1) AS SIZE_MB,
    d.LAST_MODIFIED
FROM DIRECTORY(@' || $FQ_STAGE_AV || ') d
LEFT JOIN ' || $FQ_RESULTS || ' t
    ON REGEXP_SUBSTR(d.RELATIVE_PATH, ''[^/]+$'') = t.FILE_NAME
WHERE t.FILE_NAME IS NULL
ORDER BY d.LAST_MODIFIED DESC';
EXECUTE IMMEDIATE $SQL_CMD;


----------------------------------
-- Step 6: Resume both tasks
----------------------------------
-- Resume the refresh task first so the directory table is being maintained
-- before the transcription gate goes live.
ALTER TASK IDENTIFIER($PROJECT_TASK_REFRESH) RESUME;
ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) RESUME;

-- Confirm both are started
SHOW TASKS IN SCHEMA IDENTIFIER($PROJECT_SCHEMA);


----------------------------------
-- Step 7: OPTIONAL — manually trigger a transcription run
----------------------------------
-- ⚠️  Leave this COMMENTED OUT unless Step 5 returned rows AND you do not want
--     to wait for the 5-minute task cycle.
--
-- ⚠️  Do NOT run this while TRANSCRIBE_NEW_FILES_TASK_V2 is resumed and armed.
--     The task and this statement would both launch the notebook, producing two
--     concurrent GPU containers processing the same files. Suspend the task
--     first, run this, then resume.
--
-- ⚠️  EXECUTE NOTEBOOK is synchronous — it blocks this worksheet until the
--     container tears down, which can be far longer than the transcription
--     itself.
--
-- ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) SUSPEND;
-- SET SQL_CMD = 'EXECUTE NOTEBOOK ' || $FQ_NOTEBOOK || '()';
-- EXECUTE IMMEDIATE $SQL_CMD;
-- ALTER TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) RESUME;


----------------------------------
-- Post-reset monitoring
----------------------------------
-- Watch for the runaway pattern this script exists to clear. If the task keeps
-- succeeding every 5 minutes with ~60-80s runtimes, the stream is armed again
-- with no real work and the GPU pool is burning credits for nothing.
SELECT NAME, STATE, SCHEDULED_TIME,
       DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SECS
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => $PROJECT_TASK_TRANSCRIBE,
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP())))
ORDER BY SCHEDULED_TIME DESC;

-- GPU container launches and credits per day. A healthy pipeline shows a
-- handful of sessions on days you uploaded files — not ~300 every day.
SELECT DATE(START_TIME) AS DAY,
       COUNT(*) AS CONTAINER_SESSIONS,
       ROUND(SUM(NOTEBOOK_EXECUTION_TIME_SECS) / 3600, 2) AS NODE_HOURS,
       ROUND(SUM(CREDITS), 2) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE NOTEBOOK_NAME = $PROJECT_NOTEBOOK
    AND START_TIME > DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY 1 DESC;


--#############################################################################
-- NOTE: THIS SCRIPT TREATS A SYMPTOM
--#############################################################################
--
-- The stream provides no value to this pipeline. Deduplication is already
-- handled by diffing the stage against TRANSCRIPTION_RESULTS (notebook Cell 17),
-- which is the same logic as Step 5 above. The stream only contributes two
-- failure modes: it arms permanently, and it goes stale on a 14-day timer.
--
-- The durable fix is to delete the stream and change the task's WHEN gate to
-- the Step 5 diff, so the GPU notebook launches only when an untranscribed file
-- actually exists:
--
--   CREATE OR REPLACE TASK TRANSCRIBE_NEW_FILES_TASK_V2
--       WAREHOUSE = TRANSCRIPTION_WH_V2
--       SCHEDULE = '5 MINUTE'
--       USER_TASK_TIMEOUT_MS = 14400000
--       WHEN (<Step 5 diff wrapped in EXISTS / COUNT(*) > 0>)
--   AS
--       CALL RUN_TRANSCRIPTION_NOTEBOOK();
--
-- Note that a task WHEN clause only accepts a boolean expression built from
-- SYSTEM$STREAM_HAS_DATA and simple scalars — it cannot run the diff query
-- directly. The practical implementation is a cheap XS-warehouse gate task
-- that runs the diff and conditionally calls RUN_TRANSCRIPTION_NOTEBOOK(),
-- replacing the WHEN clause entirely.
--
-- Once that is in place, this script becomes unnecessary and both the stream
-- and REFRESH_STAGE_DIRECTORY_TASK_V2 can be dropped.
--#############################################################################
