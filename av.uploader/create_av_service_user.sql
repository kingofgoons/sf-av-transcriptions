-- =====================================================
-- SERVICE USER SETUP FOR AV TRANSCRIPTION UPLOADER
-- =====================================================
-- Run this script as ACCOUNTADMIN or with appropriate privileges

-- Step 1: Use SECURITYADMIN to create role
USE ROLE SECURITYADMIN;

CREATE ROLE IF NOT EXISTS AV_UPLOADER_SERVICE_ROLE
  COMMENT = 'Role for AV transcription uploader service account';

-- Step 2: Use USERADMIN to create the service user
USE ROLE USERADMIN;

-- TYPE = SERVICE designates this as a service account (not a person)
CREATE USER IF NOT EXISTS AV_UPLOADER_SERVICE_USER
  TYPE = SERVICE
  DEFAULT_WAREHOUSE = TRANSCRIPTION_WH
  DEFAULT_NAMESPACE = TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA
  DEFAULT_ROLE = AV_UPLOADER_SERVICE_ROLE
  COMMENT = 'Service account for uploading audio/video files with RSA key authentication';

-- Step 3: Use SECURITYADMIN to grant roles and privileges
USE ROLE SECURITYADMIN;

-- Grant the role to the service user
GRANT ROLE AV_UPLOADER_SERVICE_ROLE TO USER AV_UPLOADER_SERVICE_USER;

-- Grant database and schema privileges
GRANT USAGE ON DATABASE TRANSCRIPTION_DB TO ROLE AV_UPLOADER_SERVICE_ROLE;
GRANT USAGE ON SCHEMA TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA TO ROLE AV_UPLOADER_SERVICE_ROLE;

-- Grant privileges needed for stage operations (READ and WRITE to upload files)
GRANT READ, WRITE ON STAGE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE TO ROLE AV_UPLOADER_SERVICE_ROLE;

-- Grant warehouse privileges for PUT operations
GRANT USAGE ON WAREHOUSE TRANSCRIPTION_WH TO ROLE AV_UPLOADER_SERVICE_ROLE;

-- Optional: Grant privileges to query transcription results (read-only)
GRANT SELECT ON TABLE TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIPTION_RESULTS TO ROLE AV_UPLOADER_SERVICE_ROLE;
GRANT SELECT ON VIEW TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIPTION_SUMMARY TO ROLE AV_UPLOADER_SERVICE_ROLE;

-- Step 4: Use USERADMIN to configure RSA public key authentication
USE ROLE USERADMIN;

-- To generate RSA key pair on your local machine:
--   openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
--   openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
-- 
-- Then copy the content between -----BEGIN PUBLIC KEY----- and -----END PUBLIC KEY-----
-- and paste it below (as a single line, without the headers)

ALTER USER AV_UPLOADER_SERVICE_USER SET RSA_PUBLIC_KEY = '<RSA_PUBLIC_KEY>';

-- Optional: Set a second RSA public key for key rotation
-- ALTER USER AV_UPLOADER_SERVICE_USER SET RSA_PUBLIC_KEY_2 = '<RSA_PUBLIC_KEY_2>';

-- =====================================================
-- VERIFICATION QUERIES
-- =====================================================

-- Verify the user was created
SHOW USERS LIKE 'AV_UPLOADER_SERVICE_USER';

-- Verify the role and grants
SHOW GRANTS TO ROLE AV_UPLOADER_SERVICE_ROLE;

-- Verify the user's RSA public key is set
DESC USER AV_UPLOADER_SERVICE_USER;

-- =====================================================
-- NOTES
-- =====================================================
-- 1. Replace <RSA_PUBLIC_KEY> with your actual public key value
-- 2. The public key should be a single line without BEGIN/END headers
-- 3. The service user will authenticate using the private key from client side
-- 4. Password authentication is disabled by default for SERVICE type users
-- 5. Store the private key (rsa_key.p8) securely in the project root directory
-- 6. Do NOT commit rsa_key.p8 to git - it's already in .gitignore

