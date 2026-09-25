## Goal

Replace `EXECUTE NOTEBOOK` with a **synchronous** `EXECUTE JOB SERVICE` inside
`TRANSCRIBE_IF_NEW_FILES()`, so the pipeline runs the payload validated in Tiers 2–4 (4/4 clean,
5s tail) instead of the notebook that hangs 1 run in 2.

Task 2 already proved this is the smallest possible change: `EXECUTE JOB SERVICE` **is** permitted
in an `EXECUTE AS OWNER` procedure, so the task-body refactor and the extra compute-pool grants the
plan feared are not needed. The security model does not change.

Launch mode is **synchronous** (decided): the task stays `EXECUTING` for the run, which is what
`STARTING` and `HUNG` detection in `sf_pipeline.py` depend on. This matches `EXECUTE NOTEBOOK`'s
current behavior, so operational feel is unchanged. It costs ~3–6 min of warehouse hold per run
(~0.2 credits/day at current volume) and leaves runs bounded by `USER_TASK_TIMEOUT_MS = 30 min`.

---

## 0. Resolve two blockers first (cheap, and both can invalidate design below)

**BOTH RESOLVED.** Outcomes below; they simplify tasks 4 and 5 rather than complicating them.

**0a. Job-service name reuse — REJECTED, use drop-then-create.**
`EXECUTE JOB SERVICE NAME = X` against an existing `DONE` job fails with
`Object '...SPIKE_PROC_JOB_02' already exists`. `DROP SERVICE IF EXISTS` then re-launching the same
name succeeds, and both statements work inside the `EXECUTE AS OWNER` procedure.

Chosen: **one stable `PROJECT_JOB_NAME`, dropped immediately before launch.** Generated names would
avoid the drop but nothing would ever clean them up - a handful of runs a day is ~1500 job objects a
year cluttering `SHOW SERVICES`. The durable audit trail is `TRANSCRIPTION_RUN_EVENTS`, not job
metadata, and the one job object that survives is always the most recent, which is the one worth
debugging.

**Footgun this creates, and the guard for it.** A manual `CALL TRANSCRIBE_IF_NEW_FILES()` while a run
is in flight would `DROP` that run's service and kill it mid-transcription. The task itself cannot
overlap, so only the manual path is exposed - and `agents.md` currently *recommends* that manual
call (see task 6). Guard by refusing to launch when a run is genuinely active:

```sql
SELECT COUNT(*) INTO :active FROM <FQ_RUN_STATUS_VIEW>
WHERE IS_ACTIVE AND SECONDS_SINCE_HEARTBEAT < <PROJECT_RUN_STALE_SECS>;
```

This is a plain `SELECT` (permitted), and it reuses the exact signal the dashboard uses, so the gate
and the UI cannot disagree - the same principle `get_backlog()` already follows. The heartbeat bound
matters: keying on `IS_ACTIVE` alone would let a run that died without emitting a terminal event
block the gate permanently.

**0b. Task timeout does NOT orphan the job — no teardown machinery needed.**
`SPIKE_TIMEOUT_TASK` (`USER_TASK_TIMEOUT_MS = 90000`) called a procedure launching a 300s job
synchronously. The task `FAILED` at 91s with `000630 Statement reached its statement or warehouse
timeout of 90 second(s) and was canceled`, and the job service was **removed entirely**: absent from
`SHOW SERVICES`, pool back to `num_jobs = 0` / `active_nodes = 0`. Cancelling the task tears down the
job with it. The finalizer/teardown design this task held in reserve is unnecessary.

**Separate exposure the test surfaced, which is NOT introduced by this port.** `persist()` is called
once *after* the per-file loop (`transcribe_job.py:759`, loop at `:724`), so a run is all-or-nothing:
a timeout mid-run discards every transcript from that run. At ~65s/file, `USER_TASK_TIMEOUT_MS =
30 min` caps a batch at roughly 25 files. Event-driven batches are small, so this is latent rather
than urgent, and `EXECUTE NOTEBOOK` behaves the same way today - but a first-ever bulk run over the
385-file stage would burn GPU time and write nothing. Per-file persist would fix it. Out of scope
here; record it as a follow-up.

**0a. Job-service name reuse.** Tier 4 used a distinct name per run (`TIER4_RUN_01`…`04`). A
production gate reuses one name. Test whether `EXECUTE JOB SERVICE NAME = X` succeeds when `X`
already exists in `DONE` state.

(Superseded by the resolutions above; retained for the reasoning that framed the tests.)

---

## 1. Config additions (`scripts/00_config.sql.template`)

Add after the compute-pool block, with a comment block explaining the launch-mode switch:

```sql
SET PROJECT_STAGE_PAYLOAD = 'PAYLOAD_STAGE';
SET PROJECT_JOB_NAME      = 'TRANSCRIBE_JOB';
SET PROJECT_JOB_SPEC_FILE = 'transcribe_job_spec.yaml';
SET PROJECT_JOB_IMAGE     = '/snowflake/images/snowflake_images/container_runtime/gpu_x86_64:2.9.0';
SET PROJECT_LAUNCH_MODE   = 'JOB_SERVICE';   -- or 'NOTEBOOK' to roll back
```

Plus `SET FQ_STAGE_PAYLOAD = $FQ_SCHEMA || '.' || $PROJECT_STAGE_PAYLOAD;` in the derived block, and
all five names added to the `V_PROJECT_CONFIG` emitter.

**Image tag must stay pinned to `:9.0`-style full tags.** Bare `:2.9` does not resolve and fails
instantly, provisioning nothing.

Then: `scripts/config_revision.sh` to bump `CONFIG_REVISION`, `scripts/publish_config.sh` to stage
it, and add the five names to `ADDED_SINCE_EXTRACTION` in `tests/test_config.py` so
`test_template_matches_preextract_fixture` stays a real guard rather than being loosened.

## 2. Fix the `PAYLOAD_STAGE` drift (`scripts/02_setup.sql`)

`PAYLOAD_STAGE` exists on the account but appears **nowhere in code** — I created it ad hoc during
the spike, which violates the "Deployed state must match code (ENFORCED)" rule in `agents.md`. Add:

```sql
CREATE STAGE IF NOT EXISTS IDENTIFIER($PROJECT_STAGE_PAYLOAD);
```

No `DIRECTORY=(ENABLE=true)` — `SPECIFICATION_FILE` does not need a directory table, and the live
stage does not have one. Code should match what is deployed.

**Grant check, easy to miss:** the live stage is owned by ACCOUNTADMIN, but the gate procedure runs
as **SYSADMIN**. SYSADMIN needs `READ` on the stage to launch `FROM @PAYLOAD_STAGE`. Add the grant
and verify with `SHOW GRANTS ON STAGE`; do not assume ownership implies access.

Add a `PAYLOAD_STAGE` existence/grant row to `scripts/09_drift_check.sql`.

## 3. Author and deploy the service spec

**New `scripts/payload/transcribe_job_spec.yaml.template`** — rendered, not hardcoded, so the
DB/schema/table/image values come from config rather than drifting from it. Contents mirror the
spec validated in Tier 4, which is the one known-good configuration:

- `resources.requests`/`limits: nvidia.com/gpu: 1` — **mandatory**. Omitting it left the spike
  `PENDING` forever with only a `WARN` in `SYSTEM$GET_SERVICE_STATUS`: no error, no timeout. A spec
  missing this line is indistinguishable from a hang.
- env: `PROJECT_DB`, `PROJECT_SCHEMA`, `PROJECT_WH`, `PROJECT_STAGE_AV`, `PROJECT_RESULTS_TABLE`,
  `PROJECT_RUN_EVENTS_TABLE`, `WHISPER_MODEL`
- `volumeMounts` + `volumes` pointing at `@PAYLOAD_STAGE`
- command: `uv pip install --system --break-system-packages --quiet openai-whisper` then
  `python transcribe_job.py`. **`pip install` fails with PEP 668** in this image; `uv` is required.
- **No colons in any command string.** `- echo "spike: text"` fails YAML deserialization at
  `command[2]` because `: ` makes it a mapping. This would break at launch, after the gate has
  already decided to run.

**New `scripts/05_deploy_payload.sh`**, modeled on `04_deploy_notebook.sh`, which uploads
`transcribe_job.py`, `transcribe_functions.py` (the payload does `import transcribe_functions as tf`)
and the rendered spec, then **verifies by downloading them back and comparing content**.

This exists because of a failure I caused: Tier 3 run 1 ran a **stale payload**. I fixed
`FILE_PATH` locally after uploading and never re-uploaded, so the column came back empty while the
run looked successful. Notes for the script:

- Compare **md5/content, not byte size** — stage `size` includes encryption padding (36,958 local
  → 36,960 staged), so exact-size equality will produce false failures.
- `REMOVE @PAYLOAD_STAGE/__pycache__/` — stale bytecode can shadow edited source.
- A successful `PUT` is **not** proof of deployment.

## 4. Rewire the gate procedure (`scripts/03_automate.sql`)

The procedure text is already built as a VARCHAR in an anonymous block, so branch on
`$PROJECT_LAUNCH_MODE` **at deploy time** — the procedure itself stays simple, with no runtime
branching. Replace the `EXECUTE NOTEBOOK` line with either that statement or:

```sql
EXECUTE JOB SERVICE
  IN COMPUTE POOL <pool>
  NAME = <job name>
  QUERY_WAREHOUSE = <wh>
  EXTERNAL_ACCESS_INTEGRATIONS = (<pypi>, <allow_all>)
  FROM @<FQ_STAGE_PAYLOAD>
  SPECIFICATION_FILE = '<spec>';
```

Note `EXTERNAL_ACCESS_INTEGRATIONS` is required on the statement for the `uv pip install` to reach
PyPI, and `SPECIFICATION_FILE` (not inline `$$`) is required because a procedure body is itself a
quoted string and Snowflake supports only `$$` as a dollar-quote tag.

Also in this file:

- Make the `SKIPPED` message mode-aware — it currently hardcodes "GPU notebook not launched".
- Keep the deliberate absence of an `EXCEPTION` handler around the launch. Wrapping it is what
  previously made a 2h15m failure report `SUCCEEDED`.
- Re-running this script **resets ownership and drops the uploader's `OPERATE` grant**. Redo Step 4
  and verify the grant survived.

## 5. Fix documented falsehoods (task 4d + the CALL claim)

Three stale dashboard strings, now finally testable because a task-backed launch will exist:

- `sf_pipeline.py:176-178` — `STARTING` blurb claims "the notebook installs Whisper... roughly
  60-180s". Measured: **16–18s warm, 112s cold**. Wrong in both directions.
- `sf_pipeline.py:183` and `sf_theme.py:179` — `HUNG` blurb attributes the state to the "known
  snowbook shutdown hang". Post-port that state means a **new** exit fault, not a benign one.
- `sf_pipeline.py:144`, `:338`, `:41` — same claims in comments.

Separately, a wrong claim in three places: `agents.md:31`, `agents.md:202`, and
`scripts/03_automate.sql:44` all say `CALL TRANSCRIBE_IF_NEW_FILES()` shows the verdict *without
launching a container*. **It launches when work exists.** Anyone following that note to "safely
check" will start a GPU run.

Also fix the latent `SHOW ... LIKE $VAR` bug at `scripts/07_reset.sql:58` — invalid SQL, same class
of bug already fixed in the drift checker.

## 6. Tier 5 — the test that closes both remaining gaps

**This is the tier that finally exercises the Streamlit app**, which has never been tested:
Tier 3 step 1 said "upload via the dashboard's uploader" and I substituted `snow sql PUT` without
flagging it. The dashboard uses **`put_stream`** (in-memory bytes from the browser), not `PUT`
(client filesystem) — plus `auto_compress=False` and a follow-up `ALTER STAGE REFRESH`. None of that
has been exercised.

Use **TEST13** (TEST10/11 held in reserve; `overwrite=False` means already-staged files only report
a collision, so these three are the only valid candidates). 32–45 MB against `MAX_UPLOAD_MB = 200`.

| Step | Action | Expected | Why it matters |
|---|---|---|---|
| 5a | `CALL TRANSCRIBE_IF_NEW_FILES()` with empty backlog | `SKIPPED`, pool stays `IDLE`, `num_jobs = 0` | Skip path must not start a container |
| 5b | **Drag-and-drop TEST13** into the dashboard | Upload succeeds; backlog → 1 | First-ever test of `put_stream` |
| 5c | Observe the button | **Enables** — never seen enabled; it has been blocked by `n_backlog == 0` all session | |
| 5d | Click **Start transcription** | `STARTING` appears with a *correct* blurb | First test of the ported launch + task 4d |
| 5e | Watch the run | `RUNNING` → advancing % → `SUCCEEDED` | |
| 5f | Verify data | Production +1, 23 columns sane, `FILE_PATH` populated, `SRT_SEGS == TWS_SEGS` | |
| 5g | Verify tail | Last-event-to-task-end ≤ 30s, no `WORK_COMPLETE_NOT_EXITED` | |

**5h. Rollback rehearsal.** Set `PROJECT_LAUNCH_MODE = 'NOTEBOOK'`, republish, re-run
`03_automate.sql`, confirm the notebook path launches again, then switch back. A rollback switch
that has never been exercised is not a rollback switch.

Measurement traps to avoid, both of which nearly produced wrong answers in Tier 4:

- `INFORMATION_SCHEMA.QUERY_HISTORY(RESULT_LIMIT => N)` **silently truncates** — at 400 it reported
  2 of 4 runs as though the others did not exist.
- `DIRECTORY()` is **stale after `PUT`** — it reported 383 files and missed 4 test files until
  `ALTER STAGE REFRESH` registered exactly those 4. Use `LIST`, as `discover()` does.

## 7. Cleanup and the commit gate

**TIER 5 PASSED — first end-to-end run through the real path.** Dashboard drag-and-drop of
TEST10 (33 MB, 36.5 min audio) -> upload -> kickoff -> job service -> `SUCCEEDED`.

| Measurement | Result |
|---|---|
| Task `RETURN_VALUE` | `LAUNCHED: job service run for 1 new file(s).` |
| Task state / duration | `SUCCEEDED` / 269s |
| Startup (COLD pool) | 116s |
| Work | 146s |
| **Tail** | **7s** (threshold 30s) |
| Row | production 503 -> 504, `FILE_PATH` populated, all 6 summary fields, `SRT_SEGS == TWS_SEGS` = 351 |
| Pool after | `IDLE`, `num_jobs = 0` |

What this run proved that no earlier tier could, because all of it needed a task-backed launch
that did not exist until task 5 landed:

- **`put_stream` works.** Every earlier test used CLI `PUT`, which is a different code path
  (client filesystem vs in-memory bytes from the browser). The dashboard uploader had never once
  been exercised - Tier 3 step 1 specified it and I silently substituted `PUT`.
- **The kickoff button works in its ENABLED state.** Previously only ever observed disabled.
- **`STARTING` is reachable and its text is now true.** The 116s cold start falls inside the
  rewritten "around 2 minutes if the pool has to resume from cold", which is what makes that a
  correction rather than merely a different wrong number.

**Rollback rehearsed (5h), both directions.** `PROJECT_LAUNCH_MODE = 'NOTEBOOK'` -> publish ->
rebuild produced `EXECUTE NOTEBOOK` with the job-service path absent and the mode-aware `SKIPPED`
message correct; flipping back restored `JOB_SERVICE`. Verified across the round trip:

- The in-flight `BLOCKED` guard is retained in **both** modes.
- Procedure ownership stayed SYSADMIN through both rebuilds.
- Config revision returned to **`e04848081551`**, byte-identical to pre-rollback - which also
  independently confirms the revision hash is deterministic.

Cumulative tail evidence: **5s on all four Tier 4 runs, 7s on Tier 5** - five consecutive clean
exits, against a pre-port baseline of 1 hang in 2 notebook runs (145s+ tail on the hung one).

### Remaining before commit

**COMPLETE — committed as `c055726` (not pushed; `origin/main` remains at `24f657c`).**

1. Drift check — **10/10 OK**, including the two new `PAYLOAD_STAGE` rows.
2. Test suite — **172 passing** (155 before; +17 from `tests/test_av_uploader.py`).
3. Cleanup executed in two rounds (11 transcripts / 226 event rows, then 3 / 32), each
   backed up first. **No extension was needed** after all: the existing mixed-run predicate
   already matched, because every test run processed only `_TEST##` files. The clone was
   dropped wholesale rather than filtered.
4. Stage `REMOVE` done — 11 files, then 3.
5. **Partially done by decision.** Dropped: `TRANSCRIPTION_RESULTS_PORTTEST`,
   `SPIKE_JOB_IN_PROC()`, both spike specs, and all 13 spike/tier job services.
   **Retained at the operator's request:** `TR_PREPORT_BACKUP`, `TR_PRECLEANUP_BACKUP`,
   `TR_HANGTEST_BACKUP`, `TR_MULTIFILE_BACKUP` — to be dropped in a follow-up once the
   commit is trusted.
6. Production **493**, with 493 distinct filenames and gate backlog **0**. Stage holds
   **377** media files, zero `_TEST##`, confirmed by both `LIST` and `DIRECTORY()`.
   **Note the plan's "385" expectation could not be reconciled and should not be treated
   as verified.** That figure came from session notes, and `DIRECTORY()` was separately
   demonstrated to understate this stage by 4 files, so the baseline itself may have been a
   stale reading. What IS verified: every `REMOVE` used a `_TEST##` pattern and named only
   `_TEST##` files, backlog is 0 so every staged file has a transcript, and the transcript
   count matches the pre-test baseline exactly. No real media file was removed.
7. Committed, with `DIARY.md` 2026-09-25 covering the hang measurement, the spike findings,
   the stale-payload failure, and the documentation corrections.

### Work added beyond the original plan

`av.uploader/` was never in scope, and should have been: `upload_av_files.py` is the
primary production trigger whose whole job is firing the task this plan rewired, and
`download_srts.py` consumes a column the payload writes. Neither had a single test.

- Exercised the uploader end-to-end for real: 1 file (tail 9s) and a 2-file batch
  (12/12 units, file counter advancing, 2 records written).
- Ran `download_srts.py` against payload-written rows and confirmed the downloaded `.srt`
  is **byte-identical (sha256)** to the stored `SRT_CONTENT`.
- `tests/test_av_uploader.py` (17 tests) pins the writer/reader contract, because the SRT
  generators exist **twice, independently implemented** — in the payload and in
  `download_srts.py`. Guard proven by patching one side and watching it fail.
- `upload_av_files.py` now tolerates absent stdin at the Gong prompt, which is reached
  AFTER the upload and trigger succeed and made non-interactive runs look hung.

### Open items

- **Drop the four retained backup tables** when ready.
- **Push** — the commit is local only.
- **`__pycache__` reappears on `@PAYLOAD_STAGE` after every run**, written back by the
  container through the volume mount. Harmless given the deploy ordering
  (`REMOVE __pycache__` precedes the source upload, so the next run compiles fresh), but
  worth knowing before anyone hand-edits a staged `.py` and wonders why behaviour lags.
- The three out-of-scope items below, unchanged.


## Explicitly out of scope

- **Deleting the notebook.** It stays as the rollback target until the job-service path has real
  production history.
- **Metadata backfill** (`scripts/backfill_metadata.py`) — still awaiting the
  `--strict` / all-220 / `--columns` decision. Independent of this work.
- **A `--pattern` argument for the payload.** Tier 4 showed its absence makes that tier awkward to
  repeat safely — `--force-retranscribe --limit 3` would transcribe real meeting files. Worth doing,
  but it is a new feature and should not ride along in the port commit.
