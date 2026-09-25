## Context

Reviewed all six files under `documents/`. Every one is affected, but the work is **mostly
surgical, with two genuine rewrites** — this is not a wholesale documentation redo.

Stale-term census:

| File | Lines | `notebook` | `snowbook` | `EXECUTE NOTEBOOK` | Verdict |
|---|---|---|---|---|---|
| `architecture/architecture.md` | 285 | 24 | 4 | 3 | §5 needs rewriting; rest surgical |
| `architecture/dashboard.md` | 451 | 10 | 1 | 1 | §5 needs rewriting; §8 is unrelated |
| `operations/runbook.md` | 338 | 13 | 2 | 1 | surgical + one new section |
| `operations/tool-routing.md` | 121 | 8 | 0 | 0 | almost entirely still valid |
| `architecture/*.drawio` (x2) | — | 15 | — | 2 | regenerate both |

Live figures confirmed for use in the edits (not "update the counts"):

```
TRANSCRIPTION_RESULTS   497      GONG_CALLS_MIRROR    56
UNIFIED_MEETINGS_V      553      TRANSCRIPTION_RUN_EVENTS 679
stage files             381      pool AUTO_SUSPEND_SECS  300   (docs say 3600)
warehouse AUTO_SUSPEND  600      all-time avg ratio      0.0373
```

Verified still accurate, so **not** to be touched: the §9 summary-heading table (the payload
uses the same exact-equality parser — `'**Summary**'`, `'Key Topics'`, `'Follow-up Items'`,
`'Decisions Made'`, `'Questions Raised'`), the §8 format list (payload `MEDIA_EXT` matches),
and `dashboard.md` §8, which is about a **different** port (Streamlit App Runtime) and is
forward-looking, not stale.

---

## 1. Remove the claim that costs money to follow  [do this first]

`CALL TRANSCRIBE_IF_NEW_FILES()` **launches a GPU run** whenever untranscribed media exists.
It was documented as a safe inspection in five places; three were fixed in the port commit,
and **two remain here**:

- `documents/architecture/dashboard.md:174` — in the owner's-rights capability table:
  *"Gate proc returns SKIPPED without launching a GPU"*
- `documents/operations/runbook.md:73` — *"To see what the gate would do without launching a
  GPU container"*

The runbook one is the more dangerous: it sits in a day-to-day operations doc, phrased as the
recommended way to check. Replace both with the read-only backlog query from
`scripts/03_automate.sql`, and state plainly that the `CALL` launches. This is the only item
in this plan with a direct cost if left alone, which is why it leads.

## 2. Rewrite `architecture.md` §5 — the hang is resolved, not pending

§5 is titled "Known architectural constraint: the notebook hang" and closes with *"The
architectural fix is to move the payload off `EXECUTE NOTEBOOK`... See
`.snowflake/cortex/plans/port-transcription-to-job-service.plan.md`"* — describing shipped
work as a future plan.

Retitle to something like "Resolved: the notebook hang, and why the notebook is still here",
and restructure:

- **Lead with the resolution and the evidence.** Measured 1 hang in 2 notebook runs (145s+
  tail) against **7 consecutive clean job-service exits** — 5s x4 isolated, 7s dashboard, 9s
  uploader, 6s on the live 4-file run.
- **Keep the root-cause analysis, demoted.** `EXECUTE NOTEBOOK` remains reachable via
  `PROJECT_LAUNCH_MODE = 'NOTEBOOK'`, so the hang is still live behaviour for anyone who rolls
  back — deleting the forensics would strand a rollback operator. Recommend keeping the
  thread-level detail (lines 184-199) but moving it under a clearly-marked historical
  subsection. *Alternative if you'd rather slim the file: replace with a two-line summary plus
  a pointer to `DIARY.md` 2026-08-19. I lean against it — this project's documented style is
  to preserve reasoning, and this analysis is what proved the fix had to be architectural.*
- **Correct the GPU-leak paragraph.** It says the task timeout does not kill the container.
  Still true for the notebook; **now false for the job service** — verified 2026-09-25 that a
  task timeout cancels the job and removes the service, pool back to `num_jobs = 0`. The
  distinction matters because the runbook's mandatory post-hang reclaim is keyed to it.
- Add the **new all-or-nothing exposure**: `persist()` runs once after the file loop, so a
  timeout mid-batch discards every transcript from that run. At ~0.066 measured ratio the
  30-minute task timeout caps a batch near 5 hours of audio. This replaces the old hang as the
  main reason a large run can waste GPU time.

## 3. Rewrite `dashboard.md` §5 — one instruction is now inverted

`dashboard.md:224` says **"Never add a notebook-side `SUCCEEDED` emission"**. The payload emits
`SUCCEEDED` as its terminal event, deliberately and correctly — a headless script has no
`snowbook` shutdown phase, so it *can* report its own clean exit. Anyone following that
instruction today would "fix" working code by removing it. This is the highest-risk edit in
the file.

Also in §5:

- `:214` "the notebook counts a unit" -> the payload's `RunProgress` (`PHASE_TOTAL = 6`,
  `GLOBAL_UNITS = 4`, so `UNITS_TOTAL = 4 + 4n`; verified 8 for 1 file, 12 for 2, 20 for 4).
- `:220-224` the "cannot report its own clean exit" rationale -> reframe as notebook-only, and
  explain that `TASK_HISTORY` is still read every poll because it is the independent check that
  the container actually went away.
- `:232-233` state table -> add `SUCCEEDED` and `FINISHING` as the normal job-service terminal
  and near-terminal states; mark `CELLS_COMPLETE` notebook-only.
- `:243-245` "`emit()` sits at line 147 of notebook cell 5" -> the payload's emitter, and
  replace the 60-180s window with **16-18s warm / 110-116s cold**.
- `:265-298` "Reading a hang off the dashboard" -> keep as a diagnostic, but state that
  post-port `WORK_COMPLETE_NOT_EXITED` means a **new** exit fault, not the benign one.
- `:316` "the gate would run `EXECUTE NOTEBOOK` inline and hold the app's session" -> job
  service, still synchronous, same consequence.
- `:338` "validated against the notebook's `parse_filename_metadata()`" -> the payload's.

Add the **three-verdict gate contract** (`LAUNCHED` / `SKIPPED` / `BLOCKED`), which is
currently undocumented in every file. `BLOCKED` exists because the job-service path drops the
service before creating it.

## 4. `architecture.md` surgical corrections

- **§2 flow diagram:** `EXECUTE NOTEBOOK` -> `DROP SERVICE` + `EXECUTE JOB SERVICE`
  (synchronous); notebook block -> `TRANSCRIBE_JOB` running `transcribe_job.py` from
  `@PAYLOAD_STAGE`; `pip install` -> `uv pip install --system --break-system-packages`;
  row counts 444 -> 497 and `494` -> `553`.
- **§2 prose:** "only the notebook does" writes `TRANSCRIPTION_RESULTS` -> the payload.
- **§3 Compute:** `AUTO_SUSPEND = 3600s` -> **300s**. Load-bearing, not cosmetic: the 3600
  figure is cited in the leak reasoning in both this file and the runbook.
- **§3 Pipeline:** add `@PAYLOAD_STAGE` (SYSADMIN-owned, holds `transcribe_job.py`,
  `transcribe_functions.py`, `transcribe_job_spec.yaml`) and `TRANSCRIBE_JOB` (the job service,
  one stable name, dropped and recreated per run). Update the gate row to describe the
  launch-mode branch; update `TRANSCRIPTION_RUN_EVENTS` "emitted by the notebook" -> by
  whichever engine ran.
- **§3:** add a **Rollback** row or short subsection for `TRANSCRIBE_AV_FILES_V2` — it is
  neither active nor deprecated, and currently reads as the live engine.
- **§6 dedup contract:** "the SQL gate and the notebook both rely on this" -> the payload's
  `discover()`, which uses `SELECT DISTINCT FILE_NAME` identically.
- **§7 performance envelope:** the claim "a stable ratio of ~0.035x realtime, flat across 440+
  rows" is **wrong in a way worth fixing** — the ratio varies *inversely with duration*
  (measured 0.0729 at 4 min, 0.0602 at 9 min, 0.0354 at 13 min; all-time mean 0.0373). Short
  files are dominated by fixed per-file cost. Replace with the range plus that relationship,
  and add the real datapoints: 78.5 min audio / 4 files / 310s work; 426s total end-to-end.
  Delete "A run that takes hours is not slow transcription — it is the hang in section 5."
- **§8:** "Discovered by glob in the notebook" -> `LIST` in the payload's `discover()` (it uses
  `LIST`, not `DIRECTORY()`, because the directory table goes stale after `PUT`). **Add the
  extension mismatch I found:** the uploader's `AV_EXTENSIONS` accepts `.wma`, `.wmv`, `.m4v`,
  which are absent from both the payload's `MEDIA_EXT` and the SQL gate's list — such a file
  uploads fine and then sits on the stage forever, never transcribing, with no error anywhere.
- Header: "Last verified against the live account: 2026-08-19" -> 2026-09-25.

## 5. `runbook.md` — corrections plus one missing section

- **Add a payload-deploy section** beside §3 "Deploy notebook changes": `05_deploy_payload.sh`
  has no runbook entry at all. Cover that it verifies by downloading and comparing content,
  that `PUT` success is not proof of deployment, and that a launch-mode change additionally
  requires rebuilding the gate procedure.
- **Document that `03_automate.sql` needs a role-capable connection** — it contains `USE ROLE
  SYSADMIN` and the DEMO connection rejects it. The surgical alternative is
  `migration/06_gate_proc_job_service.sql`, which is **gitignored**, so the runbook is the only
  place this can live durably.
- `:175`, `:207`, `:240` — hang framing; `:240` literally says "It goes away with the
  job-service port", which has happened.
- `:210-214` "After ANY hang: reclaim the leaked GPU node" — keep, since the notebook is the
  rollback target, but scope it to notebook mode and record that the job service self-cleans.
  Fix `AUTO_SUSPEND = 3600` -> 300 here too.
- `:318`, `:328` teardown levels — extend "refuse while an `EXECUTE NOTEBOOK` is RUNNING" to
  cover a running job service, and add `@PAYLOAD_STAGE` to the level-2 object list.
- Add the **`_hold/` convention**: the uploader takes every media file in
  `AUDIO_VIDEO_STAGE_FILES/` not already staged, and `glob` is non-recursive, so a
  subdirectory is a safe holding pen. Verified empirically (0 visible vs 13 via `rglob`).
  Also note the Gong prompt is reached *after* the upload and trigger succeed, so a
  non-interactive run looks stalled when the work is already done.

## 6. `tool-routing.md` — light touch

Almost all still correct: the notebook tools rule stands (the notebook still exists), and the
`faulthandler` forensics note documents a technique, not a live problem. Two additions:

- A line routing payload changes to `scripts/05_deploy_payload.sh`, alongside the existing
  `04_deploy_notebook.sh` reference at `:62`.
- Note that job-service container logs come from `SYSTEM$GET_SERVICE_LOGS(name, 0, 'main', N)`
  and truncate at ~4 KB — different from the notebook's event-table path described at `:107`.

## 7. Regenerate both draw.io diagrams

`architecture.md`'s maintenance contract states diagrams that disagree with it are bugs, and
both are from Aug 19 with 15 `notebook` references, 2 `EXECUTE NOTEBOOK`, `TRANSCRIBE_AV_FILES_V2`
as the engine, and the stale `444`. Regenerate via the `drawio-diagrams` skill, editing
`architecture.uncompressed.drawio` as the source and producing `architecture.drawio`.

Changes: gate -> job service, add `@PAYLOAD_STAGE`, add `TRANSCRIBE_JOB`, show the notebook as
a dashed rollback path, update counts. Do this **last**, so the diagram is generated from
finished prose rather than needing two passes.

---

## Verification

1. `grep -rn "without launching\|verdict without" documents/` returns nothing.
2. `grep -rn "SUCCEEDED emission" documents/` returns nothing (the inverted instruction).
3. `grep -rn "3600" documents/` — every remaining hit is historical context, not a current value.
4. `grep -rn "EXECUTE NOTEBOOK" documents/` — every hit is rollback or historical framing.
5. Stale counts absent: `grep -rn "444\|494\| 441" documents/`.
6. Cross-reference integrity: `architecture.md` §5 <-> `dashboard.md` §5 <-> `runbook.md` §6 must
   agree on which engine can hang and what the operator does about it. They currently agree
   *because* all three describe the notebook; the risk in this change is leaving them
   half-updated and mutually contradictory, which is worse than leaving them all stale.
7. Diagrams contain `PAYLOAD_STAGE` and `JOB SERVICE`, and no longer name the notebook as the
   active engine.

## Out of scope

- `dashboard.md` §8 (App Runtime port notes) — a different, still-pending port.
- `agents.md`, `DIARY.md`, and the skill under `.cortex/skills/` — already updated, or
  deliberately gitignored.
- The `_hold/` test-file arrangement itself, and the follow-ups already recorded in `DIARY.md`
  (payload `--pattern`, per-file `persist()`, retained backup tables).
