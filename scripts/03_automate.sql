--#############################################################################
-- 03_automate.sql — create the transcription task + gating procedure
--#############################################################################
--
-- CONFIGURATION IS LOADED, NOT PASTED.
--   All PROJECT_* / FQ_* session variables come from scripts/00_config.sql, staged
--   at @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS. EXECUTE IMMEDIATE FROM runs that file
--   in THIS session, so its variables persist for the rest of this script.
--
--   Prerequisite (once per account):  scripts/01_bootstrap.sql
--   After editing config:             scripts/publish_config.sh
--
--   Check the CONFIG_REVISION echoed by the include below. If it is not the
--   revision you just edited, the staged copy is stale — re-run publish_config.sh.
--
-- ⚠️  RE-RUNNING THIS AGAINST A LIVE DEPLOYMENT RESETS OWNERSHIP AND GRANTS.
--     The CREATE OR REPLACE statements below rebuild the task and gate procedure.
--     Whatever role runs this becomes their owner, and CREATE OR REPLACE TASK drops
--     the uploader's OPERATE grant. If you re-run it, redo Step 4 (ownership) and
--     confirm the OPERATE grant survived. See DIARY.md 2026-08-18.
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

----------------------------------
----------------------------------
/*   AUTOMATION SETUP           */
----------------------------------
----------------------------------
--
-- DESIGN: event-driven. The transcription task has NO SCHEDULE and gates on real work.
--
-- `av.uploader/upload_av_files.py` fires `EXECUTE TASK` after a successful upload.
-- The task calls TRANSCRIBE_IF_NEW_FILES(), which diffs the stage against
-- TRANSCRIPTION_RESULTS and launches the GPU notebook ONLY when an untranscribed
-- media file exists. Nothing polls; nothing runs when nothing was uploaded.
--
-- EXECUTE TASK is ASYNCHRONOUS, so the uploader returns immediately rather than
-- blocking on the transcription. (EXECUTE NOTEBOOK inside the task IS synchronous.)
--
-- ⚠️  IF YOU UPLOAD TO THE STAGE WITHOUT THE UPLOADER (manual PUT, Snowsight, any
--     other client) NOTHING WILL TRANSCRIBE IT. Trigger the pipeline yourself:
--         EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
--     or inspect first without launching anything:
--         CALL TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_IF_NEW_FILES();
--
-- WHY NOT A SCHEDULE (the previous design):
--   The task used to gate on WHEN SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2'),
--   but nothing in the pipeline ever consumed that stream — the notebook dedupes
--   by diffing the stage against TRANSCRIPTION_RESULTS. The stream offset therefore
--   never advanced, so the gate passed permanently after the first upload and the
--   GPU notebook launched every 5 minutes forever.
--
--   Measured impact before this change: ~290 GPU container launches per day at
--   ~10-13 credits/day on the GPU pool, PLUS ~26 credits/day on TRANSCRIPTION_WH_V2
--   because EXECUTE NOTEBOOK holds the warehouse for the notebook's whole duration.
--   Essentially all of it did no work. Diagnosed 2026-08-18; see DIARY.md.
--
--   Removing the schedule eliminates both: no ticks means no warehouse wake-ups and
--   no idle containers. A 5-minute gate would NOT have fixed the warehouse cost,
--   because AUTO_SUSPEND is 600s against a 300s schedule — it would never suspend.
--
--   The stream had a second failure mode: STALE_AFTER is pinned at
--   (creation + DATA_RETENTION_TIME_IN_DAYS) and never advances without a
--   consuming DML, so it went stale on a fixed 14-day timer and needed periodic
--   manual recreation via 07_reset.sql.
--
-- WHY 'DIRECTORY()' AND NOT 'LS':
--   The first attempt used LS + RESULT_SCAN. It validated correctly in an anonymous
--   block but failed on CALL with:
--       Stored procedure execution error: Unsupported statement type 'LIST_FILES'
--   Per Snowflake docs, LIST/LS is not permitted in an OWNER'S RIGHTS stored
--   procedure (it is allowed under EXECUTE AS CALLER). DDL such as
--   ALTER STAGE ... REFRESH *is* permitted, and DIRECTORY() is queryable via a
--   plain SELECT. The gate therefore refreshes the directory table itself and
--   reads DIRECTORY(), which keeps the owner's-rights privilege model and folds
--   the old refresh task's only job into the gate.
--
-- OWNERSHIP MATTERS:
--   Both the task and the procedure must be owned by SYSADMIN. Tasks run with their
--   OWNER's privileges, and the notebook expects SYSADMIN. If the procedure is owned
--   by a different role than the task, the task fails with
--   "Unknown user-defined function ... TRANSCRIBE_IF_NEW_FILES".

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

-- Fully qualified names (FQ_STAGE_AV, FQ_NOTEBOOK, FQ_RESULTS, FQ_STREAM, ...) are
-- already built by 00_config.sql, loaded at the top of this script. Do not rebuild
-- them here — duplicating that logic is what let the old config block drift.


----------------------------------
-- Step 1: Create the gating procedure
----------------------------------
-- Diffs the stage against TRANSCRIPTION_RESULTS and launches the notebook only
-- when there is untranscribed media. This is the same dedup logic the notebook
-- applies internally, hoisted out so the GPU container is never started for
-- nothing.
--
-- NOTE ON ERROR HANDLING: this procedure deliberately does NOT wrap
-- EXECUTE NOTEBOOK in an EXCEPTION handler. The previous
-- RUN_TRANSCRIPTION_NOTEBOOK() ended with
--     EXCEPTION WHEN OTHER THEN RETURN 'Error executing notebook: ' || SQLERRM
-- which converted every failure into a returned string, so the task reported
-- SUCCEEDED even for a 2h15m notebook failure. Failures now propagate and are
-- visible in TASK_HISTORY. SUSPEND_TASK_AFTER_NUM_FAILURES is left at its
-- default of 10, which is unreachable in normal operation because no-op runs
-- succeed and reset the counter.
--
-- Using an anonymous block because the SQL exceeds the 256-byte session
-- variable limit.
DECLARE
    sql_cmd VARCHAR;
BEGIN
    sql_cmd := 'CREATE OR REPLACE PROCEDURE TRANSCRIBE_IF_NEW_FILES()
        RETURNS STRING
        LANGUAGE SQL
        EXECUTE AS OWNER
    AS
    DECLARE
        new_file_count INTEGER;
        msg STRING;
        ignored STRING;
    BEGIN
        ALTER STAGE ' || $FQ_STAGE_AV || ' REFRESH;

        SELECT COUNT(*) INTO :new_file_count
        FROM DIRECTORY(@' || $FQ_STAGE_AV || ') d
        LEFT JOIN ' || $FQ_RESULTS || ' t
            ON REGEXP_SUBSTR(d.RELATIVE_PATH, ''[^/]+$'') = t.FILE_NAME
        WHERE t.FILE_NAME IS NULL
          AND LOWER(REGEXP_SUBSTR(d.RELATIVE_PATH, ''[^.]+$''))
              IN (''mp3'',''wav'',''m4a'',''flac'',''aac'',''ogg'',
                  ''mp4'',''avi'',''mov'',''mkv'',''webm'',''flv'');

        IF (new_file_count = 0) THEN
            msg := ''SKIPPED: no untranscribed media in stage; GPU notebook not launched.'';
        ELSE
            EXECUTE NOTEBOOK ' || $FQ_NOTEBOOK || '();
            msg := ''LAUNCHED: notebook run for '' || new_file_count || '' new file(s).'';
        END IF;

        BEGIN
            SELECT SYSTEM$SET_RETURN_VALUE(:msg) INTO :ignored;
        EXCEPTION
            WHEN OTHER THEN
                ignored := NULL;
        END;

        RETURN msg;
    END';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- Smoke-test the gate before wiring it to the task.
-- With no new files this must return SKIPPED and must NOT start a container.
CALL TRANSCRIBE_IF_NEW_FILES();


----------------------------------
-- Step 2: Create the transcription task
----------------------------------
-- No WHEN clause and NO SCHEDULE — the task runs only via EXECUTE TASK, fired by
-- the uploader. The task body is a Snowflake Scripting block so it can publish the
-- gate's verdict to TASK_HISTORY.RETURN_VALUE. Note SYSTEM$SET_RETURN_VALUE does
-- NOT propagate when called from inside the called procedure; it must be set here.
DECLARE
    sql_cmd VARCHAR;
    fq_proc VARCHAR;
BEGIN
    fq_proc := $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.TRANSCRIBE_IF_NEW_FILES';
    sql_cmd := 'CREATE OR REPLACE TASK ' || $PROJECT_TASK_TRANSCRIBE || '
        WAREHOUSE = ' || $PROJECT_WH || '
        USER_TASK_TIMEOUT_MS = ' || $PROJECT_TASK_TIMEOUT_MS || '
    AS
    BEGIN
        LET outcome STRING;
        CALL ' || fq_proc || '() INTO :outcome;
        CALL SYSTEM$SET_RETURN_VALUE(:outcome);
    END';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- A task with no SCHEDULE never runs on its own, so it does not need to be resumed.
-- EXECUTE TASK works on a suspended task without resuming it.

-- Let the uploader service role trigger the pipeline. OPERATE is the minimum
-- privilege to run a task you do not own; the task still runs as its OWNER.
SET SQL_CMD = 'GRANT OPERATE ON TASK ' || $PROJECT_TASK_TRANSCRIBE ||
              ' TO ROLE AV_UPLOADER_SERVICE_ROLE';
EXECUTE IMMEDIATE $SQL_CMD;

-- WAREHOUSE COST — RESOLVED BY REMOVING THE SCHEDULE:
--   TRANSCRIPTION_WH_V2 burned 477 credits in the first 18 days of August, which is
--   an XS running 24/7. Cause: EXECUTE NOTEBOOK holds the warehouse for the whole
--   notebook duration, and with a launch every 5 minutes against AUTO_SUSPEND = 600s
--   the warehouse never got a chance to suspend.
--
--   With no schedule the warehouse is only woken when an upload actually triggers a
--   run, so it now suspends between real batches. If you ever reintroduce a
--   schedule, also lower AUTO_SUSPEND or the warehouse will simply stay up:
--     ALTER WAREHOUSE TRANSCRIPTION_WH_V2 SET AUTO_SUSPEND = 60;
--
--   Note `ALTER STAGE ... REFRESH` is metadata-only and does NOT wake a warehouse
--   — the V1 refresh task has run every 5 minutes since Feb for ~0.5 credits/month.
--   It is the gate's SELECT and EXECUTE NOTEBOOK that require compute.


----------------------------------
-- Step 3: Deprecated objects — stream and stage refresh task
----------------------------------
-- Neither is used by the new design. They are intentionally NOT dropped here so
-- this script stays non-destructive and the previous design remains available as
-- a rollback path. Both are left suspended/unused.
--
-- REFRESH_STAGE_DIRECTORY_TASK_V2 must stay SUSPENDED. Resuming it costs a
-- warehouse tick every 5 minutes and maintains a directory table nothing reads.
ALTER TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH) SUSPEND;

-- When you are satisfied the new gate is working, drop both. Read
-- scripts/999_teardown.sql first and confirm no transcription is in flight.
-- Dropping these also makes scripts/07_reset.sql obsolete.
--   DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH);
--   DROP STREAM IF EXISTS IDENTIFIER($PROJECT_STREAM);
--
-- The superseded launcher procedure is likewise left in place for rollback:
--   DROP PROCEDURE IF EXISTS RUN_TRANSCRIPTION_NOTEBOOK();


----------------------------------
-- Step 4: Ownership
----------------------------------
-- Both objects must be SYSADMIN-owned. If you ran the CREATE statements above as
-- ACCOUNTADMIN, transfer them or the task will fail with
-- "Unknown user-defined function ... TRANSCRIBE_IF_NEW_FILES".
-- COPY CURRENT GRANTS preserves the OPERATE grant made in Step 2.
GRANT OWNERSHIP ON PROCEDURE TRANSCRIBE_IF_NEW_FILES() TO ROLE SYSADMIN COPY CURRENT GRANTS;
GRANT OWNERSHIP ON TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) TO ROLE SYSADMIN COPY CURRENT GRANTS;


----------------------------------
-- Verification and Management Queries
----------------------------------

-- Confirm the task has NO schedule (empty 'schedule' column) and is SYSADMIN-owned
SHOW TASKS IN SCHEMA IDENTIFIER($PROJECT_SCHEMA);

-- Confirm the uploader role can trigger it
SHOW GRANTS ON TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE);

-- What the gate currently sees. Zero rows means the notebook will not launch.
ALTER STAGE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE REFRESH;
SELECT REGEXP_SUBSTR(d.RELATIVE_PATH, '[^/]+$') AS FILE_NAME,
       ROUND(d.SIZE / 1024 / 1024, 1) AS SIZE_MB,
       d.LAST_MODIFIED
FROM DIRECTORY(@TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE) d
LEFT JOIN IDENTIFIER($PROJECT_RESULTS_TABLE) t
    ON REGEXP_SUBSTR(d.RELATIVE_PATH, '[^/]+$') = t.FILE_NAME
WHERE t.FILE_NAME IS NULL
  AND LOWER(REGEXP_SUBSTR(d.RELATIVE_PATH, '[^.]+$'))
      IN ('mp3','wav','m4a','flac','aac','ogg','mp4','avi','mov','mkv','webm','flv')
ORDER BY d.LAST_MODIFIED DESC;

-- Task history. RETURN_VALUE now distinguishes SKIPPED from LAUNCHED, so you can
-- see at a glance whether a run started a GPU container.
SELECT NAME, STATE, SCHEDULED_TIME, COMPLETED_TIME,
       DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS RUN_SECS,
       ERROR_CODE, ERROR_MESSAGE, RETURN_VALUE
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => $PROJECT_TASK_TRANSCRIBE,
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())
)) ORDER BY SCHEDULED_TIME DESC;

-- GPU container launches per day. After this change, expect a handful on days
-- you uploaded files — not ~290 every day.
SELECT DATE(START_TIME) AS DAY,
       COUNT(*) AS CONTAINER_SESSIONS,
       ROUND(SUM(NOTEBOOK_EXECUTION_TIME_SECS) / 3600, 2) AS NODE_HOURS,
       ROUND(SUM(CREDITS), 2) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE NOTEBOOK_NAME = $PROJECT_NOTEBOOK
    AND START_TIME >= DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY 1 DESC;

-- Notebook execution detail (durations, credits, compute pool)
SELECT
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

-- For container-level debugging (hangs, errors, GPU metrics) see
-- scripts/08_telemetry_debug.sql.

----------------------------------
-- Task Management Commands
----------------------------------

-- Suspend / resume is irrelevant for a scheduleless task — EXECUTE TASK works either
-- way. To stop the pipeline entirely, revoke the uploader's OPERATE grant:
-- REVOKE OPERATE ON TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE) FROM ROLE AV_UPLOADER_SERVICE_ROLE;

-- Trigger a run now (this is exactly what the uploader does)
-- EXECUTE TASK IDENTIFIER($PROJECT_TASK_TRANSCRIBE);

-- Inspect what the gate would do WITHOUT launching anything
-- CALL TRANSCRIBE_IF_NEW_FILES();

-- Drop the task if needed to reset
-- DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_TRANSCRIBE);
