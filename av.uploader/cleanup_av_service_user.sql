-- =====================================================
-- AV UPLOADER SERVICE USER CLEANUP SCRIPT
-- =====================================================
-- This script removes the AV uploader service user and role
-- NOTE: This does NOT remove the transcription database, schema, or warehouse
--       as those are part of the main transcription project
-- =====================================================

--#############################################################################
-- IMPORTANT: Copy and paste the configuration block from 00_config.sql here
-- before running this script. This allows you to cleanup service users for
-- parallel deployments.
--#############################################################################

-- Core naming - change these to match your deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH';              -- Warehouse name

-- Stage and table names -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table

-- Service account naming - update suffix for parallel deployments
SET SERVICE_ROLE = 'AV_UPLOADER_SERVICE_ROLE';
SET SERVICE_USER = 'AV_UPLOADER_SERVICE_USER';

--#############################################################################
-- END CONFIGURATION
--#############################################################################

-- Build fully qualified names
SET FQ_SCHEMA = $PROJECT_DB || '.' || $PROJECT_SCHEMA;
SET FQ_STAGE = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STAGE_AV;
SET FQ_TABLE = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_RESULTS_TABLE;
SET FQ_VIEW = $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.TRANSCRIPTION_SUMMARY';

-- =====================================================
-- 1. Revoke Role from User (as SECURITYADMIN)
-- =====================================================

USE ROLE SECURITYADMIN;

-- Revoke the role from the service user (ignore errors if already revoked)
SET SQL_CMD = 'REVOKE ROLE ' || $SERVICE_ROLE || ' FROM USER ' || $SERVICE_USER;
EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- 2. Drop Service User (as USERADMIN)
-- =====================================================

USE ROLE USERADMIN;

-- Drop the service user
SET SQL_CMD = 'DROP USER IF EXISTS ' || $SERVICE_USER;
EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- 3. Revoke All Grants from Role (as SECURITYADMIN)
-- =====================================================

USE ROLE SECURITYADMIN;

-- Revoke stage privileges
SET SQL_CMD = 'REVOKE READ, WRITE ON STAGE ' || $FQ_STAGE || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Revoke table privileges
SET SQL_CMD = 'REVOKE SELECT ON TABLE ' || $FQ_TABLE || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

SET SQL_CMD = 'REVOKE SELECT ON VIEW ' || $FQ_VIEW || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Revoke database and schema privileges
SET SQL_CMD = 'REVOKE USAGE ON SCHEMA ' || $FQ_SCHEMA || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

SET SQL_CMD = 'REVOKE USAGE ON DATABASE ' || $PROJECT_DB || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Revoke warehouse privileges
SET SQL_CMD = 'REVOKE USAGE ON WAREHOUSE ' || $PROJECT_WH || ' FROM ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- 4. Drop Role (as SECURITYADMIN)
-- =====================================================

SET SQL_CMD = 'DROP ROLE IF EXISTS ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- 5. Verify Cleanup
-- =====================================================

-- Verify user is removed
USE ROLE ACCOUNTADMIN;
SET SQL_CMD = 'SHOW USERS LIKE ''' || $SERVICE_USER || '''';
EXECUTE IMMEDIATE $SQL_CMD;  -- Should return no results

-- Verify role is removed
USE ROLE SECURITYADMIN;
SET SQL_CMD = 'SHOW ROLES LIKE ''' || $SERVICE_ROLE || '''';
EXECUTE IMMEDIATE $SQL_CMD;  -- Should return no results

-- Display cleanup status
SELECT 'AV Uploader service user cleanup complete!' AS status,
       'Service user and role have been removed' AS message;

-- =====================================================
-- Cleanup Complete
-- =====================================================

/*
OBJECTS REMOVED:
- User: (configured SERVICE_USER)
- Role: (configured SERVICE_ROLE)
- All grants to the service role

OBJECTS RETAINED:
- Database: (configured PROJECT_DB)
- Schema: (configured PROJECT_SCHEMA)
- Warehouse: (configured PROJECT_WH)
- Stage: AUDIO_VIDEO_STAGE
(These are part of the main transcription project)

RETENTION PERIOD:
- Dropped user/role are retained for DATA_RETENTION_TIME_IN_DAYS
- They can be restored using UNDROP commands during retention period

TO RESTORE (within retention period):
  USE ROLE ACCOUNTADMIN;
  UNDROP USER <SERVICE_USER>;
  USE ROLE SECURITYADMIN;
  UNDROP ROLE <SERVICE_ROLE>;
  
  -- Then re-run the grants section of create_av_service_user.sql
*/
