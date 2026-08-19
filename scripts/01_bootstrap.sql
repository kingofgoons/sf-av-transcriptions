--#############################################################################
-- 01_bootstrap.sql — ONE-TIME account bootstrap for the deployment config store
--#############################################################################
--
-- RUN THIS ONCE, EVER. Not once per deployment — once per account.
--
-- WHAT IT DOES
--   Creates a small, deployment-independent home for the shared config file that
--   every other script reads via EXECUTE IMMEDIATE FROM.
--
-- WHY A SEPARATE DATABASE
--   The config store cannot live in TRANSCRIPTION_DB_V2, because 02_setup.sql is
--   what CREATES that database. On a fresh deployment the config would not exist
--   yet — a bootstrap cycle. So config lives outside any single deployment, which
--   also lets one config file serve parallel deployments (V2, DEV, ...).
--
--   The user stage (@~) would avoid needing a database, but Snowflake does not
--   document @~ as a supported source for EXECUTE IMMEDIATE FROM, so a named
--   internal stage is used instead.
--
-- AFTER RUNNING THIS
--   1. Edit scripts/00_config.sql for your deployment.
--   2. Publish it:  ./publish_config.sh
--   3. Every other script picks it up automatically.
--
--#############################################################################

USE ROLE SYSADMIN;

-- Deployment-independent config home. Metadata + one small file; negligible cost.
CREATE DATABASE IF NOT EXISTS TRANSCRIPTION_DEPLOY
    COMMENT = 'Deployment tooling for the AV transcription project. Holds the shared config file read by all setup scripts via EXECUTE IMMEDIATE FROM. Not part of any single deployment.';

CREATE SCHEMA IF NOT EXISTS TRANSCRIPTION_DEPLOY.PUBLIC;

-- Stage holding 00_config.sql. Server-side encryption so the file stays readable
-- to EXECUTE IMMEDIATE FROM (client-side encryption would not be).
CREATE STAGE IF NOT EXISTS TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS
    DIRECTORY = (ENABLE = TRUE)
    ENCRYPTION = (TYPE = 'SNOWFLAKE_SSE')
    COMMENT = 'Shared SQL includes. 00_config.sql is read from here by 02_setup, 03_automate, 07_reset, 08_telemetry_debug, 999_teardown and the av.uploader scripts.';

-- EXECUTE IMMEDIATE FROM requires READ on an internal stage.
GRANT USAGE ON DATABASE TRANSCRIPTION_DEPLOY TO ROLE SYSADMIN;
GRANT USAGE ON SCHEMA TRANSCRIPTION_DEPLOY.PUBLIC TO ROLE SYSADMIN;
GRANT READ, WRITE ON STAGE TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS TO ROLE SYSADMIN;

-- The uploader service role does NOT need this - it never runs setup scripts.

----------------------------------
-- Verify
----------------------------------
SHOW STAGES LIKE 'SCRIPTS' IN SCHEMA TRANSCRIPTION_DEPLOY.PUBLIC;

-- Should be empty until you run ./publish_config.sh
LIST @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS;
