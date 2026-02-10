-- =====================================================
-- SERVICE USER SETUP FOR AV TRANSCRIPTION UPLOADER
-- =====================================================
-- Run this script as ACCOUNTADMIN or with appropriate privileges

--#############################################################################
-- IMPORTANT: Copy and paste the configuration block from 00_config.sql here
-- before running this script. This allows you to set up service users for
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
SET SQL_CMD = 'GRANT READ, WRITE ON STAGE ' || $FQ_STAGE || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Grant warehouse privileges for PUT operations
SET SQL_CMD = 'GRANT USAGE ON WAREHOUSE ' || $PROJECT_WH || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

-- Optional: Grant privileges to query transcription results (read-only)
SET SQL_CMD = 'GRANT SELECT ON TABLE ' || $FQ_TABLE || ' TO ROLE ' || $SERVICE_ROLE;
EXECUTE IMMEDIATE $SQL_CMD;

SET SQL_CMD = 'GRANT SELECT ON VIEW ' || $FQ_VIEW || ' TO ROLE ' || $SERVICE_ROLE;
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
