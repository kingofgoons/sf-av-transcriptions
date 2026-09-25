# Operations Runbook — Audio/Video Transcription Pipeline

Full operational detail for running, deploying, and tearing down this pipeline. `agents.md`
carries only the essential commands; this file has the reasoning, the gotchas, and the
guard semantics.

**Path conventions:** `scripts/` commands run from the `scripts/` directory. `av.uploader/`
commands normally run from `av.uploader/`.

---

## 1. Initial setup

**Once per account** — creates the shared config store:

```sql
-- Snowsight: scripts/01_bootstrap.sql
```

**After every config change** — publish config to the stage. Scripts read the STAGED copy,
not your local file, so skipping this means they silently use old values:

```bash
cd scripts/
./publish_config.sh
```

**Then create the deployment** (Snowsight, in order). Each script loads config itself:

```sql
-- scripts/02_setup.sql  →  scripts/03_automate.sql
```

Every script prints a `CONFIG_REVISION` row first. If it is not the revision you just
edited, the staged copy is stale — re-run `publish_config.sh`.

### Idempotency of `02_setup.sql`

Idempotent as of 2026-08-18 — safe to re-run against a live deployment. Stateful objects
(database, schema, all three stages, `TRANSCRIPTION_RESULTS`, compute pool, notebook) use
`IF NOT EXISTS`; only stateless definitions (network rules, integrations, file format,
`TRANSCRIPTION_SUMMARY` view) are `CREATE OR REPLACE`.

**`IF NOT EXISTS` is a no-op against an object that already exists**, including its ownership
and grants. If an object was first created by hand as the wrong role, re-running `02_setup.sql`
will not correct it — this is why the payload stage carries an explicit reconciling
`GRANT OWNERSHIP … COPY CURRENT GRANTS` rather than relying on creation alone.

**Consequence:** adding a column to the table DDL there does **not** alter an existing
table. Evolve a live deployment with `ALTER TABLE` (see `migration/`).

---

## 2. Upload media files

```bash
cd av.uploader/
python upload_av_files.py                      # reads ../AUDIO_VIDEO_STAGE_FILES/
python upload_av_files.py -d /path/to/files    # custom source directory
```

Requires **Python 3.9+** — see `agents.md` KEY RULES → Python for why 3.8 breaks the key
lookup.

The uploader triggers the pipeline itself (`EXECUTE TASK`, asynchronous) as soon as at
least one file uploads successfully. There is no polling delay. Requires `OPERATE` on the
task, granted by `av.uploader/create_av_service_user.sql`.

### It takes everything in the directory, but not subdirectories

The uploader has no `--pattern` or file-filter argument: it uploads **every** media file in the
source directory that is not already on the stage. There is no way to stage one specific file.

It scans with `Path.glob`, which is **not recursive**, so a subdirectory is a reliable holding
pen. Verified empirically: 13 files in `AUDIO_VIDEO_STAGE_FILES/_hold/` are invisible to the
uploader (`glob` finds 0, `rglob` finds 13). This is how test media is kept out of production
runs — move files *out* of `_hold/` to stage them, and back in when done.

### The Gong prompt comes after the work is done

At the end of a run the uploader asks `Sync Gong calls from Snowhouse → DEMO? [y/N]`. That
prompt is reached **after** the upload and the `EXECUTE TASK` trigger have both succeeded, so a
run that appears to be sitting there waiting has already done its job. Under a non-interactive
caller the `input()` raises `EOFError`, which is caught and treated as "no".

### Manual uploads do not auto-transcribe

Nothing polls the stage. If a file reaches `@AUDIO_VIDEO_STAGE` by any other route
(manual `PUT`, Snowsight, another client), trigger it yourself:

```sql
EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
```

### There is no dry run

**`CALL TRANSCRIBE_IF_NEW_FILES()` LAUNCHES.** It starts a GPU run whenever untranscribed
media exists, and returns `SKIPPED` only when the backlog is already empty. This runbook,
`agents.md` and `03_automate.sql` all previously described that `CALL` as a way to preview the
gate's verdict "without launching a container" — false, and it costs credits to discover.

To see what the gate *would* decide, without acting on it, use the read-only backlog query
(this is the same diff the gate performs):

```sql
ALTER STAGE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE REFRESH;

SELECT REGEXP_SUBSTR(d.RELATIVE_PATH, '[^/]+$') AS FILE_NAME,
       ROUND(d.SIZE / 1024 / 1024, 1) AS SIZE_MB,
       d.LAST_MODIFIED
FROM DIRECTORY(@TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE) d
LEFT JOIN TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS t
    ON REGEXP_SUBSTR(d.RELATIVE_PATH, '[^/]+$') = t.FILE_NAME
WHERE t.FILE_NAME IS NULL
  AND LOWER(REGEXP_SUBSTR(d.RELATIVE_PATH, '[^.]+$'))
      IN ('mp3','wav','m4a','flac','aac','ogg','mp4','avi','mov','mkv','webm','flv')
ORDER BY d.LAST_MODIFIED DESC;
```

Zero rows means a trigger would return `SKIPPED` and no container would start.

Do **not** substitute `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2')` — it reports TRUE
permanently, because nothing in the pipeline consumes that stream.

The gate returns one of **three** verdicts, visible in `TASK_HISTORY.RETURN_VALUE`:

| Verdict | Meaning |
|---|---|
| `LAUNCHED` | New media found; a GPU run was started |
| `SKIPPED` | Backlog empty; nothing started |
| `BLOCKED` | A run is already in flight; nothing started |

`BLOCKED` exists because the `JOB_SERVICE` path drops the job service before creating it, so
launching over a live run would kill a transcription in progress — and since records persist
once, after every file completes, that would lose the entire batch.

---

## 3. Deploy pipeline changes

The pipeline has **two** launch paths. `PROJECT_LAUNCH_MODE` selects which one the gate uses:
`JOB_SERVICE` (default, the active engine) or `NOTEBOOK` (rollback). They deploy separately.

### Deploy the payload — the active engine

```bash
cd scripts/
./05_deploy_payload.sh
```

Uploads three files to `@PAYLOAD_STAGE`: `transcribe_job.py`, `transcribe_functions.py`, and
`transcribe_job_spec.yaml` (rendered from `.template` using `V_PROJECT_CONFIG`). Override
`SNOW_CONNECTION` (default `DEMO`) if needed.

**A successful `PUT` is not proof of deployment.** The script downloads all three files back and
compares them byte-for-byte, because on 2026-09-24 a full validation run produced a
perfect-looking result with `FILE_PATH` silently empty: the payload had been uploaded, then
edited locally, and never re-uploaded — 35,840 staged bytes against 36,958 local. Every
dashboard signal said the run succeeded, and the fix under test was never exercised.

Two traps the script handles for you, both worth knowing if you ever deploy by hand:

- **Never compare stage `size` to local size.** Internal stages pad on encryption, so a correct
  upload reports 36,958 local → 36,960 staged. Compare content, not bytes. A check that cries
  wolf gets ignored.
- **`__pycache__` on the stage shadows edited source.** The stage is mounted as a volume, so
  stale bytecode wins at import time. The script removes it every run — and it reappears after
  every run, which is expected.

### Switching launch mode requires rebuilding the gate

`PROJECT_LAUNCH_MODE` is read by `03_automate.sql` at **deploy** time, not evaluated at run
time. The launch statement is baked into the gate procedure's body. Changing the variable in
`00_config.sql` and publishing it does **nothing** on its own — you must also rebuild the
procedure, or the gate will keep launching the old path.

### `03_automate.sql` needs a role-capable connection

The script contains `USE ROLE SYSADMIN`, and the PAT-authenticated `DEMO` connection rejects it
with `Current session is restricted. USE ROLE not allowed.` Run it from a connection that
permits role switching.

If you only need to rebuild the gate procedure, the surgical path is to issue the
`CREATE OR REPLACE PROCEDURE` plus the `GRANT OWNERSHIP … COPY CURRENT GRANTS` directly, with
the whole thing wrapped in `EXECUTE IMMEDIATE $$ … $$` — `snow sql -f` splits on semicolons, so
a bare `DECLARE … END` block arrives as fragments and fails with
`syntax error … unexpected '<EOF>'`. Note that `$body$` is **not** a valid dollar-quote tag in
Snowflake; only `$$` works, which is why a spec cannot be nested inline in a procedure body and
the job is launched with `FROM @stage SPECIFICATION_FILE = '…'` instead.

A working copy of this lives at `migration/06_gate_proc_job_service.sql`, but `migration/` is
gitignored — this runbook entry is the durable record of the pattern.

### Deploy notebook changes — rollback path only

The notebook is not the active engine, but it stays deployable so `PROJECT_LAUNCH_MODE =
'NOTEBOOK'` remains a real option.

```bash
cd scripts/
./04_deploy_notebook.sh           # reads all names from the config store
./04_deploy_notebook.sh --safe    # suspend the task during deploy, resume after
```

All object names come from `00_config.sql` via the config store — there are no V1/V2
overrides to pass. Override `SNOW_CONNECTION` (default `DEMO`) or `OWNER_ROLE`
(default `SYSADMIN`) only if needed.

The script deploys **and verifies**: it downloads the resulting live version, compares its
cell sources against your local file, and checks the notebook's owner. It exits non-zero on
any mismatch. Trust nothing that skips those checks.

### Never deploy by hand with `PUT` + `ADD LIVE VERSION FROM LAST`

`FROM LAST` restores from the last **committed version**, *not* from staged files. Once any
committed version exists, that sequence silently deploys **nothing** while still printing
`Live version successfully created.` This went unnoticed long enough that the live notebook
drifted older than git `HEAD`. See DIARY.md 2026-08-18.

Four behaviours the script exists to handle:

| Behaviour | Consequence |
|---|---|
| `ADD LIVE VERSION FROM LAST` reads the last **committed version** | A fresh `PUT` is ignored; deploy is a silent no-op |
| `COMMIT` **consumes** the live version (`is_live=false` everywhere) | Task fails instantly with `Live version is not found.` — must add live again after committing |
| `CREATE OR REPLACE NOTEBOOK` drops `EXTERNAL_ACCESS_INTEGRATIONS` | PyPI installs fail at runtime; must re-apply |
| `CREATE OR REPLACE NOTEBOOK` makes the **executing role** the owner | SYSADMIN-owned gate proc fails with `Notebook '...' does not exist or not authorized` |

`USE ROLE` cannot fix the last one — the PAT-authenticated `DEMO` connection rejects it
with `Current session is restricted. USE ROLE not allowed.` The script transfers ownership
after the DDL with `GRANT OWNERSHIP ... COPY CURRENT GRANTS` instead.

Accepted trade-off: `CREATE OR REPLACE` resets Snowflake-side version history on every
deploy. Git is the real history; the script's `COMMIT` leaves a fresh rollback point.

---

## 4. Sync Gong calls (Snowhouse → DEMO)

```bash
cd scripts/
./06_sync_gong.sh                 # sync new/updated calls
./06_sync_gong.sh --dry-run       # preview MERGE SQL without writing
```

---

## 5. Dashboard and exports

The dashboard lives in `streamlit/` as 12 modules and runs in Snowflake as
`TRANSCRIPTION_DASHBOARD` (title `transcription_dashboard_v3`). Full reference:
[../architecture/dashboard.md](../architecture/dashboard.md).

### Deploying it

```bash
cd scripts/
./09_deploy_dashboard.sh          # pre-flight, upload, recreate as app role, verify
```

**This is the only supported deploy path.** It runs `lint_dashboard.py` first, clears stale
staged modules, recreates the app **as `TRANSCRIPTION_APP_ROLE`**, then verifies every file
by download-and-diff and asserts the owner. Do not hand-deploy with
`snow streamlit deploy`: it requires `CREATE STAGE`, which the app role deliberately lacks,
and it will not set the owner correctly.

Do not skip the pre-flight. It exists because a hand-deploy shipped two undefined-name bugs
that `python -m compileall` passed — compiling proves a module *parses*, not that the names
it references exist.

**`CREATE OR REPLACE` assigns a new `url_id`**, so bookmarked direct links 404 after every
deploy. Navigate via **Projects » Streamlit**.

### Operating it

| Control | Notes |
|---|---|
| Pipeline Status panel | Live run state, phase (n of 6), and a measured completeness %. Not an estimate — units are counted only when work actually finishes |
| Auto-refresh | Sidebar toggle, **defaults OFF**. Each poll costs queries |
| Rescan stage | Opt-in `ALTER STAGE REFRESH`. Walks all 300+ files, so it is not on the auto-refresh path |
| Start transcription | Fires `EXECUTE TASK`. Disabled while a run is active or the backlog is empty. Real concurrency protection is the task's `ALLOW_OVERLAPPING_EXECUTION = FALSE`, not the button |
| Upload media | **200 MB per file, hard cap** — a warehouse-runtime limit, not configurable. Roughly 18% of existing recordings exceed it; those must go through `av.uploader` |

Upload does **not** trigger a run. Press **Start transcription** afterwards.

There is **no delete-file control and cannot be one**: owner's-rights contexts reject
`REMOVE` (`Unsupported statement type 'REMOVE_FILES'`). Remove stage files from your own
session, then `ALTER STAGE … REFRESH` to clear the directory entry.

If the status panel shows `WORK_COMPLETE_NOT_EXITED`, the transcripts are already committed
and the container is wedged — not data loss. On the notebook path this is the known `snowbook`
hang; on the job-service path it indicates a new exit fault worth investigating. See §6.

### Local / export

```bash
streamlit run streamlit/transcription_dashboard.py   # needs st.connection, not SiS session
cd av.uploader/ && python download_srts.py --today   # SRT export for today (local)
```

`download_srts.py` needs a window — `--today`, `--yesterday`, `--days N`, or `--start`+`--end`.
Dates are **local** and converted to UTC before the query, because `TRANSCRIPTION_TIMESTAMP` is
`TIMESTAMP_NTZ` holding UTC. Before 2026-09-25 the dates were compared raw, so a single-day
export actually covered 20:00 the previous evening to 20:00 that day — **an export taken before
that date is not comparable to one taken after.** See `architecture.md` §6.

---

## 6. Monitoring and diagnostics

`scripts/08_telemetry_debug.sql` holds nine queries: container sessions, activity timeline,
full log stream, errors/OOM, hung-teardown signature, GPU metrics, SQL-side correlation,
hang frequency by day, and (Q9) all-thread stack dumps for the shutdown hang.

Expect 3–5 minutes of ingestion latency. Event table `TIMESTAMP` is **UTC** while
`TASK_HISTORY` is session-local — convert before correlating.

Quick health check:

```sql
SELECT SCHEDULED_TIME, STATE,
       DATEDIFF('second', QUERY_START_TIME, COMPLETED_TIME) AS SECS,
       RETURN_VALUE, ERROR_MESSAGE
FROM TABLE(TRANSCRIPTION_DB_V2.INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => 'TRANSCRIBE_NEW_FILES_TASK_V2',
    SCHEDULED_TIME_RANGE_START => DATEADD('day', -2, CURRENT_TIMESTAMP())))
ORDER BY SCHEDULED_TIME DESC;
```

A run lasting hours means a wedged container. On the **notebook** path that is the known
`snowbook` shutdown hang. On the **job-service** path it should not happen at all — seven
consecutive runs exited in 5-9s — so treat it as a new defect rather than the known issue. See
`documents/architecture/architecture.md` §5.

### After a hang on the notebook path: reclaim the leaked GPU node

**Required, not optional — and specific to `PROJECT_LAUNCH_MODE = 'NOTEBOOK'`.** The task
timeout kills the task; it does **not** kill the notebook container. A hung run leaves a
`GPU_NV_S` node running **indefinitely** — the service has `auto_suspend_secs = 0`, and the
pool's `AUTO_SUSPEND` only counts *idle* time, which a RUNNING service prevents. Observed
2026-08-19: task died 16:22:06, container still `RUNNING` at 16:57 (65 minutes total) and had to
be stopped by hand.

**The job-service path does not leak.** Verified 2026-09-25: a task timeout **cancels the job and
removes the service**, and the pool returned to `num_jobs = 0` with no intervention. This is the
opposite of the notebook behaviour, so do not carry the notebook habit over — there is nothing
to reclaim, and `STOP ALL` on a healthy pool would kill a live run.

Check whenever the dashboard shows `HUNG (work saved)`, or after any task that FAILED with
transcripts present:

```sql
-- num_services > 0 with no run in progress means a container is leaked.
-- A DONE TRANSCRIBE_JOB is normal and is not a leak - the gate drops it on the next launch.
SHOW COMPUTE POOLS LIKE 'TRANSCRIPTION_GPU_POOL_V2';
SHOW SERVICES IN COMPUTE POOL TRANSCRIPTION_GPU_POOL_V2;   -- look for STPLATNOTEBOOK*
```

Reclaim it:

```sql
ALTER COMPUTE POOL TRANSCRIPTION_GPU_POOL_V2 STOP ALL;
ALTER COMPUTE POOL TRANSCRIPTION_GPU_POOL_V2 SUSPEND;
SHOW COMPUTE POOLS LIKE 'TRANSCRIPTION_GPU_POOL_V2';       -- expect SUSPENDED, 0 nodes
```

`STOP ALL` stops every service and job on the pool, so confirm no legitimate run is in progress
first — check the dashboard's Pipeline Status, or that `num_jobs = 0`. `auto_resume = true`, so the
next task run brings the pool back normally; suspending costs nothing but a cold start.

This is the most likely mechanism behind the historical ~230-credit runaway, and with 6 of 8
multi-file notebook runs hanging it recurred often. **The job-service port removed it** — kept
here because the notebook remains the rollback path.

### Worked example: anonymous blocks ≠ stored procedures

The Fix #1 gate procedure was originally written with `LS @stage` + `RESULT_SCAN`. The logic
validated perfectly in an anonymous block, then failed on `CALL` with
`Unsupported statement type 'LIST_FILES'` — `LS` is not permitted inside a stored procedure.
Anonymous blocks and stored procedures do **not** accept the same statement types. The
working version uses `ALTER STAGE ... REFRESH` (DDL, allowed) plus
`SELECT ... FROM DIRECTORY()`.

### Standard monitoring queries

> Do **not** use `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2')` as a health signal. Nothing
> consumes that stream, so it reports TRUE permanently. It is deprecated.

```sql
-- Files on the stage that have not been transcribed (the authoritative backlog)
ALTER STAGE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE REFRESH;

SELECT d.RELATIVE_PATH, d.SIZE, d.LAST_MODIFIED
FROM DIRECTORY(@TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.AUDIO_VIDEO_STAGE) d
LEFT JOIN TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS t
       ON d.RELATIVE_PATH = t.FILE_NAME
WHERE t.FILE_NAME IS NULL
ORDER BY d.LAST_MODIFIED DESC;

-- Recent transcriptions
SELECT FILE_NAME, DETECTED_LANGUAGE, SPEAKER_COUNT,
       AUDIO_DURATION_SECONDS, PROCESSING_TIME_SECONDS, TRANSCRIPTION_TIMESTAMP
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
ORDER BY TRANSCRIPTION_TIMESTAMP DESC
LIMIT 20;

-- Rows where the AI summary failed (should be zero; non-zero means the Cortex step broke)
SELECT COUNT(*) AS MISSING_SUMMARY
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
WHERE SUMMARY_MARKDOWN IS NULL
  AND TRANSCRIPTION_TIMESTAMP > DATEADD('day', -7, CURRENT_TIMESTAMP());

-- Whisper throughput ratio. Expect 0.035-0.06: it improves with duration, so short
-- recordings legitimately look worse. See architecture.md section 7 for the buckets.
SELECT FILE_TYPE, COUNT(*) AS FILES,
       ROUND(AVG(PROCESSING_TIME_SECONDS), 1) AS AVG_PROC_SEC,
       ROUND(AVG(AUDIO_DURATION_SECONDS), 1) AS AVG_AUDIO_SEC,
       ROUND(AVG(PROCESSING_TIME_SECONDS / NULLIF(AUDIO_DURATION_SECONDS, 0)), 4) AS RATIO
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
GROUP BY FILE_TYPE ORDER BY FILES DESC;

-- GPU container sessions and credits (catches the runaway pattern)
SELECT DATE(START_TIME) AS DAY, COUNT(*) AS SESSIONS, ROUND(SUM(CREDITS_USED), 2) AS CREDITS
FROM SNOWFLAKE.ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY
WHERE START_TIME > DATEADD('day', -14, CURRENT_TIMESTAMP())
GROUP BY 1 ORDER BY 1 DESC;

-- Compute pool state
SHOW COMPUTE POOLS LIKE 'TRANSCRIPTION_GPU_POOL_V2';
```

A healthy day shows a handful of container sessions. Roughly 290 means the gate is broken and
containers are launching with no work.

---

## 7. Teardown

`scripts/999_teardown.sql` is a single guarded block. There are no loose `DROP` statements,
so "Run All" cannot bypass the guards. Fill in the variables at the top:

```sql
SET TEARDOWN_TARGET_DB = '';                    -- type the exact DB name (guard A)
SET TEARDOWN_LEVEL = 0;                         -- 1..4 (guard B)
SET TEARDOWN_BACKUP_TABLE = '';                 -- verified clone, levels >= 3 (guard D)
SET TEARDOWN_ACKNOWLEDGE_DATA_LOSS = FALSE;     -- levels >= 3 (guard E)
```

| Level | Drops |
|---|---|
| 1 | tasks, stream, procedures |
| 2 | + notebook, payload stage, GPU pool, integrations, network rules |
| 3 | + view, `TRANSCRIPTION_RESULTS`, stages — **destroys transcripts** |
| 4 | + schema, database, warehouse |

The five guards:

| Guard | Enforces |
|---|---|
| A | `TEARDOWN_TARGET_DB` must equal the loaded `PROJECT_DB` |
| B | `TEARDOWN_LEVEL` must be 1–4 (default 0 refuses) |
| C | Levels ≥2 refuse while an `EXECUTE NOTEBOOK` is RUNNING. **Gap: does not match `EXECUTE JOB SERVICE`** — see below |
| D | Levels ≥3 require a verified zero-copy clone **outside** the target DB with a matching row count |
| E | Levels ≥3 require `TEARDOWN_ACKNOWLEDGE_DATA_LOSS` and print the row count |

> **Known gap in guard C (found 2026-09-25).** The guard matches
> `QUERY_TEXT ILIKE 'EXECUTE NOTEBOOK%'` only, so it does **not** detect a running
> `EXECUTE JOB SERVICE` — which is now the default launch path. A level-2 teardown during a live
> job-service run would drop the GPU pool and `@PAYLOAD_STAGE` mid-transcription, and because
> `persist()` writes once after every file completes, the entire in-flight batch would be lost.
>
> Existing transcripts are still protected: that requires level 3, which guards D and E cover
> independently. So this is a lost-batch risk, not an archive risk.
>
> **Until the guard is widened, check manually before any level ≥2 teardown:**
>
> ```sql
> SHOW SERVICES IN COMPUTE POOL TRANSCRIPTION_GPU_POOL_V2;   -- TRANSCRIBE_JOB must not be RUNNING
> SELECT DERIVED_STATE, IS_ACTIVE
> FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.V_TRANSCRIPTION_RUN_STATUS;
> ```

Every abort names the variable to set. Take the backup first — instant, and no storage cost
until divergence:
```sql
CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_20260819
  CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
```
