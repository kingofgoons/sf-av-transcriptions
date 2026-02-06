-- =====================================================
-- TRANSCRIPTION PROJECT TEARDOWN SCRIPT
-- =====================================================
-- This script removes all Snowflake objects created by this project.
-- 
-- USAGE: Uncomment the section(s) you want to execute.
--        Each level includes all previous levels.
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

-- Set context
USE ROLE SYSADMIN;
USE DATABASE TRANSCRIPTION_DB;
USE SCHEMA TRANSCRIPTION_SCHEMA;
USE WAREHOUSE TRANSCRIPTION_WH;

-- =====================================================
-- LEVEL 1: RESET AUTOMATION
-- =====================================================
-- Removes: Tasks, Stream, Stored Procedure
-- Keeps: Notebook, Compute Pool, Tables, Stages
-- Use when: Automation is broken, need to reconfigure tasks
-- =====================================================

/*
-- Suspend tasks first (required before dropping)
ALTER TASK IF EXISTS TRANSCRIBE_NEW_FILES_TASK SUSPEND;
ALTER TASK IF EXISTS REFRESH_STAGE_DIRECTORY_TASK SUSPEND;

-- Drop tasks
DROP TASK IF EXISTS TRANSCRIBE_NEW_FILES_TASK;
DROP TASK IF EXISTS REFRESH_STAGE_DIRECTORY_TASK;

-- Drop stream
DROP STREAM IF EXISTS AV_STAGE_STREAM;

-- Drop stored procedure
DROP PROCEDURE IF EXISTS RUN_TRANSCRIPTION_NOTEBOOK();

-- Verify Level 1 cleanup
SELECT 'Level 1 Complete' AS status;
SHOW TASKS LIKE '%TRANSCRI%' IN SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;
SHOW STREAMS LIKE '%AV_STAGE%' IN SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;
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
DROP NOTEBOOK IF EXISTS TRANSCRIBE_AV_FILES;

-- Switch to ACCOUNTADMIN for compute pool and integrations
USE ROLE ACCOUNTADMIN;

-- Stop and drop compute pool
ALTER COMPUTE POOL IF EXISTS TRANSCRIPTION_GPU_POOL STOP ALL;
DROP COMPUTE POOL IF EXISTS TRANSCRIPTION_GPU_POOL;

-- Drop external access integrations
DROP INTEGRATION IF EXISTS TRANSCRIPTION_PYPI_ACCESS_INTEGRATION;
DROP INTEGRATION IF EXISTS TRANSCRIPTION_ALLOW_ALL_INTEGRATION;

-- Drop network rules (back to SYSADMIN)
USE ROLE SYSADMIN;
DROP NETWORK RULE IF EXISTS TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.pypi_network_rule;
DROP NETWORK RULE IF EXISTS TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.allow_all_rule;

-- Verify Level 2 cleanup
SELECT 'Level 2 Complete' AS status;
SHOW NOTEBOOKS LIKE '%TRANSCRI%' IN SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;
SHOW COMPUTE POOLS LIKE '%TRANSCRI%';
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
USE DATABASE TRANSCRIPTION_DB;
USE SCHEMA TRANSCRIPTION_SCHEMA;

-- Drop view first (depends on table)
DROP VIEW IF EXISTS TRANSCRIPTION_SUMMARY;

-- Drop results table (YOUR TRANSCRIPTION DATA)
DROP TABLE IF EXISTS TRANSCRIPTION_RESULTS;

-- Drop file format
DROP FILE FORMAT IF EXISTS CSVFORMAT;

-- Drop stages (YOUR UPLOADED FILES)
-- WARNING: This deletes all files in the stages!
DROP STAGE IF EXISTS NOTEBOOK_STAGE;
DROP STAGE IF EXISTS AUDIO_VIDEO_STAGE;

-- Verify Level 3 cleanup
SELECT 'Level 3 Complete' AS status;
SHOW TABLES LIKE '%TRANSCRI%' IN SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;
SHOW STAGES IN SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;
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
DROP SCHEMA IF EXISTS TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA;

-- Drop database
DROP DATABASE IF EXISTS TRANSCRIPTION_DB;

-- Drop warehouse
DROP WAREHOUSE IF EXISTS TRANSCRIPTION_WH;

-- Verify Level 4 cleanup
SELECT 'Level 4 Complete - Full Teardown' AS status;
SHOW DATABASES LIKE 'TRANSCRIPTION_DB';
SHOW WAREHOUSES LIKE 'TRANSCRIPTION_WH';
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
UNDROP DATABASE TRANSCRIPTION_DB;

-- Undrop individual objects
UNDROP TABLE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIPTION_RESULTS;
UNDROP STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE;

-- Check retention settings
SHOW PARAMETERS LIKE 'DATA_RETENTION_TIME_IN_DAYS' IN ACCOUNT;
*/

-- =====================================================
-- QUICK REFERENCE: What each level removes
-- =====================================================
/*
Level 1 - Automation:
  - TRANSCRIBE_NEW_FILES_TASK
  - REFRESH_STAGE_DIRECTORY_TASK
  - AV_STAGE_STREAM
  - RUN_TRANSCRIPTION_NOTEBOOK (procedure)

Level 2 - Compute:
  - TRANSCRIBE_AV_FILES (notebook)
  - TRANSCRIPTION_GPU_POOL
  - TRANSCRIPTION_PYPI_ACCESS_INTEGRATION
  - TRANSCRIPTION_ALLOW_ALL_INTEGRATION
  - pypi_network_rule
  - allow_all_rule

Level 3 - Data:
  - TRANSCRIPTION_RESULTS (table)
  - TRANSCRIPTION_SUMMARY (view)
  - CSVFORMAT (file format)
  - NOTEBOOK_STAGE
  - AUDIO_VIDEO_STAGE

Level 4 - Infrastructure:
  - TRANSCRIPTION_SCHEMA
  - TRANSCRIPTION_DB
  - TRANSCRIPTION_WH
*/
