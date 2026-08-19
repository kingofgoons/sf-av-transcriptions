# Streamlit transcription status, kickoff, and upload

## Context

Four additions to `transcription_dashboard.py`, plus documentation for a future Snowflake App
Runtime port. The dashboard is currently **1,555 lines, strictly read-only** — 10 uncached
queries against exactly one object (`TRANSCRIPTION_RESULTS`), no `st.session_state`, no writes,
no task or procedure calls. This work introduces the first write path, the first privileged
operation, and the first query surface outside that table.

### Decisions taken

| Question | Decision |
|---|---|
| Runtime | **Stay on warehouse runtime** — 200 MB upload cap documented as a known limit |
| Progress display | **Step N of M**, plus an exact discrete % (see below) |
| Empty-table `st.stop()` | **Status panel renders above the guard**; data tabs guarded individually |
| App ownership | **Re-own to a dedicated least-privilege role** |

### One deviation to confirm

The original request asked for "a mathematical completeness %", and the chosen "Step count only"
option removes the percentage. This plan includes an **exact discrete** percentage —
`units_done / units_total`, counted from actually-completed work units with **no time
interpolation and no ETA**. That is a measurement, not the ±20% estimate that was rejected, and
it prevents a multi-minute transcription from appearing frozen. If you want it gone, drop
`PCT_COMPLETE` from the view in task 2 and the caption in task 5; nothing else changes.

### Verified constraints

**Upload cap is hard.** Warehouse-runtime SiS caps `st.file_uploader` at 200 MB and, per the
limitations doc, "in warehouse runtimes, this isn't configurable." Measured against the existing
corpus: **80 of 443 files (18%) exceed 200 MB; the largest is 1,741.7 MB.** In-app upload is
therefore a convenience path for small files, not a replacement for `av.uploader`.

**`EXECUTE TASK` may be rejected.** Warehouse-runtime apps "run as a stored procedure and are
subject to the same restrictions as owner's rights stored procedures." The permitted statement
list is SELECT, DML, DDL, GRANT/REVOKE, variable assignment, DESCRIBE/SHOW, LIST — `EXECUTE TASK`
is absent. Counter-evidence: `TRANSCRIBE_IF_NEW_FILES()` is an `EXECUTE AS OWNER` procedure that
runs `EXECUTE NOTEBOOK` successfully today, so the list is not strictly enforced. This is the
single largest unknown and gates task 6, hence the spike first.

**Privileges are already sufficient, ownership is not.** `SHOW GRANTS` on the task returns
OWNERSHIP to SYSADMIN and OPERATE to `AV_UPLOADER_SERVICE_ROLE`. The app is owned by
**ACCOUNTADMIN**, which inherits SYSADMIN, so it can operate the task — but that means a kickoff
button lets any viewer spend GPU credits with ACCOUNTADMIN reach. Task 4 fixes that first.

**Calibration.** Across 443 rows: `PROCESSING_TIME_SECONDS / AUDIO_DURATION_SECONDS` has
median 0.03671, mean 0.03719, SD 0.00740, range 0.00648–0.08964. Not used for ETA under the
chosen approach, but recorded here because it is the evidence that transcription time is
predictable, and it belongs in the docs.

**Filename contract.** `parse_filename_metadata()` requires
`YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext` — a **space** between date and time, `_` delimiters.
It splits on `_` and takes `parts[1]` as the account. A file uploaded with any other name silently
lands `ACCOUNT_NAME = NULL` and `CALL_START_TS = NULL`. The uploader must validate this.

### The hang interaction

The notebook completes its work and writes rows, then wedges in `snowbook`'s
`on_scriptrunner_ready` for ~2h07m on multi-file runs (capped at 30 min by
`USER_TASK_TIMEOUT_MS`). The status panel must not report a run as healthy-in-progress during
that window. Modelling status from a **heartbeat** rather than a start/end pair makes the hang
directly visible as `WORK_COMPLETE_NOT_EXITED` — a genuine diagnostic win, and the reason the
schema below separates "work finished" from "run finished".

---

## Task 1 — Spike the two uncertain capabilities

Do this before writing any feature code. Both are cheap.

Deploy a scratch Streamlit app (or a temporary tab behind a feature flag) that attempts, each in
its own try/except and reporting the exact exception:

1. `session.sql("EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2").collect()`
2. `session.sql("CALL TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_IF_NEW_FILES()").collect_nowait()`
3. `session.file.put_stream(io.BytesIO(b"probe"), "@TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE/_probe.txt", auto_compress=False, overwrite=True)`
4. `session.sql("LIST @...AUDIO_VIDEO_STAGE").collect()` and the `DIRECTORY()` equivalent
5. `SELECT CURRENT_ROLE(), CURRENT_DATABASE(), CURRENT_SCHEMA(), CURRENT_WAREHOUSE()`

Item 5 resolves a real open question the code review surfaced: **every object reference in the
dashboard is unqualified**, so nothing in the file proves it reads V2. Confirm before building.

Clean up `_probe.txt` afterwards.

**Launch decision:**
- (1) works → use `EXECUTE TASK`. Async by design, matches what `av.uploader` already does.
- (1) fails, (2) works → async `CALL` via `collect_nowait()`, returning immediately with a query
  ID the panel can poll. Note the honest downside: the statement runs under the app's warehouse
  session and is bounded by `STATEMENT_TIMEOUT_IN_SECONDS = 14400`, not by the task's 30-minute
  cap, so a hung run costs more. Prefer (1).
- Both fail → claim-table pattern: the button inserts a request row; a small scheduled task
  (1-minute) drains it. Rejected unless forced — it reintroduces polling and a scheduled task,
  the exact pattern this project removed to stop ~300 no-op launches a day.

If (3) fails, task 7 is not buildable on warehouse runtime; report that rather than working
around it, and the container-runtime migration becomes the path.

## Task 2 — Run-event instrumentation

The pipeline currently emits progress only as `print()` to the event table, which lands 3–5
minutes late and is unusable for a live panel. Add a purpose-built table.

**Append-only, not update-in-place.** Appends avoid write contention with a concurrently-reading
UI, cost less than updates, and preserve the history needed to answer "which step did it die
on". Expect roughly 6 rows per file plus 8 global rows — about 40 rows for a 5-file run.

```sql
CREATE TABLE IF NOT EXISTS <FQ>.TRANSCRIPTION_RUN_EVENTS (
    RUN_ID           VARCHAR      NOT NULL,
    SEQ              NUMBER       NOT NULL,
    EVENT_TS         TIMESTAMP_LTZ NOT NULL,
    RUN_SOURCE       VARCHAR,   -- NOTEBOOK | JOB_SERVICE | MANUAL
    STATUS           VARCHAR,   -- RUNNING | WORK_COMPLETE | SUCCEEDED | FAILED
    PHASE            VARCHAR,   -- STARTUP|DISCOVER|DOWNLOAD|TRANSCRIBE|PERSIST|COMPLETE
    PHASE_NUM        NUMBER,
    PHASE_TOTAL      NUMBER,
    FILE_INDEX       NUMBER,
    FILE_TOTAL       NUMBER,
    CURRENT_FILE     VARCHAR,
    FILE_STEP        VARCHAR,   -- EXTRACT_AUDIO|TRANSCRIBE|GENERATE_SRT|GENERATE_SUMMARY
    FILE_STEP_NUM    NUMBER,
    FILE_STEP_TOTAL  NUMBER,
    UNITS_DONE       NUMBER,
    UNITS_TOTAL      NUMBER,
    MESSAGE          VARCHAR,
    ERROR_MESSAGE    VARCHAR
);
```

`RUN_ID` is generated once at notebook startup (`uuid4()`), so it survives the port unchanged.

**Discrete unit accounting.** `UNITS_TOTAL = 4 + (files × 4)` — four global units (STARTUP,
DISCOVER, DOWNLOAD, PERSIST) and four per-file units. `UNITS_DONE` increments only when a unit
actually finishes. `EXTRACT_AUDIO` is skipped for audio-only input, so the emitter must count
that unit as complete immediately for audio files rather than leaving the total unreachable —
otherwise an all-audio run tops out below 100%.

Add a status view that derives current state and, critically, staleness:

```sql
CREATE OR REPLACE VIEW <FQ>.V_TRANSCRIPTION_RUN_STATUS AS
WITH latest AS (
    SELECT *, ROW_NUMBER() OVER (PARTITION BY RUN_ID ORDER BY SEQ DESC) AS rn
    FROM <FQ>.TRANSCRIPTION_RUN_EVENTS
)
SELECT RUN_ID, RUN_SOURCE, STATUS, PHASE, PHASE_NUM, PHASE_TOTAL,
       FILE_INDEX, FILE_TOTAL, CURRENT_FILE,
       FILE_STEP, FILE_STEP_NUM, FILE_STEP_TOTAL,
       UNITS_DONE, UNITS_TOTAL,
       CASE WHEN COALESCE(UNITS_TOTAL,0) > 0
            THEN ROUND(100.0 * UNITS_DONE / UNITS_TOTAL, 1) END AS PCT_COMPLETE,
       EVENT_TS AS LAST_HEARTBEAT_AT,
       DATEDIFF('second', EVENT_TS, CURRENT_TIMESTAMP()) AS SECONDS_SINCE_HEARTBEAT,
       MESSAGE, ERROR_MESSAGE,
       CASE
         WHEN STATUS IN ('SUCCEEDED','FAILED')                              THEN STATUS
         WHEN STATUS = 'WORK_COMPLETE'                                      THEN 'WORK_COMPLETE_NOT_EXITED'
         WHEN DATEDIFF('second', EVENT_TS, CURRENT_TIMESTAMP()) > 600       THEN 'STALLED'
         ELSE 'RUNNING'
       END AS DERIVED_STATE
FROM latest WHERE rn = 1;
```

The 600-second staleness threshold must exceed the longest legitimate gap between heartbeats.
The dominant gap is a single Whisper call: at ratio 0.037, a 90-minute recording is ~200s, and
the longest file in the corpus (1.7 GB) will be well under 600s. Add a heartbeat inside the
download loop too, since a 1.7 GB `session.file.get` is a long silent stretch. **Verify the
worst observed gap from real run data before trusting 600** — if any legitimate gap approaches
it, raise the threshold rather than accepting false STALLED reports.

Add both objects to `scripts/02_setup.sql` with names sourced from `00_config.sql` (bump
`CONFIG_REVISION`, republish via `publish_config.sh`). Never inline the names.

## Task 3 — Progress emitter in the notebook

Add a self-contained emitter, written so it moves to `transcribe_job.py` unchanged:

```python
class RunProgress:
    def __init__(self, session, table, run_id, run_source, units_total):
        ...
    def emit(self, status, phase, phase_num, **kw):  # INSERT one row, autocommit
        ...
    def complete_unit(self, n=1):
        ...
```

Every `emit` is a single-row `INSERT` executed with `.collect()` so it commits immediately and
the UI can see it. Wrap the body in try/except that **never raises** — instrumentation must not
be able to fail a transcription run. Log and continue.

Wiring, matching the existing structure:

| Phase | Cell | Emission point |
|---|---|---|
| STARTUP (1/6) | 8, 12 | whisper install, `whisper.load_model` |
| DISCOVER (2/6) | 17 | after the `SELECT DISTINCT FILE_NAME` and `LIST` |
| DOWNLOAD (3/6) | 17 | per file inside the download loop (heartbeat) |
| TRANSCRIBE (4/6) | 24 | per file, and per sub-step inside `transcribe_media_file` |
| PERSIST (5/6) | 28 | before and after the 23-column bulk INSERT |
| COMPLETE (6/6) | 28 | `WORK_COMPLETE` immediately after the INSERT commits |
| — | 35 | `SUCCEEDED` as the last statement in teardown |

The `WORK_COMPLETE` → `SUCCEEDED` split is what exposes the hang: a hung run stops at
`WORK_COMPLETE` and the view reports `WORK_COMPLETE_NOT_EXITED`.

`transcribe_media_file` currently takes only `file_path` and relies on module globals. Pass the
emitter in as an optional keyword argument defaulting to `None` so the function stays callable
without instrumentation — that keeps the port clean and avoids adding another implicit global to
the set the port plan already has to untangle.

`UNITS_TOTAL` is known only after DISCOVER determines the file count, so emit STARTUP rows with
`UNITS_TOTAL = NULL` and let the view's `COALESCE` guard handle it.

**Editing constraints:** use the `notebook_*` tools only — `edit`/`write` corrupt the JSON. Note
that `notebook_edit_cell` replaces a **substring**, not the cell; replacing only a header leaves
the old body appended, which produced a 161-line duplicated cell on 2026-08-18. Verify each cell
after editing. Deploy exclusively with `scripts/04_deploy_notebook.sh`, which verifies deployed
cell sources and owner and exits non-zero on mismatch.

## Task 4 — Dedicated app role

Do this before adding any write path.

```sql
CREATE ROLE IF NOT EXISTS TRANSCRIPTION_APP_ROLE;
GRANT USAGE ON DATABASE  <db>            TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT USAGE ON SCHEMA    <db>.<schema>   TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT USAGE ON WAREHOUSE TRANSCRIPTION_WH_V2 TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT SELECT ON TABLE    <fq>.TRANSCRIPTION_RESULTS       TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT SELECT ON TABLE    <fq>.TRANSCRIPTION_RUN_EVENTS    TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT SELECT ON VIEW     <fq>.V_TRANSCRIPTION_RUN_STATUS  TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT OPERATE ON TASK    <fq>.TRANSCRIBE_NEW_FILES_TASK_V2 TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT READ, WRITE ON STAGE <fq>.AUDIO_VIDEO_STAGE         TO ROLE TRANSCRIPTION_APP_ROLE;
GRANT ROLE TRANSCRIPTION_APP_ROLE TO ROLE SYSADMIN;
GRANT OWNERSHIP ON STREAMLIT <fq>.TRANSCRIPTION_DASHBOARD
    TO ROLE TRANSCRIPTION_APP_ROLE COPY CURRENT GRANTS;
```

`COPY CURRENT GRANTS` is mandatory — without it every existing viewer grant is dropped.
Capture `SHOW GRANTS ON STREAMLIT ...` **before and after** and diff them.

Two known traps: `USE ROLE` is unavailable on the PAT-authenticated `DEMO` connection
(`003107: Current session is restricted`), so run these as the current role and transfer
ownership rather than switching. And if the app uses any context function such as
`CURRENT_USER()` for an audit trail, the owner role additionally needs
`GRANT READ SESSION ON ACCOUNT` — decide whether to record who triggered a run, and grant it only
if so.

Add to `scripts/02_setup.sql`, idempotent.

## Task 5 — Dashboard refactor and status panel

**5a. Fully-qualified config.** Add module-level constants near the top and replace all 10 bare
`TRANSCRIPTION_RESULTS` references. This satisfies the standing rule against relying on session
context, and it is a prerequisite for the new queries, which reach objects that will not resolve
implicitly.

**5b. Restructure `main()`.** Currently: header (842) → sidebar (852) → data load (867) →
`st.stop()` if empty (897–904) → `get_summary_stats` (906) → five tabs (910).

Target order:

```
header
status_and_controls_panel()      # <- always renders, before any guard
load data
if df.empty: st.info(...)        # warn, do not stop
tabs = st.tabs([... , "⚙️ Status"])
  each data tab: if df.empty -> st.info and skip
```

Removing `st.stop()` means every tab body must tolerate an empty frame. The audit already found
unguarded truthiness on possibly-`None` values at lines 1195, 1230, and 1508 (`NaN > 0` is safe,
a real `None` raises `TypeError`) — harden those while restructuring, since an empty frame makes
them reachable.

**5c. Status panel.** Wrap in `@st.fragment(run_every="5s")` so auto-refresh re-runs only the
panel. This matters: there is no `@st.cache_data` anywhere in the file, so all 10 queries
re-execute on every rerun — an unscoped refresh loop would hammer the warehouse every 5 seconds.
`st.fragment` is the only thing making this affordable.

Display, per the chosen step-count approach:

```
● RUNNING          Phase 4 of 6 — TRANSCRIBE
                   File 2 of 3 — 2026-08-17 14-36-28_Alvarez.and.Marsal_dbt.discussion.mp4
                   Step 3 of 4 — Generating AI summary
                   11 of 19 units complete (58%)
                   last heartbeat 4s ago
```

State colours follow the existing house idiom — inline style overrides on `.info-box`-style
left-accent cards, matching how lines 394 and 402 colour match/context states, rather than new
CSS classes:

| `DERIVED_STATE` | Accent | Meaning |
|---|---|---|
| `RUNNING` | `#1f77b4` | heartbeat fresh |
| `WORK_COMPLETE_NOT_EXITED` | `#FF9800` | rows written, container wedged — the known hang |
| `STALLED` | `#FF5722` | no heartbeat >600s |
| `SUCCEEDED` | `#4CAF50` | clean exit |
| `FAILED` | `#9E9E9E` | with `ERROR_MESSAGE` |
| no rows | neutral | idle |

Reuse `get_snowflake_connection()` as-is. Follow the established fetcher shape: `session` first,
`if session is None` guard, try/except, return an empty frame or zeroed dict. Note
`.metric-container` (line 23) is defined but never used — use it here rather than adding CSS.

Also show the untranscribed backlog, using the same logic the task gate uses so the UI and the
gate cannot disagree:

```sql
ALTER STAGE <fq>.AUDIO_VIDEO_STAGE REFRESH;   -- DDL, permitted under owner's rights
SELECT d.RELATIVE_PATH, d.SIZE, d.LAST_MODIFIED
FROM DIRECTORY(@<fq>.AUDIO_VIDEO_STAGE) d
LEFT JOIN <fq>.TRANSCRIPTION_RESULTS t ON d.RELATIVE_PATH = t.FILE_NAME
WHERE t.FILE_NAME IS NULL;
```

Do **not** use `SYSTEM$STREAM_HAS_DATA` — nothing consumes `AV_STAGE_STREAM_V2`, so it is
permanently TRUE.

Use **bind parameters** in all new SQL. The audit found four existing f-string interpolation
sites (207, 178, 210/213/216, 220); leaving them is out of scope here, but do not add a fifth.

## Task 6 — Kickoff button

Gated on task 1's outcome.

```
[ ▶ Start transcription ]   3 file(s) waiting
```

Disable, with the reason shown, when: a run is `RUNNING` or `WORK_COMPLETE_NOT_EXITED`; the
backlog is zero (offer a "force anyway" expander that is explicit about launching a GPU for no
new work); or the compute pool is `SUSPENDED` and would need to resume (surface the cold-start
delay rather than appearing hung).

**Concurrency authority is the task, not the UI.** `TRANSCRIBE_NEW_FILES_TASK_V2` has
`allow_overlapping_execution = false`, so the platform already prevents a concurrent second run.
The UI check is advisory and inherently racy — two users can pass it simultaneously. Confirm
during the spike what `EXECUTE TASK` actually does against an already-executing task, and surface
that result honestly instead of claiming the button prevented it.

After a successful trigger, write a `RUN_SOURCE = 'MANUAL'` marker row and `st.rerun()` so the
fragment picks up the new run. Do not fabricate a `RUNNING` row — let the notebook's own first
emission establish real state, otherwise a failed launch leaves a phantom run in the view.

## Task 7 — In-app upload

Gated on spike item 3.

```python
MAX_UPLOAD_MB = 200  # warehouse-runtime hard limit, not configurable
```

Display the cap **before** the picker, not as a post-failure error. State plainly that files over
200 MB must go through `av.uploader/upload_av_files.py`, and that this affects a real fraction of
typical recordings — 18% of the current corpus, up to 1.7 GB.

Write with `session.file.put_stream(io.BytesIO(f.getbuffer()), stage_path, auto_compress=False, overwrite=False)`.
`auto_compress=False` is essential: gzipping media would break `ffprobe` and the extension-based
format detection in cell 21. `overwrite=False` protects existing recordings; surface a collision
as a clear message and require an explicit opt-in to replace.

**Validate the filename against the metadata contract** before upload, since
`parse_filename_metadata()` silently yields `NULL` account and timestamp otherwise:

- Required: `YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext` — space between date and time
- Extension in the supported set (audio: mp3/wav/m4a/flac/aac/ogg; video: mp4/avi/mov/mkv/webm/flv)
- On mismatch, warn and show the expected shape, but allow an explicit override — the transcript
  is still valuable without account attribution

After upload, `ALTER STAGE ... REFRESH` so the directory table and the backlog count reflect the
new file immediately, then `st.rerun()`.

Do not auto-trigger transcription on upload. Upload and kickoff stay separate, as requested.

## Task 8 — Documentation

**`documents/architecture/dashboard.md`** — the App Runtime port reference. Must cover: the five
existing tabs and the new status panel; all queries with their object dependencies; the session
bootstrap (`get_active_session()`, SiS-only, no local fallback); the single `@st.cache_resource`
and the absence of `cache_data`; the CSS palette and layout idioms; every warehouse-runtime
limitation that a port would lift (200 MB uploads, 32 MB message size, single-session caching, no
package-based v2 components, owner's-rights statement restrictions); and the write paths with the
privileges each needs.

Include an explicit **port-blocker list**: `get_active_session()` must become a connection
appropriate to the target runtime; `srt_download_link()` exists only to work around Snowsight
appending `?title=` and breaking presigned URLs, and should be deleted rather than ported; the
four f-string SQL sites must be parameterised before the code runs anywhere less trusted; and
`convert_speaker_segments_to_srt` (lines 730–771) is dead — the live path uses the pre-generated
`SRT_CONTENT` column, so drop it.

Record honestly that **container runtime, not App Runtime, is the smaller step** that fixes the
upload cap and the `EXECUTE TASK` restriction, so the port decision should weigh it.

**Updates to existing docs** (enforced by `agents.md`):
- `documents/architecture/architecture.md` — add `TRANSCRIPTION_RUN_EVENTS`,
  `V_TRANSCRIPTION_RUN_STATUS`, and the dashboard's new write paths to the object inventory;
  record the ownership change; add the calibration figures to the performance envelope.
- Regenerate `architecture.uncompressed.drawio` → `architecture.drawio` via the `drawio-diagrams`
  skill, with round-trip validation. A diagram that disagrees with `architecture.md` is a bug.
- `documents/operations/runbook.md` — operating the panel, reading `DERIVED_STATE`, what
  `WORK_COMPLETE_NOT_EXITED` means, and the upload size decision.
- `.cortex/skills/av-transcription-dev/references/known-issues.md` — the 200 MB cap as a
  documented limitation. Keep the other four reference files as pointers; do not re-add facts.
- `DIARY.md` entry.

## Verification

**Task 1 gates** — do not build past these:
- `EXECUTE TASK` from SiS either works or a fallback is proven working
- `put_stream` writes a probe file and it appears in `DIRECTORY()`; probe removed
- `CURRENT_DATABASE()` / `CURRENT_SCHEMA()` confirm the app targets **V2**

**Instrumentation (tasks 2–3):**
- A 1-file run produces a monotonic `SEQ`, ends `SUCCEEDED`, and reaches exactly
  `UNITS_DONE = UNITS_TOTAL` — verifying the audio-only `EXTRACT_AUDIO` skip does not strand the
  total below 100%
- A **3-file** run is mandatory: the hang is multi-file-only (0/8 single-file vs 4/6 multi-file),
  so single-file testing proves nothing about the hang path
- Confirm `PCT_COMPLETE` is monotonically non-decreasing and never exceeds 100
- Measure the **largest real gap between consecutive heartbeats** and confirm the 600s staleness
  threshold sits comfortably above it; raise it if not
- Force a failure (rename the stage mid-run) and confirm `FAILED` with a populated
  `ERROR_MESSAGE` rather than a silent stall
- Confirm a run whose transcription succeeds but whose container wedges reports
  `WORK_COMPLETE_NOT_EXITED`, not `RUNNING`

**Ownership (task 4):**
- `SHOW GRANTS ON STREAMLIT` before/after diff shows viewer grants preserved
- App loads and all five original tabs render under the new owner role
- `SHOW GRANTS TO ROLE TRANSCRIPTION_APP_ROLE` contains no ACCOUNTADMIN-level privilege

**UI (tasks 5–7):**
- Panel renders with `TRANSCRIPTION_RESULTS` empty — the case the old `st.stop()` forbade. Test
  against an empty clone, never by deleting rows.
- All five original tabs still work; no regression in the existing 10 queries
- Kickoff disabled while running, with the reason shown; enabled when idle with backlog
- Query count per 5-second refresh stays bounded — confirm via `QUERY_HISTORY` that the fragment
  is not re-firing all 10 dashboard queries
- A 250 MB file is rejected client-side with the cap explained, not a stack trace
- A file named `test.mp4` triggers the filename warning; a correctly named file populates
  `ACCOUNT_NAME` and `CALL_START_TS`
- Uploaded file appears in the backlog count without a manual refresh

**Data safety, non-negotiable:**
`TRANSCRIPTION_RESULTS` must never lose rows. Current count is **443 with 3 legacy NULL summaries**
(Oct 2025, Nov 2025, Jan 2026 — all predating the `import re` fix; not a regression). Record the
count before starting and confirm it after every task. Never delete a transcript to create a test
condition — use a zero-copy clone, as with the existing `TR_BACKUP_GOOD` pattern.

**Cost guard:**
Every kickoff test launches a GPU container. Prefer `CALL TRANSCRIBE_IF_NEW_FILES()` to exercise
the gate's decision without launching, and reserve full runs for the multi-file verification.
Check `NOTEBOOKS_CONTAINER_RUNTIME_HISTORY` afterwards — a healthy day is a handful of sessions;
~290 means the gate is broken.

## Out of scope

- Fixing the four existing f-string SQL sites (flagged; new code uses binds)
- The `EXECUTE NOTEBOOK` → job service port — separate active plan, and this instrumentation is
  deliberately built to move to it unchanged
- Turning off `HANG_FORENSICS`, which stays armed until the port lands
- Container-runtime migration (documented as the recommended follow-on)
- The duplicate `get_speaker_segments` call in tab 3 and the `.head(20)` Browse cap
