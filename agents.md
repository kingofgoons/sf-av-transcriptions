# Audio/Video Transcription Pipeline — Persistent Agent Context

> **Read this file at the start of every session on this project before doing anything else.**

---

## PROJECT

End-to-end automated pipeline for transcribing audio/video files inside Snowflake, using:

- **GPU Container Runtime** + **OpenAI Whisper** for transcription
- **Snowflake Cortex LLM** (`SNOWFLAKE.CORTEX.COMPLETE`) for AI-generated meeting summaries
- A **Streamlit dashboard** for searching, browsing, and exporting results

A secondary **Gong sync** component mirrors call recordings from a Snowhouse account into the same results database for centralized search.

**Config**: all deployment-specific object names live in **`scripts/00_config.sql` only**. Every other script loads them with a single `EXECUTE IMMEDIATE FROM` line — do NOT paste config blocks into scripts. Edit the file, then run `scripts/publish_config.sh`. The active deployment uses `_V2` suffixes (see Snowflake Account section).

### Pipeline Flow

```
AUDIO_VIDEO_STAGE_FILES/ (local drop folder)
  → av.uploader/upload_av_files.py  (RSA key-pair auth)
  → @AUDIO_VIDEO_STAGE (Snowflake stage)
  → uploader fires EXECUTE TASK (async) ── event-driven, no polling
  → TRANSCRIBE_NEW_FILES_TASK_V2 (no schedule)
  → TRANSCRIBE_IF_NEW_FILES() gate: ALTER STAGE REFRESH + diff DIRECTORY() vs results
  → launches notebook ONLY if untranscribed media exists
  → audio_video_transcription.ipynb (GPU_NV_S, Whisper model)
  → SNOWFLAKE.CORTEX.COMPLETE() (AI summary)
  → TRANSCRIPTION_RESULTS table
  → transcription_dashboard.py (Streamlit)
```

> **⚠️ MANUAL UPLOADS DO NOT AUTO-TRANSCRIBE.** Nothing polls the stage. If a file
> reaches `@AUDIO_VIDEO_STAGE` by any route other than `upload_av_files.py`
> (manual `PUT`, Snowsight, another client), you must trigger the pipeline yourself:
> ```sql
> EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
> ```
> To see what the gate would do without launching anything:
> ```sql
> CALL TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_IF_NEW_FILES();
> ```

> **STATUS (2026-08-18):** **Fix #1 (gate on real work) is deployed and fully
> validated end-to-end** — a real upload fired `EXECUTE TASK` 6s after the file
> landed, the task succeeded in 223s with
> `RETURN_VALUE = 'LAUNCHED: notebook run for 1 new file(s).'`, and the skip path
> returns `SKIPPED` in 2–4s without launching the GPU.
>
> **Fix #2 (`FORCE_KERNEL_EXIT` / `os._exit(0)`) was measured and then REVERTED.**
> It bounded a run to 243s vs 8,112s, but severing the kernel makes
> `EXECUTE NOTEBOOK` return `92848 UNAVAILABLE: ... connection termination`, so the
> task reports **FAILED** even though the transcript was written. `FORCE_KERNEL_EXIT`
> is now `False` by default and kept only as a documented escape hatch.
>
> **Root cause of the ~2h07m hang is now known and is NOT in our code.** Teardown
> diagnostics show **0 multiprocessing children** and **2 non-daemon threads**
> (`asyncio_0`, `ScriptRunner.scriptThread`) that belong to the Snowflake notebook
> runtime itself (Container Runtime notebooks are Streamlit-hosted). Python's
> shutdown joins non-daemon threads, so if either fails to wind down the process
> hangs. **It cannot be fixed from inside the notebook.** Options: the escape hatch,
> a lower `USER_TASK_TIMEOUT_MS`, or raise it with Snowflake. The
> `resource_tracker: leaked semaphore` warning appears on clean runs too — red herring.
>
> Notebook version history was reset on 2026-08-18 (see deploy notes); git is the
> rollback source. See DIARY.md 2026-08-18.


### Key Pipeline Objects

| Component | Object | Notes |
|---|---|---|
| Stage | `@AUDIO_VIDEO_STAGE` | Hard-coded in notebook — do not rename |
| Stream | `AV_STAGE_STREAM_V2` | **DEPRECATED** — never consumed by the pipeline; see DIARY.md 2026-08-18 |
| Stage Refresh Task | `REFRESH_STAGE_DIRECTORY_TASK_V2` | **DEPRECATED** — keep suspended; existed only to feed the stream |
| Transcription Task | `TRANSCRIBE_NEW_FILES_TASK_V2` | **No schedule** — triggered by the uploader via `EXECUTE TASK`. Owned by SYSADMIN. |
| Gate Procedure | `TRANSCRIBE_IF_NEW_FILES()` | Refreshes stage directory, diffs vs results, launches notebook only if new. SYSADMIN-owned, `EXECUTE AS OWNER`. |
| GPU Notebook | `TRANSCRIBE_AV_FILES_V2` | Whisper transcription on GPU_NV_S |
| Compute Pool | `TRANSCRIPTION_GPU_POOL_V2` | GPU_NV_S, 1–3 nodes, auto-suspends 1 hr |
| Results Table | `TRANSCRIPTION_RESULTS` | Hard-coded in notebook — do not rename |
| Cortex LLM | `SNOWFLAKE.CORTEX.COMPLETE()` | `claude-opus-4-5`; called once per file, ~23–31s each |
| Gong Mirror | `GONG_CALLS_MIRROR` | Synced from Snowhouse via `scripts/06_sync_gong.sh` |
| Unified View | `UNIFIED_MEETINGS_V` | `TRANSCRIPTION_RESULTS` ∪ `GONG_CALLS_MIRROR` — common schema |
| Cortex Search | `MEETING_SEARCH` | On `UNIFIED_MEETINGS_V.SEARCH_TEXT`; `TARGET_LAG = 1 hour`, INCREMENTAL |
| Semantic View | `MEETINGS_SEMANTIC_VIEW` | Cortex Analyst model over `UNIFIED_MEETINGS_V` |
| Cortex Agent | `MEETING_INTELLIGENCE` | Tools: `search_meetings`, `analyze_meetings` |
| MCP Server | `MEETING_INTELLIGENCE_MCP` | Exposed to CoCo as `mcp_meeting-intel_meeting_intelligence` |
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

> Object names are controlled by **`scripts/00_config.sql` alone**. For parallel deployments (dev/staging), change the `SET` variables there, bump `CONFIG_REVISION`, and run `scripts/publish_config.sh`. Never re-introduce a pasted config block — that is what caused three scripts to silently target V1 while the live deployment was V2 (see DIARY.md 2026-08-18).

---

## FOLDER LAYOUT

```
agents.md                                   ← this file (read at session start)
DIARY.md                                    ← project diary — write an entry after every change
plans/                                      ← saved implementation plans
scripts/
  00_config.sql                             ← SINGLE SOURCE OF TRUTH for all object names
  01_bootstrap.sql                          ← deploy DB + config stage (run ONCE per account)
  02_setup.sql                              ← create all Snowflake objects (idempotent — safe to re-run)
  03_automate.sql                           ← create the transcription task + gating procedure
  04_deploy_notebook.sh                     ← upload notebook + update live version
  05_gong_objects.sql                       ← create GONG_CALLS_MIRROR table
  06_sync_gong.sh                           ← sync Gong calls: Snowhouse → DEMO
  07_reset.sql                              ← reset the stream (stopgap; obsolete once stream is dropped)
  08_telemetry_debug.sql                    ← container telemetry diagnostics (hangs, GPU metrics, errors)
  999_teardown.sql                           ← GUARDED teardown (4 levels; 5 guards — see file header)
  publish_config.sh                         ← utility: PUT 00_config.sql to the shared stage
  sync_gong_query.sql                       ← utility: SELECT run against Snowhouse by 06_sync_gong.sh
  install_ffmpeg.sh                         ← utility: install ffmpeg in container
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

### 1. Initial Setup

**Once per account** (creates the shared config store):

```sql
-- Snowsight: scripts/01_bootstrap.sql
```

**Then, and after any config change** — publish config to the stage. Scripts read the
STAGED copy, not the local file, so skipping this means they silently use old values:

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

> `02_setup.sql` is **idempotent** as of 2026-08-18 — safe to re-run on a live
> deployment. Stateful objects (database, schema, both stages, `TRANSCRIPTION_RESULTS`,
> compute pool, notebook) use `IF NOT EXISTS`; only stateless definitions (network
> rules, integrations, file format, `TRANSCRIPTION_SUMMARY` view) are `CREATE OR REPLACE`.
>
> Consequence: adding a column to the table DDL there does **not** alter an existing
> table. Evolve a live deployment with `ALTER TABLE` (see `migration/`).

### 2. Upload Media Files

```bash
# Drop .mp4/.mp3/etc. into AUDIO_VIDEO_STAGE_FILES/, then:
python av.uploader/upload_av_files.py

# Custom source directory:
python av.uploader/upload_av_files.py -d /path/to/files
```

The uploader triggers the pipeline itself (`EXECUTE TASK`, asynchronous) as soon as
at least one file uploads successfully. There is no polling delay. Requires
`OPERATE` on the task — granted by `av.uploader/create_av_service_user.sql`.

### 3. Deploy Notebook Changes

```bash
cd scripts/
./04_deploy_notebook.sh           # reads all names from the config store
./04_deploy_notebook.sh --safe    # suspend the task during deploy, resume after
```

All object names come from `00_config.sql` via the config store — there are no
V1/V2 overrides to pass. Override `SNOW_CONNECTION` (default `DEMO`) or
`OWNER_ROLE` (default `SYSADMIN`) only if you need to.

The script deploys **and verifies**: it downloads the resulting live version and
compares its cell sources to your local file, then checks the notebook's owner.
It exits non-zero on any mismatch. Trust nothing that skips those checks.

> **Never deploy with a bare `PUT` + `ALTER NOTEBOOK ... ADD LIVE VERSION FROM LAST`.**
> `FROM LAST` restores from the last **committed version**, *not* from staged files, so
> once any committed version exists that sequence silently deploys **nothing** while
> still printing `Live version successfully created.` This went unnoticed long enough
> that the live notebook drifted older than git `HEAD`. See DIARY.md 2026-08-18.

Four behaviours the script exists to handle — remember them if you ever deploy by hand:

| Behaviour | Consequence |
|---|---|
| `ADD LIVE VERSION FROM LAST` reads the last **committed version** | A fresh `PUT` is ignored; deploy is a silent no-op |
| `COMMIT` **consumes** the live version (`is_live=false` everywhere) | Task fails instantly with `Live version is not found.` — must add live again after committing |
| `CREATE OR REPLACE NOTEBOOK` drops `EXTERNAL_ACCESS_INTEGRATIONS` | PyPI installs fail at runtime; must re-apply |
| `CREATE OR REPLACE NOTEBOOK` makes the **executing role** the owner | SYSADMIN-owned gate proc fails with `Notebook '...' does not exist or not authorized` |

`USE ROLE` cannot be used to fix the last one — the PAT-authenticated `DEMO`
connection rejects it with `Current session is restricted. USE ROLE not allowed.`
The script transfers ownership after the DDL with
`GRANT OWNERSHIP ... COPY CURRENT GRANTS` instead.

Accepted trade-off: `CREATE OR REPLACE` resets Snowflake-side version history on
every deploy. Git is the real history; the script's `COMMIT` leaves a fresh
rollback point.

### 4. Sync Gong Calls (Snowhouse → DEMO)

```bash
cd scripts/
./06_sync_gong.sh                 # sync new/updated calls
./06_sync_gong.sh --dry-run       # preview MERGE SQL without writing
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

`scripts/999_teardown.sql` is a single guarded block. There are no loose `DROP`
statements — "Run All" cannot bypass the guards. Fill in the variables at the top:

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

Levels 3–4 require a **zero-copy clone outside the target database whose row count
matches** the source, plus explicit acknowledgement. Level 2+ refuses while an
`EXECUTE NOTEBOOK` is still RUNNING. Every abort names the variable to set.

```sql
-- Take the backup first (instant, no storage cost until divergence):
CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_20260818
  CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
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
- The pipeline is **event-driven with no polling**. A file landing on the stage by any route other than `upload_av_files.py` will never be transcribed until someone runs `EXECUTE TASK ...TRANSCRIBE_NEW_FILES_TASK_V2`.
- Before manually triggering transcription, run `CALL TRANSCRIBE_IF_NEW_FILES()` to see the verdict without launching a container. Do **not** rely on `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2')` — it reports TRUE permanently because nothing consumes the stream.
- `TRANSCRIBE_NEW_FILES_TASK_V2` and `TRANSCRIBE_IF_NEW_FILES()` must both be owned by **SYSADMIN**. Tasks run with the owner's privileges; a mismatch fails with `Unknown user-defined function ... TRANSCRIBE_IF_NEW_FILES`. After any `CREATE OR REPLACE` as ACCOUNTADMIN, transfer ownership with `COPY CURRENT GRANTS` to preserve the uploader's `OPERATE` grant.
- Never run `EXECUTE NOTEBOOK` manually at the same time as a triggered task run — both would launch the notebook against the same files concurrently.
- Do not truncate or replace `TRANSCRIPTION_RESULTS` without confirming `SKIP_ALREADY_TRANSCRIBED = True` is set in the notebook — it relies on this table for deduplication, and so does the task gate.
- Before any DROP statement, read `scripts/999_teardown.sql` and match the appropriate level (1–4).

### SQL — Style

- Fully qualify object names: `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.OBJECT`.
- **Never paste a config block into a script.** Load it: `EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;`. All `PROJECT_*` and `FQ_*` variables come from there, including the fully-qualified `FQ_` names — do not rebuild those locally either.
- After editing `00_config.sql`, bump `CONFIG_REVISION` and run `scripts/publish_config.sh`. Scripts read the staged copy; without publishing they silently use stale values.
- Snowflake Scripting blocks (`DECLARE ... END;`) must be wrapped in `EXECUTE IMMEDIATE $$ ... $$;` or `snow sql -f` splits them on semicolons and fails with `syntax error ... unexpected '<EOF>'`.
- Uppercase SQL keywords. Snake_case for column aliases.

### Notebook & Pipeline

- Config knobs live in Cell 4 of `audio_video_transcription.ipynb`: `WHISPER_MODEL`, `ENABLE_SPEAKER_DIARIZATION`, `SKIP_ALREADY_TRANSCRIBED`, `FORCE_RETRANSCRIBE`.
- `base` is the default Whisper model — `large` is ~10× slower on GPU_NV_S. Flag the tradeoff before upsizing.
- `EXECUTE NOTEBOOK` is **synchronous** — it blocks until the notebook finishes (or is killed by timeout). The warehouse is used only for initialization and SQL pushdown; Python/Whisper compute runs on the GPU pool. The task's `USER_TASK_TIMEOUT_MS` (4 hours) is the ceiling. Monitor via `ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY` for actual durations.
- SRT subtitles are pre-generated at transcription time and stored in `TRANSCRIPTION_RESULTS`. Do not attempt to generate them dynamically at query time.

### Python

- RSA key-pair auth is required for the service account — do not switch to password auth.
- `config.json` is gitignored. Structural changes go in `config.template.json`; keep both in sync.
- Local environment (`environment.yml`): Python 3.9 + `snowflake-snowpark-python`. Don't add heavy deps without a clear reason.
- **Python 3.9+ is required, not optional.** `upload_av_files.py` resolves the private key as `Path(__file__).parent.parent / 'rsa_key.p8'`, which only works because `__file__` is absolute in 3.9+. Under Python 3.8 `__file__` stays relative and the key is not found. The `pysnowpark` conda env is 3.8 — don't use it for this project.
- Normal invocation is from `av.uploader/` (`python upload_av_files.py`); `-d ../AUDIO_VIDEO_STAGE_FILES` resolves against the cwd while the key resolves against the script path.
- Keep `av.uploader/upload_av_files.py` runnable from the project root.
- The notebook has **no `import re` safety net** — cell 19's `parse_summary_sections()` needs it. It was missing until 2026-08-18, which silently wrote `SUMMARY_MARKDOWN = NULL` on every run. If you add a cell that uses a stdlib module, import it in that same cell.


### Cost Guardrails

- Do not increase `AUTO_SUSPEND_SECS` on `TRANSCRIPTION_GPU_POOL_V2` without a reason — GPU_NV_S is expensive at idle.
- `SNOWFLAKE.CORTEX.COMPLETE` is called once per file — long recordings (1+ hr) produce large prompts and high credit use.
- `REFRESH_STAGE_DIRECTORY_TASK_V2` is deprecated and must stay suspended — resuming it costs a warehouse tick every 5 minutes to maintain a directory table nothing reads.
- Watch for the runaway pattern: if `TRANSCRIBE_NEW_FILES_TASK_V2` succeeds every 5 minutes at ~60–80s, the gate is broken and GPU containers are launching with no work. Healthy days show a handful of container sessions, not ~290. Check with the credits query in `scripts/03_automate.sql`.
- Every notebook launch loads Whisper onto the GPU in Cell 15 unconditionally, even with nothing to transcribe — so an idle launch is not free.
- Set `FORCE_RETRANSCRIBE = True` only with awareness that every file will re-consume GPU time and Cortex credits.

---

## TOOL ROUTING

Pick the right tool before acting. The rules below are ordered by how often they matter on this project.

### Rule 1 — Never assert Snowflake behavior from memory

Snowflake's supported-statement lists, function signatures, and object limits change frequently, and getting them wrong wastes a deploy cycle. If a claim about what Snowflake *can or cannot do* is load-bearing, look it up.

Precedence for Snowflake platform truth:

| Order | Tool | Use for |
|---|---|---|
| 1 | `mcp_sfke-mcp-serv_Snowflake_Documentation_Agent` | **Source of truth for Snowflake documentation.** Keyword + vector search over the docs corpus. Best for "is X supported", "what are the limits on Y", error-code meanings. Pass `verbose: true` when you need the reasoning trail. |
| 2 | `snowflake_product_docs` | Returns **full page contents** for top hits. Use when you need a complete doc page rather than an answer, or to confirm exact syntax. |
| 3 | `web_search` | Last resort only — release notes, or behavior too new for the docs corpus. Cite sources. |

> **Worked example (2026-08-18):** the Fix #1 gate procedure was written using `LS @stage` + `RESULT_SCAN`. The logic validated perfectly in an anonymous block, then failed on `CALL` with `Unsupported statement type 'LIST_FILES'` — `LS` is not permitted inside a stored procedure. Anonymous blocks and stored procedures do **not** accept the same statement types. Checking the docs first would have caught it.

### Rule 2 — SFKE is docs; Glean is code

These are different sources of truth and are easy to confuse:

| Tool | Source of truth for |
|---|---|
| `mcp_sfke-mcp-serv_Snowflake_Documentation_Agent` | Snowflake **product documentation** — public, behavioral, syntax |
| `mcp_glean_code_search` | Snowflake **internal code** — repositories, implementations, actual behavior when docs are silent or ambiguous |

When docs and observed behavior disagree, the code search settles it.

### Rule 3 — Inspect the account before writing SQL against it

| Tool | Use for |
|---|---|
| `snowflake_sql_execute` | All diagnostics, DDL, and queries. Set `only_compile: true` to validate SQL you do **not** want to run. Bump `timeout_seconds` for long operations. |
| `snowflake_object_search` | Discover tables/views/schemas. Required before writing SQL against any table whose columns you have not already seen. |
| `snowflake_semantic_view_search` | Locate semantic views and their joins/metrics. **This project has one** — `MEETINGS_SEMANTIC_VIEW`. |

Never assume a column exists from the table name — `DESCRIBE` or search first.

### Rule 4 — Notebooks use the notebook tools, never the file tools

`notebooks/audio_video_transcription.ipynb` is the core of this pipeline. Editing it with `edit`, `multi_edit`, or `write` corrupts notebook JSON.

Use `notebook_read`, `notebook_add_cell`, `notebook_edit_cell`, `notebook_delete_cell`. Call `notebook_add_cell` sequentially, never in parallel. Before executing anything, call `notebook_get_kernel_status` first.

Note that the local kernel is **not** the GPU Container Runtime — local execution will not reproduce Whisper/GPU behavior. To test real pipeline behavior, deploy with `scripts/04_deploy_notebook.sh` and inspect telemetry via `scripts/08_telemetry_debug.sql`.

### Rule 5 — `meeting-intel` is THIS project's own MCP server, not an outside source

`mcp_meeting-intel_meeting_intelligence` is the `MEETING_INTELLIGENCE_MCP` server built by this project (see DIARY.md 2026-04-02). Its chain is:

```
mcp_meeting-intel_meeting_intelligence
  → MEETING_INTELLIGENCE (Cortex Agent)
      → search_meetings   → MEETING_SEARCH (Cortex Search on UNIFIED_MEETINGS_V)
      → analyze_meetings  → Cortex Analyst on MEETINGS_SEMANTIC_VIEW
  → UNIFIED_MEETINGS_V = TRANSCRIPTION_RESULTS ∪ GONG_CALLS_MIRROR
```

**It reads the same tables this pipeline writes.** Treat it as a fast natural-language query path over our own transcripts — never as corroboration. If it agrees with `TRANSCRIPTION_RESULTS`, that is not validation; it is the same data. To verify a transcript independently you need an outside source, not this server.

Useful when you want a semantic/keyword search over transcript content without hand-writing SQL. Note `MEETING_SEARCH` has `TARGET_LAG = 1 hour`, so freshly inserted transcripts will not be searchable immediately.

### Rule 6 — Situational and out-of-scope tools

| Tool | Status on this project |
|---|---|
| `mcp_glean_chat` / `mcp_glean_search` / `mcp_glean_read_document` | Situational — internal decisions, design docs, prior art not in this repo |
| `mcp_glean_employee_search` | Situational — finding owners of an internal system |
| `mcp_google-drive_google_drive` | Situational — specs and docs referenced by the user |
| `mcp_snowhouse-rav_raven_sales_assistant` | Situational — `06_sync_gong.sh` pulls from Snowhouse, so this can help with Gong/Snowhouse-side questions. Not needed for the transcription pipeline itself. |
| `mcp_snowflake-inv_*` (portfolio-analyst, investment-knowledge-search) | **Not relevant.** Investment research. Do not use. |
| `call_cortex_analyst`, `evaluate_semantic_view`, `reflect_semantic_model` | **Relevant** — this project owns `MEETINGS_SEMANTIC_VIEW`, which backs the `analyze_meetings` tool. Use `reflect_semantic_model` before any Cortex Analyst call, and `evaluate_semantic_view` when text-to-SQL returns wrong answers. |
| `browser_*` / `browser` subagent | Only for Snowsight UI verification the SQL API can't cover. Prefer SQL. |

### Rule 7 — Delegate broad exploration, keep narrow lookups in-context

Use the `task` tool with `subagent_type: "explore"` for open-ended searches spanning many files. Do **not** spawn a subagent to find one known function or read one known file.

Use `subagent_type: "sql-verify"` after writing non-trivial analytical SQL — it catches join fanout, `NULL` comparison traps, and `UNION` mistakes that produce silently wrong results. Monitoring queries in this project join `TRANSCRIPTION_RESULTS` against stage listings and `ACCOUNT_USAGE` views, which is exactly where fanout hides.

### Rule 8 — Telemetry is already configured; do not "set it up"

The account emits notebook container telemetry to the Snowflake-provided default event table `SNOWFLAKE.TELEMETRY.EVENTS`. `SHOW PARAMETERS LIKE 'EVENT_TABLE' IN ACCOUNT` returns it with a **blank `level`**, meaning the default is in use and was never explicitly configured.

**Never run `ALTER ACCOUNT SET EVENT_TABLE`.** It is account-wide and would redirect telemetry for every other object in the account.

Use `scripts/08_telemetry_debug.sql`. Expect 3–5 minutes of ingestion latency. Event table `TIMESTAMP` is **UTC**, while `TASK_HISTORY` is session-local — convert before correlating.

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
