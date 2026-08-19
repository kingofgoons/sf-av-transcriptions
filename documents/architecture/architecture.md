# Architecture — Audio/Video Transcription Pipeline

> **Maintenance contract:** this file is the authoritative architecture description for
> this project. It MUST be updated in the same change that alters the architecture, and
> whenever it changes the companion draw.io diagrams in this folder MUST be regenerated
> via the `drawio-diagrams` skill. See `agents.md` -> KEY RULES -> Architecture
> Documentation. Diagrams that disagree with this file are bugs.
>
> Last verified against the live account: 2026-08-19 (locator `ZCB17403`).

## 1. What this application does

Transcribes local screen/meeting recordings with OpenAI Whisper on Snowflake GPU compute,
generates structured AI summaries with Cortex, stores everything in Snowflake, and exposes
it for search and conversational querying. Gong calls are mirrored in and unioned with the
local recordings so both sources are queryable through one interface.

## 2. End-to-end flow

```
Local machine                Snowflake                                    Consumption
-------------                ---------                                    -----------
AUDIO_VIDEO_STAGE_FILES/
      |
      | av.uploader/upload_av_files.py
      |   - key-pair auth as AV_UPLOADER_SERVICE_USER
      |   - skips files already in stage
      v
  @AUDIO_VIDEO_STAGE  ------------------------------+
      ^     |                                      |
      |     | EXECUTE TASK (uploader, or dashboard) |
      |     v                                      |
      |  TRANSCRIBE_NEW_FILES_TASK_V2 (no schedule)|
      |     |   ALLOW_OVERLAPPING_EXECUTION = FALSE  <- the real concurrency guard
      |     | CALL                                 |
      |     v                                      |
      |  TRANSCRIBE_IF_NEW_FILES()  <-- gate ------+
      |     |   ALTER STAGE REFRESH + DIRECTORY() diff vs TRANSCRIPTION_RESULTS
      |     |   no new files -> return SKIPPED, nothing launched
      |     |   new files    -> EXECUTE NOTEBOOK (synchronous)
      |     v
      |  TRANSCRIBE_AV_FILES_V2  (GPU notebook on TRANSCRIPTION_GPU_POOL_V2)
      |     |   pip install openai-whisper
      |     |   GET files from stage -> whisper.load_model('base') -> transcribe
      |     |   SNOWFLAKE.CORTEX.COMPLETE -> markdown summary -> parsed into fields
      |     |
      |     +--> TRANSCRIPTION_RUN_EVENTS (append-only progress telemetry)
      |     |          |
      |     |          v
      |     |    V_TRANSCRIPTION_RUN_STATUS (DERIVED_STATE, IS_ACTIVE)
      |     |          |
      |     v          |
      |  TRANSCRIPTION_RESULTS (444 rows)          GONG_CALLS_MIRROR (50 rows)
      |     |          |                                  ^
      |     |          |                                  | scripts/06_sync_gong.sh
      |     |          |                                  | MERGE from Snowhouse
      |     +----------|------------+---------------------+
      |                |            v
      |                |    UNIFIED_MEETINGS_V  (LOCAL 444 + GONG 50 = 494)
      |                |            |
      |                |  +---------+---------+------------------+
      |                |  v         v         v                  v
      |                | MEETING_  MEETINGS_  MEETING_      TRANSCRIPTION_DASHBOARD
      |                | SEARCH    SEMANTIC_  INTELLIGENCE   (transcription_dashboard_v3)
      |                |  (Search)   VIEW      (Agent)        owner TRANSCRIPTION_APP_ROLE
      |                |  LAG 1hr  (Analyst)      |                 |
      |                |                          v                 |
      |                +--------- status ---------|--- MEETING_INTELLIGENCE_MCP
      |                                           |    (MCP server for agent clients)
      +---- put_stream upload (<= 200 MB) --------+
            EXECUTE TASK kickoff (guarded)
```

The dashboard is both a consumer and a control surface: it reads `TRANSCRIPTION_RESULTS` and
`V_TRANSCRIPTION_RUN_STATUS`, and it can write media to the AV stage and fire the task. It
never writes `TRANSCRIPTION_RESULTS` or `TRANSCRIPTION_RUN_EVENTS` — only the notebook does.

## 3. Object inventory

All names come from `scripts/00_config.sql`, which is the single source of truth. Nothing
below should be hard-coded anywhere else.

### Compute

| Object | Type | Notes |
|---|---|---|
| `TRANSCRIPTION_WH_V2` | Warehouse | XS. Only runs initialization and SQL pushdown; all heavy work is on the GPU pool. `STATEMENT_TIMEOUT_IN_SECONDS = 14400` |
| `TRANSCRIPTION_GPU_POOL_V2` | Compute pool | `GPU_NV_S`, 1-3 nodes, `AUTO_SUSPEND = 3600s`. Runs the notebook container |

### Pipeline

| Object | Type | Notes |
|---|---|---|
| `@AUDIO_VIDEO_STAGE` | Internal stage | Media files. **Name hard-coded in the notebook — do not rename** |
| `@NOTEBOOK_STAGE` | Internal stage | Notebook payload. Also hard-coded in the notebook |
| `TRANSCRIBE_NEW_FILES_TASK_V2` | Task | **No schedule** — event-driven only, via `EXECUTE TASK`. SYSADMIN-owned. `USER_TASK_TIMEOUT_MS = 1800000` (30 min, task-scoped) |
| `TRANSCRIBE_IF_NEW_FILES()` | Procedure | The gate. SYSADMIN-owned, `EXECUTE AS OWNER`. Refreshes the stage directory, diffs against results, launches the notebook only when there is real work |
| `TRANSCRIBE_AV_FILES_V2` | Notebook | GPU Container Runtime (cpython-3.10), Whisper `base`. SYSADMIN-owned |
| `SNOWFLAKE.CORTEX.COMPLETE` | Cortex LLM | Model **`claude-sonnet-4-6`**, called once per file, ~25-50s each. Transcript truncated to 28,000 chars. Verified against the notebook source 2026-08-19 (`agents.md` previously claimed `claude-opus-4-5` — stale) |
| `TRANSCRIPTION_RESULTS` | Table | 23 columns. Transcript, SRT, speaker segments, and parsed summary fields |
| `TRANSCRIPTION_RUN_EVENTS` | Table | 18 columns, **append-only** progress telemetry emitted by the notebook. SYSADMIN-owned. No UPDATE path by design, so a wedged container cannot rewrite history |
| `V_TRANSCRIPTION_RUN_STATUS` | View | Latest event per run plus `DERIVED_STATE` / `IS_ACTIVE`. This is where `WORK_COMPLETE_NOT_EXITED` (the hang) is derived from heartbeat age. SYSADMIN-owned |
| `TRANSCRIPTION_SUMMARY` | View | Aggregate stats by file type and language |

### Consumption

| Object | Type | Notes |
|---|---|---|
| `UNIFIED_MEETINGS_V` | View | Unions LOCAL (`TRANSCRIPTION_RESULTS`) and GONG (`GONG_CALLS_MIRROR`) into one shape |
| `GONG_CALLS_MIRROR` | Table | Gong calls MERGEd in from Snowhouse by `scripts/06_sync_gong.sh` |
| `MEETING_SEARCH` | Cortex Search Service | `TARGET_LAG = 1 hour`, `snowflake-arctic-embed-m-v1.5`, INCREMENTAL. Searches a concatenated `SEARCH_TEXT` built from title, account, brief, topics, next steps, decisions, questions, and full transcript |
| `MEETINGS_SEMANTIC_VIEW` | Semantic view | Cortex Analyst model for meeting analytics (frequency, duration, talk ratio, coverage) |
| `MEETING_INTELLIGENCE` | Cortex Agent | Conversational interface over the above |
| `MEETING_INTELLIGENCE_MCP` | MCP server | Exposes the agent to MCP clients. This project's own server — not an independent source |
| `TRANSCRIPTION_DASHBOARD` | Streamlit | Warehouse runtime, title `transcription_dashboard_v3`, owned by **`TRANSCRIPTION_APP_ROLE`**. 12 modules under `streamlit/`. Reads results and run status; can trigger the task and upload to the AV stage. See **[dashboard.md](dashboard.md)** |

### Deploy / config

| Object | Type | Notes |
|---|---|---|
| `TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS` | Stage | Holds `00_config.sql`. Every script loads it with `EXECUTE IMMEDIATE FROM`, which runs in the same session so `SET` variables persist |
| `TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG` | View | Emitted by `00_config.sql` itself, projecting the configured names as one row. Exists because owner's-rights contexts **cannot read session variables** (`090244`), so the dashboard cannot run `00_config.sql` — it reads this view instead. Keeps the app off a second copy of the names |
| `TRANSCRIPTION_APP_ROLE` | Role | Least-privilege owner of the Streamlit app. Granted to SYSADMIN so ACCOUNTADMIN inherits. Read-only on data; `OPERATE` on the task; `READ, WRITE` on the AV stage. **Note `GRANT OWNERSHIP ON STREAMLIT` is unsupported** — the app must be recreated as this role, which `09_deploy_dashboard.sh` does |
| `AV_UPLOADER_SERVICE_USER` / `_ROLE` | User / role | Key-pair auth for the uploader. Needs WRITE on the AV stage and `OPERATE` on the task |

### Deprecated — keep suspended

| Object | Why |
|---|---|
| `AV_STAGE_STREAM_V2` | Stream was never consumed by the notebook, so the old `SYSTEM$STREAM_HAS_DATA` gate passed permanently after the first upload |
| `REFRESH_STAGE_DIRECTORY_TASK_V2` | Existed only to feed that stream. Resuming it costs a warehouse tick every 5 minutes for nothing |
| `RUN_TRANSCRIPTION_NOTEBOOK()` | Superseded by `TRANSCRIBE_IF_NEW_FILES()`, which gates before launching |

## 4. Why the trigger is event-driven

The pipeline originally ran a task every 5 minutes gated on a stream that nothing ever
advanced, so the GPU notebook launched ~300 times a day and did nothing on almost all of
them. It now works the other way around: the uploader fires `EXECUTE TASK` immediately after
a successful upload, and the gate procedure decides whether there is real work.

`EXECUTE TASK` is asynchronous and works on a **suspended** task, so the task deliberately
has no schedule and stays suspended. The uploader returns as soon as the task is queued.

**If you upload to the stage by any means other than `av.uploader/upload_av_files.py`, you
must fire the trigger yourself:**

```sql
EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
```

## 5. Known architectural constraint: the notebook hang

`EXECUTE NOTEBOOK` is **synchronous**, so the calling task blocks for the notebook's entire
lifetime. On multi-file runs the notebook completes every cell and writes its rows, then
Snowflake's `snowbook` runtime fails to exit: the script runner waits forever in
`on_scriptrunner_ready` while the main thread sits in an asyncio loop serving gRPC. Nothing
signals the process to exit, so the task blocks to a ~8,100s transport timeout.

- Root-caused 2026-08-19 with `faulthandler` stack dumps, and **re-confirmed against a live
  hang the same day** — 11 dump cycles at 120s intervals, byte-identically sized stacks, and
  **zero notebook frames** in any post-completion dump. It is inside Snowflake's runtime, not
  this project's code.
- **Multi-file only:** 0/8 single-file runs hung; **6/8** multi-file runs did. Three 3-file runs
  on 2026-08-19 went hang / clean / hang within six hours — it is intermittent, so one clean
  run is not evidence of improvement.
- Mitigation in place: `USER_TASK_TIMEOUT_MS = 1800000` caps the **task** at 30 minutes — but
  **NOT the container.** Corrected 2026-08-19: the 15:52 hung run's task died at 16:22 while its
  container held a `GPU_NV_S` node until manually stopped at 16:57. The service has
  `auto_suspend_secs = 0` and the pool's `AUTO_SUSPEND = 3600` only counts *idle* time, which a
  RUNNING service prevents — so **a hung run leaks a GPU node indefinitely.** After any hang run
  `ALTER COMPUTE POOL TRANSCRIPTION_GPU_POOL_V2 STOP ALL;` then `SUSPEND`. See
  [../operations/runbook.md](../operations/runbook.md) §6.
- **Now observable**, since 2026-08-19: `V_TRANSCRIPTION_RUN_STATUS` surfaces the hang as
  `WORK_COMPLETE_NOT_EXITED` and the dashboard renders it as a distinct state, so the
  operator can see that the data is safe but the container is wedged.
- The architectural fix is to move the payload off `EXECUTE NOTEBOOK` to a headless GPU job
  (`EXECUTE JOB SERVICE` on the same `snowbooks` GPU image), which removes the runtime
  entirely. See `.snowflake/cortex/plans/port-transcription-to-job-service.plan.md`.

Full evidence is in `DIARY.md` (2026-08-19). The instrumentation that makes it observable is
documented in [dashboard.md](dashboard.md) §5, including why the notebook **cannot** report
its own clean exit.

**Two threads park, and neither is ever signalled.** The main thread blocks in `selectors.select`
inside `asyncio run_forever`, reached via `snowbook/snowflake/snowflake_run_adaptor.py:264
run_till_end`. The script-runner thread does **not** exit — it moves from executing the notebook
to waiting in `snowbook/runtime/notebook_script_requests.py:232 on_scriptrunner_ready` for a next
script request that never arrives. That frame is absent from the baseline dump and present in
every dump afterwards, which marks the moment the hang begins.

**What that proves:** the hang cannot be fixed from inside the notebook. Across 35 minutes of
live telemetry exactly one stack frame belonged to project code — cell 35 calling
`dump_traceback()` itself. No cell, teardown handler, `atexit` hook or thread cleanup can affect
a process in which none of our code is running. **What it does not prove:** why the completion
signal never fires; the stacks show where the process waits, not what failed to notify it.

The narrower claim matters, because "nothing we can do" is false at other layers: the task
timeout bounds the task (though **not** the container — see above), the dashboard detects it, and the job-service port deletes the failing
stack — a headless script has no gRPC server, no Streamlit adaptor and no `run_till_end`.

## 6. Data model — `TRANSCRIPTION_RESULTS`

23 columns, written in one INSERT per run.

| Group | Columns |
|---|---|
| File identity | `FILE_PATH`, `FILE_NAME`, `FILE_TYPE`, `FILE_SIZE_BYTES` |
| Audio properties | `DETECTED_LANGUAGE`, `AUDIO_DURATION_SECONDS`, `SPEAKER_COUNT` |
| Transcript | `TRANSCRIPT`, `TRANSCRIPT_WITH_SPEAKERS` (VARIANT), `SRT_CONTENT`, `SRT_WITH_SPEAKERS` |
| AI summary | `SUMMARY_MARKDOWN`, `MEETING_TITLE`, `CALL_BRIEF`, `KEY_POINTS`, `NEXT_STEPS`, `DECISIONS_MADE`, `QUESTIONS_RAISED` |
| Metadata | `ACCOUNT_NAME`, `CALL_START_TS`, `PARTICIPANTS_JSON` (VARIANT), `PROCESSING_TIME_SECONDS`, `TRANSCRIPTION_TIMESTAMP` |

**Dedup contract:** `FILE_NAME` alone, matched exactly and case-sensitively, via
`SELECT DISTINCT FILE_NAME FROM TRANSCRIPTION_RESULTS`. The SQL gate and the notebook both
rely on this, so any change to `FILE_NAME` semantics must change both together or they will
disagree about what needs transcribing.

`ACCOUNT_NAME` and `CALL_START_TS` are parsed from the filename convention
`YYYY-MM-DD HH-MM-SS_Account_description.mp4`.

## 7. Performance envelope

Whisper `base` on `GPU_NV_S` runs at a stable ratio of ~0.035x realtime
(`PROCESSING_TIME_SECONDS / AUDIO_DURATION_SECONDS`), flat across 440+ rows. Cortex summary
generation adds ~25-50s per file and is often the larger share on short recordings.

| Workload | Observed |
|---|---|
| 8-min recording, 1 file | ~120s end to end |
| 143 min of audio, 3 files | ~600s of real work |
| 18,202s of audio, 10 files | 978s (largest legitimate run on record) |

A run that takes hours is not slow transcription — it is the hang in section 5.

## 8. Supported file formats

Discovered by glob in the notebook; anything else on the stage is ignored.

- **Audio:** MP3, WAV, M4A, FLAC, AAC, OGG
- **Video:** MP4, AVI, MOV, MKV, WEBM, FLV

Video files are converted to 16 kHz mono PCM WAV with `ffmpeg` before transcription; duration
comes from `ffprobe`. Audio files go straight to `whisper.load_audio`.

## 9. AI summary format

`SUMMARY_MARKDOWN` holds the raw Cortex response wrapped in a generated header. The prompt
instructs the model to emit exactly these sections, and `parse_summary_sections()` splits them
into the structured columns by matching the literal heading text:

| Heading in the response | Parsed into |
|---|---|
| `# Meeting Summary: <title>` | `MEETING_TITLE` (regex) |
| `**Summary**` | `CALL_BRIEF` |
| `Key Topics` | `KEY_POINTS` |
| `Follow-up Items` | `NEXT_STEPS` |
| `Decisions Made` | `DECISIONS_MADE` |
| `Questions Raised` | `QUESTIONS_RAISED` |

Follow-up items are prefixed by owner category: `**[SNOWFLAKE]**` for platform tasks,
`**[BO LANDSMAN - SE]**` for Sales Engineering actions, `**[GENERAL]**` for everything else.

Two known fragilities: the parser depends on the model reproducing those headings verbatim,
and the transcript is interpolated into SQL with only quote-doubling (`'` → `''`) before the
`CORTEX.COMPLETE` call. If parsing fails the whole function returns `None`, which NULLs all
seven summary columns at once — so a NULL `SUMMARY_MARKDOWN` means the summary step failed,
not that the recording had nothing in it.

## 10. Diagrams in this folder

Regenerate these whenever this file changes, using the `drawio-diagrams` skill.

| File | Contents |
|---|---|
| `architecture.uncompressed.drawio` | Editable source. **Always edit this one** |
| `architecture.drawio` | Compressed output for draw.io / LucidChart import |

Import into LucidChart via File -> Import.

## 11. Related documents

| Document | Covers |
|---|---|
| [dashboard.md](dashboard.md) | The Streamlit app: module layout, owner's-rights capability matrix, status/controls, theming, deploy, and port notes |
| [../operations/runbook.md](../operations/runbook.md) | Day-to-day operation, deploys, recovery |
