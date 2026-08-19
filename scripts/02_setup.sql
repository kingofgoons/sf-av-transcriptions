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
