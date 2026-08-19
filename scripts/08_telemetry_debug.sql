--#############################################################################
-- 08_telemetry_debug.sql — Container telemetry diagnostics for the GPU notebook
--#############################################################################
--
-- WHAT THIS IS FOR
--   Debugging the transcription notebook's container behaviour: startup, work
--   phase, teardown, hangs, GPU/memory pressure, and Python-level errors.
--
-- NO SETUP REQUIRED
--   This account already emits notebook container telemetry to the Snowflake-
--   provided default event table SNOWFLAKE.TELEMETRY.EVENTS. Verified 2026-08-18:
--   the notebook's container produced LOG, METRIC, and EVENT records with no
--   configuration changes. Do NOT run ALTER ACCOUNT SET EVENT_TABLE — the default
--   is already active and account-wide, and overwriting it would redirect
--   telemetry for every other object in the account.
--
--   Confirm with:
--       SHOW PARAMETERS LIKE 'EVENT_TABLE' IN ACCOUNT;
--   A blank "level" column means the Snowflake default is in use.
--
-- REQUIRED PRIVILEGES
--   ACCOUNTADMIN, or the SNOWFLAKE.EVENTS_VIEWER / SNOWFLAKE.EVENTS_ADMIN
--   application role.
--
-- LATENCY
--   Expect a 3–5 minute delay before container logs appear.
--
-- WHAT IS *NOT* CAPTURED
--   Python's `logging` module defaults to WARNING inside the notebook, so
--   `logging.info(...)` calls will NOT appear. Container stdout/stderr (i.e.
--   plain `print()`) IS captured. To capture application-level logging, add to
--   the notebook:
--       import logging
--       logging.getLogger().setLevel(logging.INFO)
--
--#############################################################################

-- Object names come from scripts/00_config.sql (staged), not from this file.
--   Prerequisite (once per account):  scripts/01_bootstrap.sql
--   After editing config:             scripts/publish_config.sh
EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

-- Narrow this window before running anything below. Event table TIMESTAMP is
-- TIMESTAMP_NTZ in UTC — convert from local time first (EDT = UTC-4).
SET WINDOW_START = '2026-08-18 14:30:00'::TIMESTAMP_NTZ;
SET WINDOW_END   = '2026-08-18 17:00:00'::TIMESTAMP_NTZ;


----------------------------------
-- Q1: Which container sessions ran in the window?
----------------------------------
-- Each notebook execution gets its own service name (STPLATNOTEBOOK<id>).
-- Use the SERVICE value from here to drill into the queries below.
-- A high LOG count with a long span is the signature of a hung teardown.
SELECT
    RESOURCE_ATTRIBUTES:"snow.service.name"::STRING AS SERVICE,
    MIN(TIMESTAMP) AS FIRST_EVENT,
    MAX(TIMESTAMP) AS LAST_EVENT,
    DATEDIFF('second', MIN(TIMESTAMP), MAX(TIMESTAMP)) AS SPAN_SECS,
    COUNT_IF(RECORD_TYPE = 'LOG') AS LOGS,
    COUNT_IF(RECORD_TYPE = 'METRIC') AS METRICS
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.executable.name"::STRING = $PROJECT_NOTEBOOK
    AND TIMESTAMP BETWEEN $WINDOW_START AND $WINDOW_END
GROUP BY 1
ORDER BY SPAN_SECS DESC;


----------------------------------
-- Q2: Activity timeline — find the exact moment work stopped
----------------------------------
-- Set SERVICE below from Q1. Dense buckets are real work; a long tail of 1-2
-- logs per 10 minutes is the Grafana infra heartbeat, i.e. the container is
-- alive but the Python kernel is doing nothing.
SET SERVICE = 'STPLATNOTEBOOK23090324279216006';  -- <-- replace from Q1

SELECT
    DATE_TRUNC('minute', TIMESTAMP) AS MINUTE_BUCKET,
    COUNT(*) AS LOGS
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.service.name"::STRING = $SERVICE
    AND RECORD_TYPE = 'LOG'
GROUP BY 1
ORDER BY 1;


----------------------------------
-- Q3: Full container log stream
----------------------------------
-- The notebook's own print() output appears here, so you can follow cell-by-cell
-- progress. Cell 34's "🎉 Transcription process complete!" marks the end of all
-- notebook work — anything after that is teardown.
SELECT
    TIMESTAMP,
    RECORD:"severity_text"::STRING AS SEVERITY,
    SUBSTR(VALUE::STRING, 1, 500) AS LOG_MSG
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.service.name"::STRING = $SERVICE
    AND RECORD_TYPE = 'LOG'
ORDER BY TIMESTAMP
LIMIT 2000;


----------------------------------
-- Q4: Errors, tracebacks, and OOM indicators
----------------------------------
SELECT
    TIMESTAMP,
    RESOURCE_ATTRIBUTES:"snow.service.name"::STRING AS SERVICE,
    SUBSTR(VALUE::STRING, 1, 800) AS LOG_MSG
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.executable.name"::STRING = $PROJECT_NOTEBOOK
    AND RECORD_TYPE = 'LOG'
    AND TIMESTAMP BETWEEN $WINDOW_START AND $WINDOW_END
    AND (
        VALUE::STRING ILIKE '%Traceback%'
        OR VALUE::STRING ILIKE '%CUDA out of memory%'
        OR VALUE::STRING ILIKE '%OutOfMemory%'
        OR VALUE::STRING ILIKE '%Killed%'
        OR VALUE::STRING ILIKE '%MemoryError%'
        OR VALUE::STRING ILIKE '%SIGKILL%'
        OR VALUE::STRING ILIKE '%SIGTERM%'
        OR RECORD:"severity_text"::STRING IN ('ERROR', 'FATAL')
    )
ORDER BY TIMESTAMP;


----------------------------------
-- Q5: Hung-teardown signature
----------------------------------
-- The leaked-semaphore warning is emitted at Python interpreter shutdown. It
-- appears on EVERY run, so its presence alone is not the problem — what matters
-- is HOW LONG AFTER the notebook's last cell it arrives.
--   Normal teardown: within seconds of the final cell.
--   Hung teardown:   ~2h07m later, immediately before the container is killed
--                    with error 092848 "UNAVAILABLE: io exception".
SELECT
    TIMESTAMP,
    RESOURCE_ATTRIBUTES:"snow.service.name"::STRING AS SERVICE,
    SUBSTR(VALUE::STRING, 1, 200) AS LOG_MSG
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.executable.name"::STRING = $PROJECT_NOTEBOOK
    AND RECORD_TYPE = 'LOG'
    AND VALUE::STRING ILIKE '%leaked semaphore%'
    AND TIMESTAMP BETWEEN $WINDOW_START AND $WINDOW_END
ORDER BY TIMESTAMP DESC;


----------------------------------
-- Q6: GPU and memory metrics — rule resource pressure in or out
----------------------------------
-- AVG_AFTER_WORK_DONE isolates the idle period. If GPU memory stays pinned with
-- utilization at 0, the Whisper model is still resident on the GPU while the
-- container does nothing — pure waste, and proof the hang is not a workload.
-- Adjust WORK_DONE_AT to the timestamp of the final notebook cell from Q3.
SET WORK_DONE_AT = '2026-08-18 14:46:00'::TIMESTAMP_NTZ;

SELECT
    RECORD:"metric":"name"::STRING AS METRIC_NAME,
    MIN(TIMESTAMP) AS FIRST_SAMPLE,
    MAX(TIMESTAMP) AS LAST_SAMPLE,
    COUNT(*) AS SAMPLES,
    ROUND(MAX(VALUE::FLOAT), 2) AS PEAK,
    ROUND(AVG(VALUE::FLOAT), 2) AS AVG_OVERALL,
    ROUND(AVG(CASE WHEN TIMESTAMP > $WORK_DONE_AT THEN VALUE::FLOAT END), 2) AS AVG_AFTER_WORK_DONE
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.service.name"::STRING = $SERVICE
    AND RECORD_TYPE = 'METRIC'
GROUP BY 1
ORDER BY 1;


----------------------------------
-- Q7: Correlate with the SQL side
----------------------------------
-- Pairs the container view with EXECUTE NOTEBOOK outcomes. Note that the
-- RUN_TRANSCRIPTION_NOTEBOOK procedure swallows exceptions, so the TASK reports
-- SUCCEEDED even when EXECUTE NOTEBOOK here shows FAILED_WITH_ERROR.
SELECT
    START_TIME,
    ROUND(TOTAL_ELAPSED_TIME / 1000) AS ELAPSED_SECS,
    EXECUTION_STATUS,
    ERROR_CODE,
    ERROR_MESSAGE
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_TEXT ILIKE 'EXECUTE NOTEBOOK%' || $PROJECT_NOTEBOOK || '%'
    AND START_TIME > DATEADD('day', -14, CURRENT_TIMESTAMP())
    AND TOTAL_ELAPSED_TIME > 300000   -- runs longer than 5 minutes
ORDER BY TOTAL_ELAPSED_TIME DESC;


----------------------------------
-- Q8: Hang frequency by day
----------------------------------
-- Distinguishes the ~65s no-op launches from genuine work, and counts how often
-- teardown hangs. Long runs should only appear on days files were uploaded.
SELECT
    DATE(START_TIME) AS DAY,
    COUNT(*) AS RUNS,
    COUNT_IF(EXECUTION_STATUS = 'FAILED_WITH_ERROR') AS FAILED,
    COUNT_IF(ERROR_MESSAGE ILIKE '%UNAVAILABLE%') AS HUNG_TEARDOWNS,
    ROUND(MAX(TOTAL_ELAPSED_TIME) / 1000) AS MAX_SECS,
    ROUND(AVG(TOTAL_ELAPSED_TIME) / 1000) AS AVG_SECS
FROM SNOWFLAKE.ACCOUNT_USAGE.QUERY_HISTORY
WHERE QUERY_TEXT ILIKE 'EXECUTE NOTEBOOK%' || $PROJECT_NOTEBOOK || '%'
    AND START_TIME > DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1
ORDER BY 1 DESC;


----------------------------------
-- Q9: Hang forensics — all-thread stack dumps
----------------------------------
-- The notebook's teardown cell arms faulthandler.dump_traceback_later(120,
-- repeat=True, exit=False) as its final action. On a HEALTHY run the kernel exits
-- in well under a second, so nothing is ever dumped and this query returns only
-- the 'HANG FORENSICS ARMED' line. If the kernel fails to exit, every thread's
-- stack is dumped every 120s for the life of the hang.
--
-- WHY THIS EXISTS: telemetry already proved WHERE it stops (after the last line of
-- the last cell, during interpreter shutdown — every cell completes first), but not
-- WHAT is blocked. A clean run showed 2 non-daemon threads (asyncio_0,
-- ScriptRunner.scriptThread), but those are present on healthy runs too, so their
-- existence proves nothing. These stacks give the actual blocking call site.
--
-- HOW TO READ IT: look for the same frame repeating across multiple dumps — a
-- stable frame is genuinely stuck, a changing one is just slow. Then decide:
--   * frame in OUR code / Snowpark / connector  -> fixable here, no port needed
--   * frame in streamlit / IPython / asyncio    -> platform-side; port or support case
--   * no dumps at all during a confirmed hang   -> wedged below Python; support case
SELECT
    TIMESTAMP,
    VALUE::STRING AS LOG_MSG
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RECORD_TYPE = 'LOG'
    AND RESOURCE_ATTRIBUTES:"snow.executable.name"::STRING = $PROJECT_NOTEBOOK
    AND TIMESTAMP > DATEADD('hour', -3, SYSTIMESTAMP())
    AND (VALUE::STRING ILIKE '%HANG FORENSICS%'
        OR VALUE::STRING ILIKE '%Timeout (0:%'      -- faulthandler dump header
        OR VALUE::STRING ILIKE '%Thread 0x%'        -- per-thread header
        OR VALUE::STRING ILIKE '%Current thread%'
        OR VALUE::STRING ILIKE '%  File "%')        -- stack frames
ORDER BY TIMESTAMP;

