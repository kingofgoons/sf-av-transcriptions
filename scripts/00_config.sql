--#############################################################################
-- 00_config.sql — THE SINGLE SOURCE OF TRUTH for deployment object names
--#############################################################################
--
-- THIS IS THE ONLY FILE THAT SHOULD CONTAIN `SET PROJECT_...` STATEMENTS.
--
-- Every other script loads these values with one line:
--
--     EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;
--
-- EXECUTE IMMEDIATE FROM runs the file IN THE SAME SESSION, so the session
-- variables set below persist into the calling script. There is no config table
-- and no stored procedure involved.
--
-- ----------------------------------------------------------------------------
-- HOW TO CHANGE CONFIG
-- ----------------------------------------------------------------------------
--   1. Edit the values below.
--   2. Bump CONFIG_REVISION.
--   3. Run ./publish_config.sh
--
--   Step 3 is REQUIRED. Scripts read the STAGED copy, not this local file. If you
--   skip it, every script silently keeps using the previous values. The revision
--   stamp echoed at the bottom is how you detect that.
--
-- ----------------------------------------------------------------------------
-- WHY THIS EXISTS
-- ----------------------------------------------------------------------------
--   This block used to be copy-pasted into 7 files. It drifted: as of 2026-08-18,
--   999_teardown.sql, create_av_service_user.sql and cleanup_av_service_user.sql
--   all still pointed at the V1 names while the active deployment was V2. The
--   service role ended up with grants on BOTH V1 and V2 objects as a result.
--   See DIARY.md 2026-08-18.
--
-- ----------------------------------------------------------------------------
-- PARALLEL DEPLOYMENTS
-- ----------------------------------------------------------------------------
--   To run a second deployment (dev/staging), copy this file to a new name on the
--   stage (e.g. 00_config_dev.sql) with different values, and point the include at
--   that file. Do NOT maintain two copies of the values in different scripts.
--
--#############################################################################

-- Bump this on every edit. Echoed at load time so a stale staged copy is visible.
SET CONFIG_REVISION = '2026-08-19a';

-- Core naming - change these to create a parallel deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB_V2';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA_V2';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH_V2';              -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL_V2';  -- GPU compute pool name

-- Derived names (automatically built from above)
SET PROJECT_NOTEBOOK = 'TRANSCRIBE_AV_FILES_V2';     -- Notebook name
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STAGE_NB = 'NOTEBOOK_STAGE';          -- Stage for notebook assets -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STREAM = 'AV_STAGE_STREAM_V2';           -- Stream — DEPRECATED, no longer used
SET PROJECT_TASK_TRANSCRIBE = 'TRANSCRIBE_NEW_FILES_TASK_V2';  -- Transcription task
SET PROJECT_TASK_REFRESH = 'REFRESH_STAGE_DIRECTORY_TASK_V2';  -- Stage refresh task — DEPRECATED

-- Service account for av.uploader
SET SERVICE_ROLE = 'AV_UPLOADER_SERVICE_ROLE';
SET SERVICE_USER = 'AV_UPLOADER_SERVICE_USER';

-- Timeout configuration
--
-- 30 minutes, set at the TASK level only (SHOW PARAMETERS ... IN TASK shows level=TASK).
-- The account default is left at Snowflake's 3600000 and is NOT modified.
--
-- WHY 30 MIN AND NOT 4 HOURS: this was 14400000 (4h), set on 2026-07-28 in response to
-- runs dying at exactly 3600s. That was misdiagnosed at the time as "large batches can
-- exceed an hour" - it was actually the snowbook shutdown hang hitting the 60-minute
-- ceiling. Raising it to 4h did not fix anything, it just let each hang burn 2h15m of GPU
-- instead of being cut off at 1h. Root-caused 2026-08-19; see DIARY.md.
--
-- The largest legitimately long run ever observed is 978s (16 min) for a 10-file,
-- 18,202s-audio batch, so 30 minutes leaves roughly 2x headroom while capping the waste
-- from a hang. If you ever do a deliberate bulk backfill of hundreds of files, raise this
-- temporarily rather than leaving it high.
SET PROJECT_TASK_TIMEOUT_MS = 1800000;              -- 30 min; EXECUTE NOTEBOOK is synchronous (blocks until notebook finishes)
SET PROJECT_WH_STATEMENT_TIMEOUT = 14400;           -- Warehouse-level ceiling, left at 4h; the task timeout above is the binding limit

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration_V2';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration_V2';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule_V2';
SET PROJECT_PYPI_RULE = 'pypi_network_rule_V2';

--#############################################################################
-- DERIVED FULLY-QUALIFIED NAMES
-- Built once here so consuming scripts never rebuild them (another past source
-- of drift). Needed for statements that do not accept IDENTIFIER().
--#############################################################################

SET FQ_SCHEMA   = $PROJECT_DB || '.' || $PROJECT_SCHEMA;
SET FQ_STAGE_AV = $FQ_SCHEMA || '.' || $PROJECT_STAGE_AV;
SET FQ_STAGE_NB = $FQ_SCHEMA || '.' || $PROJECT_STAGE_NB;
SET FQ_NOTEBOOK = $FQ_SCHEMA || '.' || $PROJECT_NOTEBOOK;
SET FQ_RESULTS  = $FQ_SCHEMA || '.' || $PROJECT_RESULTS_TABLE;
SET FQ_STREAM   = $FQ_SCHEMA || '.' || $PROJECT_STREAM;
SET FQ_TASK     = $FQ_SCHEMA || '.' || $PROJECT_TASK_TRANSCRIBE;
SET FQ_VIEW     = $FQ_SCHEMA || '.TRANSCRIPTION_SUMMARY';
SET FQ_GATE_PROC = $FQ_SCHEMA || '.TRANSCRIBE_IF_NEW_FILES';
SET FQ_ALLOW_ALL_RULE = $FQ_SCHEMA || '.' || $PROJECT_ALLOW_ALL_RULE;
SET FQ_PYPI_RULE      = $FQ_SCHEMA || '.' || $PROJECT_PYPI_RULE;

--#############################################################################
-- LOAD ECHO
-- Printed by every script that includes this file. If CONFIG_REVISION is not what
-- you just edited, the staged copy is stale — run ./publish_config.sh.
--#############################################################################

SELECT
    $CONFIG_REVISION AS CONFIG_REVISION,
    $PROJECT_DB AS DB,
    $PROJECT_SCHEMA AS SCHEMA_NAME,
    $PROJECT_WH AS WAREHOUSE_NAME,
    $PROJECT_COMPUTE_POOL AS COMPUTE_POOL,
    $PROJECT_TASK_TRANSCRIBE AS TASK_NAME;
