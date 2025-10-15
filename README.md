# Audio/Video Transcription with Snowflake and OpenAI Whisper

A complete solution for batch transcribing audio and video files using OpenAI's Whisper model in Snowflake's GPU-accelerated Container Runtime environment.

## 🎯 Overview

This project provides a scalable transcription pipeline that:
- Processes audio/video files from Snowflake stages
- Uses OpenAI Whisper for accurate speech-to-text conversion
- Leverages GPU acceleration for faster processing
- Stores results in structured Snowflake tables
- Provides analytics and search capabilities

## ✨ Features

- **Batch Processing**: Transcribe multiple files automatically
- **Multi-format Support**: MP3, WAV, MP4, AVI, MOV, and more
- **Language Detection**: Automatic language identification
- **Speaker Diarization**: Identify and separate different speakers
- **JSON Output**: Structured transcripts with speaker segments and timestamps
- **GPU Acceleration**: Faster processing with Snowflake's GPU compute
- **Metadata Capture**: File size, duration, processing time, speaker count
- **Search & Analytics**: Query transcriptions with SQL
- **Streamlit Dashboard**: Optional web interface for results

## 🏗️ Architecture

```
┌─────────────────┐    ┌──────────────────┐    ┌─────────────────┐
│  Audio/Video    │────│  Snowflake       │────│  Transcription  │
│  Files in Stage │    │  GPU Runtime     │    │  Results Table  │
│                 │    │  + Whisper       │    │                 │
└─────────────────┘    └──────────────────┘    └─────────────────┘
                                │
                       ┌────────────────┐
                       │   Analytics &  │
                       │   Search       │
                       └────────────────┘
```

## 🚀 Quick Start

### Prerequisites

- Snowflake account with ACCOUNTADMIN privileges
- Access to GPU compute pools (contact your Snowflake rep if needed)
- Audio/video files to transcribe

### Step 1: Database Setup

1. Run the setup script in Snowflake:
```sql
-- Execute the contents of scripts/setup.sql
```

2. Upload your audio/video files to the `AUDIO_VIDEO_STAGE`:
```sql
-- In Snowsight, navigate to Data > Databases > TRANSCRIPTION_DB > TRANSCRIPTION_SCHEMA > Stages > AUDIO_VIDEO_STAGE
-- Use the web interface to upload your files
```

### Step 2: Upload Notebook

1. Upload the notebook to the `NOTEBOOK_STAGE`:
   - In Snowsight: Data > Databases > TRANSCRIPTION_DB > TRANSCRIPTION_SCHEMA > Stages > NOTEBOOK_STAGE
   - Upload `notebooks/audio_video_transcription.ipynb`
   - Note: FFmpeg is pre-installed in Container Runtime GPU environments, no additional scripts needed

### Step 3: Create and Run Notebook

```sql
-- Create the notebook object
CREATE OR REPLACE NOTEBOOK TRANSCRIPTION_MAIN
FROM '@TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.NOTEBOOK_STAGE'
MAIN_FILE = 'audio_video_transcription.ipynb'
QUERY_WAREHOUSE = 'TRANSCRIPTION_WH'
COMPUTE_POOL='TRANSCRIPTION_GPU_POOL'
RUNTIME_NAME='SYSTEM$GPU_RUNTIME';

-- Enable external access
ALTER NOTEBOOK TRANSCRIPTION_MAIN ADD LIVE VERSION FROM LAST;
ALTER NOTEBOOK TRANSCRIPTION_MAIN SET EXTERNAL_ACCESS_INTEGRATIONS = (
    "TRANSCRIPTION_PYPI_ACCESS_INTEGRATION", 
    "TRANSCRIPTION_ALLOW_ALL_INTEGRATION"
);
```

### Step 4: Execute Transcription

1. Open the notebook in Snowsight
2. Run all cells to process your files
3. Monitor progress in the notebook output

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

### Whisper Model Options

Edit the notebook to change the Whisper model:

```python
# Options: tiny, base, small, medium, large
model = whisper.load_model("base")  # Change this line
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
- Browse transcription results
- Search across transcripts
- View analytics and charts
- Filter by language, file type, date

## 🏢 Production Deployment

### Automated Processing with Tasks

Set up automatic processing for new files:

```sql
-- Create stream to monitor stage changes
CREATE STREAM audio_video_stream ON STAGE AUDIO_VIDEO_STAGE;

-- Create task for automatic processing
CREATE OR REPLACE TASK auto_transcription_task
    WAREHOUSE = TRANSCRIPTION_WH
    SCHEDULE = '5 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('audio_video_stream')
AS
    EXECUTE NOTEBOOK TRANSCRIPTION_MAIN;

-- Start the task
ALTER TASK auto_transcription_task RESUME;
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
│   ├── setup.sql              # Snowflake database setup
│   └── install_ffmpeg.sh      # FFmpeg installation (optional, usually pre-installed)
├── notebooks/
│   └── audio_video_transcription.ipynb  # Main transcription notebook
├── streamlit/
│   └── transcription_dashboard.py       # Streamlit in Snowflake dashboard
├── environment.yml            # Minimal conda environment
└── README.md                 # This file
```

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

---

**Built with ❤️ using Snowflake, OpenAI Whisper, and Python** 