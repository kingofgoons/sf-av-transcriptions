---
name: "diagnose-notebook-hang-first"
created: "2026-08-19T15:39:54.480Z"
status: pending
---

# Diagnose the notebook hang with real evidence before porting anything

> STATUS: PAUSED MID-EXECUTION on 2026-08-18 \~17:41 EDT. Read "RESUME HERE" first. There was a run IN FLIGHT when this was written, and there is UNRESTORED STATE (3 deleted transcript rows and a lowered task timeout). Do not skip the safety checks in "RESUME HERE".

---

## RESUME HERE

### 1. Safety first: are the 3 deleted transcripts back?

To force a multi-file run, 3 real transcript rows were DELETED. They are backed up in `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TR_MULTIFILE_BACKUP` (verified: 3 rows, all with TRANSCRIPT + SUMMARY\_MARKDOWN + SRT\_CONTENT, 8,585.2s total audio):

- `2026-08-17 13-03-40_internal_Bo.Adam_1-1.mp4`
- `2026-08-17 14-36-28_Alvarez.and.Marsal_dbt.discussion.mp4`
- `2026-08-18 09-35-22_Mediaocean_Innovid_summit.in.60.for.just.Lolo.mp4`

The in-flight run should have regenerated them. Check:

```sql
SELECT COUNT(*) AS TOTAL FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
-- Expected 443 = 442 baseline - 3 deleted + 3 regenerated + 1 new Moodys file

SELECT FILE_NAME, LENGTH(TRANSCRIPT) AS T, LENGTH(SUMMARY_MARKDOWN) AS S
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
WHERE FILE_NAME IN (
    '2026-08-17 13-03-40_internal_Bo.Adam_1-1.mp4',
    '2026-08-17 14-36-28_Alvarez.and.Marsal_dbt.discussion.mp4',
    '2026-08-18 09-35-22_Mediaocean_Innovid_summit.in.60.for.just.Lolo.mp4');
```

If any of the 3 are MISSING, restore them immediately:

```sql
INSERT INTO TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
SELECT * FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TR_MULTIFILE_BACKUP
WHERE FILE_NAME NOT IN (
    SELECT FILE_NAME FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS);
```

### 2. What did the in-flight run do?

Run started 2026-08-18 17:39:00 EDT, 4 files (the 3 above + the newly uploaded `2026-08-18 14-42-35_Moodys_Streamlit.PMs.mp4`, 85.6 MB). At pause time it was `EXECUTING` at 96s. Expected legitimate work is roughly 7-10 minutes.

```sql
SELECT SCHEDULED_TIME, STATE,
       DATEDIFF('second', QUERY_START_TIME, COALESCE(COMPLETED_TIME, CURRENT_TIMESTAMP())) AS SECS,
       ERROR_CODE, ERROR_MESSAGE, RETURN_VALUE
FROM TABLE(TRANSCRIPTION_DB_V2.INFORMATION_SCHEMA.TASK_HISTORY(
    TASK_NAME => 'TRANSCRIBE_NEW_FILES_TASK_V2',
    SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP())))
ORDER BY SCHEDULED_TIME DESC LIMIT 5;
```

Interpretation:

- **SUCCEEDED in roughly 400-700s** -> no hang on this attempt. 4-file runs hung 4 of 6 times historically, so this is expected sometimes. Go to step 4 and try again.
- **SUCCEEDED/FAILED at approximately 1800s** -> it hit the 30-minute timeout cap, which means IT HUNG. **This is the case we want.** Go to step 3 and read the stacks.

### 3. THE PAYOFF: read the stack dumps

This is the entire point of the exercise. Query 9 was added to scripts/08\_telemetry\_debug.sql, or run directly:

```sql
SELECT TIMESTAMP, VALUE::STRING AS LOG_MSG
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RECORD_TYPE = 'LOG'
  AND RESOURCE_ATTRIBUTES:"snow.executable.name"::STRING = 'TRANSCRIBE_AV_FILES_V2'
  AND TIMESTAMP > DATEADD('hour', -6, SYSTIMESTAMP())
  AND (VALUE::STRING ILIKE '%FORENSICS%'
       OR VALUE::STRING ILIKE '%Timeout (0:%'
       OR VALUE::STRING ILIKE '%Thread 0x%'
       OR VALUE::STRING ILIKE '%  File "%'
       OR VALUE::STRING ILIKE '%Current thread%')
ORDER BY TIMESTAMP;
```

A hung run produces a dump every 120s. Compare against the healthy-run baseline in "Baseline (healthy run)" below and decide:

- Frame in OUR code, Snowpark, or the connector -> fixable here. No port needed.
- Frame in `snowbook`, streamlit, IPython, or asyncio internals -> platform-side. Resume port-transcription-to-job-service.plan.md and file a Snowflake support case with the stacks attached.
- Confirmed hang but NO dumps at all -> wedged below the Python level. Support case.

### 4. If you need another attempt

Multi-file is mandatory (see "Key finding" below). Re-arm by deleting rows for files already in the stage; no upload needed. Back up first, always:

```sql
CREATE OR REPLACE TABLE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TR_MULTIFILE_BACKUP AS
SELECT * FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS
WHERE FILE_NAME IN ( ...the 3 names above... );

-- verify 3 rows with content, THEN delete, THEN:
EXECUTE TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2;
```

### 5. Cleanup when the investigation ends

```sql
-- Restore the production timeout (currently capped at 30 min for diagnostics)
ALTER TASK TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIBE_NEW_FILES_TASK_V2
  SET USER_TASK_TIMEOUT_MS = 14400000;

-- Drop scratch backups ONLY after confirming all transcripts are present
DROP TABLE IF EXISTS TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TR_MULTIFILE_BACKUP;
DROP TABLE IF EXISTS TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TR_HANGTEST_BACKUP;
```

`TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_GOOD` (441-row zero-copy clone) stays as the standing backup. `HANG_FORENSICS` can stay `True` permanently - it is inert on healthy runs. Record conclusions in `DIARY.md` (gitignored, newest entry first).

---

## Key finding this session: the hang is MULTI-FILE ONLY

This invalidated the first four test attempts, which all used a single file.

| Run                         | Files | Audio   | Result      |
| --------------------------- | ----- | ------- | ----------- |
| Aug 4 11:32                 | 6     | 25,551s | HUNG 8,196s |
| Aug 4 13:52                 | 3     | 8,822s  | HUNG 8,076s |
| Aug 7 14:52                 | 6     | 10,992s | clean 618s  |
| Aug 7 15:07                 | 4     | 7,220s  | HUNG 8,047s |
| Aug 17 12:47                | 10    | 18,202s | clean 978s  |
| Aug 18 10:37                | 3     | 8,585s  | HUNG 8,111s |
| 8 separate single-file runs | 1     | various | ALL clean   |

**0 of 8 single-file runs hung. 4 of 6 multi-file runs hung.** Any future test MUST use at least 3 files to sit in the failing regime. Credit for this goes to Bo, who challenged the single-file test design.

A corollary correction: after four clean single-file runs I suggested the teardown cleanup might have fixed the hang. That was unsupported - those runs were in the regime that never hangs.

## Baseline (healthy run, 2026-08-18 21:23 UTC)

`faulthandler` needs a REAL file descriptor. Snowflake notebooks replace `sys.stderr` with a capture proxy that has no `fileno()`, so the first deploy failed with `could not arm hang forensics: fileno`. Fixed by resolving `sys.__stderr__` (fd 2), falling back to `os.dup(2)`. Raw fd 2 does reach the event table - the interpreter's own `resource_tracker` warning arrives that way.

Healthy-run threads (12 alive, 2 non-daemon):

```
Current thread            -> Cell [None] line 176 (the teardown cell itself)
                             snowbook/executor/compiler_utils.py:201 _do_exec_and_eval
Thread 0x...714e06c0      -> tqdm/_monitor.py:60 in run            (daemon)
Thread 0x...6effd6c0      -> IPython/core/history.py:903 in run    (daemon)
Thread 0x...6f7fe6c0      -> threading.py:1116 _wait_for_tstate_lock
                             threading.py:1096 join
                             snowbook/executor/compiler_utils.py:227 join_if_started
                             snowbook/executor/compiler_utils.py:187 exec_or_eval_with_thread
                             snowbook/executor/notebook_compiler.py:554 compile_and_run_notebook
Multiprocessing children: 0
Max RSS: 1,903,280 KB
```

**Leading mechanism hypothesis.** Snowflake's notebook executor runs each cell in its own thread and then `join()`s it (`join_if_started` -> `_wait_for_tstate_lock`). On a healthy run that join returns. If a cell thread never releases its tstate lock, that join blocks forever - matching an \~8,100s fixed-timeout hang with GPU memory pinned at 929 MB and 0% utilisation. Multi-file runs do N rounds of CUDA work, N ffmpeg subprocesses, and N Cortex calls, which fits the multi-file-only correlation. The hung stacks will confirm or kill this.

Also established: `resource_tracker: leaked semaphore` appears on healthy runs too, so it is a red herring, and there are ZERO lingering multiprocessing children, so the original whisper/torch-worker theory is dead.

## Current environment state

| Item                   | Value                                                              |
| ---------------------- | ------------------------------------------------------------------ |
| Notebook live version  | 36 cells, verified byte-identical to local, owner SYSADMIN         |
| `HANG_FORENSICS`       | True, 120s interval, armed and confirmed working                   |
| `FORCE_KERNEL_EXIT`    | False (reverted; forced exit made the task report FAILED)          |
| `USER_TASK_TIMEOUT_MS` | **1800000 (30 min) - TEMPORARY, restore to 14400000**              |
| Transcripts            | 439 at pause (442 - 3 deleted); expect 443 after the in-flight run |
| Stage files            | 327 (326 + the new Moodys upload)                                  |
| Scratch backups        | `TR_MULTIFILE_BACKUP` (3 rows), `TR_HANGTEST_BACKUP` (1 row)       |
| Standing backup        | `TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_GOOD` (441 rows)            |

Uncommitted git changes: instrumented notebook, rewritten `scripts/04_deploy_notebook.sh`, new Query 9 in `scripts/08_telemetry_debug.sql`, plus the earlier session's config refactor. Nothing has been committed.

Run the uploader from `av.uploader/` with **Python 3.9+** (use `/Users/blandsman/anaconda3/bin/python3`, which is 3.10). The `pysnowpark` env is Python 3.8, where `__file__` is relative and the key lookup fails.

## Decision gate

| Stacks show                                | Action                                                                                                                                   |
| ------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Our code / Snowpark / connector            | Fix directly. No port.                                                                                                                   |
| `snowbook` / streamlit / IPython / asyncio | Resume the port plan + support case                                                                                                      |
| Confirmed hang, no dumps                   | Support case; wedged below Python                                                                                                        |
| 4+ more multi-file runs all clean          | Reconsider: the earlier teardown cleanup may genuinely have mitigated it. Keep the timeout cap and let normal usage accumulate evidence. |

Do not treat the hang as fixed on the strength of a few clean runs. It was intermittent at roughly 2/3 of multi-file runs, so several consecutive clean multi-file runs are needed before declaring it resolved.
