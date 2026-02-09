--#############################################################################
-- IMPORTANT: Copy and paste the configuration block from 00_config.sql here
-- before running this script. This allows you to deploy parallel instances.
--#############################################################################

-- Core naming - change these to create a parallel deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH';              -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL';  -- GPU compute pool name

-- Derived names (automatically built from above)
SET PROJECT_NOTEBOOK = 'TRANSCRIBE_AV_FILES';     -- Notebook name
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files
SET PROJECT_STAGE_NB = 'NOTEBOOK_STAGE';          -- Stage for notebook assets
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule';
SET PROJECT_PYPI_RULE = 'pypi_network_rule';

--#############################################################################
-- END CONFIGURATION
--#############################################################################

USE ROLE SYSADMIN;

-- Create warehouse, database, and schema for transcription project
CREATE OR REPLACE WAREHOUSE IDENTIFIER($PROJECT_WH); --by default, this creates an XS Standard Warehouse
CREATE OR REPLACE DATABASE IDENTIFIER($PROJECT_DB);
CREATE OR REPLACE SCHEMA IDENTIFIER($PROJECT_SCHEMA);

USE WAREHOUSE IDENTIFIER($PROJECT_WH);
USE DATABASE IDENTIFIER($PROJECT_DB);
USE SCHEMA IDENTIFIER($PROJECT_SCHEMA);

----------------------------------
----------------------------------
/* NOTEBOOK AND COMPUTE SETUP */
----------------------------------
----------------------------------
USE ROLE ACCOUNTADMIN;

-- Create GPU compute pool for Whisper transcription
DROP COMPUTE POOL IF EXISTS IDENTIFIER($PROJECT_COMPUTE_POOL);

CREATE COMPUTE POOL IDENTIFIER($PROJECT_COMPUTE_POOL)
        MIN_NODES = 1
        MAX_NODES = 3
        INSTANCE_FAMILY = GPU_NV_S; -- May need to change this based on region

-- Create network rules for external access (fully qualified with variables)
-- Note: Network rules live in the database/schema, integrations are account-level
CREATE OR REPLACE NETWORK RULE IDENTIFIER($PROJECT_ALLOW_ALL_RULE)
          TYPE = HOST_PORT
          MODE = EGRESS
          VALUE_LIST = ('0.0.0.0:443','0.0.0.0:80');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION IDENTIFIER($PROJECT_ALLOW_ALL_INTEGRATION)
        ALLOWED_NETWORK_RULES = (IDENTIFIER($PROJECT_ALLOW_ALL_RULE))
        ENABLED = TRUE;

CREATE OR REPLACE NETWORK RULE IDENTIFIER($PROJECT_PYPI_RULE)
          TYPE = HOST_PORT
          MODE = EGRESS
          VALUE_LIST = ('pypi.org', 'pypi.python.org', 'pythonhosted.org', 'files.pythonhosted.org');

CREATE OR REPLACE EXTERNAL ACCESS INTEGRATION IDENTIFIER($PROJECT_PYPI_INTEGRATION)
        ALLOWED_NETWORK_RULES = (IDENTIFIER($PROJECT_PYPI_RULE))
        ENABLED = TRUE;

-- Grant ownership to SYSADMIN
GRANT OWNERSHIP ON COMPUTE POOL IDENTIFIER($PROJECT_COMPUTE_POOL) TO ROLE SYSADMIN;
GRANT OWNERSHIP ON INTEGRATION IDENTIFIER($PROJECT_PYPI_INTEGRATION) TO ROLE SYSADMIN;
GRANT OWNERSHIP ON INTEGRATION IDENTIFIER($PROJECT_ALLOW_ALL_INTEGRATION) TO ROLE SYSADMIN;

USE ROLE SYSADMIN;

----------------------------------
----------------------------------
/*          DATA SETUP          */
----------------------------------
----------------------------------

-- Create file format for CSV output
CREATE OR REPLACE FILE FORMAT CSVFORMAT 
    SKIP_HEADER = 1
    TYPE = 'CSV'
    FIELD_OPTIONALLY_ENCLOSED_BY = '"';

-- Create stages
CREATE OR REPLACE STAGE IDENTIFIER($PROJECT_STAGE_NB) DIRECTORY=(ENABLE=true); -- to store notebook assets
CREATE OR REPLACE STAGE IDENTIFIER($PROJECT_STAGE_AV) 
    DIRECTORY = (ENABLE = TRUE) 
    ENCRYPTION=(TYPE='SNOWFLAKE_SSE'); -- to store audio/video files for transcription

-- Create table to store transcription results
CREATE OR REPLACE TABLE IDENTIFIER($PROJECT_RESULTS_TABLE) (
    FILE_PATH VARCHAR(500),
    FILE_NAME VARCHAR(255),
    FILE_TYPE VARCHAR(10),
    DETECTED_LANGUAGE VARCHAR(50),
    TRANSCRIPT TEXT,
    TRANSCRIPT_WITH_SPEAKERS VARIANT,  -- JSON object with speaker segments
    PROCESSING_TIME_SECONDS FLOAT,
    TRANSCRIPTION_TIMESTAMP TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP(),
    FILE_SIZE_BYTES NUMBER,
    AUDIO_DURATION_SECONDS FLOAT,
    SPEAKER_COUNT NUMBER,              -- Number of identified speakers
    SRT_CONTENT TEXT,                  -- Pre-generated SRT (without speakers)
    SRT_WITH_SPEAKERS TEXT,            -- Pre-generated SRT (with speakers)  
    SUMMARY_MARKDOWN TEXT              -- AI-generated summary with follow-ups
);

-- Create a view for easy querying
CREATE OR REPLACE VIEW TRANSCRIPTION_SUMMARY AS
SELECT 
    FILE_TYPE,
    DETECTED_LANGUAGE,
    COUNT(*) as FILE_COUNT,
    AVG(PROCESSING_TIME_SECONDS) as AVG_PROCESSING_TIME,
    AVG(AUDIO_DURATION_SECONDS) as AVG_DURATION,
    AVG(FILE_SIZE_BYTES) as AVG_FILE_SIZE,
    AVG(SPEAKER_COUNT) as AVG_SPEAKERS,
    MIN(TRANSCRIPTION_TIMESTAMP) as FIRST_TRANSCRIPTION,
    MAX(TRANSCRIPTION_TIMESTAMP) as LAST_TRANSCRIPTION
FROM IDENTIFIER($PROJECT_RESULTS_TABLE)
GROUP BY FILE_TYPE, DETECTED_LANGUAGE
ORDER BY FILE_COUNT DESC;

-- Create notebook (uncomment after uploading notebook files)
-- Note: CREATE NOTEBOOK doesn't support IDENTIFIER() for stage paths, using dynamic SQL
EXECUTE IMMEDIATE 
    'CREATE OR REPLACE NOTEBOOK ' || $PROJECT_NOTEBOOK || '
     FROM ''@' || $PROJECT_DB || '.' || $PROJECT_SCHEMA || '.' || $PROJECT_STAGE_NB || '''
     MAIN_FILE = ''audio_video_transcription.ipynb''
     QUERY_WAREHOUSE = ''' || $PROJECT_WH || '''
     COMPUTE_POOL=''' || $PROJECT_COMPUTE_POOL || '''
     RUNTIME_NAME=''SYSTEM$GPU_RUNTIME''';

ALTER NOTEBOOK IDENTIFIER($PROJECT_NOTEBOOK) ADD LIVE VERSION FROM LAST;

-- Set external access integrations using dynamic SQL (ALTER doesn't support IDENTIFIER for integration list)
EXECUTE IMMEDIATE
    'ALTER NOTEBOOK ' || $PROJECT_NOTEBOOK || ' SET EXTERNAL_ACCESS_INTEGRATIONS = ("' || 
    UPPER($PROJECT_PYPI_INTEGRATION) || '", "' || 
    UPPER($PROJECT_ALLOW_ALL_INTEGRATION) || '")';

-- Sample queries to test after transcription:
/*
-- View all transcriptions
SELECT * FROM TRANSCRIPTION_RESULTS ORDER BY TRANSCRIPTION_TIMESTAMP DESC;

-- Search transcripts for specific content
SELECT FILE_NAME, TRANSCRIPT, DETECTED_LANGUAGE 
FROM TRANSCRIPTION_RESULTS 
WHERE TRANSCRIPT ILIKE '%your_search_term%';

-- Get summary statistics
SELECT * FROM TRANSCRIPTION_SUMMARY;

-- Find longest/shortest audio files
SELECT FILE_NAME, AUDIO_DURATION_SECONDS, TRANSCRIPT
FROM TRANSCRIPTION_RESULTS 
ORDER BY AUDIO_DURATION_SECONDS DESC;
*/
