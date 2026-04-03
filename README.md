# Meeting Intelligence Pipeline

An end-to-end pipeline for transcribing, summarizing, and searching meeting recordings using Snowflake's GPU Container Runtime, OpenAI Whisper, and Cortex AI — with Gong CRM integration and a Cortex Agent for natural-language meeting queries.

## Overview

This project provides:

- **Automated transcription** of audio/video files via OpenAI Whisper on Snowflake GPU compute
- **AI-powered meeting summaries** with structured fields (key points, next steps, decisions, questions) via Cortex LLM
- **Gong CRM sync** that mirrors call records from a Snowhouse account into the pipeline
- **Unified search** across all meetings (local recordings + Gong calls) via Cortex Search
- **Text-to-SQL analytics** via a Semantic View and Cortex Analyst
- **A Cortex Agent** (`MEETING_INTELLIGENCE`) that combines search and analytics into a single conversational interface
- **A Streamlit dashboard** for browsing, searching, and exporting meeting data

## Architecture

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
                       │  • Speaker diarization (WhisperX)        │
                       │  • SRT subtitle generation               │
                       └──────────────────────────────────────────┘
                                                         │
                                                         ▼
                       ┌──────────────────────────────────────────┐
                       │  Snowflake Cortex LLM (claude-sonnet-4-6) │
                       │  • Structured meeting summaries          │
                       │  • Categorized follow-up items           │
                       │  • Meeting title inference               │
                       └──────────────────────────────────────────┘
                                                         │
                       ┌─────────────────────────────────▼────────┐
                       │  TRANSCRIPTION_RESULTS                   │
                       │  Transcript, SRT, speakers, summary,     │
                       │  structured fields, account metadata     │
                       └──────────────────────────────────────────┘
                                                         │
        ┌────────────────────────────────────────────────┤
        │                                                │
        ▼                                                ▼
┌───────────────┐   ┌──────────────────────────────────────────┐
│ Gong Calls    │──▶│  UNIFIED_MEETINGS_V                      │
│ (Snowhouse    │   │  224 local recordings + 12 Gong calls    │
│  sync)        │   └──────────────────────────────────────────┘
└───────────────┘                    │
                        ┌────────────┼────────────┐
                        ▼            ▼            ▼
                  ┌──────────┐ ┌──────────┐ ┌──────────┐
                  │ MEETING  │ │ MEETINGS │ │ Streamlit│
                  │ _SEARCH  │ │ _SEMANTIC│ │ Dashboard│
                  │ (Cortex  │ │ _VIEW    │ │          │
                  │  Search) │ │ (Analyst)│ │          │
                  └────┬─────┘ └────┬─────┘ └──────────┘
                       │            │
                       ▼            ▼
                  ┌──────────────────────┐
                  │  MEETING_INTELLIGENCE│
                  │  (Cortex Agent)      │
                  │  + MCP Server        │
                  └──────────────────────┘
```

## Key Components

### Transcription Pipeline
- Files uploaded to a Snowflake stage are detected by a stream and processed automatically
- Whisper generates transcripts with speaker diarization and SRT subtitles
- Cortex LLM (`claude-sonnet-4-6`) produces structured summaries: meeting title, call brief, key points, next steps (categorized as `[SNOWFLAKE]`, `[BO LANDSMAN - SE]`, or `[GENERAL]`), decisions made, and questions raised
- Account name and call timestamp are extracted from the filename

### Gong Integration
- `scripts/05_sync_gong.sh` pulls Gong call records from a Snowhouse account via cross-account JSON export and idempotent MERGE
- `GONG_CALLS_MIRROR` stores synced records with call briefs, key points, talk time, and participant data
- The AV uploader offers a Gong sync prompt after each upload batch

### Unified Search & Analytics
- `UNIFIED_MEETINGS_V` — UNION ALL view over local recordings and Gong calls with a common schema
- `MEETING_SEARCH` — Cortex Search Service indexing all text fields (transcripts, summaries, key points) with `TARGET_LAG = 1 hour`
- `MEETINGS_SEMANTIC_VIEW` — Semantic View for Cortex Analyst with dimensions (account, source, date, language, direction) and metrics (meeting count, duration, talk ratio)

### Cortex Agent
- `MEETING_INTELLIGENCE` — two-tool agent combining `search_meetings` (Cortex Search) and `analyze_meetings` (Cortex Analyst)
- Exposed via `MEETING_INTELLIGENCE_MCP` MCP Server for integration with Cortex Code and other MCP clients

### Streamlit Dashboard
- Deployed to Snowflake via `snowflake.yml`
- Browse, search, and filter meetings by account, language, file type, and date
- View structured summaries with key points, next steps, decisions, and questions
- Export to CSV, SRT (with/without speakers), and Markdown

## Quick Start

### Prerequisites

- Snowflake account with ACCOUNTADMIN privileges
- Access to GPU compute pools
- [Snowflake CLI](https://docs.snowflake.com/developer-guide/snowflake-cli/) installed

### Setup

```bash
# 1. Create database objects, warehouse, compute pool, and table
snow sql -f scripts/01_setup.sql --connection YOUR_CONNECTION

# 2. Deploy the notebook to Snowflake
./scripts/03_deploy_notebook.sh

# 3. Set up automated pipeline (streams + tasks)
snow sql -f scripts/02_automate.sql --connection YOUR_CONNECTION

# 4. Create Gong integration objects (view, search, semantic view, agent)
snow sql -f scripts/06_gong_objects.sql --connection YOUR_CONNECTION

# 5. Deploy Streamlit dashboard
snow streamlit deploy --replace --connection YOUR_CONNECTION
```

### Upload Files

```bash
# Using the AV uploader (includes Gong sync prompt)
python av.uploader/upload_av_files.py --directory /path/to/recordings

# Or directly via Snowflake CLI
snow stage copy "*.mp4" @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE \
    --connection YOUR_CONNECTION
```

Files are automatically detected and transcribed within 5 minutes.

### Sync Gong Calls

```bash
# Manual sync from Snowhouse → DEMO
./scripts/05_sync_gong.sh

# Preview without writing
./scripts/05_sync_gong.sh --dry-run
```

## Output Schema

### TRANSCRIPTION_RESULTS

| Column | Type | Description |
|--------|------|-------------|
| `FILE_NAME` | VARCHAR | Original filename |
| `FILE_TYPE` | VARCHAR | Extension (mp3, mp4, etc.) |
| `DETECTED_LANGUAGE` | VARCHAR | Auto-detected language |
| `TRANSCRIPT` | TEXT | Full plain-text transcript |
| `TRANSCRIPT_WITH_SPEAKERS` | VARIANT | Speaker segments with timestamps |
| `AUDIO_DURATION_SECONDS` | FLOAT | Length of audio/video |
| `SPEAKER_COUNT` | NUMBER | Number of identified speakers |
| `SRT_CONTENT` | TEXT | SRT subtitles (no speakers) |
| `SRT_WITH_SPEAKERS` | TEXT | SRT with `[Speaker_N]` labels |
| `SUMMARY_MARKDOWN` | TEXT | Full AI-generated summary |
| `MEETING_TITLE` | VARCHAR | LLM-inferred meeting title |
| `CALL_BRIEF` | TEXT | Summary prose |
| `KEY_POINTS` | TEXT | Bullet list of main topics |
| `NEXT_STEPS` | TEXT | Categorized follow-up items |
| `DECISIONS_MADE` | TEXT | Decisions reached |
| `QUESTIONS_RAISED` | TEXT | Open questions |
| `ACCOUNT_NAME` | VARCHAR | Account name from filename |
| `CALL_START_TS` | TIMESTAMP_NTZ | Call start time from filename |
| `PARTICIPANTS_JSON` | VARIANT | Participant metadata |

### GONG_CALLS_MIRROR

Synced from Snowhouse. Includes Gong-specific fields: `DIRECTION`, `CALL_RESULT`, `CALL_OUTCOME`, `CALL_SCORE`, `TALK_TIME_US_SECONDS`, `TALK_TIME_THEM_SECONDS`, `TOPICS_JSON`, `STATS_JSON`, and Salesforce IDs.

## Configuration

### Notebook Config (Cell 4)

```python
WHISPER_MODEL = "base"                # tiny | base | small | medium | large
ENABLE_SPEAKER_DIARIZATION = False    # True to identify speakers
SKIP_ALREADY_TRANSCRIBED = True       # Skip files already in results table
FORCE_RETRANSCRIBE = False            # Re-process all files
```

**Model tradeoffs:** `base` is the default (~16x realtime on GPU_NV_S). `large` is ~10x slower — don't upsize without considering the GPU cost.

### Parallel Deployments

Edit `scripts/00_config.sql` to deploy multiple instances (dev/staging/prod) without conflicts:

```sql
SET PROJECT_DB = 'TRANSCRIPTION_DEV';
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';
SET PROJECT_WH = 'TRANSCRIPTION_DEV_WH';
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_DEV_GPU_POOL';
```

## Project Structure

```
audio-video-transcription-snowflake/
├── scripts/
│   ├── 00_config.sql                 # Session variables for parallel deployments
│   ├── 01_setup.sql                  # Database, schema, stage, compute pool, table
│   ├── 02_automate.sql               # Streams, tasks, stored procedure
│   ├── 03_deploy_notebook.sh         # Deploy notebook via Snowflake CLI
│   ├── 04_teardown.sql               # Teardown all project objects
│   ├── 05_sync_gong.sh               # Cross-account Gong sync orchestrator
│   ├── 05_sync_gong.sql              # Gong call SELECT (runs on Snowhouse)
│   └── 06_gong_objects.sql           # Gong mirror table, unified view, search,
│                                     #   semantic view, and Cortex Agent (runs on DEMO)
├── notebooks/
│   └── audio_video_transcription.ipynb  # GPU transcription notebook
├── streamlit/
│   └── transcription_dashboard.py       # Streamlit in Snowflake dashboard
├── av.uploader/
│   ├── upload_av_files.py               # CLI uploader with Gong sync prompt
│   ├── config.template.json             # Connection config template
│   ├── create_av_service_user.sql       # Service account setup
│   └── cleanup_av_service_user.sql      # Service account teardown
├── snowflake.yml                        # Streamlit deploy definition
├── agents.md                            # Project instructions for Cortex Code
└── environment.yml                      # Conda environment (Python 3.9)
```

## Cost Guardrails

- **GPU compute pool** auto-suspends after 1 hour of inactivity
- **Cortex LLM** (`claude-sonnet-4-6`) is called once per file — cost scales with transcript length
- **Stage refresh task** runs every 5 minutes on an XS warehouse — suspend when the pipeline is not in use
- **Cortex Search** (`MEETING_SEARCH`) refreshes incrementally every hour
- Set `FORCE_RETRANSCRIBE = True` only with awareness that every file will consume GPU time and Cortex credits

## Troubleshooting

| Issue | Solution |
|---|---|
| FFmpeg installation fails | Ensure GPU compute pool is active; check external access integrations |
| Out of memory | Use smaller Whisper model (`tiny`/`base`); reduce batch size |
| Slow processing | Verify GPU pool is active; consider `base` model over `large` |
| Stream staleness | Check `DATA_RETENTION_TIME_IN_DAYS >= 14` on the database |
| Gong sync fails | Verify `snowhouse` connection is configured; check `--enable-templating NONE` flag |
| Duplicate transcriptions | Ensure `SKIP_ALREADY_TRANSCRIBED = True` in notebook Cell 4 |
