-- =====================================================
-- TRANSCRIPTION PROJECT TEARDOWN SCRIPT
-- =====================================================
-- This script removes all Snowflake objects created by this project.
-- 
-- USAGE: 
--   1. Update the configuration variables below (or copy from 00_config.sql)
--   2. Uncomment the section(s) you want to execute.
--   Each level includes all previous levels.
--
-- LEVELS:
--   Level 1: Reset automation (tasks, stream, procedure)
--   Level 2: Reset compute (+ notebook, compute pool, integrations)
--   Level 3: Reset data (+ tables, stages) 
--   Level 4: Full teardown (+ database, warehouse)
--
-- WARNING: This script is DESTRUCTIVE. Data cannot be recovered
--          after the retention period expires.
-- =====================================================

--#############################################################################
-- IMPORTANT: Copy and paste the configuration block from 00_config.sql here
-- before running this script. This allows you to teardown parallel instances.
--#############################################################################

-- Core naming - change these to teardown a specific deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH';              -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL';  -- GPU compute pool name

-- Derived names (automatically built from above)
SET PROJECT_NOTEBOOK = 'TRANSCRIBE_AV_FILES';     -- Notebook name
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STAGE_NB = 'NOTEBOOK_STAGE';          -- Stage for notebook assets -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STREAM = 'AV_STAGE_STREAM';           -- Stream for file detection
SET PROJECT_TASK_TRANSCRIBE = 'TRANSCRIBE_NEW_FILES_TASK';  -- Transcription task
SET PROJECT_TASK_REFRESH = 'REFRESH_STAGE_DIRECTORY_TASK';  -- Stage refresh task

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule';
SET PROJECT_PYPI_RULE = 'pypi_network_rule';

--#############################################################################
-- END CONFIGURATION
--#############################################################################

-- Set context
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);
USE WAREHOUSE IDENTIFIER($PROJECT_WH);

-- =====================================================
-- LEVEL 1: RESET AUTOMATION
-- =====================================================
-- Removes: Tasks, Stream, Stored Procedure
-- Keeps: Notebook, Compute Pool, Tables, Stages
-- Use when: Automation is broken, need to reconfigure tasks
-- =====================================================

/*
-- Suspend tasks first (required before dropping)
ALTER TASK IF EXISTS IDENTIFIER($PROJECT_TASK_TRANSCRIBE) SUSPEND;
ALTER TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH) SUSPEND;

-- Drop tasks
DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_TRANSCRIBE);
DROP TASK IF EXISTS IDENTIFIER($PROJECT_TASK_REFRESH);

-- Drop stream
DROP STREAM IF EXISTS IDENTIFIER($PROJECT_STREAM);

-- Drop stored procedure
DROP PROCEDURE IF EXISTS RUN_TRANSCRIPTION_NOTEBOOK();

-- Verify Level 1 cleanup
SELECT 'Level 1 Complete' AS status;
SHOW TASKS IN SCHEMA;
SHOW STREAMS IN SCHEMA;
*/

-- =====================================================
-- LEVEL 2: RESET COMPUTE
-- =====================================================
-- Removes: Level 1 + Notebook, Compute Pool, Integrations, Network Rules
-- Keeps: Tables, Stages (your data)
-- Use when: Need to recreate notebook or compute pool
-- =====================================================

/*
-- First run Level 1 above, then:

-- Drop notebook
DROP NOTEBOOK IF EXISTS IDENTIFIER($PROJECT_NOTEBOOK);

-- Switch to ACCOUNTADMIN for compute pool and integrations
USE ROLE ACCOUNTADMIN;

-- Stop and drop compute pool
ALTER COMPUTE POOL IF EXISTS IDENTIFIER($PROJECT_COMPUTE_POOL) STOP ALL;
DROP COMPUTE POOL IF EXISTS IDENTIFIER($PROJECT_COMPUTE_POOL);

-- Drop external access integrations
DROP INTEGRATION IF EXISTS IDENTIFIER($PROJECT_PYPI_INTEGRATION);
DROP INTEGRATION IF EXISTS IDENTIFIER($PROJECT_ALLOW_ALL_INTEGRATION);

-- Drop network rules (back to SYSADMIN)
USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);
DROP NETWORK RULE IF EXISTS IDENTIFIER($PROJECT_PYPI_RULE);
DROP NETWORK RULE IF EXISTS IDENTIFIER($PROJECT_ALLOW_ALL_RULE);

-- Verify Level 2 cleanup
SELECT 'Level 2 Complete' AS status;
SHOW NOTEBOOKS IN SCHEMA;
SHOW COMPUTE POOLS;
*/

-- =====================================================
-- LEVEL 3: RESET DATA
-- =====================================================
-- Removes: Level 1-2 + Tables, Views, Stages, File Format
-- Keeps: Database, Schema, Warehouse
-- Use when: Need clean slate but want to keep DB structure
-- WARNING: This deletes all transcription results and uploaded files!
-- =====================================================

/*
-- First run Levels 1-2 above, then:

USE ROLE SYSADMIN;
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);

-- Drop view first (depends on table)
DROP VIEW IF EXISTS TRANSCRIPTION_SUMMARY;

-- Drop results table (YOUR TRANSCRIPTION DATA)
DROP TABLE IF EXISTS IDENTIFIER($PROJECT_RESULTS_TABLE);

-- Drop file format
DROP FILE FORMAT IF EXISTS CSVFORMAT;

-- Drop stages (YOUR UPLOADED FILES)
-- WARNING: This deletes all files in the stages!
DROP STAGE IF EXISTS IDENTIFIER($PROJECT_STAGE_NB);
DROP STAGE IF EXISTS IDENTIFIER($PROJECT_STAGE_AV);

-- Verify Level 3 cleanup
SELECT 'Level 3 Complete' AS status;
SHOW TABLES IN SCHEMA;
SHOW STAGES IN SCHEMA;
*/

-- =====================================================
-- LEVEL 4: FULL TEARDOWN
-- =====================================================
-- Removes: Everything - Database, Schema, Warehouse
-- Use when: Completely removing the project from your account
-- WARNING: This is irreversible after retention period!
-- =====================================================

/*
-- First run Levels 1-3 above, then:

USE ROLE SYSADMIN;

-- Drop schema (should be empty after Level 3)
SET SQL_CMD = 'DROP SCHEMA IF EXISTS ' || $PROJECT_DB || '.' || $PROJECT_SCHEMA;
EXECUTE IMMEDIATE $SQL_CMD;

-- Drop database
DROP DATABASE IF EXISTS IDENTIFIER($PROJECT_DB);

-- Drop warehouse
DROP WAREHOUSE IF EXISTS IDENTIFIER($PROJECT_WH);

-- Verify Level 4 cleanup
SELECT 'Level 4 Complete - Full Teardown' AS status;
-- Note: Can't use IDENTIFIER() in SHOW LIKE, use actual names or omit LIKE clause
SHOW DATABASES;
SHOW WAREHOUSES;
*/

-- =====================================================
-- OPTIONAL: CLEANUP SERVICE USER
-- =====================================================
-- If you created the AV uploader service user, run:
-- av.uploader/cleanup_av_service_user.sql
-- =====================================================

-- =====================================================
-- RECOVERY (within retention period)
-- =====================================================
/*
-- Undrop database (restores everything inside it)
UNDROP DATABASE IDENTIFIER($PROJECT_DB);

-- Undrop individual objects (use dynamic SQL for fully qualified names)
EXECUTE IMMEDIATE 'UNDROP TABLE ' || $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_RESULTS_TABLE;
EXECUTE IMMEDIATE 'UNDROP STAGE ' || $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STAGE_AV;

-- Check retention settings
SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN ACCOUNT;
*/

-- =====================================================
-- QUICK REFERENCE: What each level removes
-- =====================================================
/*
Level 1 - Automation:
  - $PROJECT_TASK_TRANSCRIBE (task)
  - $PROJECT_TASK_REFRESH (task)
  - $PROJECT_STREAM (stream)
  - RUN_TRANSCRIPTION_NOTEBOOK (procedure)

Level 2 - Compute:
  - $PROJECT_NOTEBOOK (notebook)
  - $PROJECT_COMPUTE_POOL (compute pool)
  - $PROJECT_PYPI_INTEGRATION (integration)
  - $PROJECT_ALLOW_ALL_INTEGRATION (integration)
  - $PROJECT_PYPI_RULE (network rule)
  - $PROJECT_ALLOW_ALL_RULE (network rule)

Level 3 - Data:
  - $PROJECT_RESULTS_TABLE (table)
  - TRANSCRIPTION_SUMMARY (view)
  - CSVFORMAT (file format)
  - $PROJECT_STAGE_NB (stage)
  - $PROJECT_STAGE_AV (stage)

Level 4 - Infrastructure:
  - $PROJECT_SCHEMA (schema)
  - $PROJECT_DB (database)
  - $PROJECT_WH (warehouse)
*/
