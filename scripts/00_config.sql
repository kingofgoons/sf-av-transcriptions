--#############################################################################
-- PROJECT CONFIGURATION
-- Edit these values to deploy a separate instance of the transcription project
-- Then copy/paste this block at the top of 01_setup.sql, 02_automate.sql, 04_teardown.sql
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
SET PROJECT_STREAM = 'AV_STAGE_STREAM';           -- Stream for file detection
SET PROJECT_TASK_TRANSCRIBE = 'TRANSCRIBE_NEW_FILES_TASK';  -- Transcription task
SET PROJECT_TASK_REFRESH = 'REFRESH_STAGE_DIRECTORY_TASK';  -- Stage refresh task

-- Integration names (these are account-level, so include prefix to avoid conflicts)
SET PROJECT_ALLOW_ALL_INTEGRATION = 'transcription_allow_all_integration';
SET PROJECT_PYPI_INTEGRATION = 'transcription_pypi_access_integration';
SET PROJECT_ALLOW_ALL_RULE = 'allow_all_rule';
SET PROJECT_PYPI_RULE = 'pypi_network_rule';

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
