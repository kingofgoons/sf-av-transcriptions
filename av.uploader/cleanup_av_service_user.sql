-- =====================================================
-- AV UPLOADER SERVICE USER CLEANUP SCRIPT
-- =====================================================
-- This script removes the AV uploader service user and role
-- NOTE: This does NOT remove the transcription database, schema, or warehouse
--       as those are part of the main transcription project
-- =====================================================

-- =====================================================
-- 1. Revoke Role from User (as SECURITYADMIN)
-- =====================================================

USE ROLE SECURITYADMIN;

-- Revoke the role from the service user
REVOKE ROLE AV_UPLOADER_SERVICE_ROLE FROM USER AV_UPLOADER_SERVICE_USER;

-- =====================================================
-- 2. Drop Service User (as USERADMIN)
-- =====================================================

USE ROLE USERADMIN;

-- Drop the service user
DROP USER IF EXISTS AV_UPLOADER_SERVICE_USER;

-- =====================================================
-- 3. Revoke All Grants from Role (as SECURITYADMIN)
-- =====================================================

USE ROLE SECURITYADMIN;

-- Revoke stage privileges
REVOKE READ, WRITE ON STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE FROM ROLE AV_UPLOADER_SERVICE_ROLE;

-- Revoke table privileges
REVOKE SELECT ON TABLE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIPTION_RESULTS FROM ROLE AV_UPLOADER_SERVICE_ROLE;
REVOKE SELECT ON VIEW TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIPTION_SUMMARY FROM ROLE AV_UPLOADER_SERVICE_ROLE;

-- Revoke database and schema privileges
REVOKE USAGE ON SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA FROM ROLE AV_UPLOADER_SERVICE_ROLE;
REVOKE USAGE ON DATABASE TRANSCRIPTION_DB FROM ROLE AV_UPLOADER_SERVICE_ROLE;

-- Revoke warehouse privileges
REVOKE USAGE ON WAREHOUSE TRANSCRIPTION_WH FROM ROLE AV_UPLOADER_SERVICE_ROLE;

-- =====================================================
-- 4. Drop Role (as SECURITYADMIN)
-- =====================================================

DROP ROLE IF EXISTS AV_UPLOADER_SERVICE_ROLE;

-- =====================================================
-- 5. Verify Cleanup
-- =====================================================

-- Verify user is removed
USE ROLE ACCOUNTADMIN;
SHOW USERS LIKE 'AV_UPLOADER_SERVICE_USER';  -- Should return no results

-- Verify role is removed
USE ROLE SECURITYADMIN;
SHOW ROLES LIKE 'AV_UPLOADER_SERVICE_ROLE';  -- Should return no results

-- Display cleanup status
SELECT 'AV Uploader service user cleanup complete!' AS status,
       'Service user and role have been removed' AS message;

-- =====================================================
-- Cleanup Complete
-- =====================================================

/*
OBJECTS REMOVED:
- User: AV_UPLOADER_SERVICE_USER (service account)
- Role: AV_UPLOADER_SERVICE_ROLE
- All grants to: AV_UPLOADER_SERVICE_ROLE

OBJECTS RETAINED:
- Database: TRANSCRIPTION_DB
- Schema: TRANSCRIPTION_SCHEMA
- Warehouse: TRANSCRIPTION_WH
- Stage: AUDIO_VIDEO_STAGE
(These are part of the main transcription project)

RETENTION PERIOD:
- Dropped user/role are retained for DATA_RETENTION_TIME_IN_DAYS
- They can be restored using UNDROP commands during retention period

TO RESTORE (within retention period):
  USE ROLE ACCOUNTADMIN;
  UNDROP USER AV_UPLOADER_SERVICE_USER;
  USE ROLE SECURITYADMIN;
  UNDROP ROLE AV_UPLOADER_SERVICE_ROLE;
  
  -- Then re-run the grants section of create_av_service_user.sql
*/

