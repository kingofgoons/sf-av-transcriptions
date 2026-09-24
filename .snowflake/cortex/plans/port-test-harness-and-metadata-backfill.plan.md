---
name: "port-test-harness-and-metadata-backfill"
created: "2026-09-24T16:02:46.036Z"
status: pending
---

# Port plan reevaluation: test harness and metadata backfill

**Status:** pending **Created:** 2026-09-24 **Companion to:** `.snowflake/cortex/plans/port-transcription-to-job-service.plan.md`, which stays the implementation spec. This plan adds what that one is missing and names the specific amendments it needs.

## Reevaluation verdict

The port plan is **sound on architecture and risk** and should not be restructured. Its root-cause evidence is strong, the `snowbook.web.cli` trap is documented, and the launch-site risk is tested early with a fallback. Three things are wrong with it.

### 1. There is no automated test layer at all

Grepped for `pytest`, `unit test`, `fixture`, `golden`: **no mention**. Every check is one-shot manual validation gated behind GPU runs.

That is inconsistent with the rigour just applied elsewhere: a **config-file refactor** got 27 offline tests, while **rewriting 1,814 notebook lines into \~700** gets none. It is also backwards economically — GPU runs cost money and, during the transition, carry hang risk; offline tests cost nothing.

**8 of the 9 functions being ported are pure**, measured from cell 19:

| Function                     | Lines | Regexes | Branches | Purity               |
| ---------------------------- | ----- | ------- | -------- | -------------------- |
| `parse_summary_sections`     | 61    | 1       | **14**   | pure                 |
| `generate_summary_markdown`  | 100   | 0       | 5        | pure                 |
| `extract_audio_from_video`   | 45    | 0       | 1        | pure                 |
| `parse_filename_metadata`    | 24    | 0       | 2        | pure                 |
| `generate_srt_content`       | 20    | 0       | 1        | pure                 |
| `generate_srt_with_speakers` | 20    | 0       | 1        | pure                 |
| `format_timestamp_srt`       | 7     | 0       | 0        | pure                 |
| `get_file_size`              | 6     | 0       | 0        | pure                 |
| `transcribe_media_file`      | —     | —       | —        | impure (needs model) |

`parse_summary_sections` at **14 branches** is the highest port risk, and it produces `MEETING_TITLE`, `CALL_BRIEF`, `KEY_POINTS`, `NEXT_STEPS`. The port plan's only check on it is that those fields are "non-null and structurally correct" — an assertion a mis-ported branch can satisfy while returning the wrong text.

### 2. There is no rollback path

Grepped for `rollback`, `revert`, `back out`: **no mention**. The plan retires the notebook path in task 7 with no documented route back if the job service misbehaves in production.

### 3. A timing criterion is miscalibrated

"Task duration approximately equals actual work time (expect roughly 2-4 minutes for an 8-minute recording)" predates the measurement that task duration carries **126-149s of container startup** beyond work. For a job service, startup differs again (no `pip install` if deps are in the image). The criterion needs startup broken out rather than folded in.

Already corrected on 2026-09-24 (commits `ed495a8`, `c59418e`): tail-gap threshold 15s to 30s after a 2-file SUCCEEDED run measured 18s and was falsely flagged HUNG; the pre-port baseline is captured as a table before `TASK_HISTORY` expires; the row-count anchor is 491.

## The finding that changes the test strategy

`TRANSCRIPTION_RESULTS` stores **both** the raw `SUMMARY_MARKDOWN` **and** its parsed outputs, and **both** `TRANSCRIPT_WITH_SPEAKERS` **and** the derived `SRT_CONTENT`. So 493 historical rows are a **free, deterministic golden corpus** — no GPU, no LLM, no re-transcription.

| Golden pair                                                           | Rows available | Validates                                      |
| --------------------------------------------------------------------- | -------------- | ---------------------------------------------- |
| `TRANSCRIPT_WITH_SPEAKERS` to `SRT_CONTENT`                           | **493**        | `generate_srt_content`, `format_timestamp_srt` |
| `TRANSCRIPT_WITH_SPEAKERS` to `SRT_WITH_SPEAKERS`                     | **493**        | `generate_srt_with_speakers`                   |
| `SUMMARY_MARKDOWN` to parsed fields                                   | **240**        | `parse_summary_sections`                       |
| `FILE_NAME` to `ACCOUNT_NAME` / `CALL_START_TS` / `PARTICIPANTS_JSON` | **270**        | `parse_filename_metadata`                      |

This is far stronger than the port plan's "diff all 23 columns for one file", and it runs in CI forever.

**Critical constraint: use only known-good rows.** A naive "reproduce the stored values" harness would enshrine a bug, because a large cohort has NULL parsed fields (below). The corpus must be filtered to rows where the fields were genuinely produced.

## Pre-existing data-quality gap (separable from the port)

Fill rates across the 490 rows that have a summary:

| Field                                                                              | Populated | Missing |
| ---------------------------------------------------------------------------------- | --------- | ------- |
| `MEETING_TITLE`                                                                    | 240       | **250** |
| `CALL_BRIEF` / `KEY_POINTS` / `NEXT_STEPS` / `DECISIONS_MADE` / `QUESTIONS_RAISED` | 276       | **214** |
| `ACCOUNT_NAME`                                                                     | 270       | **220** |
| `CALL_START_TS`                                                                    | 269       | **221** |
| `PARTICIPANTS_JSON`                                                                | 276       | **214** |

By month, `MEETING_TITLE` ran 100% through 2026-01, fell to \~66% in Feb-Mar, hit **0% for April through July (181 rows)**, then recovered to 100% in September.

**This is not a parser failure.** Of the 250 rows missing a title, **125 contain a perfectly parseable `# Meeting Summary:` line** — the parser would have extracted it. Those rows were never run through the parser. The other 125 have no such line and are a genuinely different summary format.

`ACCOUNT_NAME` is NULL across the same window, and that comes from a *different* function parsing the filename, which is always present. So filename-derived fields are **100% recoverable** for every affected row.

**All of this is backfillable at zero cost**, because every input is already stored. No GPU, no LLM, no re-transcription.

It matters today, not just for the port: the dashboard's Browse and Search tabs and anything built over this data are missing metadata on roughly 45% of rows.

**And the backfill is the strongest possible port validation.** If the ported parser both reproduces the 240 known-good rows exactly and correctly backfills the 125 parseable-but-unparsed ones, correctness is established against real data before a single GPU run.

## A decision I should not make alone

`parse_summary_sections` matches section headers by **exact string equality** on stripped lines — `'**Summary**'`, `'Key Topics'`, `'Follow-up Items'`. Any drift (`## Key Topics`, `Key Topics:`) silently yields NULL. Evidence of the brittleness: of the 240 successfully parsed rows, only **39** contain the `**Summary**` marker.

Two options, and they conflict:

- **Port verbatim.** Byte-for-byte reproducible against the golden corpus, so parity is provable. Preserves the brittleness.
- **Make it robust while porting.** Better fill rates, but it will *not* reproduce stored values, so the golden corpus can no longer prove parity.

My recommendation: **port verbatim, prove parity, then improve the parser as a separate change with its own tests.** Two variables at once makes a parity failure ambiguous. Flagging it because it is a product decision, not just an engineering one.

## Work

**Status 2026-09-24: items 1, 2 and 4 are complete and committed (`2760f71`). Item 3 (backfill) is
not started.** 122 tests pass, offline and free.

### 1. Offline unit tests for the pure functions — DONE

`tests/test_payload_functions.py`, mirroring the structure of `tests/test_config.py`. Import the ported functions directly; no Snowflake, no GPU. Prioritise by risk from the table above: `parse_summary_sections` first (14 branches), then `generate_summary_markdown`.

Cover explicitly: empty and None input, missing sections, sections in unexpected order, a summary with no recognised markers at all, `format_timestamp_srt` at 0 / sub-second / hour boundaries / non-integer seconds, and SRT numbering starting at 1.

### 2. Golden-corpus parity harness — DONE, with one deliberate deviation

`tests/test_payload_parity.py`, plus a one-off extract script that pulls the corpus to `tests/fixtures/golden/` as newline-delimited JSON so the tests stay offline and run in CI without a connection.

**Delivered cohorts differ from the estimates below, and the reason matters more than the numbers.**

| | Planned | Delivered | Why |
|---|---|---|---|
| SRT | 493 rows | **21 sampled from 377 in-era** | 493 rows of full text is 49 MB. Stratified NTILE sample spanning 33-2869 segments plus the corpus max; expected output stored as SHA-256. Fixtures total 936 KB. |
| Summary | 240 rows | **55** | Era-scoped, see below. |
| Filename | 270 rows | **163** | Era-scoped, see below. |

**DEVIATION from "record any mismatches as accepted-diff fixtures".** The first run produced 78
failures: 6 SRT rows and 72 summary rows. I did **not** record them as accepted diffs, and I did not
loosen any assertion. I **narrowed the corpus** instead, which needs justifying because narrowing a
corpus is otherwise an excellent way to hide real failures.

The justification is that those rows were written by **different code**, so they were never evidence
about this port:

- The SRT generator stopped skipping empty-text segments between 2026-01 and 2026-02. Proven by
  comparing `REGEXP_COUNT(SRT_CONTENT,' --> ')` to `ARRAY_SIZE(...:speakers)` per processing month:
  80 of 116 rows differ before the boundary, **0 of 377 after**. For each failing row the segment
  delta equalled its count of empty-text segments exactly (+1, +4, +5, +6).
- The summary prompt emitted `## Key Topics` markdown headings from **2026-02-10 18:10 to
  2026-08-17 17:03**, which `parse_summary_sections` cannot match because it compares bare headers
  by exact string equality. The 2026-08-18 commit that added the missing `import re` also reverted
  the prompt — one change, both effects, which is why the two dates are a day apart.

An accepted-diff fixture would have recorded "these 78 rows differ" as a permanent expectation,
which is worse: it would keep dead-era rows in the suite forever and normalise 78 known diffs, so a
real 79th would not stand out. Excluding them and **asserting the exclusion held** is stronger.
Two tests do that — `test_srt_cohort_is_era_consistent` and
`test_summary_corpus_contains_no_off_era_rows` — so an extractor regression fails at the cause
instead of as hundreds of downstream mismatches.

**Guard against the obvious abuse:** the suite was mutation-tested to prove it is not vacuous.
Rounding milliseconds instead of truncating fails 43 tests; changing one summary header literal
fails 4; `import datetime` in place of `from datetime import datetime` fails 5.

**One finding worth carrying forward.** `generate_summary_markdown` stores a *wrapper* document as
`SUMMARY_MARKDOWN` but calls the parser on the *inner* LLM text. Feeding the stored value back in
sweeps the 40-character footer into the last section, which mismatched `QUESTIONS_RAISED` on 240 of
240 rows before `inner_summary()` was written to recover the parser's true input. Anything else that
re-parses `SUMMARY_MARKDOWN` — **including the backfill in item 3** — has to do the same unwrapping
or it will silently corrupt the last section.

**Also corrected:** filtering must be on `TRANSCRIPTION_TIMESTAMP` (processing date), never on the
date in `FILE_NAME` (meeting date). They differ by weeks, and grouping by filename date makes the
eras appear interleaved and hides both boundaries completely.

- ~~**SRT regeneration, 493 rows.**~~ Superseded by the table above; hash-compared, not byte-compared, for size.
- ~~**Parser reproduction, 240 known-good rows only.**~~ Superseded: 55 in-era rows, every field exact.
- ~~**Filename reproduction, 270 rows.**~~ Superseded: 163 in-era rows. `ACCOUNT_NAME` and `CALL_START_TS` asserted; `PARTICIPANTS_JSON` extracted but not yet compared.
- **Record any mismatches as accepted-diff fixtures with a written reason.** Do not loosen an assertion to make a suite green. — Honoured in spirit; see the deviation note above.

### 3. Metadata backfill (deliverable in its own right) — NOT STARTED

A script that re-derives the parsed fields from stored `SUMMARY_MARKDOWN` and `FILE_NAME` and `UPDATE`s only NULL columns. Never touches `TRANSCRIPT`, `SRT_*`, or `SUMMARY_MARKDOWN`.

Dry-run first, reporting rows affected per column. Take a zero-copy clone before the first write, matching the existing `TR_BACKUP_GOOD` pattern. Expect roughly 125 titles and roughly 220 filename-derived rows recovered; the remaining 125 unparseable-format rows stay NULL and get counted, not forced.

Run it with the **ported** functions, so it doubles as the port's correctness evidence.

**Two constraints discovered while building item 2 — both will silently corrupt this backfill if
missed:**

1. **Unwrap before parsing.** `SUMMARY_MARKDOWN` is a wrapper document; the parser expects the
   inner LLM text. Reuse `inner_summary()` from `tests/test_payload_parity.py` (or move it into the
   payload module) rather than passing the stored column directly, or the trailing
   `*Generated by Snowflake Cortex AI*` footer lands inside `QUESTIONS_RAISED` on every row.
2. **The `##`-header era is not backfillable with the current parser.** Rows processed between
   2026-02-10 and 2026-08-17 use `## Key Topics`-style headings that `parse_summary_sections`
   cannot match, so re-running it over them yields NULL for all five section fields and the row is
   simply skipped. Two options, and the dry-run should quantify both before anyone chooses:
   either teach the parser to accept `##`-prefixed headers as well (a small, safe widening — but
   it changes behaviour, so it needs its own before/after evidence and a fixture re-extract), or
   accept that those rows stay NULL. Do not discover this after the clone is taken; the dry-run
   must report recoverable-vs-unrecoverable split by era, not a single total.

   Note the previously-estimated "125 of 250 title-less rows are backfillable" figure counted rows
   containing a parseable `# Meeting Summary:` line. The **title** is regex-matched and therefore
   era-insensitive, so that estimate should still hold for `MEETING_TITLE`. The five **section**
   fields are a different population. Report them separately.

### 4. Amendments to the port plan — DONE

All five applied to `port-transcription-to-job-service.plan.md`:

- ~~Add tasks 1-3 above as prerequisites to its task 4.~~ **Done** — task 4 now opens with the
  satisfied-prerequisite note and a `pytest tests/ -q` gate that must be green before a compute
  pool is requested.
- ~~Replace "expect roughly 2-4 minutes" with separate budgets.~~ **Done** — replaced with a
  three-budget table (startup / work / tail) that must account for essentially all of task
  duration, plus the instruction to record all three per run. The 126-149s figure is startup-ish
  overhead, which is why conflating it with the tail was so misleading.
- ~~Add a rollback section.~~ **Done** — new `## Rollback` section: `PROJECT_LAUNCH_MODE` config
  flag, five explicit rollback triggers, a four-step procedure, an evidence-preservation step, and
  the condition for finally retiring `NOTEBOOK` mode. Task 7 amended not to delete the notebook
  path prematurely.
- ~~Update stale inventory.~~ **Done** — 1,814 lines, cell 19 = 526, cell 5 = 260, cell 28 = 154,
  with a pointer to the already-extracted `transcribe_functions.py`.
- ~~Decide whether the payload carries the resource ledger.~~ **Done** — new task 4c, decision
  **yes**, with the three reasons and five porting notes (chiefly: keep `_os_children()` returning
  `None` rather than `[]`, and verify stdout is actually retrievable from a container log).

**One unplanned amendment, found while editing.** The plan contained a self-contradiction: the
acceptance criterion said a tail under `~30s` while the "honest success criterion" paragraph still
said `~15s`. Reconciled to 30s. Separately, flagged that the **`6 of 8` hang rate is stale** — it
dates from 2026-08-19, and the current 7-day window shows 1 hang in 6 runs, that hang being the
window's only 4-file run. Multi-file specificity still holds; the *rate* is unquantified, which
weakens "4 consecutive clean runs" as a bar. Noted with the arithmetic.

## Verification

Tiered as before. **Tiers 0 and 1 need nothing from you.**

### Tier 0 — offline, zero cost, no Snowflake

| Test             | Assertion                                                                            |
| ---------------- | ------------------------------------------------------------------------------------ |
| Unit tests       | All pure functions pass, including the edge cases in task 1                          |
| SRT parity       | 493/493 rows byte-identical, or every exception documented as an accepted diff       |
| Parser parity    | 240/240 known-good rows reproduce all six fields                                     |
| Filename parity  | 270/270 rows reproduce all three fields                                              |
| Corpus integrity | The extract contains only known-good rows; a deliberately NULL-field row is excluded |
| Backfill dry-run | Reports expected counts and writes nothing                                           |

### Tier 1 — read-only against Snowflake

| Test               | Assertion                                                                                     |
| ------------------ | --------------------------------------------------------------------------------------------- |
| Corpus freshness   | Fixture row count matches the live table's eligible rows, so the corpus has not silently aged |
| Fill-rate snapshot | Current per-field counts recorded as the pre-backfill baseline                                |

### Tier 2 — writes. HUMAN ACTION REQUIRED

**HUMAN 1 — approve the backfill.** It `UPDATE`s roughly 345 rows across 490. Only NULL columns are touched, transcripts and SRTs are never written, and a zero-copy clone is taken first. This is the only destructive-capable step here.

**HUMAN 2 — eyeball Browse/Search afterwards.** Confirm the recovered metadata renders and nothing regressed.

| Test                 | Assertion                                                  |
| -------------------- | ---------------------------------------------------------- |
| Row count unchanged  | 493 before and after; backfill must never insert or delete |
| Only NULLs filled    | Every previously non-NULL value byte-identical afterwards  |
| Counts match dry-run | Actual rows updated equals the dry-run projection          |
| Reversible           | The clone restores exactly, verified before relying on it  |

**No GPU run anywhere in this plan.**

## Critical files

- .snowflake/cortex/plans/port-transcription-to-job-service.plan.md - the spec this amends; task 4 gains prerequisites
- notebooks/audio\_video\_transcription.ipynb - cell 19 holds the 8 pure functions; cell 28 the 23-column INSERT
- tests/test\_config.py - the harness pattern to mirror, including the fake-binary-on-PATH technique
- streamlit/tab\_browse.py - consumer of the metadata the backfill restores

## Scope note

Tasks 1 and 2 are pure additions and block nothing. Task 3 delivers value independently of whether the port ever happens. Task 4 is documentation.

**Deliberately excluded:** making `parse_summary_sections` robust. It is a real improvement and a real product decision, but doing it simultaneously with the port makes any parity failure ambiguous. Separate change, own tests, after parity is banked.
