--#############################################################################
-- 999_teardown.sql — GUARDED teardown of a transcription deployment
--#############################################################################
--
-- CONFIGURATION IS LOADED, NOT PASTED.
--   PROJECT_* / FQ_* session variables come from scripts/00_config.sql, staged at
--   @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS.
--
--   Prerequisite (once per account):  scripts/01_bootstrap.sql
--   After editing config:             scripts/publish_config.sh
--
--#############################################################################
-- READ THIS BEFORE RUNNING
--#############################################################################
--
-- Until 2026-08-18 this file carried a hardcoded config block that still pointed at
-- the V1 names, so running it against the active V2 deployment did nothing to V2.
-- That accidental safety is GONE — it now resolves to whatever 00_config.sql
-- describes and will actually destroy it. Hence the guards below.
--
-- HOW TO USE
--   1. Fill in the variables in the SET block.
--   2. Run the single guarded block. It validates everything BEFORE touching
--      anything, then performs exactly the level you asked for.
--
--   There are no loose DROP statements in this file. "Run All" cannot skip the
--   guards, because the guards and the DDL live in the same block.
--
-- LEVELS (cumulative)
--   1  Automation ....... tasks, stream, procedures
--   2  + Compute ........ notebook, compute pool, integrations, network rules
--   3  + Data ........... DESTROYS transcripts and staged media
--   4  + Infrastructure . schema, database, warehouse
--
-- GUARDS
--   A  Typed name .......... TEARDOWN_TARGET_DB must match the loaded PROJECT_DB
--   B  Explicit level ...... TEARDOWN_LEVEL must be 1-4 (default 0 refuses)
--   C  In-flight check ..... levels >= 2: no EXECUTE NOTEBOOK currently running
--   D  Verified backup ..... levels >= 3: a zero-copy clone outside the target DB
--                            whose row count matches TRANSCRIPTION_RESULTS
--   E  Data-loss ack ....... levels >= 3: explicit acknowledgement, and the abort
--                            message shows you the row count you are destroying
--
-- RECOVERY (Time Travel, within DATA_RETENTION_TIME_IN_DAYS = 14)
--   UNDROP DATABASE <db>;   UNDROP TABLE <db>.<schema>.TRANSCRIPTION_RESULTS;
--   UNDROP STAGE <db>.<schema>.AUDIO_VIDEO_STAGE;
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

USE ROLE ACCOUNTADMIN;

--#############################################################################
-- FILL THESE IN
--#############################################################################

-- Guard A: type the exact database name you intend to tear down.
SET TEARDOWN_TARGET_DB = '';

-- Guard B: 1, 2, 3 or 4. Left at 0, nothing happens.
SET TEARDOWN_LEVEL = 0;

-- Guard D (levels >= 3): fully-qualified backup table, OUTSIDE the target database.
--   CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_20260818
--     CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
SET TEARDOWN_BACKUP_TABLE = '';

-- Guard E (levels >= 3): explicit acknowledgement that data will be destroyed.
SET TEARDOWN_ACKNOWLEDGE_DATA_LOSS = FALSE;


--#############################################################################
-- THE GUARDED BLOCK — validates, then acts. Nothing destructive runs before all
-- applicable guards pass.
--
-- Wrapped in EXECUTE IMMEDIATE $$ ... $$ so it is ONE statement. Without the
-- wrapper, `snow sql -f` splits the file on semicolons and the block fails with
-- "syntax error line 2 ... unexpected '<EOF>'". The wrapper is also what Snowflake
-- documents for running Scripting blocks from the CLI / SnowSQL.
--#############################################################################

EXECUTE IMMEDIATE $$
DECLARE
    lvl           INTEGER;
    target_db     STRING;
    cfg_db        STRING;
    backup_tbl    STRING;
    ack           BOOLEAN;
    src_rows      INTEGER DEFAULT 0;
    bkp_rows      INTEGER DEFAULT 0;
    results_exists BOOLEAN DEFAULT TRUE;
    running_nb    INTEGER DEFAULT 0;
    actions       STRING DEFAULT '';
    stmt          STRING;
BEGIN
    SELECT $TEARDOWN_LEVEL, $TEARDOWN_TARGET_DB, $PROJECT_DB,
           $TEARDOWN_BACKUP_TABLE, $TEARDOWN_ACKNOWLEDGE_DATA_LOSS
      INTO :lvl, :target_db, :cfg_db, :backup_tbl, :ack;

    ----------------------------------------------------------------
    -- Guard B: explicit level
    ----------------------------------------------------------------
    IF (lvl IS NULL OR lvl < 1 OR lvl > 4) THEN
        RETURN 'TEARDOWN ABORTED (guard B): TEARDOWN_LEVEL is ' ||
               COALESCE(lvl::STRING, 'NULL') ||
               '. Set it to 1, 2, 3 or 4. Nothing was changed.';
    END IF;

    ----------------------------------------------------------------
    -- Guard A: typed-name confirmation
    ----------------------------------------------------------------
    IF (target_db IS NULL OR TRIM(target_db) = '') THEN
        RETURN 'TEARDOWN ABORTED (guard A): TEARDOWN_TARGET_DB is empty. ' ||
               'Set it to ''' || cfg_db || ''' to confirm the target. Nothing was changed.';
    END IF;

    IF (UPPER(TRIM(target_db)) <> UPPER(TRIM(cfg_db))) THEN
        RETURN 'TEARDOWN ABORTED (guard A): TEARDOWN_TARGET_DB = ''' || target_db ||
               ''' but the loaded config points at ''' || cfg_db ||
               '''. Either you targeted the wrong deployment, or the staged config is ' ||
               'not the one you think (re-run publish_config.sh). Nothing was changed.';
    END IF;

    ----------------------------------------------------------------
    -- Guard C (levels >= 2): nothing may be mid-transcription
    ----------------------------------------------------------------
    IF (lvl >= 2) THEN
        SELECT COUNT(*) INTO :running_nb
        FROM TABLE(TRANSCRIPTION_DEPLOY.INFORMATION_SCHEMA.QUERY_HISTORY(
                 END_TIME_RANGE_START => DATEADD('hour', -5, CURRENT_TIMESTAMP()),
                 RESULT_LIMIT => 10000))
        WHERE EXECUTION_STATUS = 'RUNNING'
          AND QUERY_TEXT ILIKE 'EXECUTE NOTEBOOK%';

        IF (running_nb > 0) THEN
            RETURN 'TEARDOWN ABORTED (guard C): ' || running_nb ||
                   ' EXECUTE NOTEBOOK run(s) are still RUNNING. Dropping the notebook or ' ||
                   'compute pool now would kill an in-flight transcription. Wait for it to ' ||
                   'finish (see scripts/08_telemetry_debug.sql) and retry. Nothing was changed.';
        END IF;
    END IF;

    ----------------------------------------------------------------
    -- Guards D and E (levels >= 3): protect the transcripts
    ----------------------------------------------------------------
    IF (lvl >= 3) THEN
        -- How much data is actually at risk? If the table is gone already, there is
        -- nothing to protect and both guards are skipped.
        BEGIN
            LET rs_src RESULTSET := (EXECUTE IMMEDIATE
                'SELECT COUNT(*) AS N FROM ' || $FQ_RESULTS);
            LET c_src CURSOR FOR rs_src;
            OPEN c_src;
            FETCH c_src INTO :src_rows;
            CLOSE c_src;
        EXCEPTION
            WHEN OTHER THEN
                results_exists := FALSE;
                src_rows := 0;
        END;

        IF (results_exists AND src_rows > 0) THEN

            -- Guard E: explicit acknowledgement, with the real number in front of you
            IF (ack IS NULL OR ack = FALSE) THEN
                RETURN 'TEARDOWN ABORTED (guard E): level ' || lvl || ' destroys ' ||
                       src_rows || ' rows in ' || $FQ_RESULTS ||
                       ' plus everything in the media stage. If that is intended, set ' ||
                       'TEARDOWN_ACKNOWLEDGE_DATA_LOSS = TRUE. Nothing was changed.';
            END IF;

            -- Guard D: a verified backup must exist outside the target database
            IF (backup_tbl IS NULL OR TRIM(backup_tbl) = '') THEN
                RETURN 'TEARDOWN ABORTED (guard D): level ' || lvl || ' destroys ' ||
                       src_rows || ' transcripts and no backup was given. Take a zero-copy ' ||
                       'clone (instant, no storage cost until divergence):' || CHR(10) ||
                       '  CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_' ||
                       TO_CHAR(CURRENT_DATE(), 'YYYYMMDD') || ' CLONE ' || $FQ_RESULTS || ';' || CHR(10) ||
                       'then SET TEARDOWN_BACKUP_TABLE to that name. Nothing was changed.';
            END IF;

            IF (UPPER(TRIM(backup_tbl)) LIKE UPPER(TRIM(cfg_db)) || '.%') THEN
                RETURN 'TEARDOWN ABORTED (guard D): backup ''' || backup_tbl ||
                       ''' lives inside ' || cfg_db ||
                       ', which this teardown destroys. Put the backup in another database ' ||
                       '(e.g. TRANSCRIPTION_DEPLOY.PUBLIC). Nothing was changed.';
            END IF;

            BEGIN
                LET rs_bkp RESULTSET := (EXECUTE IMMEDIATE
                    'SELECT COUNT(*) AS N FROM ' || :backup_tbl);
                LET c_bkp CURSOR FOR rs_bkp;
                OPEN c_bkp;
                FETCH c_bkp INTO :bkp_rows;
                CLOSE c_bkp;
            EXCEPTION
                WHEN OTHER THEN
                    RETURN 'TEARDOWN ABORTED (guard D): backup table ''' || backup_tbl ||
                           ''' could not be read (does it exist? is the name fully qualified?). ' ||
                           'Nothing was changed.';
            END;

            IF (bkp_rows <> src_rows) THEN
                RETURN 'TEARDOWN ABORTED (guard D): backup row count mismatch. ' ||
                       backup_tbl || ' has ' || bkp_rows || ' rows but ' || $FQ_RESULTS ||
                       ' has ' || src_rows || '. Re-clone the backup so it is current. ' ||
                       'Nothing was changed.';
            END IF;
        END IF;
    END IF;

    ----------------------------------------------------------------
    -- All applicable guards passed. Perform the teardown.
    ----------------------------------------------------------------

    -- LEVEL 1: automation
    EXECUTE IMMEDIATE 'ALTER TASK IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_TASK_TRANSCRIBE || ' SUSPEND';
    EXECUTE IMMEDIATE 'ALTER TASK IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_TASK_REFRESH || ' SUSPEND';
    EXECUTE IMMEDIATE 'DROP TASK IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_TASK_TRANSCRIBE;
    EXECUTE IMMEDIATE 'DROP TASK IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_TASK_REFRESH;
    EXECUTE IMMEDIATE 'DROP STREAM IF EXISTS ' || $FQ_STREAM;
    EXECUTE IMMEDIATE 'DROP PROCEDURE IF EXISTS ' || $FQ_SCHEMA || '.RUN_TRANSCRIPTION_NOTEBOOK()';
    EXECUTE IMMEDIATE 'DROP PROCEDURE IF EXISTS ' || $FQ_SCHEMA || '.TRANSCRIBE_IF_NEW_FILES()';
    actions := 'L1 automation dropped (tasks, stream, procedures)';

    -- LEVEL 2: compute
    IF (lvl >= 2) THEN
        EXECUTE IMMEDIATE 'DROP NOTEBOOK IF EXISTS ' || $FQ_NOTEBOOK;
        EXECUTE IMMEDIATE 'ALTER COMPUTE POOL IF EXISTS ' || $PROJECT_COMPUTE_POOL || ' STOP ALL';
        EXECUTE IMMEDIATE 'DROP COMPUTE POOL IF EXISTS ' || $PROJECT_COMPUTE_POOL;
        EXECUTE IMMEDIATE 'DROP INTEGRATION IF EXISTS ' || $PROJECT_PYPI_INTEGRATION;
        EXECUTE IMMEDIATE 'DROP INTEGRATION IF EXISTS ' || $PROJECT_ALLOW_ALL_INTEGRATION;
        EXECUTE IMMEDIATE 'DROP NETWORK RULE IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_PYPI_RULE;
        EXECUTE IMMEDIATE 'DROP NETWORK RULE IF EXISTS ' || $FQ_SCHEMA || '.' || $PROJECT_ALLOW_ALL_RULE;
        actions := actions || ' | L2 compute dropped (notebook, pool, integrations, rules)';
    END IF;

    -- LEVEL 3: data
    IF (lvl >= 3) THEN
        EXECUTE IMMEDIATE 'DROP VIEW IF EXISTS ' || $FQ_VIEW;
        EXECUTE IMMEDIATE 'DROP TABLE IF EXISTS ' || $FQ_RESULTS;
        EXECUTE IMMEDIATE 'DROP FILE FORMAT IF EXISTS ' || $FQ_SCHEMA || '.CSVFORMAT';
        EXECUTE IMMEDIATE 'DROP STAGE IF EXISTS ' || $FQ_STAGE_NB;
        EXECUTE IMMEDIATE 'DROP STAGE IF EXISTS ' || $FQ_STAGE_AV;
        actions := actions || ' | L3 data dropped (' || src_rows || ' transcripts, stages)';
    END IF;

    -- LEVEL 4: infrastructure
    IF (lvl >= 4) THEN
        EXECUTE IMMEDIATE 'DROP SCHEMA IF EXISTS ' || $FQ_SCHEMA;
        EXECUTE IMMEDIATE 'DROP DATABASE IF EXISTS ' || $PROJECT_DB;
        EXECUTE IMMEDIATE 'DROP WAREHOUSE IF EXISTS ' || $PROJECT_WH;
        actions := actions || ' | L4 infrastructure dropped (schema, database, warehouse)';
    END IF;

    RETURN 'TEARDOWN COMPLETE at level ' || lvl || ' on ' || cfg_db || '. ' || actions ||
           CASE WHEN lvl >= 3 AND src_rows > 0
                THEN '. Backup retained at ' || backup_tbl || ' (' || bkp_rows || ' rows).'
                ELSE '.' END;
END;
$$;


--#############################################################################
-- POST-TEARDOWN NOTES
--#############################################################################
--
-- The service account is NOT touched here. To remove it:
--     av.uploader/cleanup_av_service_user.sql
--
-- The shared deployment config (TRANSCRIPTION_DEPLOY) is NOT touched here either —
-- it is account-level tooling, not part of any deployment. Drop it only if you are
-- removing the project entirely:
--     DROP DATABASE IF EXISTS TRANSCRIPTION_DEPLOY;
--
-- To rebuild after a teardown:
--     scripts/publish_config.sh  ->  02_setup.sql  ->  03_automate.sql  ->  04_deploy_notebook.sh
--#############################################################################
