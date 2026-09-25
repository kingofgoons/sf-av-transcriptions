--#############################################################################
-- 09_drift_check.sql — does the ACCOUNT match the CODE?
--
-- Read-only. Reports EXPECTED (from 00_config.sql, via V_PROJECT_CONFIG) against
-- ACTUAL (from SHOW output) for every deployed setting that can silently drift.
-- Writes nothing, alters nothing, costs a warehouse tick.
--
-- Run it: after 02_setup.sql, after any ALTER done by hand, and before believing
-- anything this repo says about the deployment.
--
--     snow sql -f scripts/09_drift_check.sql -c DEMO
--
-- WHY THIS EXISTS
--
-- On 2026-09-24 two settings were found to have drifted, and neither drift was a
-- decision anyone made:
--
--     GPU pool AUTO_SUSPEND_SECS   code: never set   account: 3600   cost: ~12.6 cr/35d
--     Warehouse AUTO_SUSPEND       code: 60          account: 600
--
-- The mechanism is `CREATE ... IF NOT EXISTS`, which the project uses everywhere for
-- good reasons - it protects 493 irreplaceable transcripts and a live compute pool from
-- a careless re-run. The cost is that it is a no-op against an existing object, so a
-- value written only into a CREATE is never enforced. The script asserts a setting; the
-- account ignores it; nobody notices because nothing compares them.
--
-- Reading the script is therefore NOT evidence of how the account is configured. That
-- is the whole problem, and the only fix is to compare. 02_setup.sql now also ALTERs to
-- reconcile, so the expected workflow is: this check flags drift -> re-run 02_setup.sql
-- -> this check comes back clean.
--
-- WHAT IT DELIBERATELY DOES NOT CHECK
--
-- Table schemas. Adding a column to a CREATE TABLE IF NOT EXISTS is also unenforced,
-- but reconciling that automatically means generating ALTER TABLE against live data.
-- That belongs in migration/, reviewed by a human, not in a drift checker.
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

USE ROLE ACCOUNTADMIN;
USE WAREHOUSE IDENTIFIER($PROJECT_WH);

-- SHOW cannot be joined directly, so each is captured via RESULT_SCAN.
--
-- NOTE: `SHOW ... LIKE $VARIABLE` is NOT valid - SHOW takes a literal pattern, and a
-- session variable there fails with "syntax error ... unexpected '$PROJECT_...'".
-- Rather than build these with dynamic SQL, SHOW everything and filter by name against
-- the config view below. (scripts/07_reset.sql line 58 has the same latent bug.)
SHOW COMPUTE POOLS;
SET Q_POOL = LAST_QUERY_ID();

SHOW WAREHOUSES;
SET Q_WH = LAST_QUERY_ID();

SHOW TASKS IN ACCOUNT;
SET Q_TASK = LAST_QUERY_ID();

SHOW STAGES IN SCHEMA IDENTIFIER($FQ_SCHEMA);
SET Q_STAGE = LAST_QUERY_ID();

WITH cfg AS (
    SELECT * FROM TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG
), pool AS (
    SELECT "auto_suspend_secs"::NUMBER AS AUTO_SUSPEND_SECS,
           "min_nodes"::NUMBER         AS MIN_NODES,
           "max_nodes"::NUMBER         AS MAX_NODES,
           "instance_family"::VARCHAR  AS INSTANCE_FAMILY,
           "state"::VARCHAR            AS STATE
    FROM TABLE(RESULT_SCAN($Q_POOL))
    WHERE "name"::VARCHAR = (SELECT PROJECT_COMPUTE_POOL FROM cfg)
), wh AS (
    -- Normalise the size string. Snowflake accepts 'XSMALL' in DDL but REPORTS 'X-Small'
    -- in SHOW output, so a naive comparison flags permanent drift on a correct warehouse.
    -- That false positive matters more than it looks: a check that cries wolf gets
    -- ignored, and then the real drift it exists to catch goes unnoticed too.
    SELECT "auto_suspend"::NUMBER AS AUTO_SUSPEND,
           REPLACE(UPPER("size"::VARCHAR), '-', '') AS WH_SIZE
    FROM TABLE(RESULT_SCAN($Q_WH))
    WHERE "name"::VARCHAR = (SELECT PROJECT_WH FROM cfg)
), task AS (
    SELECT "state"::VARCHAR     AS STATE,
           "owner"::VARCHAR     AS OWNER_,
           "schedule"::VARCHAR  AS SCHEDULE_
    FROM TABLE(RESULT_SCAN($Q_TASK))
    WHERE "name"::VARCHAR = (SELECT PROJECT_TASK_TRANSCRIBE FROM cfg)
), payload_stage AS (
    SELECT COUNT(*)::NUMBER                      AS N_FOUND,
           MAX("owner"::VARCHAR)                 AS OWNER_
    FROM TABLE(RESULT_SCAN($Q_STAGE))
    WHERE "name"::VARCHAR = (SELECT PROJECT_STAGE_PAYLOAD FROM cfg)
), checks AS (
    SELECT 'pool.AUTO_SUSPEND_SECS' AS SETTING,
           cfg.POOL_AUTO_SUSPEND_SECS::VARCHAR AS EXPECTED,
           pool.AUTO_SUSPEND_SECS::VARCHAR     AS ACTUAL,
           'cost: ~0.6 cr/hr of idle GPU while non-compliant' AS WHY_IT_MATTERS
    FROM pool, cfg
    UNION ALL
    SELECT 'pool.MIN_NODES', cfg.POOL_MIN_NODES::VARCHAR, pool.MIN_NODES::VARCHAR,
           'a floor above 1 holds nodes the pipeline never uses'
    FROM pool, cfg
    UNION ALL
    SELECT 'pool.MAX_NODES', cfg.POOL_MAX_NODES::VARCHAR, pool.MAX_NODES::VARCHAR,
           'caps blast radius of a runaway launch pattern'
    FROM pool, cfg
    UNION ALL
    SELECT 'pool.INSTANCE_FAMILY', cfg.POOL_INSTANCE_FAMILY, pool.INSTANCE_FAMILY,
           'NOT auto-reconciled - changing it replaces nodes, so fix by hand'
    FROM pool, cfg
    UNION ALL
    SELECT 'warehouse.AUTO_SUSPEND', cfg.WH_AUTO_SUSPEND::VARCHAR, wh.AUTO_SUSPEND::VARCHAR,
           'too LOW also costs money - 60s minimum billing on every resume'
    FROM wh, cfg
    UNION ALL
    SELECT 'warehouse.SIZE', REPLACE(UPPER(cfg.WH_SIZE), '-', ''), wh.WH_SIZE,
           'XSMALL is sufficient; Whisper runs on the GPU pool, not here'
    FROM wh, cfg
    UNION ALL
    -- Not a config value, but the single most expensive thing to get wrong: a task
    -- owned by the wrong role fails with "Unknown user-defined function".
    SELECT 'task.OWNER', 'SYSADMIN', task.OWNER_,
           'task runs with owner privileges; a mismatch breaks the gate procedure'
    FROM task
    UNION ALL
    -- The deprecated polling schedule caused a ~230-credit runaway. It must stay absent.
    SELECT 'task.SCHEDULE', '(none - event-driven)',
           COALESCE(NULLIF(TRIM(task.SCHEDULE_), ''), '(none - event-driven)'),
           'a schedule here launches no-op GPU containers on a timer'
    FROM task
    UNION ALL
    -- The payload stage holds the job spec the gate procedure reads. It must EXIST, or
    -- the launch fails after the gate has already decided there is work to do.
    SELECT 'payload_stage.EXISTS', '1', payload_stage.N_FOUND::VARCHAR,
           'gate reads SPECIFICATION_FILE from here; absent means no run can launch'
    FROM payload_stage
    UNION ALL
    -- Ownership is not cosmetic here. The gate procedure runs as SYSADMIN, so a stage
    -- owned by anything else leaves SYSADMIN with no privilege on it and the launch
    -- fails at runtime against a stage that looks perfectly healthy. This stage was
    -- ACCOUNTADMIN-owned for a day after being created ad hoc during the port spike.
    SELECT 'payload_stage.OWNER', 'SYSADMIN', COALESCE(payload_stage.OWNER_, '(absent)'),
           'SYSADMIN gate procedure cannot read a stage it has no privilege on'
    FROM payload_stage
)
SELECT CASE WHEN EXPECTED = ACTUAL THEN 'OK' ELSE '*** DRIFT ***' END AS VERDICT,
       SETTING,
       EXPECTED,
       ACTUAL,
       CASE WHEN EXPECTED = ACTUAL THEN '' ELSE WHY_IT_MATTERS END AS WHY_IT_MATTERS
FROM checks
ORDER BY CASE WHEN EXPECTED = ACTUAL THEN 1 ELSE 0 END, SETTING;

-- Reminder rather than an assertion: the staged config is what every script actually
-- reads, so a local edit that was never published is its own category of drift.
-- scripts/check_config_fresh.sh compares the two by content hash.
SELECT CONFIG_REVISION AS STAGED_CONFIG_REVISION,
       'compare against scripts/config_revision.sh output on your working copy' AS NOTE
FROM TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG;
