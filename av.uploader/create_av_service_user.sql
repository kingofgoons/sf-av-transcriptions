-- =====================================================
-- SERVICE USER SETUP FOR AV TRANSCRIPTION UPLOADER
-- =====================================================
-- Run this script as ACCOUNTADMIN or with appropriate privileges

--#############################################################################
-- CONFIGURATION IS LOADED, NOT PASTED.
--   All PROJECT_* / FQ_* / SERVICE_* session variables come from
--   scripts/00_config.sql, staged at @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS.
--
--   Prerequisite (once per account):  scripts/01_bootstrap.sql
--   After editing config:             scripts/publish_config.sh
--
--   NOTE: until 2026-08-18 this file carried its own hardcoded copy of the config
--   block that still pointed at the V1 names (TRANSCRIPTION_DB /
--   TRANSCRIPTION_SCHEMA / TRANSCRIPTION_WH) while the active deployment was V2.
--   That is why AV_UPLOADER_SERVICE_ROLE ended up holding grants on BOTH V1 and V2
--   objects. Loading config from one place removes that failure mode.
--#############################################################################

EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

-- Step 1: Use SECURITYADMIN to create role
USE ROLE SECURITYADMIN;

SET SQL_CMD = 'CREATE ROLE IF NOT EXISTS ' || $SERVICE_ROLE || 
              ' COMMENT = ''Role for AV transcription uploader service account''';
EXECUTE IMMEDIATE $SQL_CMD;

-- Step 2: Use USERADMIN to create the service user
USE ROLE USERADMIN;

-- TYPE = SERVICE designates this as a service account (not a person)
-- Note: Using anonymous block because SQL exceeds 256-byte limit
DECLARE
    sql_cmd VARCHAR;
BEGIN
    sql_cmd := 'CREATE USER IF NOT EXISTS ' || $SERVICE_USER || 
               ' TYPE = SERVICE' ||
               ' DEFAULT_WAREHOUSE = ' || $PROJECT_WH ||
               ' DEFAULT_NAMESPACE = ' || $FQ_SCHEMA ||
               ' DEFAULT_ROLE = ' || $SERVICE_ROLE ||
               ' COMMENT = ''Service account for uploading audio/video files with RSA key authentication''';
    EXECUTE IMMEDIATE sql_cmd;
END;

-- Step 3: Use SECURITYADMIN to grant roles and privileges
USE ROLE SECURITYADMIN;

-- Grant the role to the service user
SET SQL_CMD = 'GRANT ROLE ' || $SERVICE_ROLE || ' TO USER ' || $SERVICE_USER;
EXECUTE IMMEDIATE $SQL_CMD;

-- Grant database and schema privileges
SET SQL_CMD = 'GRANT USAGE ON DATABASE ' || $PROJECT_DB || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

SET SQL_CMD = 'GRANT USAGE ON SCHEMA ' || $FQ_SCHEMA || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Grant privileges needed for stage operations (READ and WRITE to upload files)
SET SQL_CMD = 'GRANT READ, WRITE ON STAGE ' || $FQ_STAGE_AV || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Grant warehouse privileges for PUT operations
SET SQL_CMD = 'GRANT USAGE ON WAREHOUSE ' || $PROJECT_WH || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Optional: Grant privileges to query transcription results (read-only)
SET SQL_CMD = 'GRANT SELECT ON TABLE ' || $FQ_RESULTS || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

SET SQL_CMD = 'GRANT SELECT ON VIEW ' || $FQ_VIEW || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Allow the uploader to trigger the transcription pipeline after uploading.
--
-- The transcription task has NO SCHEDULE - it runs only when triggered. The
-- uploader fires EXECUTE TASK at the end of a successful upload, which replaced a
-- 5-minute polling task that launched a GPU container on every tick regardless of
-- whether there was work to do.
--
-- OPERATE is the minimum privilege needed to run a task you do not own. The task
-- still executes with its OWNER's privileges (SYSADMIN), so this grant does NOT
-- give the uploader role the notebook's privileges.
--
-- Without this grant, uploads succeed but nothing transcribes them automatically.
SET SQL_CMD = 'GRANT OPERATE ON TASK ' || $FQ_TASK || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Step 4: Use USERADMIN to configure RSA public key authentication
USE ROLE USERADMIN;

-- To generate RSA key pair on your local machine:
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
--   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
-- 
-- Then copy the content between -----BEGIN PUBLIC KEY----- and -----END PUBLIC KEY-----
-- and paste it below (as a single line, without the headers)

-- IMPORTANT: Replace <RSA_PUBLIC_KEY> with your actual public key before running!
SET SQL_CMD = 'ALTER USER ' || $SERVICE_USER || ' SET RSA_PUBLIC_KEY = ''<RSA_PUBLIC_KEY>''';
EXECUTE IMMEDIATE $SQL_CMD;

-- Optional: Set a second RSA public key for key rotation
-- SET SQL_CMD = 'ALTER USER ' || $SERVICE_USER || ' SET RSA_PUBLIC_KEY_2 = ''<RSA_PUBLIC_KEY_2>''';
-- EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- VERIFICATION QUERIES
-- =====================================================

-- Verify the user was created
SET SQL_CMD = 'SHOW USERS LIKE ''' || $SERVICE_USER || '''';
EXECUTE IMMEDIATE $SQL_CMD;

-- Verify the role and grants
SET SQL_CMD = 'SHOW GRANTS TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Verify the user's RSA public key is set
SET SQL_CMD = 'DESC USER ' || $SERVICE_USER;
EXECUTE IMMEDIATE $SQL_CMD;

-- =====================================================
-- NOTES
-- =====================================================
-- 1. Replace <RSA_PUBLIC_KEY> with your actual public key value
-- 2. The public key should be a single line without BEGIN/END headers
-- 3. The service user will authenticate using the private key from client side
-- 4. Password authentication is disabled by default for SERVICE type users
-- 5. Store the private key (rsa_key.p8) securely in the project root directory
-- 6. Do NOT commit rsa_key.p8 to git - it's already in .gitignore
-- 7. For parallel deployments, update SERVICE_ROLE and SERVICE_USER with a suffix
--    (e.g., AV_UPLOADER_SERVICE_ROLE_V2, AV_UPLOADER_SERVICE_USER_V2)
