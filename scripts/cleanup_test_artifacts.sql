--#############################################################################
-- cleanup_test_artifacts.sql — remove the _TESTnn validation files and everything
-- derived from them.
--
-- DRY RUN BY DEFAULT. Reports what it would remove and changes nothing. To actually
-- delete, change CLEANUP_EXECUTE to TRUE below and re-run.
--
--     snow sql -f scripts/cleanup_test_artifacts.sql -c DEMO --enable-templating NONE
--
-- WHAT IT REMOVES
--
--   1. Stage files    @AUDIO_VIDEO_STAGE matching _TESTnn.(mp3|mp4)
--   2. Transcripts    TRANSCRIPTION_RESULTS rows for those files
--   3. Run events     TRANSCRIPTION_RUN_EVENTS rows for runs that ONLY touched test files
--
-- It does NOT remove the local copies in AUDIO_VIDEO_STAGE_FILES/ — those are yours, and
-- deleting a user's local media is not a cleanup script's business.
--
-- WHY THE PATTERN IS A REGEX AND NOT A LIKE
--
-- `FILE_NAME LIKE '%TEST%'` would also match the real, permanent transcript
-- '2026-07-22 14-41-17_DoubleVerify_onsite.AI_Brain.Testing.sync.mp4'. Anchoring on
-- _TEST followed by exactly two digits and a known extension cannot. Verified
-- 2026-09-24: the anchored pattern matches 0 of 377 pre-existing stage files and 0 of
-- 493 pre-existing transcripts. The authoritative list of what was staged for testing is
-- tests/fixtures/test_av_manifest.txt.
--
-- THE RUN_EVENTS PREDICATE IS THE SUBTLE ONE
--
-- RUN_EVENTS has no per-row FILE_NAME; it has CURRENT_FILE, which is NULL on run-level
-- events (STARTUP, DISCOVER, COMPLETE). So "delete events whose CURRENT_FILE looks like a
-- test file" would strip the per-file rows and orphan the run-level ones, leaving a
-- half-run in the dashboard's history.
--
-- Worse, deleting by RUN_ID for "any run that touched a test file" would destroy the
-- history of a MIXED run — one that processed real files alongside test files. That would
-- be permanent loss of real operational history to clean up disposable data.
--
-- So the predicate is: delete a RUN_ID only when EVERY non-null CURRENT_FILE in that run
-- matches the test pattern. A mixed run is reported and deliberately left alone.
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE IDENTIFIER($PROJECT_WH);
USE SCHEMA IDENTIFIER($FQ_SCHEMA);

-- ============================ THE ONLY SWITCH ============================
SET CLEANUP_EXECUTE = FALSE;    -- FALSE = report only. TRUE = delete.
-- =========================================================================

SET TEST_RE = '.*_TEST[0-9]{2}\\.(mp3|mp4)$';

--------------------------------------------------------------------------------
-- 1. REPORT
--------------------------------------------------------------------------------

-- Transcripts that would go.
SELECT 'transcripts to delete' AS SCOPE_,
       COUNT(*)                AS N,
       LISTAGG(REGEXP_SUBSTR(FILE_NAME, '_TEST[0-9]{2}'), ', ')
           WITHIN GROUP (ORDER BY FILE_NAME) AS MARKERS
FROM IDENTIFIER($FQ_RESULTS)
WHERE RLIKE(FILE_NAME, $TEST_RE);

-- Run-event partitioning: pure-test runs are removable, mixed runs are NOT.
WITH classified AS (
    SELECT RUN_ID,
           COUNT(*)                                                       AS EVENTS,
           COUNT(DISTINCT CURRENT_FILE)                                   AS FILES_SEEN,
           COUNT(DISTINCT CASE WHEN RLIKE(CURRENT_FILE, $TEST_RE)
                               THEN CURRENT_FILE END)                     AS TEST_FILES,
           COUNT(DISTINCT CASE WHEN CURRENT_FILE IS NOT NULL
                                AND NOT RLIKE(CURRENT_FILE, $TEST_RE)
                               THEN CURRENT_FILE END)                     AS REAL_FILES
    FROM IDENTIFIER($FQ_RUN_EVENTS)
    GROUP BY RUN_ID
)
SELECT CASE WHEN TEST_FILES > 0 AND REAL_FILES = 0 THEN 'PURE TEST - will delete'
            WHEN TEST_FILES > 0 AND REAL_FILES > 0 THEN 'MIXED - will KEEP, real history'
            ELSE 'no test files - untouched' END AS CLASSIFICATION,
       COUNT(*) AS RUNS,
       SUM(EVENTS) AS EVENT_ROWS
FROM classified
GROUP BY CLASSIFICATION
ORDER BY CLASSIFICATION;

-- Stage files that would go. LIST, not DIRECTORY: the directory table goes stale after
-- PUT and has previously shown a months-old view of this very stage.
LIST @AUDIO_VIDEO_STAGE;
SET Q_STAGE = LAST_QUERY_ID();

SELECT 'stage files to remove' AS SCOPE_,
       COUNT(*)                AS N,
       ROUND(SUM("size") / 1024 / 1024, 1) AS MB
FROM TABLE(RESULT_SCAN($Q_STAGE))
WHERE RLIKE(REGEXP_REPLACE("name", '^.*/', ''), $TEST_RE);

--------------------------------------------------------------------------------
-- 2. EXECUTE (gated)
--------------------------------------------------------------------------------

EXECUTE IMMEDIATE $$
DECLARE
    n_backup   NUMBER;
    n_results  NUMBER;
    n_events   NUMBER;
    stage_name VARCHAR;
BEGIN
    IF (NOT $CLEANUP_EXECUTE) THEN
        RETURN 'DRY RUN. Nothing deleted. Set CLEANUP_EXECUTE = TRUE to remove.';
    END IF;

    -- Backup first, unconditionally. This is a DELETE against the table holding 493
    -- irreplaceable transcripts; the pattern is narrow and verified, but a free
    -- zero-copy clone is cheaper than being wrong.
    CREATE OR REPLACE TABLE TR_PRECLEANUP_BACKUP CLONE IDENTIFIER($FQ_RESULTS);
    SELECT COUNT(*) INTO n_backup FROM TR_PRECLEANUP_BACKUP;

    -- Run events: pure-test runs only.
    DELETE FROM IDENTIFIER($FQ_RUN_EVENTS)
    WHERE RUN_ID IN (
        SELECT RUN_ID
        FROM IDENTIFIER($FQ_RUN_EVENTS)
        GROUP BY RUN_ID
        HAVING COUNT(DISTINCT CASE WHEN RLIKE(CURRENT_FILE, $TEST_RE)
                                   THEN CURRENT_FILE END) > 0
           AND COUNT(DISTINCT CASE WHEN CURRENT_FILE IS NOT NULL
                                    AND NOT RLIKE(CURRENT_FILE, $TEST_RE)
                                   THEN CURRENT_FILE END) = 0
    );
    n_events := SQLROWCOUNT;

    DELETE FROM IDENTIFIER($FQ_RESULTS) WHERE RLIKE(FILE_NAME, $TEST_RE);
    n_results := SQLROWCOUNT;

    RETURN 'Backed up ' || n_backup || ' rows to TR_PRECLEANUP_BACKUP. Deleted '
        || n_results || ' transcripts and ' || n_events || ' run-event rows. '
        || 'Stage files must be removed separately - see the REMOVE statement below.';
END;
$$;

--------------------------------------------------------------------------------
-- 3. STAGE FILES — deliberately manual
--
-- REMOVE takes a literal pattern and cannot be driven from a session variable or gated
-- on CLEANUP_EXECUTE, so automating it here would mean it fires on every dry run. Run
-- this line by hand once the report above looks right:
--
--     REMOVE @TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE
--            PATTERN = '.*_TEST[0-9]{2}\\.(mp3|mp4)';
--
-- Then confirm:
--     LIST @TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE PATTERN = '.*_TEST.*';
--------------------------------------------------------------------------------

-- 4. Verification — run after executing. Every count must be 0, and the pre-existing
-- transcript total must be intact.
SELECT (SELECT COUNT(*) FROM IDENTIFIER($FQ_RESULTS)
        WHERE RLIKE(FILE_NAME, $TEST_RE))                       AS TEST_TRANSCRIPTS_LEFT,
       (SELECT COUNT(*) FROM IDENTIFIER($FQ_RUN_EVENTS)
        WHERE RLIKE(CURRENT_FILE, $TEST_RE))                    AS TEST_EVENT_ROWS_LEFT,
       (SELECT COUNT(*) FROM IDENTIFIER($FQ_RESULTS))           AS TRANSCRIPTS_TOTAL,
       '493 was the pre-test baseline on 2026-09-24'            AS NOTE;
