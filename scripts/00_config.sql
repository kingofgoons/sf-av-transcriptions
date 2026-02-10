--#############################################################################
-- PROJECT CONFIGURATION
-- Edit these values to deploy a separate instance of the transcription project
-- Then copy/paste this block at the top of 01_setup.sql, 02_automate.sql, 04_teardown.sql
--#############################################################################

-- Core naming - change these to create a parallel deployment
SET PROJECT_DB = 'TRANSCRIPTION_DB_V2';              -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA_V2';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_WH_V2';              -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL_V2';  -- GPU compute pool name

-- Derived names (automatically built from above)
SET PROJECT_NOTEBOOK = 'TRANSCRIBE_AV_FILES_V2';     -- Notebook name
SET PROJECT_STAGE_AV = 'AUDIO_VIDEO_STAGE';       -- Stage for media files -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STAGE_NB = 'NOTEBOOK_STAGE';          -- Stage for notebook assets -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_RESULTS_TABLE = 'TRANSCRIPTION_RESULTS';  -- Results table -- DON'T UPDATE (hard-coded in notebook)
SET PROJECT_STREAM = 'AV_STAGE_STREAM_V2';           -- Stream for file detection
SET PROJECT_TASK_TRANSCRIBE = 'TRANSCRIBE_NEW_FILES_TASK_V2';  -- Transcription task
SET PROJECT_TASK_REFRESH = 'REFRESH_STAGE_DIRECTORY_TASK_V2';  -- Stage refresh task

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration_V2';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration_V2';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule_V2';
SET PROJECT_PYPI_RULE = 'pypi_network_rule_V2';

--#############################################################################
-- Example: To create a DEV instance, change the top values:
--   SET PROJECT_DB = 'TRANSCRIPTION_DB_DEV';
--   SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';
--   SET PROJECT_WH = 'TRANSCRIPTION_WH_DEV';
--   SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_GPU_POOL_DEV';
--   SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_dev_allow_all_integration';
--   SET PROJECT_PYPI_INTEGRATION = 'transcription_dev_pypi_access_integration';
--#############################################################################

-- Verify configuration
SELECT 
    $PROJECT_DB AS DATABASE_NAME,
    $PROJECT_SCHEMA AS SCHEMA_NAME,
    $PROJECT_WH AS WAREHOUSE_NAME,
    $PROJECT_COMPUTE_POOL AS COMPUTE_POOL_NAME;
