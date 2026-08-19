--#############################################################################
-- 02_setup.sql — create all Snowflake objects for a deployment
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
--#############################################################################
-- IDEMPOTENT: SAFE TO RE-RUN ON A LIVE DEPLOYMENT (as of 2026-08-18)
--#############################################################################
--
-- This script used to be destructive. It used CREATE OR REPLACE on the database,
-- schema, media stage and results table, so a second run silently discarded every
-- transcript (441 rows / 250 hours) and every staged media file (325). That is fixed.
--
-- THE RULE APPLIED HERE: STATEFUL objects use IF NOT EXISTS. STATELESS definitions
-- keep CREATE OR REPLACE, because you WANT a re-run to pick up edits to them.
--
--   IF NOT EXISTS (holds data / history - never replace):
--     DATABASE                  the whole deployment
--     SCHEMA                    every object in it
--     AUDIO_VIDEO_STAGE         uploaded media
--     NOTEBOOK_STAGE            backs the notebook's VERSION$n history
--     TRANSCRIPTION_RESULTS     the transcripts
--     COMPUTE POOL              dropping it kills in-flight transcription
--     NOTEBOOK                  owns VERSION$n + live version
--
--   CREATE OR REPLACE (pure definition - refresh on every run):
--     NETWORK RULEs             egress allow-lists
--     EXTERNAL ACCESS INTEGRATIONs
--     FILE FORMAT CSVFORMAT
--     VIEW TRANSCRIPTION_SUMMARY
--     WAREHOUSE                 (already IF NOT EXISTS)
--
-- CONSEQUENCE TO KNOW ABOUT:
--   Because TRANSCRIPTION_RESULTS is now IF NOT EXISTS, adding a column to the DDL
--   below does NOT change an existing table. Evolve a live deployment with explicit
--   ALTER TABLE statements (see migration/), not by re-running this script.
--
--   Likewise, changing MIN/MAX_NODES or INSTANCE_FAMILY here does not resize an
--   existing compute pool - use ALTER COMPUTE POOL.
--
-- Object names come from 00_config.sql, so pointing PROJECT_DB at a new name and
-- re-running is how you create a parallel deployment.
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

USE ROLE SYSADMIN;

-- Create warehouse, database, and schema for transcription project
CREATE WAREHOUSE IF NOT EXISTS IDENTIFIER($PROJECT_WH)
  WAREHOUSE_SIZE = 'XSMALL'
  AUTO_SUSPEND = 60
  AUTO_RESUME = TRUE
  STATEMENT_TIMEOUT_IN_SECONDS = 14400;  -- 4 hours: EXECUTE NOTEBOOK blocks until completion

-- STATEFUL: holds the schema, stages and transcripts. IF NOT EXISTS, never REPLACE.
CREATE DATABASE IF NOT EXISTS IDENTIFIER($PROJECT_DB);

-- Increase data retention to 14 days to prevent streams from going stale.
-- Default is 1 day, which is too short — if the stream isn't consumed within
-- the retention window (e.g., task errors, warehouse suspension, or no new files),
-- the stream's offset expires and it becomes permanently stale, requiring a
-- manual recreate + backfill. 14 days provides a comfortable buffer.
ALTER DATABASE IDENTIFIER($PROJECT_DB) SET DATA_RETENTION_TIME_IN_DAYS = 14;

-- STATEFUL: contains the results table and stages. IF NOT EXISTS, never REPLACE.
CREATE SCHEMA IF NOT EXISTS IDENTIFIER($PROJECT_SCHEMA);

USE WAREHOUSE IDENTIFIER($PROJECT_WH);
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);

----------------------------------
----------------------------------
/* NOTEBOOK AND COMPUTE SETUP */
----------------------------------
----------------------------------
USE ROLE ACCOUNTADMIN;

-- Create GPU compute pool for Whisper transcription.
-- IF NOT EXISTS rather than DROP + CREATE: dropping the pool kills any in-flight
-- transcription, and agents.md forbids dropping it without first confirming none is
-- running. To change MIN/MAX_NODES or INSTANCE_FAMILY on an existing pool, use
-- ALTER COMPUTE POOL instead of re-running this script.
CREATE COMPUTE POOL IF NOT EXISTS IDENTIFIER($PROJECT_COMPUTE_POOL)
        MIN_NODES = 1
        MAX_NODES = 3
        INSTANCE_FAMILY = GPU_NV_S; -- May need to change this based on region

-- Create network rules for external access (fully qualified with variables)
-- Note: Network rules live in the database/schema, integrations are account-level
CREATE OR REPLACE NETWORK RULE IDENTIFIER($PROJECT_ALLOW_ALL_RULE)
          TYPE = HOST_PORT
          MODE = EGRESS
          VALUE_LIST = ('0.0.0.0:443','0.0.0.0:80');

-- Use dynamic SQL for integrations (IDENTIFIER() not supported in ALLOWED_NETWORK_RULES).
-- FQ_ALLOW_ALL_RULE comes from 00_config.sql.
SET SQL_CMD = 'CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION ' || $PROJECT_ALLOW_ALL_INTEGRATION || 
              ' ALLOWED_NETWORK_RULES = (' || $FQ_ALLOW_ALL_RULE || ') ENABLED = TRUE';
EXECUTE IMMEDIATE $SQL_CMD;

CREATE OR REPLACE NETWORK RULE IDENTIFIER($PROJECT_PYPI_RULE)
          TYPE = HOST_PORT
          MODE = EGRESS
          VALUE_LIST = ('pypi.org', 'pypi.python.org', 'pythonhosted.org', 'files.pythonhosted.org');

-- FQ_PYPI_RULE comes from 00_config.sql.
SET SQL_CMD = 'CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION ' || $PROJECT_PYPI_INTEGRATION || 
              ' ALLOWED_NETWORK_RULES = (' || $FQ_PYPI_RULE || ') ENABLED = TRUE';
EXECUTE IMMEDIATE $SQL_CMD;

-- Grant ownership to SYSADMIN
GRANT OWNERSHIP ON COMPUTE POOL IDENTIFIER($PROJECT_COMPUTE_POOL) TO ROLE SYSADMIN;
GRANT OWNERSHIP ON INTEGRATION IDENTIFIER($PROJECT_PYPI_INTEGRATION) TO ROLE SYSADMIN;
GRANT OWNERSHIP ON INTEGRATION IDENTIFIER($PROJECT_ALLOW_ALL_INTEGRATION) TO ROLE SYSADMIN;

USE ROLE SYSADMIN;

----------------------------------
----------------------------------
/*          DATA SETUP          */
----------------------------------
----------------------------------

-- Create file format for CSV output
CREATE OR REPLACE FILE FORMAT CSVFORMAT 
    SKIP_HEADER = 1
    TYPE = 'CSV'
    FIELD_OPTIONALLY_ENCLOSED_BY = '"';

-- Create stages.
-- Both are STATEFUL and use IF NOT EXISTS:
--   NOTEBOOK_STAGE   holds the deployed .ipynb and backs the notebook's versions;
--                    replacing it breaks VERSION$n history (and any rollback point).
--   AUDIO_VIDEO_STAGE holds the uploaded media (325 files as of 2026-08-18).
CREATE STAGE IF NOT EXISTS IDENTIFIER($PROJECT_STAGE_NB) DIRECTORY=(ENABLE=true); -- to store notebook assets
CREATE STAGE IF NOT EXISTS IDENTIFIER($PROJECT_STAGE_AV)
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION=(TYPE='SNOWFLAKE_SSE'); -- to store audio/video files for transcription

-- Create table to store transcription results.
-- STATEFUL: 441 transcripts / 250 hours of audio as of 2026-08-18. IF NOT EXISTS.
--
-- CAVEAT: because this no longer replaces the table, adding a column here does NOT
-- alter an existing deployment. Schema changes to a live table go in migration/ as
-- explicit ALTER TABLE statements.
CREATE TABLE IF NOT EXISTS IDENTIFIER($PROJECT_RESULTS_TABLE) (
    FILE_PATH VARCHAR(500),
    FILE_NAME VARCHAR(255),
    FILE_TYPE VARCHAR(10),
    DETECTED_LANGUAGE VARCHAR(50),
    TRANSCRIPT TEXT,
    TRANSCRIPT_WITH_SPEAKERS VARIANT,  -- JSON object with speaker segments
    PROCESSING_TIME_SECONDS FLOAT,
    TRANSCRIPTION_TIMESTAMP TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    FILE_SIZE_BYTES NUMBER,
    AUDIO_DURATION_SECONDS FLOAT,
    SPEAKER_COUNT NUMBER,              -- Number of identified speakers
    SRT_CONTENT TEXT,                  -- Pre-generated SRT (without speakers)
    SRT_WITH_SPEAKERS TEXT,            -- Pre-generated SRT (with speakers)
    SUMMARY_MARKDOWN TEXT,             -- Full AI-generated summary (markdown)
    -- Structured fields parsed from SUMMARY_MARKDOWN (mirrors Gong schema)
    MEETING_TITLE VARCHAR(500),        -- LLM-inferred meeting title
    CALL_BRIEF TEXT,                   -- Summary section prose
    KEY_POINTS TEXT,                   -- Key Topics bullet list
    NEXT_STEPS TEXT,                   -- Follow-up Items bullet list
    DECISIONS_MADE TEXT,               -- Decisions Made bullet list
    QUESTIONS_RAISED TEXT,             -- Questions Raised bullet list
    -- Filename-derived metadata
    ACCOUNT_NAME VARCHAR(255),         -- Account name extracted from filename
    CALL_START_TS TIMESTAMP_NTZ,       -- Call start time extracted from filename
    PARTICIPANTS_JSON VARIANT          -- Participant metadata (name/email/title/affiliation)
);

--#############################################################################
-- RUN PROGRESS INSTRUMENTATION (added 2026-08-19b)
--#############################################################################
--
-- WHY THIS EXISTS: the pipeline previously emitted progress only as print() to the
-- event table, which lands 3-5 minutes late and is useless for a live UI. This table
-- is written by the notebook (and, after the port, by transcribe_job.py) and read by
-- the Streamlit status panel.
--
-- APPEND-ONLY BY DESIGN. Appends avoid write contention with a concurrently-reading
-- UI, cost less than updates, and preserve the history needed to answer "which step
-- did it die on". Expect ~6 rows per file plus ~8 global rows per run.
--
-- STATEFUL: IF NOT EXISTS. Adding a column here does NOT alter an existing
-- deployment - put schema changes in migration/ as explicit ALTER TABLE.
CREATE TABLE IF NOT EXISTS IDENTIFIER($PROJECT_RUN_EVENTS_TABLE) (
    RUN_ID           VARCHAR(64)   NOT NULL,  -- uuid4, generated once per run
    SEQ              NUMBER        NOT NULL,  -- monotonic within RUN_ID
    EVENT_TS         TIMESTAMP_LTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),
    RUN_SOURCE       VARCHAR(32),             -- NOTEBOOK | JOB_SERVICE | MANUAL
    STATUS           VARCHAR(32),             -- RUNNING | WORK_COMPLETE | SUCCEEDED | FAILED
    PHASE            VARCHAR(32),             -- STARTUP|DISCOVER|DOWNLOAD|TRANSCRIBE|PERSIST|COMPLETE
    PHASE_NUM        NUMBER,
    PHASE_TOTAL      NUMBER,
    FILE_INDEX       NUMBER,                  -- 1-based position in this run
    FILE_TOTAL       NUMBER,
    CURRENT_FILE     VARCHAR(500),
    FILE_STEP        VARCHAR(32),             -- EXTRACT_AUDIO|TRANSCRIBE|GENERATE_SRT|GENERATE_SUMMARY
    FILE_STEP_NUM    NUMBER,
    FILE_STEP_TOTAL  NUMBER,
    UNITS_DONE       NUMBER,                  -- discrete completed work units
    UNITS_TOTAL      NUMBER,                  -- 4 global + (files * 4)
    MESSAGE          VARCHAR(1000),
    ERROR_MESSAGE    VARCHAR(4000)
);

-- Derived current-status view. One row per run, latest event wins.
--
-- STATUS VOCABULARY, and why it is shaped this way:
--
--   RUNNING         in progress
--   WORK_COMPLETE   transcripts are durable in TRANSCRIPTION_RESULTS
--   CELLS_COMPLETE  every notebook cell has finished; container teardown begins
--   FAILED          the run gave up
--   SUCCEEDED       reserved for an EXTERNAL observer, not emitted by the notebook
--
-- The notebook CANNOT report its own clean exit. The snowbook shutdown hang occurs
-- after the last cell finishes, during interpreter shutdown, so any code in the final
-- cell also runs on a hung run. CELLS_COMPLETE is therefore the notebook's terminal
-- state, and distinguishing "container exited" from "container wedged" requires
-- TASK_HISTORY - which the dashboard cross-checks. Do not add a SUCCEEDED emission to
-- the notebook: it would be written on hung runs and hide the hang.
--
-- WORK_COMPLETE going stale still IS a reliable hang signal, because it means the
-- container wedged between committing rows and finishing teardown.
--
-- STATELESS: CREATE OR REPLACE on purpose. Wrapped in EXECUTE IMMEDIATE $$ ... $$ so
-- `snow sql -f` does not split the block on semicolons.
EXECUTE IMMEDIATE $$
DECLARE
    view_sql VARCHAR;
BEGIN
    view_sql := 'CREATE OR REPLACE VIEW ' || $PROJECT_RUN_STATUS_VIEW || ' AS
WITH latest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY RUN_ID ORDER BY SEQ DESC) AS rn
    FROM ' || $PROJECT_RUN_EVENTS_TABLE || '
)
SELECT
    RUN_ID, RUN_SOURCE, STATUS,
    PHASE, PHASE_NUM, PHASE_TOTAL,
    FILE_INDEX, FILE_TOTAL, CURRENT_FILE,
    FILE_STEP, FILE_STEP_NUM, FILE_STEP_TOTAL,
    UNITS_DONE, UNITS_TOTAL,
    CASE WHEN COALESCE(UNITS_TOTAL, 0) > 0
         THEN ROUND(100.0 * UNITS_DONE / UNITS_TOTAL, 1) END AS PCT_COMPLETE,
    EVENT_TS AS LAST_HEARTBEAT_AT,
    DATEDIFF(''second'', EVENT_TS, CURRENT_TIMESTAMP()) AS SECONDS_SINCE_HEARTBEAT,
    MESSAGE, ERROR_MESSAGE,
    CASE
        WHEN STATUS IN (''SUCCEEDED'', ''FAILED'') THEN STATUS
        WHEN STATUS = ''CELLS_COMPLETE'' THEN ''CELLS_COMPLETE''
        WHEN STATUS = ''WORK_COMPLETE''
             AND DATEDIFF(''second'', EVENT_TS, CURRENT_TIMESTAMP()) > ' || $PROJECT_RUN_STALE_SECS || '
             THEN ''WORK_COMPLETE_NOT_EXITED''
        WHEN STATUS = ''WORK_COMPLETE'' THEN ''FINISHING''
        WHEN DATEDIFF(''second'', EVENT_TS, CURRENT_TIMESTAMP()) > ' || $PROJECT_RUN_STALE_SECS || '
             THEN ''STALLED''
        ELSE ''RUNNING''
    END AS DERIVED_STATE,
    -- TRUE while the run may still be holding a GPU container. The kickoff button uses
    -- this as its advisory block; the task NO_OVERLAP setting is the real authority.
    CASE WHEN STATUS IN (''SUCCEEDED'', ''FAILED'', ''CELLS_COMPLETE'') THEN FALSE
         ELSE TRUE END AS IS_ACTIVE
FROM latest
WHERE rn = 1';
    EXECUTE IMMEDIATE view_sql;
    RETURN $PROJECT_RUN_STATUS_VIEW || ' view created';
END;
$$;

-- Create a view for easy querying (using dynamic SQL to resolve table name).
-- STATELESS: keeps CREATE OR REPLACE on purpose - it holds no data, and a re-run
-- SHOULD pick up edits to the view definition.
--
-- Wrapped in EXECUTE IMMEDIATE $$ ... $$ so `snow sql -f` does not split the block on
-- semicolons (it otherwise fails with "syntax error ... unexpected '<EOF>'").
EXECUTE IMMEDIATE $$
DECLARE
    view_sql VARCHAR;
BEGIN
    view_sql := 'CREATE OR REPLACE VIEW TRANSCRIPTION_SUMMARY AS
SELECT 
    FILE_TYPE,
    DETECTED_LANGUAGE,
    COUNT(*) as FILE_COUNT,
    AVG(PROCESSING_TIME_SECONDS) as AVG_PROCESSING_TIME,
    AVG(AUDIO_DURATION_SECONDS) as AVG_DURATION,
    AVG(FILE_SIZE_BYTES) as AVG_FILE_SIZE,
    AVG(SPEAKER_COUNT) as AVG_SPEAKERS,
    MIN(TRANSCRIPTION_TIMESTAMP) as FIRST_TRANSCRIPTION,
    MAX(TRANSCRIPTION_TIMESTAMP) as LAST_TRANSCRIPTION
FROM ' || $PROJECT_RESULTS_TABLE || '
GROUP BY FILE_TYPE, DETECTED_LANGUAGE
ORDER BY FILE_COUNT DESC';
    EXECUTE IMMEDIATE view_sql;
    RETURN 'TRANSCRIPTION_SUMMARY view created';
END;
$$;

-- Create notebook.
-- STATEFUL: the notebook object owns its VERSION$n history plus the live version.
-- CREATE OR REPLACE would wipe that history, including any version being kept as a
-- rollback point (e.g. VERSION$4, the pre-Fix-#2 notebook). So: IF NOT EXISTS.
-- Notebook CONTENT is managed by 04_deploy_notebook.sh, not by this script.
--
-- Wrapped in EXECUTE IMMEDIATE $$ ... $$ so `snow sql -f` does not split the block on
-- semicolons (it otherwise fails with "syntax error ... unexpected '<EOF>'").
EXECUTE IMMEDIATE $$
DECLARE
    sql_cmd VARCHAR;
    note    VARCHAR DEFAULT '';
BEGIN
    sql_cmd := 'CREATE NOTEBOOK IF NOT EXISTS ' || $PROJECT_NOTEBOOK ||
               ' FROM ''@' || $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STAGE_NB || '''' ||
               ' MAIN_FILE = ''audio_video_transcription.ipynb''' ||
               ' QUERY_WAREHOUSE = ''' || $PROJECT_WH || '''' ||
               ' COMPUTE_POOL=''' || $PROJECT_COMPUTE_POOL || '''' ||
               ' RUNTIME_NAME=''SYSTEM$GPU_RUNTIME''';
    EXECUTE IMMEDIATE sql_cmd;

    -- Only meaningful on a brand-new notebook. On an existing one this fails with
    -- 099106 "There is already a live version. Please commit it first." That is
    -- expected on a re-run, so tolerate it and leave version management to
    -- 04_deploy_notebook.sh.
    BEGIN
        sql_cmd := 'ALTER NOTEBOOK ' || $PROJECT_NOTEBOOK || ' ADD LIVE VERSION FROM LAST';
        EXECUTE IMMEDIATE sql_cmd;
        note := 'live version created';
    EXCEPTION
        WHEN OTHER THEN
            note := 'live version already present - left as is (use 04_deploy_notebook.sh)';
    END;

    sql_cmd := 'ALTER NOTEBOOK ' || $PROJECT_NOTEBOOK || ' SET EXTERNAL_ACCESS_INTEGRATIONS = ("' ||
               UPPER($PROJECT_PYPI_INTEGRATION) || '", "' ||
               UPPER($PROJECT_ALLOW_ALL_INTEGRATION) || '")';
    EXECUTE IMMEDIATE sql_cmd;

    RETURN 'Notebook ready: ' || note;
END;
$$;

--#############################################################################
-- STREAMLIT APP ROLE (added 2026-08-19b)
--#############################################################################
--
-- WHY A DEDICATED ROLE: warehouse-runtime Streamlit apps run with OWNER'S RIGHTS, so
-- every viewer executes queries with the app owner's privileges. The dashboard was
-- originally created by ACCOUNTADMIN, which meant adding a kickoff button or an upload
-- control would let any viewer act with ACCOUNTADMIN reach. This role holds only what
-- the dashboard actually needs.
--
-- CRITICAL PLATFORM CONSTRAINT: `GRANT OWNERSHIP ON STREAMLIT` is NOT SUPPORTED
-- ("Unsupported feature GRANT/REVOKE OWNERSHIP ON STREAMLIT"). A Streamlit app runs as
-- the role that CREATED it, and that cannot be changed after the fact. The only way to
-- change the owner is to recreate the object while using the target role. Consequence
-- for deploys: `snow streamlit deploy` runs as the connection's role, so it MUST be run
-- with --role set to the app role, or the app silently comes back owned by the
-- connection role. See documents/architecture/dashboard.md.
--
-- Deliberately NOT granted:
--   INSERT on the run-events table - the app only reads progress; the notebook writes it
--   READ SESSION                   - only needed if the app calls context functions
--   Anything on the compute pool   - warehouse runtime does not use one
EXECUTE IMMEDIATE $$
DECLARE
    approle STRING := $PROJECT_APP_ROLE;
    sch     STRING := $FQ_SCHEMA;
BEGIN
    EXECUTE IMMEDIATE 'CREATE ROLE IF NOT EXISTS ' || approle;

    EXECUTE IMMEDIATE 'GRANT USAGE ON DATABASE '  || $PROJECT_DB || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT USAGE ON SCHEMA '    || sch         || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT USAGE ON WAREHOUSE ' || $PROJECT_WH || ' TO ROLE ' || approle;

    -- Read what the dashboard displays
    EXECUTE IMMEDIATE 'GRANT SELECT ON TABLE ' || sch || '.' || $PROJECT_RESULTS_TABLE   || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT SELECT ON TABLE ' || sch || '.' || $PROJECT_RUN_EVENTS_TABLE || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT SELECT ON VIEW '  || sch || '.' || $PROJECT_RUN_STATUS_VIEW  || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT SELECT ON VIEW '  || sch || '.TRANSCRIPTION_SUMMARY TO ROLE ' || approle;

    -- Trigger transcription. OPERATE is the minimum for EXECUTE TASK and cannot alter
    -- or drop the task. Verified working from an owner's-rights context 2026-08-19.
    EXECUTE IMMEDIATE 'GRANT OPERATE ON TASK ' || sch || '.' || $PROJECT_TASK_TRANSCRIBE || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT USAGE ON PROCEDURE ' || sch || '.TRANSCRIBE_IF_NEW_FILES() TO ROLE ' || approle;

    -- Upload media. WRITE for put_stream, READ for DIRECTORY()/LIST. Note that REMOVE is
    -- blocked under owner's rights regardless, so the app can never delete stage files.
    EXECUTE IMMEDIATE 'GRANT READ, WRITE ON STAGE ' || sch || '.' || $PROJECT_STAGE_AV || ' TO ROLE ' || approle;

    -- Needed to create/replace the app object itself
    EXECUTE IMMEDIATE 'GRANT CREATE STREAMLIT ON SCHEMA ' || sch || ' TO ROLE ' || approle;
    EXECUTE IMMEDIATE 'GRANT READ ON STAGE ' || sch || '.STREAMLIT_STAGE TO ROLE ' || approle;

    -- Put the role in the standard hierarchy so SYSADMIN, and therefore ACCOUNTADMIN,
    -- retains access to the app after ownership moves.
    EXECUTE IMMEDIATE 'GRANT ROLE ' || approle || ' TO ROLE SYSADMIN';

    RETURN approle || ' ready';
END;
$$;

-- Sample queries to test after transcription:
/*
-- View all transcriptions
SELECT * FROM TRANSCRIPTION_RESULTS ORDER BY TRANSCRIPTION_TIMESTAMP DESC;

-- Search transcripts for specific content
SELECT FILE_NAME, TRANSCRIPT, DETECTED_LANGUAGE 
FROM TRANSCRIPTION_RESULTS 
WHERE TRANSCRIPT ILIKE '%your_search_term%';

-- Get summary statistics
SELECT * FROM TRANSCRIPTION_SUMMARY;

-- Find longest/shortest audio files
SELECT FILE_NAME, AUDIO_DURATION_SECONDS, TRANSCRIPT
FROM TRANSCRIPTION_RESULTS 
ORDER BY AUDIO_DURATION_SECONDS DESC;
*/
