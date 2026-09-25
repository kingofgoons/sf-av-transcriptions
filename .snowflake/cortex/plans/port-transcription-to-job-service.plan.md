---
name: "port-transcription-to-job-service"
created: "2026-08-19T16:23:34.191Z"
status: pending
---

# Port transcription from EXECUTE NOTEBOOK to a headless GPU job service

## Context

### Why

The \~2h07m hang is **not in our code, and this is now proven** rather than inferred.

On 2026-08-19 a `faulthandler` watchdog was armed in the notebook teardown (`dump_traceback_later(120, repeat=True, exit=False)`, writing to a real fd via `sys.__stderr__`) and a hang was reproduced deliberately with the same 3-file workload that hung on 2026-08-18. The notebook finished all work and wrote its 3 rows normally, then dumps fired at +2min and +4min with an **identical** frame:

```
Thread 0x00007fcf7d7fa6c0:
  threading.py:355 in wait_for
  snowbook/runtime/notebook_script_requests.py:232 in on_scriptrunner_ready
  snowbook/runtime/notebook_script_runner.py:286 in _run_script_thread
MAIN THREAD:
  asyncio/base_events.py:603 in run_forever
  snowbook/snowflake/snowflake_run_adaptor.py:264 in run_till_end
  snowbook/snowflake/streamlit_base_adaptor.py:96 in start
  snowbook/web/cli.py:211 in main
```

`snowbook`'s script runner parks forever in `on_scriptrunner_ready` waiting on a condition variable while the main thread sits in `asyncio run_forever` serving gRPC. **No notebook code is on any stack** — every cell has completed. This is inside Snowflake's runtime and cannot be fixed from the notebook. `EXECUTE NOTEBOOK` is synchronous, so the calling task blocks with it.

### Re-confirmed against a live hang, 2026-08-19 15:52 — and what it means for this port

A second hang was caught **while wedged** and sampled for 20 minutes: **11 dump cycles at exactly
120s intervals, identically sized stacks throughout** (10 threads, 63 frames), and **zero notebook
frames** in any post-completion dump. Across 35 minutes of telemetry exactly one frame belonged to
project code — cell 35 calling `dump_traceback()` itself.

That capture sharpens three things for this plan:

**1. The four parked threads are ALL notebook-runtime machinery, which is why this port should
work.** Previously this was reasoned; now it is enumerated. The threads that never wind down:

| Frame | Thread's job | Exists in a headless script? |
|---|---|---|
| `notebook_script_requests.py:232 on_scriptrunner_ready` | waiting for the *next* cell-execution request | No |
| `snowflake_run_adaptor.py:264 run_till_end` → `asyncio run_forever` | serving gRPC for the notebook UI | No |
| `web/stage_copier.py:803 stage_file_watcher` | syncing notebook files to/from stage | No |
| `status_updater/status_thread.py:145 status_fun` | reporting notebook status | No |

Every one is tied to hosting an interactive notebook. `python transcribe_job.py` starts none of
them. This is the strongest evidence yet that the port removes the failure mode rather than
relocating it.

**2. HARD REQUIREMENT: never invoke `snowbook.web.cli`.** The live stack begins
`runpy.py:196 _run_module_as_main` → `web/cli.py:379 <module>` → `web/cli.py:211 main`, meaning
snowbook is started as `python -m snowbook.web.cli`. **This port reuses the same snowbooks image**,
so that module is present in the container and is one command line away from reintroducing the
exact hang. The service spec `command` must be `python transcribe_job.py` and must never be
`python -m snowbook...`. Reusing the image is safe; invoking its entrypoint is not. Assert this
in review of the service spec.

**3. The script runner does NOT exit after the last cell** — it stays alive and *moves* from
executing the notebook into `on_scriptrunner_ready`. That frame is absent from the baseline dump
and present in every dump afterwards, which marks the moment the hang begins. Do not model this as
"cleanup failed to run"; nothing is trying to clean up.

**Scope note: this is a cost and observability fix, not a correctness fix.** Both hangs on
2026-08-19 committed all 3 rows, with summaries and SRTs intact, before wedging. Across every hang
observed, no transcript has ever been lost. The port's value is reclaiming GPU time leaked by hung
runs — **which is indefinite, not the ~30 min the task timeout suggests** (see "the timeout does not
stop the container" below) — and getting a truthful task status. Not repairing corrupted data.
Weigh it accordingly, and do not let the port introduce a data risk that the hang never posed.

**The task timeout does NOT stop the container — corrected 2026-08-19.** This matters for sizing the
benefit of this port. `USER_TASK_TIMEOUT_MS` kills the task, not the notebook container. The 15:52
hung run's task died at 16:22:06 while its container `STPLATNOTEBOOK23090324279953822` was still
`RUNNING` at 16:57 — 35 minutes past task death, 65 minutes total — and had to be stopped manually
with `ALTER COMPUTE POOL ... STOP ALL`. It would never have self-resolved: the service has
`auto_suspend_secs = 0`, and the pool's `AUTO_SUSPEND = 3600` only counts *idle* time, which a
RUNNING service prevents. So **each hung multi-file run leaks a `GPU_NV_S` node indefinitely.** At 6
of 8 multi-file runs hanging, that is the real cost case for this port, and a plausible contributor
to the historical ~230-credit runaway.

**Add to task 6 validation:** after a ported run, confirm `SHOW COMPUTE POOLS` reports
`num_services = 0` and `num_jobs = 0`, and that the pool reaches `SUSPENDED` on its own. A job
service that exits should release the node without intervention — verify it, since this is the
failure the notebook path hides.

**The hang is multi-file-only:** 0 of 8 single-file runs hung; **6 of 8** multi-file runs did
(as measured 2026-08-19). Any reproduction must use 3+ files.

**Rate re-checked 2026-09-24: still live, but do not read the raw ratio as improvement.** In the
7-day `TASK_HISTORY` window, **1 of 6** runs hung — and it was the **only** run with 4 files. Every
1-2 file run passed. The file-count correlation holds; recent batches have simply been smaller. The
6-of-8 figure for 3+ file runs is **not contradicted, only un-remeasured**: seven 3-file runs exist
since 08-28, but only one (09-17, which passed) falls inside the retention window. Do not size this
port on "1 in 6".

**The leak hypothesis is now REFUTED by direct measurement, which strengthens the case for this
port.** A resource ledger deployed 2026-08-19 instrumented the 09-21 hung run at every per-file
boundary: `fd=76 threads=12 nondaemon=3 os_children=1 tmp_wav=0` held **constant across all four
files**, with `created=4 removed=4 unaccounted=0 on_disk=0 OK` — and then the container hung for 23
minutes. Nothing accumulated. The hang is a **race inside snowbook**, established by measurement
rather than inferred from a teardown census, so no notebook-side fix exists. Two incidental
corrections from that data: `os_children` is persistently **1** (an OS child that
`multiprocessing.active_children()` reports as 0 and cannot see — the blind spot behind the
original "zero children" claim), and `nondaemon` is **3**, not 2.

**It is intermittent — one clean run proves nothing.** Three 3-file runs on 2026-08-19 went
hang (10:07) / clean (15:21) / hang (15:52) within six hours. That clean run was nearly read as
the problem receding.

Three earlier hypotheses were **disproven** by the hung-run stacks and must not be re-investigated: lingering multiprocessing children (zero on every dump), GPU/CUDA cleanup (the hung run had cleanup and hung anyway), and `join_if_started` (present in healthy baselines only). The `resource_tracker: leaked semaphore` warning appears on healthy runs too and is noise.

The port remains the right move, and the evidence strengthens it: a plain Python script has no `snowbook` script runner, no Streamlit host, and no IPython kernel, so it removes the entire failure class rather than betting on a specific thread.

Switching notebook *varieties* (Warehouse vs Container Runtime, CPU vs GPU) would not help: all Snowflake notebooks are Streamlit-hosted, and Warehouse runtime cannot provide a GPU for Whisper.

**Interim mitigation already in place:** `USER_TASK_TIMEOUT_MS = 1800000` (30 min, task-scoped) caps the task — **but not the container, which leaks a GPU node indefinitely; see above.** Rows still land before the hang, so the data is correct while the task reports FAILED. Diagnostic instrumentation (`HANG_FORENSICS = True`) is currently armed in the deployed notebook — **leave it armed until the port is validated**; it produced the live 11-cycle stack capture on 2026-08-19 that proved the root cause, and it costs nothing on healthy runs (one baseline dump).

**The terminal error is not a stable signature.** Do not key validation or alerting on one error code. The 10:07 hang died at 1045s with error **604** "SQL execution canceled"; the 15:52 hang ran the full timeout and died at 1802s with error **000630** "Statement reached its statement or warehouse timeout of 1,800 second(s)". Neither message mentions notebooks or hanging. The durable signal is *transcripts present + task FAILED*, and the reliable tell is the gap between the last transcript write and the task end (7.5 min and ~10 min respectively) — not duration.

### Key findings from research

**No Docker image build is required.** The GPU Container Runtime image is available in the account:

```
snowflake/images/snowflake_images/st_plat/runtime/x86/generic_gpu/runtime_image/snowbooks:2.5.1-py310
```

This is the same `snowbooks` image family the notebook already runs on. Newer tags (2.7.0, 2.8.0) are py312-only; the notebook logs show `cpython-3.10`, so a `py310` tag matches the validated environment. `2.5.1-py310` is the newest py310.

**ffmpeg is already preinstalled** — telemetry confirms `ffmpeg version 6.1.1-3ubuntu5` plus `ffprobe` on every run. The `apt-get` fallback in notebook cell 8 has never fired, and scripts/install\_ffmpeg.sh is vestigial. This matters because job service containers are **non-privileged**, so `apt-get` would not have worked. Since we reuse the same image, ffmpeg comes for free and no static-binary or pip-wheel workaround is needed.

**Authentication inside the container differs from the notebook.** `get_active_session()` will not be available. Job service containers get an OAuth token file:

```python
def get_login_token():
    with open('/snowflake/session/token', 'r') as f:
        return f.read()

conn = snowflake.connector.connect(
    host=os.getenv('SNOWFLAKE_HOST'),
    account=os.getenv('SNOWFLAKE_ACCOUNT'),
    token=get_login_token(),
    authenticator='oauth',
)
```

The session runs as the **service owner role**. The token file is refreshed every few minutes; once connected, the connection is not bound to the token's 1-hour validity.

**`EXECUTE JOB SERVICE` is synchronous.** That is fine — the whole point is that a plain script exits when `main()` returns, so synchronous blocking is bounded by real work.

**Open risk on the launch site.** Owner's-rights stored procedures are documented to allow only SELECT, DML, DDL, GRANT/REVOKE, variable assignment, and DESCRIBE/SHOW. `EXECUTE JOB SERVICE` is not on that list, so it may be rejected inside `TRANSCRIBE_IF_NEW_FILES()` (`EXECUTE AS OWNER`) — the same way `LIST` was rejected earlier in this project. However `EXECUTE NOTEBOOK` works there today, so the allow-list is not strictly predictive. This must be tested early (task 2), with a clean fallback.

### Notebook inventory

**Re-measured 2026-09-24: 1,814 lines of code across 19 code cells** (36 cells total). Grew from \~1,312 before the 2026-08-19 progress instrumentation and \~1,671 at the last measurement; the resource ledger added the most recent \~150. Realistic target is **650-750 lines** of headless Python, plus the progress emitter. The work concentrates in a few places:

- **Cell 19 (`helper_functions`) is 526 lines** — the largest cell and the bulk of the port. Pure functions with no notebook coupling; ports nearly verbatim. Contains the load-bearing `import re`. **8 of its functions were already extracted on 2026-09-24** into `scripts/payload/transcribe_functions.py`, AST-verified identical, and are now covered by 122 offline tests — see the prerequisite note on task 4. Reuse that module rather than re-extracting.
- **Cell 5 session bootstrap is 260 lines** and must be rewritten (OAuth instead of `get_active_session`, drop `session.use_role("SYSADMIN")`, drop the unused `Root(session)`). It also now holds `RunProgress` (task 4b) and the resource ledger (task 4c).
- Cells 26, 30, 31, 32, 35 and cell 10 are presentation-only and get dropped, including the entire teardown apparatus.
- Cell 12 is a duplicate `openai-whisper` install and gets dropped.
- **Cell 28 is 154 lines.** Its `except` branch is **dead and broken** — it supplies 14 positional values against a 23-column table. Do not port it; replace with a real error path.
- `media_files/` is a relative path dependent on cwd; use an absolute temp dir.

Only `openai-whisper` and `pandas` need installing; `torch` comes from the image.

### Architecture

```mermaid
graph TD
    subgraph before [Current - hangs]
        U1[upload_av_files.py] -->|EXECUTE TASK| T1[TRANSCRIBE_NEW_FILES_TASK_V2]
        T1 --> G1["TRANSCRIBE_IF_NEW_FILES() owner rights"]
        G1 -->|EXECUTE NOTEBOOK, synchronous| N1[Streamlit notebook runtime]
        N1 --> H1["work done in ~2min, then snowbook script runner parks in on_scriptrunner_ready for ~2h07m"]
    end
```

```mermaid
graph TD
    subgraph after [Proposed - bounded]
        U2[upload_av_files.py] -->|EXECUTE TASK| T2[TRANSCRIBE_NEW_FILES_TASK_V2]
        T2 --> G2["gate: new files?"]
        G2 -->|"no"| S2[return SKIPPED]
        G2 -->|"yes, EXECUTE JOB SERVICE"| J2["snowbooks GPU image, python transcribe_job.py"]
        J2 --> W2[transcribe, summarize, INSERT]
        W2 --> E2["main returns, container exits"]
    end
```

The uploader, the task, and the gate logic are all unchanged in behaviour. Only the payload execution vehicle changes.

## Implementation steps

### 1. Spike: validate every assumption — COMPLETE 2026-09-24, ALL GATES PASS

Run against `TRANSCRIPTION_GPU_POOL_V2` as three throwaway job services
(`SPIKE_PROBE_02`, `SPIKE_FULL_01`, `SPIKE_WHISPER_01` — retained 30 days as evidence).

**The image path was the one genuinely unknown, and the plan had it wrong.** The notebook's
`runtime_name` is `SYSTEM$GPU_RUNTIME` — a *named notebook runtime*, not an image URI — and
system-managed service specs return an empty `spec` column, so the image cannot be read off the
existing notebook. It had to be found by probing. The answer:

```
/snowflake/images/snowflake_images/container_runtime/gpu_x86_64:2.9.0
```

Bare `:2.9` does **not** resolve; the tag needs the patch level. A wrong tag fails instantly with
`Image ... not found` and provisions nothing, so probing tags is free.

This is the **Container Runtime** image, not the snowbooks notebook image — a better target than the
plan assumed. Note `snowbooks 1.76.10rc1` is still *installed* in it, so the safety rule stands:
never invoke `python -m snowbook.web.cli`. Being present is harmless; being invoked is the hang.

**Measured results:**

| Check | Result | Gate |
|---|---|---|
| `sys.version` | 3.10.19 | PASS — matches notebook |
| `which ffmpeg` | `/usr/bin/ffmpeg`, **6.1.1**-3ubuntu5 | PASS — 6.x as required |
| `which ffprobe` | `/usr/bin/ffprobe` | PASS |
| `torch.cuda.is_available()` | True, **NVIDIA A10G** | PASS |
| OAuth session | role + `TRANSCRIPTION_WH_V2`, `COUNT(*) = 493` | PASS — matches live count |
| AV stage volume mount | 377 media files, 378 entries | PASS — matches `LIST` exactly |
| `SYSTEM$GET_SERVICE_LOGS` | full stdout retrieved | PASS |
| Whisper install | `uv pip install --system --break-system-packages` → rc=0, **1s** | PASS *after fix* |
| `whisper.load_model('base')` | **4.7s**, `cuda:0`, 279.4 MB allocated | PASS |

**`pip install` fails in this image and the fix is mandatory.** Plain `pip install openai-whisper`
exits 1 with PEP 668 `This environment is externally managed / managed by uv`. The payload must use
`uv pip install --system --break-system-packages`, or bake the dependency into a CRE. This would
have failed the first real run otherwise.

**Startup is far better than the notebook, which changes the budgets and the dashboard copy:**

| | Notebook | Job service (measured) |
|---|---|---|
| Cold, first image pull | 60-180s | **105s** (`SPIKE_FULL_01`) |
| Warm | 60-180s | **18s** (`SPIKE_WHISPER_01`, install + model load + exit) |

279.4 MB CUDA allocated after model load corroborates the notebook's \~287 MB figure, so the model
footprint is unchanged — the improvement is all install/startup overhead.

**Two risks the spike surfaced that were not in the plan:**

1. **`torch` is 2.9.1+cu129 here versus 2.6.0+cu126 in the notebook runtime.** A two-minor-version
   jump under Whisper can change transcription output. This does not break the port, but it means
   **transcript text is not guaranteed byte-identical to the notebook's**, so task 4's 23-column
   diff must treat `TRANSCRIPT`, `SRT_*` and segment boundaries as *expected to differ slightly*
   rather than exact. Pin the comparison to structure and language detection, not exact text. If
   exact parity matters, pin torch in the payload install.
2. **All three spike jobs reached `DONE` and exited cleanly**, with no forced exit and no phantom
   session. That is the first direct evidence this launch path exits — but none ran the
   transcription workload, so it is *not* evidence about the hang. Do not over-read it.

### 1b. Remaining spike item — launch site (was task 2)

Still unverified: whether `EXECUTE JOB SERVICE` is permitted inside an `EXECUTE AS OWNER`
procedure. Covered by task 2 below; the spike deliberately launched as `ACCOUNTADMIN` from a
worksheet, which proves the mechanism but not the production caller.


### 2. Decide the launch site

Test `EXECUTE JOB SERVICE` from inside an `EXECUTE AS OWNER` procedure.

- If permitted: swap `EXECUTE NOTEBOOK` for `EXECUTE JOB SERVICE` inside `TRANSCRIBE_IF_NEW_FILES()` — smallest possible change.
- If rejected: refactor so the gate procedure only **decides** (returns `LAUNCH` or `SKIP` plus the file count) and the **task body** performs `EXECUTE JOB SERVICE`. Task bodies run as the task owner and are not bound by the stored-procedure allow-list. This keeps the gate logic in one place and avoids granting the uploader's service role privileges on the compute pool.

Prefer the task-body variant if there is any doubt; it is more robust and does not change the security model.

**RESOLVED — `EXECUTE JOB SERVICE` IS permitted inside `EXECUTE AS OWNER`.** Take the first branch:
swap `EXECUTE NOTEBOOK` for `EXECUTE JOB SERVICE` inside `TRANSCRIBE_IF_NEW_FILES()`. The
task-body refactor is not needed, so the security model does not change.

Evidence: `SPIKE_JOB_IN_PROC()` (`EXECUTE AS OWNER`, ACCOUNTADMIN) ran a synchronous
`EXECUTE JOB SERVICE` to completion and the container's own stdout came back via
`SYSTEM$GET_SERVICE_LOGS` — proof the container executed, not merely that the service was created.
The documented allow-list is therefore not predictive here, exactly as this task suspected.

Three mechanics this spike pinned down, all of which task 5 depends on:

- **Use `FROM @stage SPECIFICATION_FILE = '...'`, not inline `FROM SPECIFICATION $$...$$`.** A
  procedure body is itself a quoted string and Snowflake supports only `$$` as a dollar-quote tag
  (`$body$` fails to parse), so an inline spec cannot be nested. A spec file on a stage is also the
  better answer: it is version-controllable. Task 5 should author
  `scripts/payload/transcribe_job_spec.yaml` and deploy it to `@PAYLOAD_STAGE`.
- **YAML colon trap.** `- echo "spike: text"` fails with a deserialization error pointing at
  `command[2]`, because `: ` makes YAML read the scalar as a mapping. Quote any command string
  containing a colon. The real payload command has none today, but any added `echo` with a colon
  would break the spec at launch time — after the gate has already decided to run.
- **A GPU-pool container must request a GPU or it never schedules.** Omitting
  `resources.requests.nvidia.com/gpu` left the job `PENDING` indefinitely with only a `WARN` in
  `SYSTEM$GET_SERVICE_STATUS` — no failure, no error, no timeout. A spec that forgets this looks
  like a hang rather than a misconfiguration.

### 3. Extract the headless payload

Create `scripts/payload/transcribe_job.py`:

- Config via `argparse` with env-var fallbacks: `--database`, `--schema`, `--stage`, `--results-table`, `--whisper-model`, `--force-retranscribe`, `--limit`, `--dry-run`, `--work-dir`
- OAuth session helper as shown above, wrapped so the same script can also run locally with a named connection (needed for task 4)
- ffmpeg/ffprobe preflight that fails fast with a clear message
- Port cell 19's 9 functions verbatim, keeping `import re`
- Convert the implicit globals cell 19 relies on (`model`, `session`, `DIARIZATION_AVAILABLE`, `diarization_pipeline`) into explicit parameters or a small class
- Explicit `device` selection plus a startup assertion on `torch.cuda.is_available()` so a misconfigured pool fails loudly instead of silently running on CPU
- Absolute temp work dir via `tempfile.mkdtemp()`, cleaned in a `finally`
- `logging` instead of `print`
- Preserve the dedup contract **exactly**: `SELECT DISTINCT FILE_NAME FROM <results_table>`, matched on bare filename. The SQL gate uses the same signal, so any drift makes the gate and payload disagree.
- Keep the 23-column INSERT in the same column order; drop the broken fallback. While porting, verify the `session.sql(insert_sql)` call is actually collected — cell 28 assigns the DataFrame without an obvious `.collect()` on that line.
- Exit non-zero on failure so the job service reports failure honestly

Add `scripts/payload/requirements.txt` with `openai-whisper` and `pandas`.

The Cortex model is **`claude-sonnet-4-6`**, verified against notebook line 1007. The earlier `claude-opus-4-5` reference in agents.md was stale documentation and has been corrected — port the notebook's value, and read it from config rather than hardcoding it again.

### 4. Validate the payload locally against a clone

**PREREQUISITE — already satisfied as of 2026-09-24, do not redo.** The offline test harness from
`port-test-harness-and-metadata-backfill.plan.md` tasks 1-4 is complete and committed (`2760f71`):

- `scripts/payload/transcribe_functions.py` holds 8 functions extracted from cell 19,
  AST-verified byte-identical apart from one documented change (`generate_summary_markdown` takes
  `session` as an explicit first parameter instead of reading the global).
- `tests/test_payload_functions.py` — 41 unit tests.
- `tests/test_payload_parity.py` — parity against **239 rows of stored history**, regenerating
  outputs from stored inputs and comparing to the stored values. Fixtures in
  `tests/fixtures/golden/` (936 KB).
- **122 tests, all passing, fully offline and free.** Verified non-vacuous by mutation testing.

**This changes the shape of task 4.** The original step below was the port's only correctness
check, and it validated a 1,800-to-700 line rewrite by diffing a single row by hand. That is now
the *last* line of defence rather than the only one. Run the offline suite first — it is instant
and catches whole classes of port bug before any GPU is provisioned:

```bash
pytest tests/ -q      # must be green BEFORE requesting a compute pool
```

Two real bugs were already caught this way, both of which would have shipped silently because the
notebook swallows them in a bare `except`: writing `import datetime` instead of `from datetime
import datetime` (nulls `CALL_START_TS` for every file) and the missing `import re` (nulls all six
summary fields, which is what caused the 181-row metadata gap from 2026-04 to 2026-07).

**Corpus scoping matters if you re-extract.** Parity rows are filtered to the current code era by
`TRANSCRIPTION_TIMESTAMP`, because the SRT generator changed between 2026-01 and 2026-02 and the
summary prompt's header format changed between 2026-02-10 and 2026-08-17. Unscoped, the suite
reports 78 failures that are pure historical drift. Never filter on the date in `FILE_NAME` — that
is the meeting date, not the processing date.

Then the clone check, unchanged in intent:

- `CREATE TABLE TRANSCRIPTION_RESULTS_PORTTEST CLONE TRANSCRIPTION_RESULTS` (zero-copy, free)
- Run the payload locally with `--results-table TRANSCRIPTION_RESULTS_PORTTEST` against the DoubleVerify file
- Diff all 23 columns against the row the notebook produced for the same file. Expect near-identical values; `PROCESSING_TIME_SECONDS` and `TRANSCRIPTION_TIMESTAMP` will differ, and the LLM summary text will vary run to run, but `MEETING_TITLE`, `CALL_BRIEF`, `KEY_POINTS`, `NEXT_STEPS` must be non-null and structurally correct.

The real `TRANSCRIPTION_RESULTS` is never written during this phase.

### 4b. Port the progress instrumentation — REQUIRED, not optional

**This step was missing from the original plan.** Without it the port silently kills a shipped
feature: the dashboard's Pipeline Status panel reads `V_TRANSCRIPTION_RUN_STATUS`, which is fed
*only* by the notebook's emissions. A payload that does not emit leaves every run showing `IDLE`,
the kickoff button permanently enabled, and the completeness percentage blank — with no error
anywhere to indicate why.

Port `RunProgress` from notebook cell 5 into the payload. It was written to be portable (plain
SQL INSERTs, no notebook APIs, `emit()` never raises), so this is close to a copy:

- Keep the schema of `TRANSCRIPTION_RUN_EVENTS` **unchanged** — 18 columns, append-only. The
  dashboard, the view and the derived-state logic all depend on it.
- Set `RUN_SOURCE = 'JOB_SERVICE'` instead of `'NOTEBOOK'` so old and new runs are
  distinguishable in history. Confirm the panel renders an unrecognised `RUN_SOURCE` gracefully
  — it is displayed as free text, so it should, but check rather than assume.
- Preserve the unit arithmetic exactly: `UNITS_TOTAL = 4 + (files × 4)`, with the four global
  units and four per-file steps (`EXTRACT_AUDIO`, `TRANSCRIBE`, `GENERATE_SRT`,
  `GENERATE_SUMMARY`). `finish_file()` must still snap to `baseline + 4` regardless of outcome,
  or a skipped or failed file leaves the percentage permanently short of 100%.
- Keep the six phases (`STARTUP`, `DISCOVER`, `DOWNLOAD`, `TRANSCRIBE`, `PERSIST`, `COMPLETE`)
  and `PHASE_TOTAL = 6`; `sf_config.PHASE_TOTAL` hardcodes 6.
- **Emit a real terminal state.** This is the one place the port should NOT copy the notebook.
  The notebook cannot report its own clean exit — the hang happens after the last cell — so its
  terminal state is `CELLS_COMPLETE` and the dashboard has to cross-check `TASK_HISTORY` to tell
  "finished" from "wedged". A headless script *can* report its own exit, so emit `SUCCEEDED` as
  the last statement before exit. Then `WORK_COMPLETE_NOT_EXITED` becomes genuinely diagnostic
  rather than routine: seeing it after the port would mean the job service has its own
  exit problem.
- Note the dashboard's `STARTING` state derives from "task EXECUTING, newest run terminal, last
  heartbeat older than task elapsed". That logic is launch-mechanism agnostic and should keep
  working, but the pre-emit window will change — a job service has no `pip install
  openai-whisper` ahead of the first emit if the image carries the deps, so `STARTING` may last
  seconds instead of 60-180s. Verify it does not flicker.

Validate on the clone run in task 4: the event stream should reach exactly `UNITS_DONE ==
UNITS_TOTAL` and `PCT_COMPLETE = 100.0`, with all four per-file steps present for every file.

### 4c. Port the resource ledger — DECISION: yes, port it

**Decided 2026-09-24.** The ledger is \~150 lines in cell 5 plus call sites in cell 19 and cell 34.
It counts things that must be cleaned up and re-counts them after cleanup, printing a paired
`created=N removed=N unaccounted=0 on_disk=0` verdict, alongside a snapshot of fd count, thread
count, non-daemon thread count, direct OS children and leftover temp WAVs at every per-file
boundary.

**Why port it rather than drop it as hang-investigation scaffolding:**

1. **It is what made the hang diagnosis sound.** The original investigation concluded "zero child
   processes" from `multiprocessing.active_children()`, which structurally **cannot see**
   `subprocess.Popen` children — and ffmpeg is a `Popen` child. The ledger reads
   `/proc/<pid>/task/*/children` and showed `os_children=1` persistently. The earlier claim was
   false. Dropping the instrumentation re-opens the door to the same false negative.
2. **It converts the port's central claim into a measurement.** The port's premise is that nothing
   leaks and the hang is a race inside `snowbook`. After the port, the ledger is how you confirm
   the *payload* is clean rather than assuming it. If a job-service run ever wedges, the first
   question is again "did we leak something", and the answer should be data.
3. **It has already paid for itself and costs nothing.** Five runs (09-21 ×2, 09-22, 09-23, 09-24)
   all report `unaccounted=0 on_disk=0 OK`. Output goes to stdout, not to
   `TRANSCRIPTION_RUN_EVENTS`, so there are no extra INSERTs and no schema impact. `/proc` is
   readable in the container, confirmed by the `fd=` and `os_children=` values being real numbers.

**Porting notes:**

- Keep `_os_children()` returning **`None`, not `[]`**, when `/proc` is unavailable. An empty list
  is indistinguishable from genuinely-zero children, which is precisely the false negative above.
  The snapshot prints `-1` for the unmeasurable case.
- In a headless script, stdout goes to the service's container log rather than to notebook cell
  output. Confirm the verdict line is actually retrievable via `SYSTEM$GET_SERVICE_LOGS` (or the
  event table, if the spec routes logs there) before relying on it — otherwise the ledger runs and
  reports into the void.
- Call `ledger_reconcile()` **before** removing the work directory. In the notebook it sits ahead
  of `shutil.rmtree('media_files')` deliberately: afterwards `on_disk` is always 0 and the check is
  vacuous.
- The per-file `finally` block owns the `wav_removed` increment. Keep it in `finally`, not on the
  success path, or a failed file undercounts removals and reports a phantom leak.
- A headless script can also emit a **final** post-cleanup snapshot, which the notebook cannot
  (its hang is after the last cell). Add one immediately before exit.

### 4d. Update the Streamlit dashboard — REQUIRED, three strings become FALSE

Task 4b covers keeping the *data* flowing to `V_TRANSCRIPTION_RUN_STATUS`. This covers the
dashboard's own logic and copy, which encode notebook-specific assumptions that the port
invalidates. None of these throw an error — they just quietly start lying to the operator.

All three are in `streamlit/sf_pipeline.py`:

1. **The `STARTING` blurb hardcodes the notebook's startup window** (around line 176):

   > "The notebook installs Whisper before it can report progress, so the first update takes
   > roughly 60-180s (longer on a cold GPU pool)."

   Measured job-service startup is **18s warm, 105s cold**. Reword, and drive the number from
   config rather than prose if possible. An operator who waits 3 minutes for a run that started
   18 seconds ago will conclude the pipeline is broken.

2. **`render_controls`'s comment and reasoning about the kickoff block** (around line 338) says
   `IS_ACTIVE` is false during "the 60-180s before the notebook..." The *logic* is sound and
   launch-agnostic — `is_active = IS_ACTIVE or state == 'STARTING'` — but verify the block still
   holds across an 18-second window. This is the one real behavioural risk: the `STARTING`
   detection compares `SECONDS_SINCE_HEARTBEAT > ELAPSED_SEC`, and with an 18s pre-emit window
   there is far less margin. Confirm the button does not become briefly clickable mid-run, which
   would let a second `EXECUTE TASK` fire.

3. **The `WORK_COMPLETE_NOT_EXITED` blurb blames snowbook** (around line 183):

   > "Known snowbook shutdown hang - transcripts are already saved."

   After the port this diagnosis is wrong by construction. Per task 4b the payload emits a real
   terminal `SUCCEEDED`, so this state appearing means **the job service has its own exit
   problem** — a new, unknown fault, not a known benign one. Reword so it reads as an alert
   rather than a reassurance.

Also confirm the panel renders `RUN_SOURCE = 'JOB_SERVICE'` gracefully. It is displayed as free
text at line 239, so it should, but check rather than assume.

### 5. Wire the launch path





- Add to scripts/00\_config.sql (the single source of truth): `PROJECT_JOB_IMAGE`, `PROJECT_JOB_NAME`, `PROJECT_STAGE_PAYLOAD`, plus derived `FQ_*`. Bump `CONFIG_REVISION` and republish with scripts/publish\_config.sh. **Also extend the `V_PROJECT_CONFIG` emitter at the bottom of that file** with the new names — it did not exist when this plan was written and is now how the dashboard resolves object names without drift.
- Reuse `NOTEBOOK_STAGE` for the payload or add a dedicated payload stage; either way pin the name in config, not inline.
- Author the service specification: one container on the snowbooks GPU image, `command` running `pip install -r requirements.txt && python transcribe_job.py`, a `stage` volume for the payload and one for `AUDIO_VIDEO_STAGE`, `resources` requesting `nvidia.com/gpu`, and env vars carrying the database/schema/table names. **The `command` must invoke the script directly — never `python -m snowbook.web.cli`**, which is the module at the top of the hang stack and is present in this image (see Context).
- Modify scripts/03\_automate.sql per task 2. While in that file, wrap its bare `DECLARE...END;` blocks in `EXECUTE IMMEDIATE $$ ... $$` so it survives `snow sql -f` (existing known issue).
- Add a deploy script for the payload mirroring the verify-after-deploy discipline now in scripts/04\_deploy\_notebook.sh: upload, then confirm the staged bytes match local. Do not repeat the mistake of trusting an upload as proof of deployment.

### 6. End-to-end validation

Upload a real recording through `upload_av_files.py` and confirm the full chain. Then a second trigger with no new files to confirm the skip path.

### 7. Decommission and document

Retire the headless notebook path while keeping the notebook for interactive use. The notebook and the payload will share logic by copy, not by import — accept that duplication explicitly, or note the follow-up to have the notebook import the payload module from the stage.

**Do not delete the `EXECUTE NOTEBOOK` path in this step.** See the rollback section below; it is
retired only after the port has earned it.

## End-to-end test plan

Tiered by cost and by blast radius. **Nothing is committed to git until Tier 3 passes**, per the
2026-09-24 instruction. Tiers 0-1 need nothing from the operator; Tiers 2-4 have explicit HUMAN
gates, marked inline.

**HUMAN 1 — DELIVERED 2026-09-24. 13 files staged locally in `AUDIO_VIDEO_STAGE_FILES/`.**

Real recordings re-staged under `_TESTnn` suffixes so the dedup gate treats them as new
work. **311 minutes of audio total**, 4.2 to 45.0 minutes each — a useful spread, and longer
than the 5-10 min originally requested, which is fine and arguably better.

| | |
|---|---|
| Count | 13 (8 `.mp4`, 5 `.mp3` — not all `.mp3` as first reported) |
| Audio | 311 min total; shortest `TEST12` 4.2 min, longest `TEST13` 45.0 min |
| Naming | `DATE ACCOUNT_description_TESTnn.ext` — conforms, so `ACCOUNT_NAME` parses correctly |
| Manifest | `tests/fixtures/test_av_manifest.txt` — the authoritative list |
| Cleanup | `scripts/cleanup_test_artifacts.sql` — dry-run by default |

**Expected runtime is much lower than a naive estimate.** Do not use "2-4 min per 8-min
recording" — the real anchor is the observed 10-file / 18,202s-audio / 978s run, a ratio of
**0.054x realtime**. So 311 min of audio is roughly **17 minutes of GPU across the whole
campaign**, and a 3-file run of \~120 min audio is \~6.5 min — comfortably inside the
30-minute `USER_TASK_TIMEOUT_MS`. A naive 0.3x estimate would have predicted 90+ minutes and
falsely suggested the timeout needed raising.

**Cleanup is keyed on the anchored regex `_TEST[0-9]{2}\.(mp3|mp4)$`, verified 2026-09-24:**

- Matches 0 of 377 pre-existing stage files and 0 of 493 pre-existing transcripts.
- `LIKE '%TEST%'` would have caught the real permanent transcript
  `...DoubleVerify_onsite.AI_Brain.Testing.sync.mp4`. The anchored form excludes it, along
  with `TESTING01`, single-digit `TEST1`, and non-media extensions — all 9 cases verified
  against the live database before any upload.

**The run-events predicate is the subtle part.** `RUN_EVENTS` has no per-row `FILE_NAME`,
only `CURRENT_FILE`, which is NULL on run-level events. Deleting rows by `CURRENT_FILE`
would strip per-file rows and orphan the run-level ones. Deleting whole `RUN_ID`s that
merely *touched* a test file would destroy the history of a **mixed** run — real
operational history lost to clean up disposable data. So the script deletes a `RUN_ID`
only when **every** non-null `CURRENT_FILE` in that run matches the test pattern, and
reports mixed runs as deliberately kept.

The cleanup script clones `TRANSCRIPTION_RESULTS` to `TR_PRECLEANUP_BACKUP` before any
`DELETE`, unconditionally. Stage `REMOVE` is deliberately left manual, because `REMOVE`
takes a literal pattern and cannot be gated on the dry-run flag — automating it would fire
it on every report run.

### AV files required — original estimate, retained for reference

As of 2026-09-24 the stage holds **377 files and all 377 are already transcribed**, so the dedup
gate returns `SKIP` and the pipeline has nothing to do. Testing the real path therefore needs new
files. Required counts by tier:

| Tier | New files needed | Why |
|---|---|---|
| 0 offline | 0 | pytest only |
| 1 spike | 0 | done; lists the stage, transcribes nothing |
| 2 clone | 0 | reuses an existing file via `--force-retranscribe` against a clone table |
| 3 smoke | **1** | first real end-to-end run through the whole chain |
| 4 hang validation | **12** | 4 consecutive runs × 3 files, per the honest success criterion |
| 5 skip path | 0 | the point is that there is nothing new |

**HUMAN 1 — total 13 new AV files.** Constraints that matter:

- Filenames must not already appear in the 377, or the gate skips them. Dedup is on **bare
  filename**, so a same-named file in a different folder still counts as done.
- Follow `DATE ACCOUNT_description.ext`, i.e. `2026-09-25 10-30-00_Acme_topic.mp4`. The filename
  parser takes the **second `_`-delimited field** as the account and does not validate, so a
  non-conforming name silently yields a wrong `ACCOUNT_NAME` — see `--strict` in
  `scripts/backfill_metadata.py` for the failure modes already in the data.
- Modest length. 5-10 minutes each keeps a 3-file run near 10 minutes of GPU.
- **Staging is safe and triggers nothing.** `TRANSCRIBE_NEW_FILES_TASK_V2` is suspended with no
  schedule, so files can be dropped in and drained deliberately. Files can be staged all at once
  and consumed 3 at a time via `--limit`.
- If 13 real recordings is impractical, say so: existing media can be re-staged under new
  `ZZTEST_NN_*` names to exercise the runtime. That validates the *mechanism* identically but adds
  duplicate-content rows to `TRANSCRIPTION_RESULTS`. Those rows are trivially identifiable and
  removable, but deleting them is a write to the production table and needs its own approval.
  **Operator's choice — do not assume.**

### Tier 0 — offline, free, no Snowflake

```bash
pytest tests/ -q          # 122 tests; must be green before anything else runs
```

Covers the 8 extracted functions against 239 rows of stored history. This is the gate that catches
port bugs cheaply; everything below costs GPU time.

### Tier 1 — spike. COMPLETE, all gates passed

See task 1. No files, no writes, no human action.

### Tier 2 — payload against a clone. PASSED 2026-09-24

**Split into 2a (local, free) and 2b (container, real runtime) rather than the single
"run locally" the plan originally specified.** Running the payload locally cannot work and
should not: there is no torch or whisper on the dev Mac, and macOS CPU whisper output
would not match an A10G anyway, so the comparison would be meaningless. 2b in the
container against the clone tests the real environment AND writes nothing to production —
strictly better on both counts.

**2a — local dry run, `--dry-run --results-table ..._PORTTEST`.** Validated the named
connection, `LIST`, the dedup contract and `--limit` for free. Reported `383 already
transcribed, 0 new`, correct because the clone already held all six notebook-written test
rows. Also confirmed the ledger renders `os_children=-1` on a platform with no `/proc`,
which is the `None`-not-`[]` path working as designed.

This required one payload change: `preflight(require_gpu=False)` under `--dry-run`.
Mandatory GPU checks would have made the cheap validation step impossible to run anywhere
but a GPU container, which defeats its purpose. ffmpeg is still checked either way.

**2b — `EXECUTE JOB SERVICE`, payload from `@PAYLOAD_STAGE`, `--limit 1`.** Full trace:

```
connecting with the container OAuth token
GPU: NVIDIA A10G, torch 2.9.1+cu129 · preflight OK
stage holds 384 media file(s) · 383 already transcribed, 1 new · --limit 1 applied
whisper loaded in 4.2s
transcribed ..._TEST12.mp4: 255s audio in 19s, language=en, speakers=2
inserted 1 record(s) into TRANSCRIPTION_RESULTS_PORTTEST
LEDGER RECONCILE files=1 created=1 removed=1 unaccounted=0 on_disk=0 OK
exit 0
```

**Isolation held: 1 row in the clone, 0 in `TRANSCRIPTION_RESULTS`.** Production stayed at
499 throughout.

**The ledger comparison is the headline result.** Same instrumentation, same account, same
GPU, hours apart:

| | Notebook (09-21 hung run) | Payload (Tier 2b) |
|---|---|---|
| fd | 76 | **46** |
| threads | 12 | **2** |
| non-daemon threads | 3 | **1** |
| os_children | 1 | **0** |
| exit | wedged 23 min | **exit 0** |

One non-daemon thread is just `MainThread`. The notebook's three are the snowbook
machinery the port exists to remove, and `os_children=1` is the ffmpeg `Popen` child that
`multiprocessing.active_children()` could never see. This is not proof the hang is fixed —
a single-file run proves nothing, per the honest success criterion — but the mechanism it
depends on is measurably absent.

**23-column comparison against the six notebook-written rows: structurally identical.**
`FILE_TYPE`, `DETECTED_LANGUAGE`, `SPEAKER_COUNT`, `FILE_SIZE_BYTES`, `ACCOUNT_NAME`,
`CALL_START_TS`, `PARTICIPANTS_JSON` and all seven summary fields match in shape, and
`SRT_SEGS == TWS_SEGS` (61 == 61) satisfies the current-era generator invariant.

One real difference found and fixed: **`FILE_PATH`**. The payload defaulted to `''` while
the notebook sets `INCLUDE_FILE_PATH = True` and populates it. Nothing reads the column —
it appears once in the whole repo, as a column definition — and both values are equally
worthless as provenance (the notebook writes `media_files/<name>`, a relative path to a
directory it deletes in cell 34). But a column flipping from populated to empty across a
port is a diff a reviewer must chase for no benefit, so the default is now `True`, with
`--no-file-path` to opt out. Content still differs by design and that is documented.

### Tier 2 — original plan text, retained for reference

1. `CREATE TABLE TRANSCRIPTION_RESULTS_PORTTEST CLONE TRANSCRIPTION_RESULTS` — zero-copy, free.
2. Run the payload locally with `--results-table TRANSCRIPTION_RESULTS_PORTTEST
   --force-retranscribe` against one existing file. `--force-retranscribe` is required because the
   clone already contains all 377, so the dedup gate would otherwise skip everything.
3. Diff all 23 columns against the notebook's row for the same file.

**Expect these to differ and do not treat it as failure:** `PROCESSING_TIME_SECONDS`,
`TRANSCRIPTION_TIMESTAMP`, the LLM summary text (non-deterministic), and — per the spike's torch
finding — the transcript text and segment boundaries. Assert instead that `MEETING_TITLE`,
`CALL_BRIEF`, `KEY_POINTS`, `NEXT_STEPS` are non-null and structurally correct, that the language
detection matches, and that segment count is within a few percent.

The real `TRANSCRIPTION_RESULTS` is never written in this tier.

### Tier 3 — first real end-to-end run, 1 file

**HUMAN 2 — take the backup before this tier.** `CREATE TABLE TR_PREPORT_BACKUP CLONE
TRANSCRIPTION_RESULTS`. Free, and the only thing between a payload bug and 493 irreplaceable
transcripts.

Then, with the Streamlit dashboard open throughout — this tier is as much a dashboard test as a
pipeline test:

| Step | Check | Watch for |
|---|---|---|
| 1 | Upload 1 file via the dashboard's uploader | `validate_filename` accepts it; backlog count goes to 1 |
| 2 | Click kickoff | Button **disables immediately** and stays disabled |
| 3 | 0-20s in | Panel shows `STARTING`, not `IDLE`. **Blurb must not claim 60-180s** (task 4d) |
| 4 | First progress event | Panel moves to a live phase; percentage advances |
| 5 | Mid-run | Kickoff stays disabled for the whole run — the 18s window is the risk (task 4d item 2) |
| 6 | Completion | `UNITS_DONE == UNITS_TOTAL`, `PCT_COMPLETE = 100.0`, terminal state **`SUCCEEDED`** |
| 7 | After | Panel returns to `IDLE`; kickoff re-enables; `RUN_SOURCE = 'JOB_SERVICE'` renders |
| 8 | Data | `TRANSCRIPTION_RESULTS` count increments by exactly 1; new row's 23 columns sane |
| 9 | Ledger | `unaccounted=0 on_disk=0 OK` retrievable from the container log (task 4c) |

A blank or `IDLE` panel during a live run means the instrumentation was not ported. That is the
specific silent failure tasks 4b and 4d exist to prevent.

**PASSED** — two runs. `TIER3_JOB_01` (1 file, TEST12) and `TIER3_JOB_02` (3 files: TEST09, TEST08,
TEST07, 16/16 units, run `0132675d`). Production 499 -> 500 -> 503; all rows' 23 columns sane, all
`SRT_SEGS == TWS_SEGS`, all 6 summary fields populated, `FILE_PATH` populated, ledger `OK`.
Human-observed on the dashboard: live phase with advancing percentage, file counter advancing 1..3,
current filename changing between files, kickoff disabled throughout, terminal green `SUCCEEDED`
rendering `JOB_SERVICE`.

Deviations from the steps as written, and what they cost:

- **Steps 1-3 and 7 were not exercised as specified.** No task run backs a manual
  `EXECUTE JOB SERVICE`, so `STARTING` is unreachable and step 3 could not run. Step 7's "returns to
  `IDLE`" is also wrong as written: the view surfaces the *latest* run forever, so the panel
  correctly holds `SUCCEEDED`, and the kickoff button correctly stays disabled via the
  `n_backlog == 0` path (verified: backlog 0), not the `IS_ACTIVE` path. **Both are structural, not
  defects** — but it means the 18s-window risk in step 5 is still only partly tested, because the
  first event landed while `IS_ACTIVE` was already true.
- **The 1-file run was too short to observe** — 53s total, with `FINISHING` open for 1 second. A
  single-file run is effectively invisible to a polling dashboard. Re-ran with 3 files to get a
  ~4 min window. Future dashboard checks should use >= 3 files.
- **Task 4d confirmed real but unreachable here.** Every stale user-visible string lives in a
  `STARTING` blurb (`sf_pipeline.py:176-178`) or a `HUNG` blurb (`:183`, `sf_theme.py:179`); the
  rest are comments (`:41`, `:144`, `:338`). Neither state occurred, so a clean dashboard here is
  **not** evidence 4d is done.

Process failure worth keeping: `TIER3_JOB_01` ran a **stale payload**. The `FILE_PATH` default was
fixed locally *after* the stage upload and never re-uploaded, so the column came back empty and the
fix went untested while appearing to have been exercised. The staged copy was 35,840 bytes against
36,958 local. This is exactly the trap `04_deploy_notebook.sh` documents. **Re-verify staged bytes
after every edit; a successful `PUT` is not proof of deployment.** Note stage `size` reflects
encryption padding (36,958 local -> 36,960 staged), so compare md5/timestamp, not exact bytes.
Also remove `@PAYLOAD_STAGE/__pycache__/` — stale bytecode can shadow edited source.

### Tier 4 — hang validation, 4 runs × 3 files

The only tier that says anything about the hang. Single-file runs prove **nothing** — 0 of 8 ever
hung; they sit in the regime that never failed.

Per run, record all three budgets separately (startup / work / tail) plus the ledger verdict. Pass
requires **every** run to close its last-event-to-task-end tail under **~30s** with `STATE` not
`FAILED`. Do not compute the tail as `task_duration - event_span` — that includes startup and
reports 126-149s for clean runs.

After each run also confirm the dashboard did not show `WORK_COMPLETE_NOT_EXITED`. Post-port that
state means a **new** exit fault, not the known benign one (task 4d item 3).

**HUMAN 3 — if any run hangs, stop and consult before continuing.** Two hangs in ten runs is a
rollback trigger. Also note a hung run leaks a GPU node indefinitely: `ALTER COMPUTE POOL
TRANSCRIPTION_GPU_POOL_V2 STOP ALL;` then `SUSPEND`.

**PASSED — 4 of 4 runs clean, 0 hangs.** Same 3 files every run (TEST12 255s, TEST07 562s,
TEST03 767s = 26 min audio, ~200s work) written to `TRANSCRIPTION_RESULTS_PORTTEST`:

| Run | Service | Startup | Work | **Tail** | Total | Terminal |
|---|---|---|---|---|---|---|
| 1 | `TIER4_RUN_01` / `758c06f3` | 112s | 168s | **5s** | 285s | SUCCEEDED |
| 2 | `TIER4_RUN_02` / `2f7c5ff7` | 16s | 159s | **5s** | 180s | SUCCEEDED |
| 3 | `TIER4_RUN_03` / `63d2bc8a` | 16s | 155s | **5s** | 176s | SUCCEEDED |
| 4 | `TIER4_RUN_04` / `ef824c18` | 18s | 159s | **5s** | 182s | SUCCEEDED |

Every run: 25 events, 3 files, 3/3 rows fully sane, `SUCCEEDED`. Tail **5s** against the 30s
threshold, with zero variance. Zero `FAILED` events, zero `WORK_COMPLETE_NOT_EXITED`, production
untouched at 503, pool `IDLE` with 0 jobs afterward. Against the pre-port baseline of **1 hang in 2
notebook runs** (tail 5s clean vs 145s+ hung), the port eliminates the hang on this evidence.

Two findings worth carrying forward:

- **Warm-pool startup is 16-18s, not 112s.** Run 1 paid 112s on a cold pool; runs 2-4 reused the
  warm node. The notebook's comparable figure was 127s. This makes the cost case materially better
  than the ~27% estimate, which assumed cold starts, and it is why the `STARTING` blurb's "60-180s"
  is wrong in both directions (task 4d).
- **Tail is 5s flat across all four runs.** Identical to the *clean* notebook run, so the port does
  not trade the hang for a slower teardown.

Methodology notes, because two of these were nearly measurement errors:

- **`--force-retranscribe` was NOT used, deliberately.** `discover()` takes `sorted(staged)` over the
  whole stage, so `--force-retranscribe --limit 3` would have transcribed 3 real meeting files and
  left `RUN_EVENTS` rows that `cleanup_test_artifacts.sql` cannot match by filename. Instead the
  clone was armed by deleting exactly the 3 target rows before each run, making those files the only
  new work. Verified 3 new / 0 non-test before run 1. **A `--pattern` / file-filter argument would
  make this tier much safer to repeat.**
- **Tail must be measured statement-end minus last-event.** Runs were synchronous
  (`ASYNC` omitted) so the `EXECUTE JOB SERVICE` statement returns at container exit, making
  `QUERY_HISTORY.END_TIME` the true teardown moment - the job-service analogue of task end.
- **`INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => N)` silently truncates.** At 400 it returned
  only runs 3-4 and the verdict query reported 2 of 4 rows as though 1-2 did not exist. Raising the
  limit surfaced all four. A measurement query that can quietly lose runs is worse than none.
- **`DIRECTORY()` is stale after `PUT`.** A verification query using it reported 383 media files and
  missed 4 test files entirely; `ALTER STAGE REFRESH` then registered exactly those 4. Use `LIST`,
  as `discover()` does, or refresh first.

### Tier 5 — skip path and rollback rehearsal, 0 files

1. Trigger with no new files. Expect `SKIPPED` in seconds and **no GPU launch**. Confirm the
   compute pool shows no new job.
2. **Rehearse the rollback on purpose** — flip `PROJECT_LAUNCH_MODE` to `'NOTEBOOK'`, republish,
   recreate the procedure, run one file, confirm it completes. Then flip back. Discovering the
   rollback works under pressure is the failure mode this avoids.
3. Confirm the pool shows one job per run, not phantom multi-hour sessions.

### Commit gate

**HUMAN 4 — only after Tiers 0-5 pass does anything get committed.** Commit as a series: payload,
Streamlit changes, config additions, plan updates. Keep `NOTEBOOK` mode reachable until the 4
consecutive clean multi-file runs from Tier 4 are banked, per the Rollback section.


The port replaces the only working transcription path with an unproven one, against a hang that is
**intermittent** — 1 of 6 runs in the current 7-day window, and it has previously produced a
hang/clean/hang sequence inside six hours. So a single clean run is not evidence the port worked,
and by symmetry a single failure is not evidence it is broken. That asymmetry is the whole reason
rollback needs to be cheap and pre-decided rather than improvised.

**Keep both launch paths live, selected by config.** Add to `scripts/00_config.sql.template`:

```sql
-- NOTEBOOK | JOB_SERVICE. Selects how TRANSCRIBE_IF_NEW_FILES() launches the work.
-- Keep NOTEBOOK reachable until the job service has 4 consecutive clean 3+ file runs.
SET PROJECT_LAUNCH_MODE = 'JOB_SERVICE';
```

Surface it through `V_PROJECT_CONFIG` like every other name, and have the stored procedure branch
on it. Rollback is then a config republish plus a procedure recreate — no code revert, no
redeploy, no scramble to reconstruct a deleted path while the pipeline is down.

**Rollback triggers** — any one is sufficient:

- 2 hung job-service runs (tail > 30s with `STATE = 'FAILED'`) within any 10 runs.
- A row written to `TRANSCRIPTION_RESULTS` that fails the 23-column diff against notebook output,
  or any NULL in `MEETING_TITLE`/`CALL_BRIEF`/`KEY_POINTS`/`NEXT_STEPS` on a file that previously
  produced them.
- `TRANSCRIPTION_RESULTS` count decreasing at any point.
- The dashboard Pipeline Status panel showing `IDLE` during a live run for more than one run
  (instrumentation not ported correctly).
- Compute pool showing phantom long-lived jobs, i.e. the job service has its own exit problem.

**Rollback procedure:**

1. `snow sql` the config flip to `'NOTEBOOK'`, republish via `scripts/publish_config.sh`, recreate
   the procedure. Verify with `SELECT * FROM V_PROJECT_CONFIG` before triggering anything.
2. Confirm the next run launches the notebook and completes.
3. Leave `USER_TASK_TIMEOUT_MS = 1800000` in place regardless of mode — it is the backstop for
   *either* path wedging, and it is what converts an unbounded hang into a bounded failure.
4. Record the failing run's `RUN_ID`, the three duration budgets and the ledger verdict before
   re-running anything. A rollback that discards the evidence guarantees a second attempt with no
   more information than the first.

**Retire `NOTEBOOK` mode only after** 4 consecutive clean 3+ file job-service runs, per the honest
success criterion below. At that point delete the branch, the config value, and the notebook's
headless entry point in one commit.

**Data safety.** Take a zero-copy clone of `TRANSCRIPTION_RESULTS` before the first write from the
job service, matching the existing `TR_BACKUP_GOOD` pattern. It is free and it is the only thing
standing between an unnoticed payload bug and 493 transcripts of irreplaceable history. Note the
port is additive to the table — no schema change — so rollback never requires a data migration,
only that no bad rows were written.

## Verification

**Spike gates (task 1)** — do not proceed unless all pass:

- `which ffmpeg` and `which ffprobe` both resolve; `ffmpeg -version` reports 6.x
- `torch.cuda.is_available()` is True and names a GPU
- OAuth session returns the expected role and a row count matching `TRANSCRIPTION_RESULTS` at the time of the spike (**491 as of 2026-09-24** — read it live rather than asserting a literal, since this number moves with every run. It was 447 on 2026-08-19; five weeks of normal use added 44)
- The mounted AV stage volume lists media files

**Payload parity (task 4):**

- All 23 columns populated on the clone; `SUMMARY_MARKDOWN` and `MEETING_TITLE` non-null
- `PROCESSING_TIME_SECONDS / AUDIO_DURATION_SECONDS` ratio in the historical **0.006-0.090** band (median 0.037, mean 0.037, SD 0.0074 across 447 rows), confirming GPU execution. **Do not use a narrow 0.035-0.055 band** — an earlier draft of this plan did, and it would have failed a legitimate GPU run: the 26-minute Mediaocean file on 2026-08-19 came in at **0.0306**, below that floor. Short recordings run *high* because the Cortex summary is a near-fixed 25-50s cost that dominates; long ones run low. Sanity-check against duration, not a bare threshold.
- Re-running with the file already present inserts nothing

**Instrumentation parity (task 4b):**

- `TRANSCRIPTION_RUN_EVENTS` receives events with `RUN_SOURCE = 'JOB_SERVICE'`
- The run reaches exactly `UNITS_DONE == UNITS_TOTAL` and `PCT_COMPLETE = 100.0`; all four
  per-file steps present for every file
- A terminal `SUCCEEDED` event is emitted \u2014 the notebook could never do this, so its presence is
  the signal that the port genuinely exits
- The dashboard Pipeline Status panel renders the job-service run correctly, and the kickoff
  button is blocked while it is active. **A blank or `IDLE` panel during a live run means the
  instrumentation was not ported** \u2014 that is the specific silent failure this task exists to
  prevent.

**End-to-end (task 6):**

- Task `SUCCEEDED` with a real `RETURN_VALUE`, not `FAILED`
- **Task duration splits into two budgets, measured separately.** The old single "2-4 minutes for an
  8-minute recording" criterion conflated them and is not checkable: container startup is a large,
  variable fraction of wall time, so a slow image pull looks identical to slow transcription.

  | Budget | What it covers | Expectation |
  |---|---|---|
  | **Startup** | task start → first progress event: pool resume, image pull, `pip install`, model load | Notebook baseline is **60-180s**. A job service on a pre-baked image should be **faster**; if deps ship in the image, seconds. |
  | **Work** | first progress event → last progress event | Roughly **2-4 minutes** per 8-minute recording. This is GPU-bound and the port should not change it materially. |
  | **Tail** | last progress event → task end | **Under ~30s.** This is the hang criterion. |

  Startup + work + tail should account for essentially all of task duration. If they do not, there
  is unmeasured time and the measurement is wrong — which is exactly the trap described below.
  Record all three per run, not just the total; a regression in startup and a regression in work
  need different fixes.
- **The gap between the last progress event and the task end is under ~30 seconds.** This replaces the old "no multi-hour tail" criterion, which became **unfalsifiable** once `USER_TASK_TIMEOUT_MS = 1800000` was introduced — a hang can no longer exceed 30 minutes, so "no multi-hour tail" is now satisfied by hung runs too and would give false confidence.

  **Threshold raised from 15s to 30s on 2026-09-24, because 15s produced a false positive.** A 2-file run (`d3076f58`, 09-22) closed in **18s** and the task **SUCCEEDED** — not a hang, but the 15s rule flagged it `HUNG`. An acceptance criterion that fails legitimate runs gets ignored, which is worse than one that is slightly loose. The separation is still ~2 orders of magnitude, so 30s discriminates comfortably. **Better still, require `STATE = 'FAILED'` as well as a large tail** — the two signals together have never disagreed.

  Note this is the gap from the last *progress event*, not the last *transcript write* — those differ by several seconds because the terminal `CELLS_COMPLETE` event fires after the INSERT. Measuring from the write gives ~11s on a clean run. Use one definition consistently; the query below uses the last event.

  **Do not compute the tail as `task_duration - event_span`.** That was tried on 2026-09-24 and gives 126-149s for clean runs, because task duration includes container startup (pool resume, image pull, package install). It is not comparable to the numbers here and will make clean runs look wedged.
- Container exits on its own; no `092848 UNAVAILABLE` and no forced exit involved
- `TRANSCRIPTION_RESULTS` count increments by exactly 1
- Second trigger returns `SKIPPED` in seconds and launches no GPU
- Compute pool history shows one job, not a phantom 2-hour session

**Regression guard:** confirm `TRANSCRIPTION_RESULTS` never drops below its pre-change count at any point. Take a zero-copy clone as a backup before the first write to the real table, matching the `TR_BACKUP_GOOD` pattern already in use.

**Honest success criterion:** the hang is multi-file-specific — **6 of 8** multi-file runs hung, 0 of 8 single-file runs did. A single-file job therefore proves **nothing** about the hang; it sits in the regime that never failed. Validate with **3+ file** runs, and treat the hang as resolved only when **each** run closes its last-event-to-task-end gap in under **~30s with `STATE` not `FAILED`** (not merely "under 30 minutes", which the task timeout guarantees regardless). Given the observed hang/clean/hang sequence within six hours on 2026-08-19, "several" means **at least 4 consecutive clean multi-file runs**, not one or two. Keep the `USER_TASK_TIMEOUT_MS` cap in place until then — it is also the backstop if the job service turns out to have an exit problem of its own.

**Threshold reconciled 2026-09-24.** This paragraph said `~15s` while the criterion above says `~30s`;
30s is correct and 15s produced a documented false positive on a SUCCEEDED 18s run. Use 30s, and
prefer the two-signal form (large tail **and** `STATE = 'FAILED'`), which has never disagreed.

**The `6 of 8` figure is stale and has not been re-measured.** It dates from 2026-08-19. The current
7-day window shows **1 hang in 6 runs**, and that hang was the window's *only* 4-file run — every
1-2 file run passed. So the multi-file-specificity still holds as far as the data goes, but the
*rate* is unquantified: there is no recent multi-file sample large enough to estimate it. This
matters for the acceptance bar. If multi-file runs hang at, say, 1 in 4, then 4 consecutive clean
runs is roughly a 1-in-3 chance of passing by luck. Either re-measure the base rate with several
pre-port multi-file runs, or treat 4 clean runs as necessary-but-not-sufficient and keep the
`NOTEBOOK` rollback path available longer than the criterion strictly requires.


**Measurement query** — use this rather than eyeballing durations, before and after the port. Verified working 2026-08-19:

```sql
-- Gap between the last progress event and the task ending.
-- Target after the port: consistently under ~30s on 3+ file runs.
WITH last_ev AS (
    SELECT RUN_ID, MAX(EVENT_TS) AS LAST_EVENT, MAX(RUN_SOURCE) AS SRC,
           MAX(FILE_TOTAL) AS FILES
    FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RUN_EVENTS
    GROUP BY RUN_ID
)
SELECT LEFT(e.RUN_ID, 8) AS RUN, e.SRC, e.FILES, t.STATE, t.ERROR_CODE,
       DATEDIFF('second', e.LAST_EVENT, t.COMPLETED_TIME) AS TAIL_SECS,
       CASE WHEN DATEDIFF('second', e.LAST_EVENT, t.COMPLETED_TIME) <= 30
            THEN 'clean' ELSE 'HUNG' END AS VERDICT
FROM last_ev e
JOIN TABLE(TRANSCRIPTION_DB_V2.INFORMATION_SCHEMA.TASK_HISTORY(
         TASK_NAME => 'TRANSCRIBE_NEW_FILES_TASK_V2',
         SCHEDULED_TIME_RANGE_START => DATEADD('day', -7, CURRENT_TIMESTAMP()))) t
  ON e.LAST_EVENT BETWEEN t.QUERY_START_TIME AND COALESCE(t.COMPLETED_TIME, CURRENT_TIMESTAMP())
ORDER BY e.LAST_EVENT DESC;
```

### PRE-PORT BASELINE — CAPTURED 2026-09-24, DO NOT RE-DERIVE

`INFORMATION_SCHEMA.TASK_HISTORY` retains **7 days**, so this table is the durable record. The
hung run below ages out of `TASK_HISTORY` around **2026-09-28**; after that it cannot be
reconstructed. `TRANSCRIPTION_RUN_EVENTS` persists, but it holds no task state or outcome.

| RUN | FILES | STATE | ERROR | TAIL_SECS | VERDICT |
|---|---|---|---|---|---|
| `4cbcba83` | 2 | SUCCEEDED | | **3** | clean |
| `cdd2538a` | 2 | SUCCEEDED | | **4** | clean |
| `d3076f58` | 2 | SUCCEEDED | | **18** | clean (flagged by the old 15s rule) |
| `894c49f1` | 1 | SUCCEEDED | | **3** | clean |
| `906118df` | **4** | **FAILED** | 000630 | **1279** | **HUNG** |
| `bc1788c6` | 3 | SUCCEEDED | | **4** | clean |

Clean tails cluster at **3-4s**, consistent with the 4s/5s measured on 2026-08-19. The hung
tail of **1,279s** is consistent with the 1,339s measured then. **Post-port target: every 3+
file run under 30s.**

## Critical files

- notebooks/audio\_video\_transcription.ipynb - source of the payload logic; cell 19 ports near-verbatim, cell 28 holds the authoritative 23-column INSERT, cell 5 holds the `RunProgress` class that must port with it
- scripts/03\_automate.sql - gate procedure and task definition; where `EXECUTE NOTEBOOK` becomes `EXECUTE JOB SERVICE`
- scripts/00\_config.sql - single source of truth for all object names; new job/image variables go here and nowhere else
- scripts/04\_deploy\_notebook.sh - the deploy-then-verify pattern the new payload deploy script should mirror
- av.uploader/upload\_av\_files.py - trigger path; should require no changes, which is itself worth verifying
