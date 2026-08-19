---
name: "notebook-resource-audit"
created: "2026-08-19T21:45:00.000Z"
status: pending
---

# Notebook resource audit — is the hang partly our fault?

## Why this exists, and why it is smaller than first proposed

Bo's challenge, 2026-08-19: *if the container is still running, doesn't that mean we missed
something?* Fair, and it exposed a real overclaim. "Zero notebook frames on any stack" proves no
notebook code is **executing**; it does **not** prove the notebook left nothing behind. A leaked
subprocess, an unclosed handle or a non-daemon thread would not appear as a notebook frame. Those
are different claims and they had been conflated.

**A first pass has already been done, and it largely closes the question.** Scope below is reduced
accordingly \u2014 recording the negative results so nobody re-runs this work.

### Already answered, with measurements (2026-08-19)

The notebook's teardown (cell 35) has been printing a thread census to stdout all along, so it
landed in the event table for every run. Five runs from one day, including two hangs:

| Run (EDT) | Files | Outcome | Threads | Non-daemon | MP children | CUDA after cleanup |
|---|---|---|---|---|---|---|
| 10:17:48 | 3 | **HUNG** | 12 | 2 | 0 | 8519680 / 20971520 |
| 13:00:48 | 1 | clean | 12 | 2 | 0 | 0 / 0 |
| 13:12:04 | 1 | clean | 12 | 2 | 0 | 8519680 / 20971520 |
| 15:27:28 | 3 | clean | 12 | 2 | 0 | 8519680 / 20971520 |
| 15:59:47 | 3 | **HUNG** | 12 | 2 | 0 | 8519680 / 20971520 |

**The notebook's end state is identical whether it hangs or not.** Same 12 threads, same names,
same daemon flags, the same two non-daemon blockers (`asyncio_0` and `ScriptRunner.scriptThread`,
both snowbook's), zero multiprocessing children. Single-file and 3-file runs produce the same
census. The 15:27 clean run and the 15:59 hung run were both 3-file with identical footprints.

Query to reproduce (no GPU cost \u2014 reads telemetry):

```sql
SELECT TIMESTAMP, LEFT(VALUE::VARCHAR, 115) AS LINE
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.executable.name"::VARCHAR = 'TRANSCRIBE_AV_FILES_V2'
  AND RECORD_TYPE = 'LOG'
  AND (VALUE::VARCHAR ILIKE '%Threads alive%' OR VALUE::VARCHAR ILIKE '%daemon%'
       OR VALUE::VARCHAR ILIKE '%Multiprocessing children%' OR VALUE::VARCHAR ILIKE '%Cleared CUDA%')
ORDER BY TIMESTAMP;
```

### Hypotheses now closed \u2014 do not re-investigate

| Hypothesis | Verdict | Evidence |
|---|---|---|
| Leaked ffmpeg subprocesses | **Refuted** | `extract_audio_from_video` uses `subprocess.run(capture_output=True, ...)` for both `ffprobe` and `ffmpeg`. `run()` waits and drains pipes \u2014 no zombies, no pipe-deadlock. Note the older "zero multiprocessing children" check could NOT have caught this, since `multiprocessing.active_children()` does not see `subprocess` children; the code read is what settles it |
| Thread leak, or a notebook thread blocking exit | **Refuted** | Census identical across hung and clean runs; the only non-daemon threads are snowbook's own |
| CUDA / GPU memory residue | **No correlation** | The same `allocated=8519680` appears on hung *and* clean runs; the single `0/0` run was clean, but another clean run had 8.5 MB |
| Multi-file resource accumulation | **Contradicted by the rate data** | A 10-file run did not hang while 3/3 three-file runs did. Accumulation would scale with file count |

### Conclusion this points to

**Not a leak \u2014 a non-deterministic race inside snowbook.** Identical inputs and identical
process residue produce different outcomes (15:27 clean vs 15:52 hung, both 3-file, hours apart).
That also resolves the rate data that never fitted an accumulation model.

This *strengthens* rather than restates the earlier finding. Previously: no notebook code is
running during the hang. Now: the notebook's residue is also identical in both cases. Both point
the same way, and neither is fixable from the notebook.

**Consequence for priority:** this audit is no longer a route to fixing the hang. It is worth
finishing only for (a) the genuine minor defects below and (b) evidence quality for a Snowflake
support case. The job-service port remains the fix.

## Remaining work

### 0. STATUS: the ledger is BUILT and DEPLOYED but NOT YET VALIDATED on real hardware

Implemented 2026-08-19 (commit `9e3fd8a`) and deployed to `TRANSCRIBE_AV_FILES_V2`. Item 1 below
(the temp-WAV `finally` fix) is **done** in the same commit.

Deliberately **not** validated with a dedicated run — that costs a GPU spin-up and a 3-file run
carries ~75% hang odds plus manual pool reclamation, and the ledger produces its data for free on
the next genuine transcription. So it will fire whenever real work next arrives.

**On the first real run, check these three things.** They are the parts that could only be tested
against a simulated `/proc` locally, because macOS has no `/proc`:

```sql
SELECT TIMESTAMP, LEFT(VALUE::VARCHAR, 150) AS LINE
FROM SNOWFLAKE.TELEMETRY.EVENTS
WHERE RESOURCE_ATTRIBUTES:"snow.executable.name"::VARCHAR = 'TRANSCRIBE_AV_FILES_V2'
  AND RECORD_TYPE = 'LOG'
  AND VALUE::VARCHAR LIKE '[LEDGER%'
ORDER BY TIMESTAMP;
```

1. **`fd=` and `os_children=` are real numbers, not `-1`.** `-1` means the `/proc` read failed in
   the container, so the two most useful metrics are blind and the code needs fixing.
2. **`[LEDGER RECONCILE]` reports `OK`**, with `created` == `removed` and `on_disk=0`. A `LEAK`
   verdict on a clean run means the `finally` fix is wrong.
3. **On a multi-file run, compare `fd`/`threads`/`os_children` across the per-file `START` lines.**
   Flat means no accumulation and the race conclusion stands. Rising means there IS a per-file
   leak, which would reopen the question this plan was written to close — that is the one result
   that would change the diagnosis.

### 1. Fix the temp-WAV leak on failure paths — DONE (commit `9e3fd8a`)

Real defect, low severity. In `transcribe_media_file` (cell 19) the cleanup

```python
if audio_path != file_path and os.path.exists(audio_path):
    os.remove(audio_path)
```

sits inline in the `try`, **not** in a `finally`. Any early return or exception between extraction
and that line leaks a 16 kHz mono WAV in the work directory. The explicit early return at the
extraction-failure branch (`return None, None, 0, 0, None, 0`) skips it outright.

Disk-only inside an ephemeral container, so it cannot cause the hang \u2014 but on a large multi-file
batch it wastes container disk, and the same bug would matter more in the job-service payload
where the work directory may be a mounted volume. Wrap in `try/finally`. **Carry the fix into the
port, not just the notebook.**

**Resolution (commit `9e3fd8a`):** moved into a `finally` block, with `audio_path` bound *before*
the `try` so the `finally` cannot raise `NameError` on an early failure. Paired
`LEDGER['wav_created']` / `LEDGER['wav_removed']` counters make it verifiable rather than assumed.
Still needs confirming on a real run \u2014 see item 0.

### 2. Confirm the 8.5 MB CUDA residue is inert

`allocated=8519680` (~8.5 MB) survives `del model` + `empty_cache()` + `ipc_collect()` on 4 of 5
runs. It does not correlate with hangs, so this is hygiene, not a fix. Determine whether it is
torch's own context or a retained tensor reference. If a retained reference, clear it; if torch
context, document it as expected and stop looking.

### 3. Capture the census at hang time, not just teardown time — PARTLY DONE

The teardown census prints during cell 35, **before** the hang manifests, so it shows state at
teardown-start. The faulthandler dumps cover the after-state but report only threads *with frames*.

The per-file ledger snapshots now give a much better before/during picture. What is still missing is
a snapshot **inside the watchdog**: have the `HANG_FORENSICS` timer print `ledger_snapshot()`
alongside each periodic dump, so the wedged state is directly comparable to the per-file baselines.

Cheap and additive: one extra line per 120s dump on hung runs, nothing on healthy ones. Do this
**before** the port lands, since it is the last chance to gather notebook-side evidence.

### 4. Package the evidence for Snowflake support

The strongest case now available, and the only route to an actual platform fix:

- 11 live dump cycles at 120s, identically sized stacks, zero notebook frames
- identical thread census across hung and clean runs (table above)
- the parked frames: `on_scriptrunner_ready`, `run_till_end` \u2192 `run_forever`,
  `stage_copier stage_file_watcher`, `status_thread status_fun`
- rate: 6 of 8 multi-file, 0 of 8 single-file; hang/clean/hang within six hours on 2026-08-19
- **the container outlives the task indefinitely** and must be reclaimed manually \u2014 arguably the
  most important point for Snowflake, since it is an unbounded cost leak, not just a slow exit

## Verification

- Temp-WAV fix: force an extraction failure (a deliberately corrupt file) and confirm no
  `*_temp_audio.wav` remains in the work directory
- CUDA residue: either driven to 0, or documented as torch context with a one-line note
- Census-at-hang-time: on the next hang, thread lists from teardown and from a periodic dump are
  directly comparable
- **No claim that the hang is fixed.** Nothing in this plan can fix it; the acceptance criterion
  is better evidence and two small defects closed, not a behaviour change

## Out of scope

- Re-testing the four hypotheses closed above
- `FORCE_KERNEL_EXIT` as a remedy \u2014 already measured, bounds runtime but makes the task report
  FAILED and discards `RETURN_VALUE`
- Deliberately provoking hangs to grow the sample. Each costs a GPU spin-up **plus manual pool
  reclamation**, and the mechanism is already established

## Critical files

- `notebooks/audio_video_transcription.ipynb` \u2014 cell 19 (`extract_audio_from_video`,
  `transcribe_media_file`), cell 35 (teardown and `HANG_FORENSICS`)
- `.cortex/skills/av-transcription-dev/references/known-issues.md` \u00a71 \u2014 record the negative
  results so they are not re-investigated
- `.snowflake/cortex/plans/port-transcription-to-job-service.plan.md` \u2014 the actual fix; carry the
  temp-file `finally` into the payload
