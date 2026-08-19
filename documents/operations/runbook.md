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
(database, schema, both stages, `TRANSCRIPTION_RESULTS`, compute pool, notebook) use
`IF NOT EXISTS`; only stateless definitions (network rules, integrations, file format,
`TRANSCRIPTION_SUMMARY` view) are `CREATE OR REPLACE`.

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

### Manual uploads do not auto-transcribe

Nothing polls the stage. If a file reaches `@AUDIO_VIDEO_STAGE` by any other route
(manual `PUT`, Snowsight, another client), trigger it yourself:

```sql
EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
```

To see what the gate *would* do without launching a GPU container:

```sql
CALL TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_IF_NEW_FILES();
```

---

## 3. Deploy notebook changes

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
and the container is wedged — this is the snowbook hang, not data loss. See §6.

### Local / export

```bash
streamlit run streamlit/transcription_dashboard.py   # needs st.connection, not SiS session
cd av.uploader/ && python download_srts.py           # bulk SRT export
```

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

A run lasting hours is the known snowbook shutdown hang, not slow transcription — see
`documents/architecture/architecture.md` §5.

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

-- Whisper throughput ratio; should hold near 0.035 and be flat over time
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
| 2 | + notebook, GPU pool, integrations, network rules |
| 3 | + view, `TRANSCRIPTION_RESULTS`, stages — **destroys transcripts** |
| 4 | + schema, database, warehouse |

The five guards:

| Guard | Enforces |
|---|---|
| A | `TEARDOWN_TARGET_DB` must equal the loaded `PROJECT_DB` |
| B | `TEARDOWN_LEVEL` must be 1–4 (default 0 refuses) |
| C | Levels ≥2 refuse while an `EXECUTE NOTEBOOK` is RUNNING |
| D | Levels ≥3 require a verified zero-copy clone **outside** the target DB with a matching row count |
| E | Levels ≥3 require `TEARDOWN_ACKNOWLEDGE_DATA_LOSS` and print the row count |

Every abort names the variable to set. Take the backup first — instant, and no storage cost
until divergence:

```sql
CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_20260819
  CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
```
