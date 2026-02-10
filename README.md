# Audio/Video Transcription with Snowflake and OpenAI Whisper

A complete solution for batch transcribing audio and video files using OpenAI's Whisper model in Snowflake's GPU-accelerated Container Runtime environment.

## 🎯 Overview

This project provides a scalable transcription pipeline that:
- Automatically processes audio/video files as they're uploaded to Snowflake stages
- Uses OpenAI Whisper for accurate speech-to-text conversion
- Leverages GPU acceleration for faster processing
- Stores results in structured Snowflake tables with speaker diarization
- Provides analytics, search capabilities, and real-time monitoring
- Fully automated with streams and tasks - no manual intervention needed

## ✨ Features

- **Automated Pipeline**: Stream-based task automation processes files as they arrive
- **Batch Processing**: Transcribe multiple files automatically
- **Smart Deduplication**: Skip already-transcribed files to save compute
- **Multi-format Support**: MP3, WAV, MP4, AVI, MOV, and more
- **Language Detection**: Automatic language identification
- **Speaker Diarization**: Identify and separate different speakers (optional)
- **JSON Output**: Structured transcripts with speaker segments and timestamps
- **GPU Acceleration**: Faster processing with Snowflake's GPU compute
- **Metadata Capture**: File size, duration, processing time, speaker count
- **Pre-generated SRT Subtitles**: SRT files created during transcription (with/without speakers)
- **AI-Powered Summaries**: Cortex LLM generates markdown summaries with categorized follow-up items
- **Search & Analytics**: Query transcriptions with SQL
- **Streamlit Dashboard**: Web interface with CSV/SRT/Markdown export
- **Configurable Deployment**: Session variables allow parallel instances without conflicts
- **Snowflake CLI Integration**: Easy file uploads from command line

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Audio/Video    │────│  Stream on       │────│  Task Triggers  │
│  Files Upload   │    │  Stage           │    │  Notebook       │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                                         │
                                                         ▼
                       ┌──────────────────────────────────────────┐
                       │  Snowflake GPU Runtime + Whisper Model   │
                       │  • Speech-to-text transcription          │
                       │  • Speaker diarization                   │
                       │  • SRT subtitle generation               │
                       └──────────────────────────────────────────┘
                                                         │
                                                         ▼
                       ┌──────────────────────────────────────────┐
                       │  Snowflake Cortex LLM (mistral-large2)   │
                       │  • AI-powered meeting summaries          │
                       │  • Categorized follow-up items           │
                       └──────────────────────────────────────────┘
                                                         │
                       ┌─────────────────────────────────▼────────┐
                       │  Transcription Results Table             │
                       │  • Full transcript + speaker segments    │
                       │  • Pre-generated SRT files               │
                       │  • AI summary with action items          │
                       └──────────────────────────────────────────┘
```

**Automated Pipeline:**
1. Upload files to stage via Snowflake CLI or Snowsight
2. Stream detects new files automatically
3. Task executes transcription notebook on GPU compute
4. Whisper generates transcript + SRT subtitles
5. Cortex LLM creates AI summary with follow-up items
6. Results stored in structured table
7. Query and analyze with SQL or Streamlit dashboard

## 🚀 Quick Start

### Prerequisites

- Snowflake account with ACCOUNTADMIN privileges
- Access to GPU compute pools (contact your Snowflake rep if needed)
- Snowflake CLI installed ([download here](https://docs.snowflake.com/developer-guide/snowflake-cli/))
- Audio/video files to transcribe

### Step 1: Database Setup

1. Run the setup script in Snowflake:
```sql
-- Execute the contents of scripts/01_setup.sql
```

2. Upload your audio/video files to the `AUDIO_VIDEO_STAGE`:

**Option A: Using Snowflake CLI (Recommended)**
```bash
# Upload a single file
snow stage copy "your-file.mp4" @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE --connection YOUR_CONNECTION_NAME

# Upload multiple files from a directory
cd /path/to/your/audio-files
snow stage copy "*.mp4" @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE --connection YOUR_CONNECTION_NAME
```

**Option B: Using Snowsight Web Interface**
- Navigate to: Data > Databases > TRANSCRIPTION_DB > TRANSCRIPTION_SCHEMA > Stages > AUDIO_VIDEO_STAGE
- Click "Upload Files" and select your audio/video files

### Step 2: Upload Notebook

1. Upload the notebook to the `NOTEBOOK_STAGE`:
   - In Snowsight: Data > Databases > TRANSCRIPTION_DB > TRANSCRIPTION_SCHEMA > Stages > NOTEBOOK_STAGE
   - Upload `notebooks/audio_video_transcription.ipynb`
   - Note: FFmpeg is pre-installed in Container Runtime GPU environments, no additional scripts needed

### Step 3: Create and Run Notebook

The notebook is already created by `01_setup.sql` as `TRANSCRIBE_AV_FILES`. If you need to recreate it:

```sql
-- Create the notebook object
CREATE OR REPLACE NOTEBOOK TRANSCRIBE_AV_FILES
FROM '@TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.NOTEBOOK_STAGE'
MAIN_FILE = 'audio_video_transcription.ipynb'
QUERY_WAREHOUSE = 'TRANSCRIPTION_WH'
COMPUTE_POOL='TRANSCRIPTION_GPU_POOL'
RUNTIME_NAME='SYSTEM$GPU_RUNTIME';

-- Enable external access
ALTER NOTEBOOK TRANSCRIBE_AV_FILES ADD LIVE VERSION FROM LAST;
ALTER NOTEBOOK TRANSCRIBE_AV_FILES SET EXTERNAL_ACCESS_INTEGRATIONS = (
    "TRANSCRIPTION_PYPI_ACCESS_INTEGRATION", 
    "TRANSCRIPTION_ALLOW_ALL_INTEGRATION"
);
```

### Step 4: Execute Transcription

**Option A: Manual Execution**
1. Open the notebook in Snowsight (Projects > Notebooks > TRANSCRIBE_AV_FILES)
2. Run all cells to process your files
3. Monitor progress in the notebook output

**Option B: Automated Execution (Recommended)**
1. Run the automation setup: `scripts/02_automate.sql`
2. Upload files using Snowflake CLI or Snowsight
3. Files are automatically transcribed within 5 minutes
4. Monitor with the provided verification queries

## 🤖 Automated Workflow

Once automation is set up, the workflow is:

1. **Upload File**: Use `snow stage copy` to upload an audio/video file
2. **Auto-Detection**: Stage refresh task runs every 5 minutes, detecting new files
3. **Stream Triggers**: Stream captures the new file in change data
4. **Task Executes**: Transcription task automatically runs the notebook on GPU compute
5. **Results Stored**: Transcription saved to `TRANSCRIPTION_RESULTS` table with metadata
6. **Query & Analyze**: Search transcripts, view analytics, export to CSV/SRT

**No manual intervention needed after setup!** Just upload files and check results.

## 📋 Output Schema

The `TRANSCRIPTION_RESULTS` table stores all transcription data:

| Column | Type | Description |
|--------|------|-------------|
| `FILE_PATH` | VARCHAR | Full stage path to the file |
| `FILE_NAME` | VARCHAR | Original filename |
| `FILE_TYPE` | VARCHAR | Extension (mp3, mp4, etc.) |
| `DETECTED_LANGUAGE` | VARCHAR | Auto-detected language |
| `TRANSCRIPT` | TEXT | Full plain-text transcript |
| `TRANSCRIPT_WITH_SPEAKERS` | VARIANT | JSON with speaker segments and timestamps |
| `PROCESSING_TIME_SECONDS` | FLOAT | Time to process the file |
| `TRANSCRIPTION_TIMESTAMP` | TIMESTAMP | When transcription completed |
| `FILE_SIZE_BYTES` | NUMBER | File size |
| `AUDIO_DURATION_SECONDS` | FLOAT | Length of audio/video |
| `SPEAKER_COUNT` | NUMBER | Number of identified speakers |
| `SRT_CONTENT` | TEXT | Pre-generated SRT subtitles (no speakers) |
| `SRT_WITH_SPEAKERS` | TEXT | Pre-generated SRT with `[Speaker]` labels |
| `SUMMARY_MARKDOWN` | TEXT | AI-generated summary with follow-up items |

### AI Summary Format

The `SUMMARY_MARKDOWN` column contains Cortex LLM-generated summaries with:
- Meeting overview and key discussion points
- Categorized follow-up items:
  - `[SNOWFLAKE]` - Snowflake product/platform items
  - `[BO LANDSMAN - SE]` - Sales Engineering action items
  - `[GENERAL]` - Other follow-ups

## 📊 Querying Results

### Basic Queries

```sql
-- View all transcriptions with speaker info
SELECT FILE_NAME, TRANSCRIPT, DETECTED_LANGUAGE, SPEAKER_COUNT 
FROM TRANSCRIPTION_RESULTS 
ORDER BY TRANSCRIPTION_TIMESTAMP DESC;

-- Search for specific content
SELECT FILE_NAME, TRANSCRIPT, DETECTED_LANGUAGE, SPEAKER_COUNT
FROM TRANSCRIPTION_RESULTS 
WHERE TRANSCRIPT ILIKE '%meeting%';

-- Get summary statistics including speaker data
SELECT * FROM TRANSCRIPTION_SUMMARY;
```

### Speaker Diarization Queries

```sql
-- View speaker segments from JSON data
SELECT 
    FILE_NAME,
    SPEAKER_COUNT,
    TRANSCRIPT_WITH_SPEAKERS:speakers[0]:speaker::STRING as FIRST_SPEAKER,
    TRANSCRIPT_WITH_SPEAKERS:speakers[0]:text::STRING as FIRST_SPEAKER_TEXT
FROM TRANSCRIPTION_RESULTS 
WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL;

-- Extract all speaker segments
SELECT 
    FILE_NAME,
    seg.value:speaker::STRING as SPEAKER,
    seg.value:start_time::FLOAT as START_TIME,
    seg.value:end_time::FLOAT as END_TIME,
    seg.value:text::STRING as SPEAKER_TEXT
FROM TRANSCRIPTION_RESULTS,
    LATERAL FLATTEN(input => TRANSCRIPT_WITH_SPEAKERS:speakers) seg
WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL;
```

### Advanced Analytics

```sql
-- Performance by file type
SELECT 
    FILE_TYPE,
    COUNT(*) as FILES,
    AVG(PROCESSING_TIME_SECONDS) as AVG_PROCESSING_TIME,
    AVG(AUDIO_DURATION_SECONDS) as AVG_DURATION
FROM TRANSCRIPTION_RESULTS 
GROUP BY FILE_TYPE;

-- Language distribution
SELECT 
    DETECTED_LANGUAGE,
    COUNT(*) as FILE_COUNT,
    ROUND(COUNT(*) * 100.0 / SUM(COUNT(*)) OVER(), 2) as PERCENTAGE
FROM TRANSCRIPTION_RESULTS 
GROUP BY DETECTED_LANGUAGE
ORDER BY FILE_COUNT DESC;
```

## 🔧 Configuration

### Configuration Options

All configuration is in **Cell 4** of the notebook. Edit these values before running:

```python
# Whisper Model Selection
WHISPER_MODEL = "base"  # Options: tiny, base, small, medium, large

# Speaker Diarization
ENABLE_SPEAKER_DIARIZATION = False  # Set to True to enable

# Deduplication
SKIP_ALREADY_TRANSCRIBED = True  # Skip files already in database
FORCE_RETRANSCRIBE = False  # Re-process all files regardless

# Batch Processing
PROGRESS_UPDATE_INTERVAL = 5  # Progress updates frequency
```

**Model Comparison:**
- `tiny`: Fastest, least accurate (~39x realtime)
- `base`: Good balance (default, ~16x realtime) 
- `small`: Better accuracy (~6x realtime)
- `medium`: High accuracy (~2x realtime)
- `large`: Best accuracy (~1x realtime)

### Supported File Formats

- **Audio**: MP3, WAV, M4A, FLAC, AAC, OGG
- **Video**: MP4, AVI, MOV, MKV, WEBM, FLV

## 📱 Optional Streamlit Dashboard

Launch the web dashboard to explore results:

```bash
streamlit run streamlit/transcription_dashboard.py
```

Features:
- Browse transcription results with full text and speaker views
- Search across transcripts
- View analytics and charts
- Filter by language, file type, date
- Download pre-generated SRT files (with/without speaker labels)
- Download AI-generated markdown summaries
- Preview summaries with categorized follow-up items:
  - `[SNOWFLAKE]` - Snowflake-specific action items
  - `[BO LANDSMAN - SE]` - Sales Engineering follow-ups
  - `[GENERAL]` - General action items

## 🏢 Production Deployment

### Automated Processing with Tasks

For fully automated transcription pipeline, run the automation setup script:

```sql
-- Execute the complete automation setup
-- This script creates streams, tasks, and stored procedures
-- See: scripts/02_automate.sql
```

**What the automation script does:**

1. **Grants Required Privileges**: Sets up EXECUTE TASK privilege and network rule access for SYSADMIN
2. **Creates Stream**: Monitors `AUDIO_VIDEO_STAGE` for new file uploads
3. **Creates Stored Procedure**: Wraps notebook execution with proper error handling
4. **Creates Two Tasks**:
   - `REFRESH_STAGE_DIRECTORY_TASK`: Refreshes stage metadata every 5 minutes
   - `TRANSCRIBE_NEW_FILES_TASK`: Executes transcription notebook when new files detected
5. **Provides Monitoring Queries**: Check task history and notebook execution status

**Manual Setup (if preferred):**

```sql
-- Create stream to monitor stage changes
CREATE STREAM AV_STAGE_STREAM ON STAGE AUDIO_VIDEO_STAGE;

-- Create stored procedure for notebook execution
CREATE OR REPLACE PROCEDURE RUN_TRANSCRIPTION_NOTEBOOK()
    RETURNS STRING
    LANGUAGE SQL
    EXECUTE AS OWNER
AS
DECLARE
    result STRING;
BEGIN
    EXECUTE NOTEBOOK TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.TRANSCRIBE_AV_FILES();
    result := 'Notebook execution initiated at ' || CURRENT_TIMESTAMP()::STRING;
    RETURN result;
EXCEPTION
    WHEN OTHER THEN
        RETURN 'Error executing notebook: ' || SQLERRM;
END;

-- Create task for automatic processing
CREATE OR REPLACE TASK TRANSCRIBE_NEW_FILES_TASK
    WAREHOUSE = TRANSCRIPTION_WH
    SCHEDULE = '5 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM')
AS
    CALL RUN_TRANSCRIPTION_NOTEBOOK();

-- Start the task
ALTER TASK TRANSCRIBE_NEW_FILES_TASK RESUME;
```

**Monitoring Your Automated Pipeline:**

```sql
-- Check if stream has detected new files
SELECT SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM');

-- View task execution history
SELECT NAME, STATE, ERROR_CODE, ERROR_MESSAGE, SCHEDULED_TIME, COMPLETED_TIME, RETURN_VALUE 
FROM TABLE(INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => 'TRANSCRIBE_NEW_FILES_TASK',
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -24, CURRENT_TIMESTAMP())
)) ORDER BY SCHEDULED_TIME DESC;

-- Check notebook execution history (requires ACCOUNTADMIN)
SELECT 
    NOTEBOOK_NAME,
    START_TIME,
    END_TIME,
    DATEDIFF('second', START_TIME, END_TIME) as DURATION_SECONDS,
    CREDITS AS CREDITS_USED_IN_THE_HOUR,
    COMPUTE_POOL_NAME
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE NOTEBOOK_NAME = 'TRANSCRIBE_AV_FILES'
    AND START_TIME >= DATEADD('hour', -24, CURRENT_TIMESTAMP())
ORDER BY START_TIME DESC;
```

### Scaling Considerations

- **Compute Pool Size**: Increase `MAX_NODES` for parallel processing
- **Warehouse Size**: Use larger warehouses for heavy workloads
- **File Batching**: Process files in smaller batches for memory efficiency

## 🔍 Troubleshooting

### Common Issues

1. **FFmpeg Installation Fails**
   - Ensure you're using GPU compute pool
   - Check external access integrations are enabled

2. **Out of Memory Errors**
   - Use smaller Whisper model (`tiny` or `base`)
   - Process fewer files per batch
   - Increase compute pool nodes

3. **Slow Processing**
   - Verify GPU compute pool is active
   - Use larger GPU instances
   - Consider `small` model for better speed/accuracy balance

### Performance Optimization

```python
# Process files in smaller batches
batch_size = 10
for i in range(0, len(media_files), batch_size):
    batch = media_files[i:i+batch_size]
    # Process batch...
```

## 📁 Project Structure

```
audio-video-transcription-snowflake/
├── scripts/
│   ├── 00_config.sql                 # Configuration variables for parallel deployments
│   ├── 01_setup.sql                  # Snowflake database setup
│   ├── 02_automate.sql               # Automated pipeline with streams & tasks
│   ├── 03_deploy_notebook.sh         # Deploy notebook from local to Snowflake
│   ├── 04_teardown.sql               # Remove all project objects
│   └── install_ffmpeg.sh             # FFmpeg installation (optional, usually pre-installed)
├── notebooks/
│   └── audio_video_transcription.ipynb  # Main transcription notebook
├── streamlit/
│   └── transcription_dashboard.py       # Streamlit in Snowflake dashboard
├── AUDIO_VIDEO_STAGE_FILES/           # Local folder for files to upload
├── environment.yml                    # Minimal conda environment
└── README.md                          # This file
```

## ⚙️ Parallel Deployments

Deploy multiple instances (e.g., dev/staging/prod) without conflicts using configurable database names.

### Configuration

Edit the variables in `scripts/00_config.sql`:

```sql
-- Core naming - change these to create a parallel deployment
SET PROJECT_DB = 'TRANSCRIPTION_DEV';             -- Database name
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';      -- Schema name
SET PROJECT_WH = 'TRANSCRIPTION_DEV_WH';          -- Warehouse name
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_DEV_GPU_POOL';  -- GPU compute pool
```

### Deploying a Parallel Instance

1. Copy the config block from `00_config.sql` to the top of `01_setup.sql` and `02_automate.sql`
2. Modify the variable values for your new instance
3. Run the setup scripts
4. For notebook deployment, use environment variables:

```bash
PROJECT_DB=TRANSCRIPTION_DEV \
PROJECT_SCHEMA=TRANSCRIPTION_SCHEMA \
./scripts/03_deploy_notebook.sh
```

### Teardown

Use `04_teardown.sql` with the same config block to remove a specific instance without affecting others.

## 🤝 Contributing

Contributions welcome! Please:

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Support

- **Documentation**: Check the inline notebook documentation
- **Issues**: Report bugs via GitHub issues
- **Questions**: Use GitHub discussions

## 🏷️ Version History

- **v1.0.0**: Initial release with batch transcription
- **v1.1.0**: Added Streamlit dashboard
- **v1.2.0**: Performance optimizations and error handling
- **v1.3.0**: Automated pipeline with streams, tasks, and Snowflake CLI integration
- **v1.4.0**: Pre-generated SRT subtitles, AI summaries with Cortex LLM, configurable deployments

---

**Built with ❤️ using Snowflake, OpenAI Whisper, and Python** 