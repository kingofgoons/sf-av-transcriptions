# Audio/Video Transcription Pipeline — Persistent Agent Context

> **Read this file at the start of every session on this project before doing anything else.**

---

## PROJECT

End-to-end automated pipeline for transcribing audio/video files inside Snowflake, using:

- **GPU Container Runtime** + **OpenAI Whisper** for transcription
- **Snowflake Cortex LLM** (`SNOWFLAKE.CORTEX.COMPLETE`) for AI-generated meeting summaries
- A **Streamlit dashboard** for searching, browsing, and exporting results

A secondary **Gong sync** component mirrors call recordings from a Snowhouse account into the same results database for centralized search.

**Config**: all deployment-specific object names are session variables in `scripts/00_config.sql`. Copy that block into `01_setup.sql` and `02_automate.sql` before running. The active deployment uses `_V2` suffixes (see Snowflake Account section).

### Pipeline Flow

```
AUDIO_VIDEO_STAGE_FILES/ (local drop folder)
  → av.uploader/upload_av_files.py  (RSA key-pair auth)
  → @AUDIO_VIDEO_STAGE (Snowflake stage)
  → AV_STAGE_STREAM_V2 detects new files
  → REFRESH_STAGE_DIRECTORY_TASK_V2 (5-min polling task)
  → TRANSCRIBE_NEW_FILES_TASK_V2 triggers when stream has data
  → audio_video_transcription.ipynb (GPU_NV_S, Whisper model)
  → SNOWFLAKE.CORTEX.COMPLETE() (AI summary)
  → TRANSCRIPTION_RESULTS table
  → transcription_dashboard.py (Streamlit)
```

### Key Pipeline Objects

| Component | Object | Notes |
|---|---|---|
| Stage | `@AUDIO_VIDEO_STAGE` | Hard-coded in notebook — do not rename |
| Stream | `AV_STAGE_STREAM_V2` | Detects new file uploads |
| Stage Refresh Task | `REFRESH_STAGE_DIRECTORY_TASK_V2` | 5-min polling; uses `TRANSCRIPTION_WH_V2` (XS) |
| Transcription Task | `TRANSCRIBE_NEW_FILES_TASK_V2` | Fire-and-forget `EXECUTE NOTEBOOK` |
| GPU Notebook | `TRANSCRIBE_AV_FILES_V2` | Whisper transcription on GPU_NV_S |
| Compute Pool | `TRANSCRIPTION_GPU_POOL_V2` | GPU_NV_S, 1–3 nodes, auto-suspends 1 hr |
| Results Table | `TRANSCRIPTION_RESULTS` | Hard-coded in notebook — do not rename |
| Cortex LLM | `SNOWFLAKE.CORTEX.COMPLETE()` | `mistral-large2`; called once per file |
| Gong Mirror | `GONG_CALLS_MIRROR` | Synced from Snowhouse via `scripts/05_sync_gong.sh` |
| Dashboard | `transcription_dashboard.py` | Streamlit; run locally or deploy to SPCS |

---

## SNOWFLAKE ACCOUNT

- **Connection name:** `DEMO` — used by all scripts and the uploader
- **Active database:** `TRANSCRIPTION_DB_V2`
- **Active schema:** `TRANSCRIPTION_SCHEMA_V2`
- **Warehouse:** `TRANSCRIPTION_WH_V2` (XS) — set before querying
- **Compute pool:** `TRANSCRIPTION_GPU_POOL_V2` (GPU_NV_S, auto-suspends after 1 hr)

Always fully-qualify object names: `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.OBJECT`.
Never rely on session context alone.

> Object names are controlled by `scripts/00_config.sql`. For parallel deployments (dev/staging), change the `SET` variables in that file and copy the block into `01_setup.sql` and `02_automate.sql` before running.

---

## FOLDER LAYOUT

```
agents.md                                   ← this file (read at session start)
DIARY.md                                    ← project diary — write an entry after every change
plans/                                      ← saved implementation plans
scripts/
  00_config.sql                             ← session variables — source of truth for object names
  01_setup.sql                              ← create all Snowflake objects (one-time)
  02_automate.sql                           ← create stream-driven tasks
  03_deploy_notebook.sh                     ← upload notebook + update live version
  04_teardown.sql                           ← drop objects (4 levels — read before any DROP)
  05_sync_gong.sh                           ← sync Gong calls: Snowhouse → DEMO
  05_sync_gong.sql                          ← SELECT query run against Snowhouse
  06_gong_objects.sql                       ← create GONG_CALLS_MIRROR table
  install_ffmpeg.sh                         ← install ffmpeg in container
notebooks/
  audio_video_transcription.ipynb           ← Whisper + Cortex LLM GPU notebook
av.uploader/
  upload_av_files.py                        ← upload media from AUDIO_VIDEO_STAGE_FILES/ to stage
  download_srts.py                          ← download SRT files from TRANSCRIPTION_RESULTS
  config.json                               ← gitignored — copy from config.template.json
  config.template.json                      ← template for uploader connection config
  create_av_service_user.sql                ← provision RSA-authenticated service account
  cleanup_av_service_user.sql               ← remove service account
AUDIO_VIDEO_STAGE_FILES/                    ← local drop folder — place media files here to upload
output/bundle/streamlit/
  transcription_dashboard/
    transcription_dashboard.py              ← Streamlit app bundle (SPCS deployment)
transcription_dashboard.py                 ← local Streamlit entry point
migration/                                  ← schema migration scripts (v1 → v2)
examples.to.use/                            ← reference scripts and worked examples
.cortex/skills/av-transcription-dev/       ← local project skill
  SKILL.md                                 ← skill definition
  references/                              ← on-demand reference docs (see AGENT DOCS)
```

---

## HOW TO RUN

`scripts/` commands run from the `scripts/` directory. `av.uploader/` commands run from the project root.

### 1. Initial Setup (one-time, Snowsight)

```sql
-- Run in order in Snowsight:
-- scripts/00_config.sql  →  scripts/01_setup.sql  →  scripts/02_automate.sql
```

### 2. Upload Media Files

```bash
# Drop .mp4/.mp3/etc. into AUDIO_VIDEO_STAGE_FILES/, then:
python av.uploader/upload_av_files.py

# Custom source directory:
python av.uploader/upload_av_files.py -d /path/to/files
```

Pipeline auto-triggers within 5 minutes via `REFRESH_STAGE_DIRECTORY_TASK_V2`.

### 3. Deploy Notebook Changes

```bash
cd scripts/
./03_deploy_notebook.sh           # deploy (uses DEMO connection)
./03_deploy_notebook.sh --safe    # suspend tasks before deploy, resume after
```

### 4. Sync Gong Calls (Snowhouse → DEMO)

```bash
cd scripts/
./05_sync_gong.sh                 # sync new/updated calls
./05_sync_gong.sh --dry-run       # preview MERGE SQL without writing
```

### 5. Run Dashboard Locally

```bash
streamlit run transcription_dashboard.py
```

### 6. Download SRT Files

```bash
python av.uploader/download_srts.py
```

### 7. Teardown

```sql
-- Read scripts/04_teardown.sql first and pick the appropriate level:
-- Level 1: suspend tasks only
-- Level 2: + notebook + GPU pool
-- Level 3: + tables + stages
-- Level 4: full drop including database and warehouse
```

---

## KEY RULES

### Behavior

- Be direct. No sycophancy — no "great question!", no "you're absolutely right", no flattery.
- Don't over-engineer. Only make changes that are directly requested or clearly necessary.
- Don't create documentation files unless explicitly asked.
- Don't auto-commit. Only commit when explicitly asked.
- When unsure, investigate first — don't guess and don't assume the user is correct.
- **Write a `DIARY.md` entry after every project change** — date, what changed, and why.

### SQL — Idempotency

- Use `CREATE OR REPLACE` for views, stages, stored procedures, and notebooks.
- Use `CREATE TABLE IF NOT EXISTS` for `TRANSCRIPTION_RESULTS` and any accumulating tables.
- Use `CREATE WAREHOUSE IF NOT EXISTS` and `CREATE COMPUTE POOL IF NOT EXISTS`.
- Never use bare `CREATE` — it fails on second run.

### SQL — Safety

- **NEVER** suspend or drop `TRANSCRIPTION_GPU_POOL_V2` without confirming no transcription is actively running.
- Always check `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2')` before manually triggering transcription to avoid duplicate runs.
- Verify `DATA_RETENTION_TIME_IN_DAYS` on `TRANSCRIPTION_DB_V2` is ≥ 14 days before touching the stream — lower values risk staleness.
- Do not truncate or replace `TRANSCRIPTION_RESULTS` without confirming `SKIP_ALREADY_TRANSCRIBED = True` is set in the notebook — it relies on this table for deduplication.
- Before any DROP statement, read `scripts/04_teardown.sql` and match the appropriate level (1–4).

### SQL — Style

- Fully qualify object names: `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.OBJECT`.
- Use session `SET` variables (from `00_config.sql`) for all parameterized object references in setup and automation scripts.
- Uppercase SQL keywords. Snake_case for column aliases.

### Notebook & Pipeline

- Config knobs live in Cell 4 of `audio_video_transcription.ipynb`: `WHISPER_MODEL`, `ENABLE_SPEAKER_DIARIZATION`, `SKIP_ALREADY_TRANSCRIBED`, `FORCE_RETRANSCRIBE`.
- `base` is the default Whisper model — `large` is ~10× slower on GPU_NV_S. Flag the tradeoff before upsizing.
- `EXECUTE NOTEBOOK` is fire-and-forget. The task exits immediately; transcription runs asynchronously. Monitor via `ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY`, not task history.
- SRT subtitles are pre-generated at transcription time and stored in `TRANSCRIPTION_RESULTS`. Do not attempt to generate them dynamically at query time.

### Python

- RSA key-pair auth is required for the service account — do not switch to password auth.
- `config.json` is gitignored. Structural changes go in `config.template.json`; keep both in sync.
- Local environment (`environment.yml`): Python 3.9 + `snowflake-snowpark-python`. Don't add heavy deps without a clear reason.
- Keep `av.uploader/upload_av_files.py` runnable from the project root.

### Cost Guardrails

- Do not increase `AUTO_SUSPEND_SECS` on `TRANSCRIPTION_GPU_POOL_V2` without a reason — GPU_NV_S is expensive at idle.
- `SNOWFLAKE.CORTEX.COMPLETE` is called once per file — long recordings (1+ hr) produce large prompts and high credit use.
- Confirm `REFRESH_STAGE_DIRECTORY_TASK_V2` is suspended when the pipeline is not in use.
- Set `FORCE_RETRANSCRIBE = True` only with awareness that every file will re-consume GPU time and Cortex credits.

---

## AVAILABLE SKILLS

Use the `skill` tool to invoke any of these before starting relevant work. Do not reinvent what a skill already provides.

### Local (project-specific)

| Skill | When to use |
|---|---|
| `av-transcription-dev` | **Primary skill for this project.** Load for any pipeline development, debugging, monitoring, deployment, or optimization task. |

### Global

| Skill | When to use on this project |
|---|---|
| `snowflake-notebooks` | Working with the GPU Container Runtime notebook — cell structure, EXECUTE NOTEBOOK behavior, runtime config, notebook versioning. |
| `cortex-ai-functions` | Modifying or debugging `SNOWFLAKE.CORTEX.COMPLETE()` — prompt structure, model selection, response parsing. |
| `deploy-to-spcs` | Deploying `transcription_dashboard.py` as an SPCS service — image builds, service spec, networking. |
| `sql-author` | Writing or debugging Snowflake SQL — monitoring queries, TRANSCRIPTION_RESULTS analysis, MERGE statements in the Gong sync. |
| `warehouse` | Right-sizing `TRANSCRIPTION_WH_V2`, suspension/resumption behavior, warehouse credit troubleshooting. |
| `cost-intelligence` | Analyzing GPU compute pool and Cortex LLM credit consumption; identifying cost spikes from bulk re-transcription. |
| `dynamic-tables` | If refactoring the stream-task trigger pattern to a dynamic table approach. |

---

## AGENT DOCS (read on demand)

Deeper reference material. Read only when the task requires it.

- `DIARY.md` — project diary; write an entry here after every change (date + what changed + why)
- `.cortex/skills/av-transcription-dev/references/architecture.md` — pipeline components, database objects, `TRANSCRIPTION_RESULTS` schema, supported file formats
- `.cortex/skills/av-transcription-dev/references/development-workflow.md` — notebook deploy procedure, task suspend/resume, parallel deployment steps, teardown levels
- `.cortex/skills/av-transcription-dev/references/monitoring.md` — health-check queries, stream status, task history, transcription progress tracking
- `.cortex/skills/av-transcription-dev/references/known-issues.md` — stream staleness, common failures, root causes, fixes
