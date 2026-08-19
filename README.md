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
- `scripts/06_sync_gong.sh` pulls Gong call records from a Snowhouse account via cross-account JSON export and idempotent MERGE
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
# 0. ONCE PER ACCOUNT - create the shared config store (deploy DB + stage)
snow sql -f scripts/01_bootstrap.sql --connection YOUR_CONNECTION

# 1. Edit scripts/00_config.sql for your deployment, bump CONFIG_REVISION, then
#    publish it. Every later script reads the STAGED copy, so this is required.
cd scripts/ && ./publish_config.sh && cd ..

# 2. Create database objects, warehouse, compute pool, and table
#    Idempotent: stateful objects (db, schema, stages, results table, pool, notebook)
#    use IF NOT EXISTS, so re-running preserves transcripts and media.
snow sql -f scripts/02_setup.sql --connection YOUR_CONNECTION

# 3. Deploy the notebook to Snowflake
./scripts/04_deploy_notebook.sh

# 4. Set up the event-driven pipeline (gate procedure + scheduleless task)
snow sql -f scripts/03_automate.sql --connection YOUR_CONNECTION

# 5. Create Gong integration objects (view, search, semantic view, agent)
snow sql -f scripts/05_gong_objects.sql --connection YOUR_CONNECTION

# 6. Deploy Streamlit dashboard
snow streamlit deploy --replace --connection YOUR_CONNECTION

# If your CLI session token is expired (e.g. DEMO account), use a temporary connection with a PAT:
PAT=$(grep 'password' ~/.snowflake/config.toml | head -1 | sed 's/.*= *"\(.*\)"/\1/')
snow streamlit deploy --replace --temporary-connection \
    --account YOUR_ACCOUNT --user YOUR_USER --password "$PAT" \
    --role ACCOUNTADMIN --warehouse ADHOC_WH
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
./scripts/06_sync_gong.sh

# Preview without writing
./scripts/06_sync_gong.sh --dry-run
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

`scripts/00_config.sql` is the **single source of truth** for every object name. No other
script contains a config block; each loads it with one line:

```sql
EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;
```

To deploy another instance (dev/staging/prod), edit the values, bump the revision, and
publish:

```sql
SET CONFIG_REVISION = '2026-08-18c';
SET PROJECT_DB = 'TRANSCRIPTION_DEV';
SET PROJECT_SCHEMA = 'TRANSCRIPTION_SCHEMA';
SET PROJECT_WH = 'TRANSCRIPTION_DEV_WH';
SET PROJECT_COMPUTE_POOL = 'TRANSCRIPTION_DEV_GPU_POOL';
```

```bash
cd scripts/ && ./publish_config.sh
```

Scripts read the **staged** copy, so publishing is required for changes to take effect.
Each script echoes `CONFIG_REVISION` as its first result so a stale staged copy is
obvious. To run two deployments side by side, stage a second file (e.g.
`00_config_dev.sql`) and point the include at it.

## Project Structure

```
audio-video-transcription-snowflake/
├── scripts/
│   ├── 00_config.sql                 # SINGLE SOURCE OF TRUTH for object names
│   ├── 01_bootstrap.sql              # Deploy DB + config stage (once per account)
│   ├── 02_setup.sql                  # Database, schema, stage, compute pool, table
│   ├── 03_automate.sql               # Gate procedure + scheduleless task
│   ├── 04_deploy_notebook.sh         # Deploy notebook via Snowflake CLI
│   ├── 05_gong_objects.sql           # Gong mirror table, unified view, search,
│   │                                 #   semantic view, and Cortex Agent (runs on DEMO)
│   ├── 06_sync_gong.sh               # Cross-account Gong sync orchestrator
│   ├── 07_reset.sql                  # Stream reset (stopgap)
│   ├── 08_telemetry_debug.sql        # Container telemetry diagnostics
│   ├── 999_teardown.sql               # GUARDED teardown (4 levels, 5 guards)
│   ├── publish_config.sh             # Utility: PUT 00_config.sql to the stage
│   ├── sync_gong_query.sql           # Utility: Gong call SELECT (runs on Snowhouse)
│   └── install_ffmpeg.sh             # Utility: ffmpeg install in container
├── notebooks/
│   └── audio_video_transcription.ipynb  # GPU transcription notebook
├── transcription_dashboard.py           # Streamlit in Snowflake dashboard
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
| SiS "Python Interpreter Error: TypeError: bad argument type" | The main file must be at the **stage root** — no subdirectory. `snowflake.yml` `main_file` and `artifacts` must both reference `transcription_dashboard.py` with no path prefix. Any subdirectory name that matches a Python package (e.g. `streamlit/`) will shadow that package and cause this error. |
| SiS "No such file or directory: /tmp/appRoot/transcription_dashboard.py" | Same root cause as above — the main file is in a subdirectory on the stage. SiS resolves `MAIN_FILE` by basename only. Move the file to the stage root. |
| SiS `AttributeError: module 'streamlit' has no attribute 'rerun'` | The default SiS Streamlit runtime is older than 1.27. Use `st.rerun() if hasattr(st, 'rerun') else st.experimental_rerun()`. |
| SiS `AttributeError: module 'streamlit' has no attribute 'scatter_chart'` | `st.scatter_chart` requires Streamlit ≥ 1.25. Guard with `hasattr(st, 'scatter_chart')` and fall back to `st.line_chart`. |
| SiS app loads but shows stale content after redeploy | SiS has a server-side cache. Force a refresh by making a trivial edit to the source file before redeploying. |
