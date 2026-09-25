# Architecture — Audio/Video Transcription Pipeline

> **Maintenance contract:** this file is the authoritative architecture description for
> this project. It MUST be updated in the same change that alters the architecture, and
> whenever it changes the companion draw.io diagrams in this folder MUST be regenerated
> via the `drawio-diagrams` skill. See `agents.md` -> KEY RULES -> Architecture
> Documentation. Diagrams that disagree with this file are bugs.
>
> Last verified against the live account: 2026-09-25 (locator `ZCB17403`).

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
      |     |   run in flight -> return BLOCKED, nothing launched
      |     |   no new files  -> return SKIPPED, nothing launched
      |     |   new files     -> DROP SERVICE + EXECUTE JOB SERVICE (synchronous)
      |     v
      |  TRANSCRIBE_JOB  (headless GPU job on TRANSCRIPTION_GPU_POOL_V2)
      |     |   uv pip install --system --break-system-packages openai-whisper
      |     |   payload mounted from @PAYLOAD_STAGE -> transcribe_job.py
      |     |   GET files from stage -> whisper.load_model('base') -> transcribe
      |     |   SNOWFLAKE.CORTEX.COMPLETE -> markdown summary -> parsed into fields
      |     |
      |     +--> TRANSCRIPTION_RUN_EVENTS (append-only progress telemetry)
      |     |          |
      |     |          v
      |     |    V_TRANSCRIPTION_RUN_STATUS (DERIVED_STATE, IS_ACTIVE)
      |     |          |
      |     v          |
      |  TRANSCRIPTION_RESULTS (497 rows)          GONG_CALLS_MIRROR (56 rows)
      |     |          |                                  ^
      |     |          |                                  | scripts/06_sync_gong.sh
      |     |          |                                  | MERGE from Snowhouse
      |     +----------|------------+---------------------+
      |                |            v
      |                |    UNIFIED_MEETINGS_V  (LOCAL 497 + GONG 56 = 553)
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
never writes `TRANSCRIPTION_RESULTS` or `TRANSCRIPTION_RUN_EVENTS` — only the transcription
payload does.

## 3. Object inventory

All names come from `scripts/00_config.sql`, which is the single source of truth. Nothing
below should be hard-coded anywhere else.

### Compute

| Object | Type | Notes |
|---|---|---|
| `TRANSCRIPTION_WH_V2` | Warehouse | XS. Only runs initialization and SQL pushdown; all heavy work is on the GPU pool. `STATEMENT_TIMEOUT_IN_SECONDS = 14400` |
| `TRANSCRIPTION_GPU_POOL_V2` | Compute pool | `GPU_NV_S`, 1-3 nodes, `AUTO_SUSPEND = 300s`. Runs the transcription container |

### Pipeline

| Object | Type | Notes |
|---|---|---|
| `@AUDIO_VIDEO_STAGE` | Internal stage | Media files. The **payload** reads this name from config (spec env `PROJECT_STAGE_AV`, rendered from `__FQ_STAGE_AV__`), so it is not hard-coded there. It **is** hard-coded in the rollback notebook (cell 5, `STAGE_PATH = f"@{db}.{schema}.AUDIO_VIDEO_STAGE"`) — which is why `00_config.sql` still marks it "DON'T UPDATE". Renaming it is safe only if you also drop notebook mode |
| `@PAYLOAD_STAGE` | Internal stage | The job-service payload: `transcribe_job.py`, `transcribe_functions.py`, `transcribe_job_spec.yaml`. SYSADMIN-owned, directory enabled. Mounted at `/opt/payload` in the container. Deployed by `scripts/05_deploy_payload.sh`, which verifies by **downloading and comparing content** — a successful `PUT` is not proof |
| `@NOTEBOOK_STAGE` | Internal stage | Where the notebook artifact lives, for the rollback path. Referenced by `00_config.sql`, `02_setup.sql` and `04_deploy_notebook.sh` — **not** from inside the notebook |
| `TRANSCRIBE_NEW_FILES_TASK_V2` | Task | **No schedule** — event-driven only, via `EXECUTE TASK`. SYSADMIN-owned. `USER_TASK_TIMEOUT_MS = 1800000` (30 min, task-scoped) |
| `TRANSCRIBE_IF_NEW_FILES()` | Procedure | The gate. SYSADMIN-owned, `EXECUTE AS OWNER`. Refreshes the stage directory, diffs against results, and launches only when there is real work. Returns `LAUNCHED` / `SKIPPED` / `BLOCKED`. The launch statement is chosen at **deploy** time from `PROJECT_LAUNCH_MODE`, so switching paths requires rebuilding this procedure |
| `TRANSCRIBE_JOB` | Job service | The active engine. Headless GPU container on `TRANSCRIPTION_GPU_POOL_V2`, image `container_runtime/gpu_x86_64:2.9.0`. SYSADMIN-owned. One stable name, **dropped and recreated per run** — Snowflake rejects reuse of a job name even after it reaches `DONE` |
| `SNOWFLAKE.CORTEX.COMPLETE` | Cortex LLM | Model **`claude-sonnet-4-6`**, called once per file, ~25-50s each. Transcript truncated to 28,000 chars. Verified against the payload source 2026-09-25 (`agents.md` previously claimed `claude-opus-4-5` — stale) |
| `TRANSCRIPTION_RESULTS` | Table | 23 columns. Transcript, SRT, speaker segments, and parsed summary fields |
| `TRANSCRIPTION_RUN_EVENTS` | Table | 18 columns, **append-only** progress telemetry emitted by whichever engine ran. SYSADMIN-owned. No UPDATE path by design, so a wedged container cannot rewrite history |
| `V_TRANSCRIPTION_RUN_STATUS` | View | Latest event per run plus `DERIVED_STATE` / `IS_ACTIVE`. Where `WORK_COMPLETE_NOT_EXITED` is derived from heartbeat age — routine on the notebook path, an alarm on the job-service path. SYSADMIN-owned |
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
| `PROJECT_LAUNCH_MODE` | Config variable | `'JOB_SERVICE'` (default) or `'NOTEBOOK'`. Read by `03_automate.sql` at **deploy** time to choose the gate's launch statement — it is not evaluated at run time, so changing it requires redeploying the gate procedure |
| `TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG` | View | Emitted by `00_config.sql` itself, projecting the configured names as one row. Exists because owner's-rights contexts **cannot read session variables** (`090244`), so the dashboard cannot run `00_config.sql` — it reads this view instead. Keeps the app off a second copy of the names |
| `TRANSCRIPTION_APP_ROLE` | Role | Least-privilege owner of the Streamlit app. Granted to SYSADMIN so ACCOUNTADMIN inherits. Read-only on data; `OPERATE` on the task; `READ, WRITE` on the AV stage. **Note `GRANT OWNERSHIP ON STREAMLIT` is unsupported** — the app must be recreated as this role, which `09_deploy_dashboard.sh` does |
| `AV_UPLOADER_SERVICE_USER` / `_ROLE` | User / role | Key-pair auth for the uploader. Needs WRITE on the AV stage and `OPERATE` on the task |

### Retained for rollback — not deprecated, not active

| Object | Status |
|---|---|
| `TRANSCRIBE_AV_FILES_V2` | GPU Container Runtime notebook (cpython-3.10), Whisper `base`. SYSADMIN-owned. **Not the active engine**, but selected whenever `PROJECT_LAUNCH_MODE = 'NOTEBOOK'`, so it stays deployed and its stage stays populated. Subject to the `snowbook` shutdown hang — see §5 |

### Deprecated — keep suspended

| Object | Why |
|---|---|
| `AV_STAGE_STREAM_V2` | Stream was never consumed, so the old `SYSTEM$STREAM_HAS_DATA` gate passed permanently after the first upload |
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

## 5. Resolved: the notebook hang, and why the notebook is still here

**The hang is fixed, by removing the runtime that caused it.** The payload runs as a headless
`EXECUTE JOB SERVICE` container (`PROJECT_LAUNCH_MODE = 'JOB_SERVICE'`, shipped 2026-09-25).
A headless script has no gRPC server, no Streamlit adaptor and no `run_till_end`, so the stack
that used to park no longer exists.

Measured on the same GPU pool (`TRANSCRIPTION_GPU_POOL_V2`, `GPU_NV_S`). These figures come
from `INFORMATION_SCHEMA.TASK_HISTORY` and are reproducible:

| Launch path | Task runs | Task returned cleanly | Worst case |
|---|---|---|---|
| `EXECUTE NOTEBOOK` (2026-09-24) | 3 | 2 | 1 ran the **full 1801s timeout** and FAILED with `000630` |
| `EXECUTE JOB SERVICE` (2026-09-25) | 4 launched + 1 `SKIPPED` | **all 5** | slowest was 426s for 4 files, well inside the cap |

The job-service runs completed in 171s (1 file), 269s (1 file), 269s (2 files) and 426s
(4 files); the `SKIPPED` verdict returned in 5s without starting a container.

**Exit tail, measured precisely on the live 4-file run:** last work event 14:32:11, terminal
`SUCCEEDED` emitted in the same second, task returned 14:32:17 — a **6s** tail. Compare the
notebook's failure mode, where the task blocked from the last row write to the 30-minute
timeout.

> **Evidence note.** Additional isolated job-service launches were run during port validation
> (direct `EXECUTE JOB SERVICE`, not task-driven) and are recorded in `DIARY.md`. Their
> telemetry and transcripts were removed by the post-test cleanup, so they are **not**
> reproducible from the live account and are deliberately excluded from the table above. The
> four task-driven runs and the notebook comparison are both still queryable.

**The notebook is retained, not deprecated.** `PROJECT_LAUNCH_MODE = 'NOTEBOOK'` still selects
`EXECUTE NOTEBOOK`, and it is the rollback path if a job-service defect appears. Everything
below therefore remains live behaviour for anyone who rolls back — it is history for the
default path only.

### What changed about the GPU-leak risk

The two paths fail differently on a task timeout, and the distinction drives operations:

- **Notebook:** `USER_TASK_TIMEOUT_MS` caps the **task**, not the container. Verified
  2026-08-19 — the 15:52 run's task died at 16:22 while its container held a `GPU_NV_S` node
  until manually stopped at 16:57. A hung notebook run **leaks a GPU node indefinitely**, so
  `ALTER COMPUTE POOL … STOP ALL` then `SUSPEND` is mandatory afterwards. See
  [../operations/runbook.md](../operations/runbook.md) §6.
- **Job service:** the timeout **cancels the job and removes the service.** Verified
  2026-09-25 — the pool returned to `num_jobs = 0` with no manual intervention. There is
  nothing to reclaim.

### The exposure that replaced it

`persist()` runs **once, after the file loop**, so a run is all-or-nothing: a timeout partway
through a batch discards every transcript from that run, including files that already finished.

The binding constraint is **file count at least as much as audio length**, because Whisper is
fast and the per-file Cortex summary is not. Whisper alone at the measured ~0.037 ratio would
fit roughly eight hours of audio inside the 30-minute task timeout, but each file also costs
~25-50s of `CORTEX.COMPLETE` and the container pays a one-off ~115s install. Worked example:
the 13 retained test files total 5.2 hours of audio and estimate to **22-32 minutes** — close
enough to the timeout that a single batch of that size is a real risk of losing everything.

This, not the hang, is now the main way a large run can waste GPU time. Per-file `persist()`
and a payload-side file-count limit are the open follow-ups.

### Historical: root cause of the notebook hang

Retained because `NOTEBOOK` mode can still be selected, and because this analysis is what
established that the fix had to be architectural rather than a code change in the notebook.

`EXECUTE NOTEBOOK` is **synchronous**, so the calling task blocks for the notebook's entire
lifetime. On multi-file runs the notebook completed every cell and wrote its rows, then
Snowflake's `snowbook` runtime failed to exit: the script runner waited forever in
`on_scriptrunner_ready` while the main thread sat in an asyncio loop serving gRPC. Nothing
signalled the process to exit, so the task blocked to a ~8,100s transport timeout.

- Root-caused 2026-08-19 with `faulthandler` stack dumps, and **re-confirmed against a live
  hang the same day** — 11 dump cycles at 120s intervals, byte-identically sized stacks, and
  **zero notebook frames** in any post-completion dump. It was inside Snowflake's runtime, not
  this project's code.
- **Multi-file only:** 0/8 single-file runs hung; **6/8** multi-file runs did. Three 3-file runs
  on 2026-08-19 went hang / clean / hang within six hours — it was intermittent, which is why a
  single clean run was never evidence of improvement, and why seven consecutive clean
  job-service exits is the relevant comparison.
- **Two threads parked, and neither was ever signalled.** The main thread blocked in
  `selectors.select` inside `asyncio run_forever`, reached via
  `snowbook/snowflake/snowflake_run_adaptor.py:264 run_till_end`. The script-runner thread did
  **not** exit — it moved from executing the notebook to waiting in
  `snowbook/runtime/notebook_script_requests.py:232 on_scriptrunner_ready` for a next script
  request that never arrived. That frame is absent from the baseline dump and present in every
  dump afterwards, which marks the moment the hang begins.
- **What that proved:** the hang could not be fixed from inside the notebook. Across 35 minutes
  of live telemetry exactly one stack frame belonged to project code — cell 35 calling
  `dump_traceback()` itself. No cell, teardown handler, `atexit` hook or thread cleanup can
  affect a process in which none of our code is running. **What it did not prove:** why the
  completion signal never fired; the stacks show where the process waits, not what failed to
  notify it.

That narrower claim is what mattered: "nothing we can do" was false at other layers. The task
timeout bounded the task (though not the container), the dashboard detected the condition, and
the port deleted the failing stack outright.

Full evidence is in `DIARY.md` (2026-08-19, 2026-09-25). The instrumentation is documented in
[dashboard.md](dashboard.md) §5 — including why the **notebook** could not report its own clean
exit, and why the payload can.

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
`SELECT DISTINCT FILE_NAME FROM TRANSCRIPTION_RESULTS`. The SQL gate and the payload's
`discover()` both rely on this, so any change to `FILE_NAME` semantics must change both
together or they will disagree about what needs transcribing.

**They read the stage differently, on purpose.** The gate runs `ALTER STAGE … REFRESH` and then
diffs `DIRECTORY()`; the payload's `discover()` uses `LIST`, because the directory table goes
stale after both `PUT` and `REMOVE` and has previously reported a months-old view of this stage.
They agree in practice only because the gate refreshes first — if that `REFRESH` ever silently
fails, the gate can return `SKIPPED` on files the payload would have found. The divergence is in
*what they see*, not in *how they match*.

`ACCOUNT_NAME` and `CALL_START_TS` are parsed from the filename convention
`YYYY-MM-DD HH-MM-SS_Account_description.mp4`.

### Timestamp semantics — three types, three meanings

Nothing in the schema states which zone a timestamp is in, and two of these three columns are
routinely compared as though they matched. They do not.

| Column | Type | Written by | Reads as |
|---|---|---|---|
| `TRANSCRIPTION_RESULTS.TRANSCRIPTION_TIMESTAMP` | `TIMESTAMP_NTZ` | payload, `datetime.now(timezone.utc)` | **UTC**, but the type cannot say so |
| `TRANSCRIPTION_RESULTS.CALL_START_TS` | `TIMESTAMP_NTZ` | parsed from the filename | whatever zone the recorder used — unknowable |
| `TRANSCRIPTION_RUN_EVENTS.EVENT_TS` | `TIMESTAMP_LTZ` | `CURRENT_TIMESTAMP()` | session-local, correctly |

`TRANSCRIPTION_TIMESTAMP` and `EVENT_TS` describe the same run and disagree by the session
offset, because one comes from Python's clock inside the container and the other from
Snowflake's session. In `America/New_York` a run logged at `18:32` in the results table appears
as `14:32` in the events table. Neither is wrong; they are in different zones and nothing
reconciles them.

**Consumers must convert.** `TRANSCRIPTION_TIMESTAMP::DATE` gives the UTC date, so for a
US-Eastern operator it rolls over at 20:00 the previous evening. Filtering "today" needs local
day bounds converted to UTC first — which is what `av.uploader/download_srts.py` does in
`local_day_bounds_utc()`, client-side, so the predicate stays a range scan that can prune
micro-partitions. `CONVERT_TIMEZONE` on the column would work but needs a hardcoded zone name
and forces a per-row function call.

Until 2026-09-25 the payload used a bare `datetime.now()`. The stored values were UTC only
because the Container Runtime happens to run UTC; had that changed, the column's meaning would
have shifted mid-table with no offset stored to tell the eras apart. The call is now explicit.
`transcribe_functions.py` still uses a bare `datetime.now()` for the `**Generated:**` line in
`SUMMARY_MARKDOWN`, which is display text, never queried.

**The real fix is deferred.** `TIMESTAMP_LTZ` would make the column self-describing and make
`::DATE` and `CURRENT_DATE()` behave, as they already do for `EVENT_TS`. It is not a one-line
change: [`ALTER TABLE … ALTER COLUMN`](https://docs.snowflake.com/en/sql-reference/sql/alter-table-column)
lists changing a column to a different type as **unsupported**, and `SET DATA TYPE` accepts only
`NUMBER` and text types — so `NTZ → LTZ` means add a column, backfill, drop, rename, or CTAS and
swap. That rewrites every existing row, and the column feeds `UNIFIED_MEETINGS_V` →
`MEETING_SEARCH` (which would need reindexing) → the semantic view → the agent, plus roughly
fifteen source files. Worth doing as its own migration; not worth bundling into a CLI change.

**Related latent issue, not yet fixed.** `UNIFIED_MEETINGS_V` unions `CALL_START_TS` from
`TRANSCRIPTION_RESULTS` (`TIMESTAMP_NTZ`) with the same column from `GONG_CALLS_MIRROR`
(`TIMESTAMP_TZ`). The `UNION ALL` coerces to `TIMESTAMP_NTZ`, silently discarding the offset
Gong supplies.

## 7. Performance envelope

Whisper `base` on `GPU_NV_S` runs at **0.035-0.06x realtime**
(`PROCESSING_TIME_SECONDS / AUDIO_DURATION_SECONDS`), all-time mean **0.0373** across 497 rows.

**The ratio is not flat — it improves with duration**, because each file carries a fixed cost
(model load, ffmpeg conversion, ffprobe) that short recordings cannot amortise. An earlier
revision of this file claimed a "stable ratio of ~0.035x, flat across 440+ rows", which
understates short-file cost by nearly 2x:

| Audio duration | Files | Mean ratio |
|---|---|---|
| under 5 min | 6 | 0.061 |
| 5-20 min | 100 | 0.040 |
| over 20 min | 391 | 0.036 |

Cortex summary generation adds ~25-50s per file on top, and is often the larger share on short
recordings — which is why **file count drives runtime as much as total audio does.**

Representative runs, reconstructed by grouping `TRANSCRIPTION_RESULTS` on gaps of more than
10 minutes between consecutive writes. Whisper time is summed `PROCESSING_TIME_SECONDS`;
end-to-end comes from `TASK_HISTORY` and includes the container install, Cortex calls, stage
downloads and the final persist:

| Workload | Whisper | End to end | Ratio |
|---|---|---|---|
| 1 file, job service | — | 171s and 269s | — |
| 3 files, 8,585s audio (143 min) | 300s | — | 0.035 |
| 4 files, 4,718s audio (78.6 min), job service | 165s | **426s** | 0.035 |
| 18 files, 37,754s audio (629 min) — **largest run on record** | 1,233s | — | 0.033 |

The 4-file row is the useful one for budgeting: only **39%** of wall-clock was transcription.
An earlier revision of this file cited a "10 files / 18,202s / 978s" run as the largest on
record; no run matching those figures exists in the table, and the true maximum is the 18-file
run above.

## 8. Supported file formats

Discovered by the payload's `discover()`, which uses **`LIST`** rather than `DIRECTORY()` — the
directory table goes stale after both `PUT` and `REMOVE`, so `LIST` is the only reliable view of
what is actually on the stage. Anything whose extension is not in `MEDIA_EXT` is ignored.

- **Audio:** MP3, WAV, M4A, FLAC, AAC, OGG
- **Video:** MP4, AVI, MOV, MKV, WEBM, FLV

Video files are converted to 16 kHz mono PCM WAV with `ffmpeg` before transcription; duration
comes from `ffprobe`. Audio files go straight to `whisper.load_audio`.

**Known gap — three extensions upload but never transcribe.** The uploader's `AV_EXTENSIONS`
accepts `.wma`, `.wmv` and `.m4v`, which are absent from both the payload's `MEDIA_EXT` and the
SQL gate's extension list. A file with one of those extensions uploads successfully, fires the
task, and then sits on the stage indefinitely: the gate does not count it as new work, so the
run returns `SKIPPED` and nothing reports an error anywhere. Either add them to both lists or
remove them from the uploader.

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
