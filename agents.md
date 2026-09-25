# Audio/Video Transcription Pipeline — Persistent Agent Context

> **Read this file at the start of every session on this project before doing anything else.**
> Keep it under 300 lines. Detail belongs in the reference docs listed at the bottom — link,
> don't inline.

---

## PROJECT

End-to-end automated pipeline for transcribing audio/video files inside Snowflake, using
**GPU Container Runtime** + **OpenAI Whisper** for transcription, **Snowflake Cortex**
(`SNOWFLAKE.CORTEX.COMPLETE`, model `claude-sonnet-4-6`) for AI meeting summaries, and a
**Streamlit dashboard** for search and export. A secondary **Gong sync** mirrors call
recordings from a Snowhouse account into the same results database for centralized search.

**Architecture, object inventory, data model, and known constraints live in
[documents/architecture/architecture.md](documents/architecture/architecture.md).** Read it
before any structural change. Do not restate it here — that duplication is what goes stale.

**Config:** all deployment-specific object names live in **`scripts/00_config.sql` only**.
Every other script loads them with a single `EXECUTE IMMEDIATE FROM` line — never paste
config blocks into scripts. Edit the file, bump `CONFIG_REVISION`, then run
`scripts/publish_config.sh`. The active deployment uses `_V2` suffixes.

> **⚠️ MANUAL UPLOADS DO NOT AUTO-TRANSCRIBE.** Nothing polls the stage. If a file reaches
> `@AUDIO_VIDEO_STAGE` by any route other than `upload_av_files.py`, trigger it yourself:
> ```sql
> EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
> ```
> To see what the gate WOULD do without acting on it, run the read-only backlog query in
> the verification section of `scripts/03_automate.sql`. **Do NOT use
> `CALL ...TRANSCRIBE_IF_NEW_FILES()` for that — it LAUNCHES a GPU run whenever
> untranscribed media exists.** It returns `SKIPPED` only when the backlog is empty.

> **STATUS (2026-08-19):** Event-driven trigger deployed and validated. The ~2h hang on
> **multi-file** runs is root-caused to Snowflake's `snowbook` runtime
> (`on_scriptrunner_ready` never returns) and **cannot be fixed from inside the notebook** —
> 0/8 single-file runs hung, 4/6 multi-file did. Mitigated by a 30-min task timeout. Fix is
> porting off `EXECUTE NOTEBOOK`: `.snowflake/cortex/plans/port-transcription-to-job-service.plan.md`.
> Evidence: architecture.md §5, DIARY.md 2026-08-19.

---

## SNOWFLAKE ACCOUNT

- **Connection name:** `DEMO` — used by all scripts and the uploader
- **Active database / schema:** `TRANSCRIPTION_DB_V2` / `TRANSCRIPTION_SCHEMA_V2`
- **Warehouse:** `TRANSCRIPTION_WH_V2` (XS) — set before querying
- **Compute pool:** `TRANSCRIPTION_GPU_POOL_V2` (GPU_NV_S, auto-suspends after 1 hr)

Always fully-qualify object names: `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.OBJECT`.
Never rely on session context alone.

> For parallel deployments (dev/staging), change the `SET` variables in `00_config.sql`, bump
> `CONFIG_REVISION`, and run `publish_config.sh`. Never re-introduce a pasted config block —
> that is what caused three scripts to silently target V1 while the live deployment was V2.

---

## FOLDER LAYOUT

```
agents.md                          ← this file (read at session start)
DIARY.md                           ← project diary (gitignored) — entry after every change
documents/                         ← VERSIONED docs (see AGENT DOCS)
  architecture/                    ← ENFORCED: architecture.md + .drawio pair
  operations/runbook.md            ← full setup / deploy / teardown detail
scripts/                           ← numbered lifecycle steps; unnumbered = utilities
  00_config.sql                    ← SINGLE SOURCE OF TRUTH for all object names
  01_bootstrap.sql   02_setup.sql   03_automate.sql   04_deploy_notebook.sh
  05_gong_objects.sql  06_sync_gong.sh  07_reset.sql  08_telemetry_debug.sql
  999_teardown.sql                 ← GUARDED teardown (4 levels, 5 guards)
  publish_config.sh  sync_gong_query.sql  install_ffmpeg.sh
notebooks/audio_video_transcription.ipynb   ← Whisper + Cortex GPU notebook
av.uploader/                       ← upload_av_files.py, download_srts.py, service-user SQL
AUDIO_VIDEO_STAGE_FILES/           ← local drop folder for media
transcription_dashboard.py         ← local Streamlit entry point
output/bundle/streamlit/           ← Streamlit bundle for SPCS deployment
migration/                         ← schema migration scripts (v1 → v2)
.cortex/skills/                    ← local skills: av-transcription-dev, drawio-diagrams
```

> `.cortex/` is gitignored — both skills are local-only and NOT versioned. `documents/` IS
> versioned; the architecture doc, diagrams, and runbook get committed.

---

## HOW TO RUN

Essential commands only. **Full detail, gotchas, and guard semantics:
[documents/operations/runbook.md](documents/operations/runbook.md).**

```bash
# Setup (once per account, then after any config change)
cd scripts/ && ./publish_config.sh     # publish config BEFORE running any script
# Snowsight, in order: 01_bootstrap.sql → 02_setup.sql → 03_automate.sql

# Upload media (auto-fires EXECUTE TASK; needs Python 3.9+)
cd av.uploader/ && python upload_av_files.py

# Deploy notebook changes (deploys AND verifies live version + owner)
cd scripts/ && ./04_deploy_notebook.sh

# Gong sync, dashboard, SRT export
cd scripts/ && ./06_sync_gong.sh
streamlit run transcription_dashboard.py
cd av.uploader/ && python download_srts.py
```

Every SQL script prints a `CONFIG_REVISION` row first. If it is not the revision you just
edited, the staged copy is stale — re-run `publish_config.sh`.

Teardown is `scripts/999_teardown.sql` — a single guarded block, 4 levels, 5 guards. Levels
3–4 destroy transcripts and require a verified zero-copy clone. Read the runbook first.

---

## KEY RULES

### Behavior

- Be direct. No sycophancy — no "great question!", no "you're absolutely right", no flattery.
- Don't over-engineer. Only make changes that are directly requested or clearly necessary.
- Don't create documentation files unless explicitly asked.
- Don't auto-commit. Only commit when explicitly asked.
- When unsure, investigate first — don't guess and don't assume the user is correct.
- **Write a `DIARY.md` entry after every project change** — date, what changed, and why.

### Architecture Documentation (ENFORCED)

`documents/architecture/architecture.md` is the authoritative architecture description. It
is a maintained artifact, not background reading, and it goes stale silently if unenforced.

**When a change alters the architecture, update `architecture.md` in the SAME change.**
Architecture-altering means: adding/renaming/deprecating/removing any pipeline object
(stage, table, view, task, procedure, notebook, Cortex Search service, semantic view, agent,
MCP server, warehouse, compute pool); changing how the pipeline is triggered or gated;
changing the `TRANSCRIPTION_RESULTS` schema or the dedup contract; changing where the payload
executes; or changing the consumption layer.

**Then regenerate the diagrams** via the `drawio-diagrams` skill. Always edit
`architecture.uncompressed.drawio` and recompress to `architecture.drawio` — the compressed
file goes stale the moment the source is edited. Never hand-edit the compressed file, and
never skip round-trip validation; a silently blank LucidChart import is what it guards
against. Diagrams that disagree with `architecture.md` are bugs, not cosmetic drift.

**`documents/` is the only place architecture and procedure facts live.** The
`.cortex/skills/av-transcription-dev/references/` files are deliberately **pointers**, not
copies — they were rewritten on 2026-08-19 after drifting into documenting the deprecated
stream-polling design, V1 object names, and a deploy sequence that silently deploys nothing.
Do not re-add facts to them. If a skill reference needs to state something, put it in
`documents/` and point at it. Two copies of the same fact will drift; that is how this project
accumulated three separate stale-doc bugs in two days.

### SQL — Idempotency

- `CREATE OR REPLACE` for views, stages, stored procedures, notebooks.
- `CREATE TABLE IF NOT EXISTS` for `TRANSCRIPTION_RESULTS` and any accumulating table.
- `CREATE WAREHOUSE IF NOT EXISTS`, `CREATE COMPUTE POOL IF NOT EXISTS`.
- Never bare `CREATE` — it fails on second run.

### Deployed state must match code (ENFORCED)

**The code is not evidence of how the account is configured.** `IF NOT EXISTS` is a no-op against
an existing object, so a value written only into a `CREATE` is never enforced — the script asserts
it, the account ignores it, and nothing compares them. This is not hypothetical: on 2026-09-24 the
GPU pool was running at `AUTO_SUSPEND_SECS = 3600` (the platform default, **never set in code at
all** — `git log -S AUTO_SUSPEND_SECS` confirms) while this file said to keep it low, and the
warehouse was at `AUTO_SUSPEND = 600` while `02_setup.sql` claimed 60. The pool drift alone cost
~12.6 credits over 35 days, more than the entire job-service port saves.

Rules that follow:

- **Never state a deployed value from reading a script.** Read it from the account — `SHOW COMPUTE
  POOLS`, `SHOW WAREHOUSES`, `DESCRIBE NOTEBOOK`, `SHOW TASKS` — or say you have not checked.
  Saying "the pool is set to X" because a `CREATE` says X is the error that hid this for months.
- **Run `scripts/09_drift_check.sql` before trusting any claim about deployment state**, and after
  any hand-run `ALTER`. It compares `V_PROJECT_CONFIG` against live `SHOW` output and prints
  `OK` / `*** DRIFT ***` per setting. Read-only.
- **Every operational setting belongs in `00_config.sql`, not inline in a script.** Sizing, idle
  behaviour and timeouts are config values like object names are. Add them to the
  `V_PROJECT_CONFIG` emitter too, or the drift check cannot see them.
- **A `CREATE ... IF NOT EXISTS` for a settable object needs a reconciling `ALTER` after it**, so
  re-running the script converges a live deployment instead of only building a correct new one.
  `02_setup.sql` now does this for the warehouse and the pool.
- **Two deliberate exceptions, both because reconciling is destructive:** table schemas (use
  `migration/`, reviewed by a human) and `INSTANCE_FAMILY` (replaces nodes — change it by hand).
- **When code and account disagree, decide which is right — do not assume code wins.** The
  warehouse's deployed 600s was *better* than the coded 60s, because Snowflake bills a 60-second
  minimum on every resume and this warehouse's main consumer is interactive Streamlit sessions. The
  code was corrected to match the account. Reconciling blindly toward the script would have
  increased cost while looking like a fix.
- Same principle already applies elsewhere, for the same reason: `config.json` /
  `config.template.json`, and `00_config.sql` / its published copy on the stage (compared by
  content hash in `scripts/check_config_fresh.sh`). Deploying is not the same as verifying —
  `scripts/04_deploy_notebook.sh` verifies staged bytes against local after upload because a
  silent no-op deploy had happened before.

### SQL — Safety

- **NEVER** suspend or drop `TRANSCRIPTION_GPU_POOL_V2` without confirming no transcription is running.
- The pipeline is **event-driven with no polling**. A file landing on the stage by any route other than `upload_av_files.py` is never transcribed until someone runs `EXECUTE TASK`.
- **`CALL TRANSCRIBE_IF_NEW_FILES()` IS NOT A DRY RUN — it launches.** It starts a GPU run whenever untranscribed media exists, returning `SKIPPED` only on an empty backlog. This file previously claimed the opposite, which would cost credits to discover. To inspect without acting, use the read-only backlog query in `scripts/03_automate.sql`. Do **not** rely on `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM_V2')` either — it reports TRUE permanently because nothing consumes the stream.
- The gate returns one of three verdicts: `BLOCKED` (a run is already in flight), `SKIPPED` (no new media), or `LAUNCHED`. `BLOCKED` exists because the `JOB_SERVICE` path **drops the job service before creating it**, so launching over a live run would kill a transcription in progress and lose the whole batch — records persist once, after every file completes.
- `TRANSCRIBE_NEW_FILES_TASK_V2` and `TRANSCRIBE_IF_NEW_FILES()` must both be owned by **SYSADMIN**. Tasks run with the owner's privileges; a mismatch fails with `Unknown user-defined function ... TRANSCRIBE_IF_NEW_FILES`. After any `CREATE OR REPLACE` as ACCOUNTADMIN, transfer ownership with `COPY CURRENT GRANTS` to preserve the uploader's `OPERATE` grant.
- Never run `EXECUTE NOTEBOOK` manually while a triggered task run is active — both launch against the same files concurrently.
- Do not truncate or replace `TRANSCRIPTION_RESULTS` without confirming `SKIP_ALREADY_TRANSCRIBED = True` in the notebook — it and the task gate both dedup on that table.
- Before any DROP, read `scripts/999_teardown.sql` and match the appropriate level (1–4).
- Back up rows to a scratch table before deleting any transcript, even for a test. The hang occurs *after* the INSERT, so transcripts are never at risk from it — but deletes are.

### SQL — Style

- Fully qualify object names.
- **Never paste a config block into a script.** Load it: `EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;`. All `PROJECT_*` and `FQ_*` variables come from there — do not rebuild `FQ_` names locally.
- After editing `00_config.sql`, bump `CONFIG_REVISION` and run `publish_config.sh`. Scripts read the staged copy; without publishing they silently use stale values.
- Snowflake Scripting blocks (`DECLARE ... END;`) must be wrapped in `EXECUTE IMMEDIATE $$ ... $$;` or `snow sql -f` splits them on semicolons and fails with `syntax error ... unexpected '<EOF>'`.
- Uppercase SQL keywords. Snake_case for column aliases.

### Notebook & Pipeline

- Config knobs are in Cell 4: `WHISPER_MODEL`, `ENABLE_SPEAKER_DIARIZATION`, `SKIP_ALREADY_TRANSCRIBED`, `FORCE_RETRANSCRIBE`, `FORCE_KERNEL_EXIT`, `HANG_FORENSICS`.
- `base` is the default Whisper model — `large` is ~10× slower on GPU_NV_S. Flag the tradeoff before upsizing.
- `EXECUTE NOTEBOOK` is **synchronous** — it blocks until the notebook finishes or the task times out. The warehouse only handles initialization and SQL pushdown; Whisper runs on the GPU pool. `USER_TASK_TIMEOUT_MS` is **1800000 (30 min), task-scoped** — deliberately low to cap the hang. Do not raise it without reading architecture.md §5.
- `FORCE_KERNEL_EXIT` must stay `False`. It bounds the hang but makes the task report FAILED.
- `HANG_FORENSICS` stays `True` — inert on healthy runs, dumps all-thread stacks if the kernel fails to exit. Retrieve with Q9 in `scripts/08_telemetry_debug.sql`.
- SRT subtitles are pre-generated at transcription time and stored in the table. Do not generate them at query time.

### Python

- RSA key-pair auth is required for the service account — do not switch to password auth.
- `config.json` is gitignored. Structural changes go in `config.template.json`; keep both in sync.
- **Python 3.9+ is required, not optional.** `upload_av_files.py` resolves the key as `Path(__file__).parent.parent / 'rsa_key.p8'`, which only works because `__file__` is absolute in 3.9+. Under 3.8 it stays relative and the key is not found. The `pysnowpark` conda env is 3.8 — don't use it here.
- Normal invocation is from `av.uploader/`; `-d ../AUDIO_VIDEO_STAGE_FILES` resolves against the cwd while the key resolves against the script path.
- The notebook has **no `import re` safety net** — cell 19's `parse_summary_sections()` needs it. Its absence silently wrote `SUMMARY_MARKDOWN = NULL` on every run until 2026-08-19. If a cell uses a stdlib module, import it in that same cell.

### Cost Guardrails

- Do not increase `AUTO_SUSPEND_SECS` on the GPU pool without a reason — GPU_NV_S is expensive at idle. It is a config value (`PROJECT_POOL_AUTO_SUSPEND_SECS`, currently **300**), enforced by a reconciling `ALTER` in `02_setup.sql` and verified by `scripts/09_drift_check.sql`. It sat at the 3600s default until 2026-09-24 because it was never in the DDL — ~82% of each run's GPU cost was the pool idling after the work finished.
- **`AUTO_SUSPEND` does not bound a hung run.** It counts *idle* time only, and a hung notebook container keeps the pool non-idle, so a leaked GPU node is unbounded regardless of this setting. After any hang, follow the reclaim procedure in `documents/operations/runbook.md` — `STOP ALL` then `SUSPEND`.
- Lower is not always cheaper. Snowflake bills a **60-second minimum on every warehouse resume**, so an aggressive `AUTO_SUSPEND` on an interactively-used warehouse costs more than staying warm. `TRANSCRIPTION_WH_V2` is at 600s for this reason.
- The warehouse has been the larger cost, not the GPU: 35.44 vs 16.87 credits over the 35 days to 2026-09-24, and ~72% of warehouse time is **Streamlit sessions** (1,357 exec-minutes from one user). Closing the dashboard is a bigger saving than most code changes.
- `CORTEX.COMPLETE` is called once per file; long recordings produce large prompts and high credit use.
- `REFRESH_STAGE_DIRECTORY_TASK_V2` is deprecated and must stay suspended — resuming it costs a warehouse tick every 5 minutes to maintain a directory table nothing reads.
- Watch for the runaway pattern: if the task succeeds every 5 minutes at ~60–80s, the gate is broken and GPU containers are launching with no work. Healthy days show a handful of container sessions, not ~290.
- Every notebook launch loads Whisper onto the GPU in Cell 15 unconditionally, so an idle launch is not free.
- Set `FORCE_RETRANSCRIBE = True` only knowing every file re-consumes GPU time and Cortex credits.

---

## TOOL ROUTING

Full detail, situational tools, and worked examples:
[documents/operations/tool-routing.md](documents/operations/tool-routing.md). The rules that
matter every session:

- **Never assert Snowflake behavior from memory.** If a claim about what Snowflake can or
  cannot do is load-bearing, look it up: `mcp_sfke-mcp-serv_Snowflake_Documentation_Agent`
  first (docs source of truth), then `snowflake_product_docs` for full pages, `web_search`
  last. This project has been bitten repeatedly by skipping it.
- **SFKE is docs; `mcp_glean_code_search` is Snowflake internal code.** When docs and observed
  behavior disagree, the code settles it.
- **Inspect before writing SQL.** `snowflake_object_search` or `DESCRIBE` before querying any
  table whose columns you have not already seen. Never infer a column from a table name.
  `snowflake_sql_execute` with `only_compile: true` validates SQL you do not want to run.
- **Notebooks use the notebook tools, never `edit`/`write`** — those corrupt notebook JSON.
  Also: `notebook_edit_cell` replaces a **substring**, not the cell, so replacing a header
  leaves the old body appended. Verify after editing.
- **The local kernel is not the GPU Container Runtime.** Local runs will not reproduce
  Whisper/GPU behavior; deploy and read telemetry instead.
- **`mcp_meeting-intel_*` is THIS project's own MCP server.** It reads the tables this
  pipeline writes, so agreement with `TRANSCRIPTION_RESULTS` is not validation. Also
  `TARGET_LAG = 1 hour`, so fresh transcripts are not searchable immediately.
- **Delegate broad exploration** to `task` / `subagent_type: "explore"`; do not spawn one to
  read a single known file. Run `sql-verify` after non-trivial analytical SQL.
- **Telemetry is already configured.** Never run `ALTER ACCOUNT SET EVENT_TABLE` — it is
  account-wide. Use `scripts/08_telemetry_debug.sql`; event table `TIMESTAMP` is UTC while
  `TASK_HISTORY` is session-local.
- **`mcp_snowflake-inv_*` is not relevant** to this project. Do not use.


---

## AVAILABLE SKILLS

Invoke with the `skill` tool before starting relevant work. Do not reinvent what a skill provides.

| Skill | When to use |
|---|---|
| `av-transcription-dev` | **Local. Primary skill.** Any pipeline development, debugging, monitoring, deployment, or optimization. |
| `drawio-diagrams` | **Local. Required whenever `architecture.md` changes.** Compression chain, brand palette, legend rules. Also for ad-hoc data flow / lineage diagrams. |
| `snowflake-notebooks` | Notebook cell structure, EXECUTE NOTEBOOK behavior, runtime config, versioning. |
| `cortex-ai-functions` | Modifying or debugging `CORTEX.COMPLETE()` — prompts, model selection, response parsing. |
| `deploy-to-spcs` | Deploying the dashboard as an SPCS service. |
| `sql-author` | Writing or debugging Snowflake SQL, including the Gong MERGE. |
| `warehouse` | Right-sizing `TRANSCRIPTION_WH_V2`, suspend/resume behavior. |
| `cost-intelligence` | GPU pool and Cortex credit consumption; spike analysis. |
| `dynamic-tables` | Only if refactoring the trigger pattern to dynamic tables. |

---

## AGENT DOCS (read on demand)

Read only when the task requires it.

| Doc | Contents |
|---|---|
| [documents/architecture/architecture.md](documents/architecture/architecture.md) | **Authoritative architecture.** Flow, full object inventory, data model, dedup contract, performance envelope, the hang constraint. Read before structural changes; update in the same change. |
| [documents/operations/runbook.md](documents/operations/runbook.md) | Full setup order, upload, notebook deploy semantics (the four traps), Gong sync, monitoring queries, teardown levels and all five guards. |
| [documents/operations/tool-routing.md](documents/operations/tool-routing.md) | Full tool-routing rules with reasoning, situational and out-of-scope tools, and worked examples. |
| `DIARY.md` | Project diary (gitignored), newest entry first. Write an entry after every change. |
| `.snowflake/cortex/plans/` | Saved implementation plans, including the pending job-service port. |
| `.cortex/skills/av-transcription-dev/references/` | Older skill references (architecture, development-workflow, monitoring, known-issues). **Partly stale** — contains `mistral-large2` and V1 names. Where they disagree with `documents/`, `documents/` wins. |
