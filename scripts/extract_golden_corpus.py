#!/usr/bin/env python3
"""extract_golden_corpus.py - snapshot known-good rows for offline parity tests

WHY THIS EXISTS

TRANSCRIPTION_RESULTS stores both the INPUTS and the OUTPUTS of most of the functions in
scripts/payload/transcribe_functions.py:

    TRANSCRIPT_WITH_SPEAKERS  ->  SRT_CONTENT, SRT_WITH_SPEAKERS
    SUMMARY_MARKDOWN          ->  MEETING_TITLE, CALL_BRIEF, KEY_POINTS, NEXT_STEPS,
                                  DECISIONS_MADE, QUESTIONS_RAISED
    FILE_NAME                 ->  ACCOUNT_NAME, CALL_START_TS, PARTICIPANTS_JSON

That makes the historical table a free, deterministic regression corpus for the port: run
the ported function over the stored input and assert it reproduces the stored output. No
GPU, no LLM, no re-transcription, and it runs in CI forever.

This script snapshots that corpus to newline-delimited JSON so the tests stay OFFLINE.
Re-run it only when you deliberately want to widen the corpus.

    python scripts/extract_golden_corpus.py            # write committed fixtures
    python scripts/extract_golden_corpus.py --stats    # report eligibility only
    python scripts/extract_golden_corpus.py --full-srt # extra local-only full SRT corpus

FIXTURE SIZE IS A DESIGN CONSTRAINT

A naive dump is 52 MB, of which srt.jsonl alone is 49 MB (11.7 MB even gzipped). That
would bloat every clone of this repo forever, so two things are done about it:

  1. The SRT cohort stores the INPUT plus a SHA-256 of each expected output, not the
     expected SRT text. TRANSCRIPT_WITH_SPEAKERS averages 109 KB per row while the two
     SRTs add ~86 KB of text that is fully derivable from it. A hash is 64 bytes and
     fails just as loudly.
  2. The SRT cohort is a STRATIFIED SAMPLE across segment count (the corpus spans 33 to
     2047 segments), not every row. Segment count tracks recording length, so this also
     spans timestamp magnitude - which is what actually exercises format_timestamp_srt,
     where float precision degrades as the hour field grows.

The summary cohort keeps ALL eligible rows: it is the highest-value corpus, since
parse_summary_sections is 61 lines with 14 branches, and it gzips to well under a MB.

Use --full-srt for an exhaustive local check against every row. That file is gitignored.

CRITICAL: ONLY KNOWN-GOOD ROWS

A naive "reproduce every stored value" corpus would enshrine a bug. Roughly 45% of rows
have NULL parsed fields, because the notebook was missing `import re` for about six
months: every title extraction raised NameError inside a bare except, so MEETING_TITLE
was stored NULL. 42.5% of rows before the 2026-08-18 fix have a title versus 100% of the
55 rows after it.

So each cohort below is filtered to rows where the field was genuinely produced. A row
with a NULL output is NOT evidence that the correct output is NULL - it is evidence the
parser never ran. Those rows are the BACKFILL population, not the parity population.
"""

import argparse
import gzip
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(REPO, 'tests', 'fixtures', 'golden')
CONNECTION = os.environ.get('SNOW_CONNECTION', 'DEMO')

# Rows in the committed SRT sample. Kept small because each row carries a ~109 KB
# transcript; 20 stratified rows is roughly 2 MB before compression.
SRT_SAMPLE = 20

# The summary cohort keeps every eligible row - it is the highest-value corpus and
# compresses well.
SUMMARY_LIMIT = 1000

# ---------------------------------------------------------------------------
# CODE-ERA CUTOFFS. These are the most important constants in this file.
# ---------------------------------------------------------------------------
#
# A stored value is only parity evidence if it was produced by the code being ported.
# This table spans a year and the transcription code changed twice in ways that alter
# output, so an unscoped corpus mixes eras and reports historical drift as port bugs.
# Both boundaries below were measured from the table, not assumed.
#
# Filter on TRANSCRIPTION_TIMESTAMP (when the row was PROCESSED), never on the date in
# FILE_NAME (when the MEETING happened). Those differ by weeks, and grouping by filename
# date makes the eras appear to interleave, which hides the boundaries completely.
#
# 1. SRT GENERATOR - boundary between 2026-01 and 2026-02.
#    The older generator skipped segments whose text was empty; the current one emits an
#    entry for them. Measured by comparing REGEXP_COUNT(SRT_CONTENT,' --> ') against
#    ARRAY_SIZE(TRANSCRIPT_WITH_SPEAKERS:speakers) per processing month:
#
#        2025-10  10 of 14 rows differ        2026-02   0 of 44 differ
#        2025-11  24 of 40 rows differ        2026-03   0 of 64 differ
#        2025-12  23 of 30 rows differ        ...
#        2026-01  23 of 32 rows differ        2026-09   0 of 38 differ
#
#    Clean break: 80 of 116 before, 0 of 377 after.
SRT_ERA_START = '2026-02-01'

# 2. SUMMARY PROMPT + PARSER - boundary at 2026-08-18.
#    From 2026-02-10 18:10 to 2026-08-17 17:03 the prompt emitted markdown headings
#    ('## Key Topics'), while parse_summary_sections matches bare/bold headers by exact
#    string equality ('Key Topics', '**Summary**'). The 2026-08-18 commit that added the
#    missing `import re` ALSO reverted the prompt to bare headers - one change, both
#    effects - so the last '##' row and the re fix are a day apart.
#
#    Note this is a middle era, not simply "old": rows before 2026-02-10 also use bare
#    headers and DO reparse correctly. They are excluded anyway, because the missing
#    `import re` makes the Apr-Jul rows all-NULL and because keeping one unambiguous
#    current-code cohort is worth more than a larger mixed one.
SUMMARY_ERA_START = '2026-08-18'

QUERIES = {
    # Every row with both a segment structure and a stored SRT. This transitively
    # validates format_timestamp_srt, where a millisecond error would otherwise ship
    # broken subtitles invisibly.
    #
    # NTILE stratifies by segment count so the sample spans short to very long
    # recordings rather than clustering on whatever ran most recently. The extremes
    # matter most: the longest recordings have the largest hour fields, which is where
    # float precision in format_timestamp_srt degrades.
    'srt': """
        WITH scored AS (
            SELECT FILE_NAME,
                   TRANSCRIPT_WITH_SPEAKERS,
                   SRT_CONTENT,
                   SRT_WITH_SPEAKERS,
                   REGEXP_COUNT(SRT_CONTENT, ' --> ') AS SEGS,
                   NTILE({sample}) OVER (ORDER BY REGEXP_COUNT(SRT_CONTENT, ' --> ')) AS BUCKET
            FROM {tbl}
            WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
              AND SRT_CONTENT IS NOT NULL
              AND TRANSCRIPTION_TIMESTAMP >= '{srt_era}'
        ), picked AS (
            -- Lowest in each bucket, PLUS the highest in the top bucket. Taking only
            -- the per-bucket minimum stops the sample well short of the corpus maximum
            -- (910 of 2047 segments when first tried), which would omit the largest
            -- timestamps - exactly where float precision in format_timestamp_srt
            -- degrades and the millisecond truncation artifact appears.
            SELECT *, ROW_NUMBER() OVER (PARTITION BY BUCKET ORDER BY SEGS ASC)  AS RN_LO,
                      ROW_NUMBER() OVER (PARTITION BY BUCKET ORDER BY SEGS DESC) AS RN_HI,
                      MAX(BUCKET) OVER ()                                        AS TOP_BUCKET
            FROM scored
        )
        SELECT FILE_NAME, TRANSCRIPT_WITH_SPEAKERS, SRT_CONTENT, SRT_WITH_SPEAKERS, SEGS
        FROM picked
        WHERE RN_LO = 1
           OR (BUCKET = TOP_BUCKET AND RN_HI = 1)
        ORDER BY SEGS
    """,

    # The exhaustive variant, written only with --full-srt and gitignored.
    'srt_full': """
        SELECT FILE_NAME, TRANSCRIPT_WITH_SPEAKERS, SRT_CONTENT, SRT_WITH_SPEAKERS,
               REGEXP_COUNT(SRT_CONTENT, ' --> ') AS SEGS
        FROM {tbl}
        WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
          AND SRT_CONTENT IS NOT NULL
          AND TRANSCRIPTION_TIMESTAMP >= '{srt_era}'
        ORDER BY TRANSCRIPTION_TIMESTAMP DESC
    """,

    # Rows where the summary parser demonstrably ran: a title was extracted. Without
    # this filter the corpus would include the ~250 rows whose fields are NULL only
    # because `re` was missing. Scoped to SUMMARY_ERA_START so the header format matches
    # what parse_summary_sections actually looks for - see the constant's comment.
    'summary': """
        SELECT FILE_NAME,
               SUMMARY_MARKDOWN,
               MEETING_TITLE,
               CALL_BRIEF,
               KEY_POINTS,
               NEXT_STEPS,
               DECISIONS_MADE,
               QUESTIONS_RAISED
        FROM {tbl}
        WHERE SUMMARY_MARKDOWN IS NOT NULL
          AND MEETING_TITLE IS NOT NULL
          AND CALL_BRIEF IS NOT NULL
          AND TRANSCRIPTION_TIMESTAMP >= '{summary_era}'
        ORDER BY TRANSCRIPTION_TIMESTAMP DESC
        LIMIT {limit}
    """,

    # Filename-derived fields. ACCOUNT_NAME non-null proves the parser ran on this row.
    'filename': """
        SELECT FILE_NAME,
               ACCOUNT_NAME,
               CALL_START_TS,
               PARTICIPANTS_JSON
        FROM {tbl}
        WHERE ACCOUNT_NAME IS NOT NULL
          AND TRANSCRIPTION_TIMESTAMP >= '{srt_era}'
        ORDER BY TRANSCRIPTION_TIMESTAMP DESC
        LIMIT {limit}
    """,
}

STATS_SQL = """
SELECT COUNT(*)                                                          AS ROWS_TOTAL,
       COUNT(CASE WHEN TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
                   AND SRT_CONTENT IS NOT NULL THEN 1 END)               AS ELIGIBLE_SRT,
       COUNT(CASE WHEN SUMMARY_MARKDOWN IS NOT NULL
                   AND MEETING_TITLE IS NOT NULL THEN 1 END)             AS ELIGIBLE_SUMMARY,
       COUNT(CASE WHEN ACCOUNT_NAME IS NOT NULL THEN 1 END)              AS ELIGIBLE_FILENAME,
       COUNT(CASE WHEN SUMMARY_MARKDOWN IS NOT NULL
                   AND MEETING_TITLE IS NULL THEN 1 END)                 AS BACKFILL_CANDIDATES,
       COUNT(CASE WHEN ACCOUNT_NAME IS NULL THEN 1 END)                  AS BACKFILL_FILENAME,
       COUNT(CASE WHEN TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
                   AND SRT_CONTENT IS NOT NULL
                   AND TRANSCRIPTION_TIMESTAMP >= '{srt_era}' THEN 1 END)   AS IN_ERA_SRT,
       COUNT(CASE WHEN SUMMARY_MARKDOWN IS NOT NULL
                   AND MEETING_TITLE IS NOT NULL
                   AND CALL_BRIEF IS NOT NULL
                   AND TRANSCRIPTION_TIMESTAMP >= '{summary_era}' THEN 1 END) AS IN_ERA_SUMMARY,
       COUNT(CASE WHEN ACCOUNT_NAME IS NOT NULL
                   AND TRANSCRIPTION_TIMESTAMP >= '{srt_era}' THEN 1 END)   AS IN_ERA_FILENAME
FROM {tbl}
"""


def run_sql(sql):
    """Execute read-only SQL via the Snowflake CLI and return parsed rows."""
    proc = subprocess.run(
        ['snow', 'sql', '-q', sql, '--connection', CONNECTION,
         '--enable-templating', 'NONE', '--format', 'json'],
        capture_output=True, text=True)
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        raise SystemExit('snow sql failed (rc=%d)' % proc.returncode)
    data = json.loads(proc.stdout)
    rows = []
    for block in (data if isinstance(data, list) else [data]):
        for row in (block if isinstance(block, list) else [block]):
            if isinstance(row, dict):
                rows.append(row)
    return rows


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def write_jsonl(path, rows, compress):
    opener = gzip.open if compress else open
    with opener(path, 'wt') as fh:
        for row in rows:
            fh.write(json.dumps(row, default=str, sort_keys=True) + '\n')
    return os.path.getsize(path) / 1024.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--table', default='TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS')
    ap.add_argument('--srt-sample', type=int, default=SRT_SAMPLE)
    ap.add_argument('--stats', action='store_true', help='report eligibility and exit')
    ap.add_argument('--full-srt', action='store_true',
                    help='also write the exhaustive local-only SRT corpus (gitignored)')
    args = ap.parse_args()

    stats = run_sql(STATS_SQL.format(tbl=args.table, srt_era=SRT_ERA_START,
                                     summary_era=SUMMARY_ERA_START))[0]
    print('Corpus eligibility in %s:' % args.table)
    print('  rows total                     %s' % stats['ROWS_TOTAL'])
    print('  eligible: SRT regeneration     %s' % stats['ELIGIBLE_SRT'])
    print('  eligible: summary parser       %s' % stats['ELIGIBLE_SUMMARY'])
    print('  eligible: filename parser      %s' % stats['ELIGIBLE_FILENAME'])
    print()
    print('  IN CODE ERA (what is actually extracted):')
    print('    SRT      >= %s      %s' % (SRT_ERA_START, stats['IN_ERA_SRT']))
    print('    summary  >= %s      %s' % (SUMMARY_ERA_START, stats['IN_ERA_SUMMARY']))
    print('    filename >= %s      %s' % (SRT_ERA_START, stats['IN_ERA_FILENAME']))
    print()
    print('  NOT eligible, these are the BACKFILL population, not parity rows:')
    print('    summary present, title NULL  %s' % stats['BACKFILL_CANDIDATES'])
    print('    account name NULL            %s' % stats['BACKFILL_FILENAME'])
    if args.stats:
        return 0

    os.makedirs(OUT_DIR, exist_ok=True)
    print()

    # --- SRT: stratified sample, expected outputs stored as hashes -----------------
    # The transcript is the INPUT and must be kept. Both SRTs are fully derivable from
    # it, so storing their text would add ~86 KB per row for no extra diagnostic power:
    # a hash mismatch fails just as loudly and costs 64 bytes.
    rows = run_sql(QUERIES['srt'].format(tbl=args.table, sample=args.srt_sample, limit=0,
                                         srt_era=SRT_ERA_START))
    srt_rows = [{
        'FILE_NAME': r['FILE_NAME'],
        'SEGS': r['SEGS'],
        'TRANSCRIPT_WITH_SPEAKERS': r['TRANSCRIPT_WITH_SPEAKERS'],
        'SRT_CONTENT_SHA256': sha(r['SRT_CONTENT']),
        'SRT_WITH_SPEAKERS_SHA256': sha(r['SRT_WITH_SPEAKERS']) if r['SRT_WITH_SPEAKERS'] else None,
        'SRT_CONTENT_LEN': len(r['SRT_CONTENT']),
    } for r in rows]
    kb = write_jsonl(os.path.join(OUT_DIR, 'srt.jsonl.gz'), srt_rows, compress=True)
    segs = [r['SEGS'] for r in srt_rows]
    print('  wrote %-20s %4d rows  %8.1f KB  segments %s-%s'
          % ('srt.jsonl.gz', len(srt_rows), kb, min(segs), max(segs)))

    # --- summary: every eligible row, compressed ----------------------------------
    rows = run_sql(QUERIES['summary'].format(tbl=args.table, limit=SUMMARY_LIMIT, sample=0,
                                             summary_era=SUMMARY_ERA_START))
    kb = write_jsonl(os.path.join(OUT_DIR, 'summary.jsonl.gz'), rows, compress=True)
    print('  wrote %-20s %4d rows  %8.1f KB' % ('summary.jsonl.gz', len(rows), kb))

    # --- filename: every eligible row, plain so diffs stay readable ----------------
    rows = run_sql(QUERIES['filename'].format(tbl=args.table, limit=SUMMARY_LIMIT, sample=0,
                                              srt_era=SRT_ERA_START))
    kb = write_jsonl(os.path.join(OUT_DIR, 'filename.jsonl'), rows, compress=False)
    print('  wrote %-20s %4d rows  %8.1f KB' % ('filename.jsonl', len(rows), kb))

    # --- optional exhaustive SRT corpus, gitignored --------------------------------
    if args.full_srt:
        rows = run_sql(QUERIES['srt_full'].format(tbl=args.table, limit=0, sample=0,
                                                  srt_era=SRT_ERA_START))
        full = [{
            'FILE_NAME': r['FILE_NAME'],
            'SEGS': r['SEGS'],
            'TRANSCRIPT_WITH_SPEAKERS': r['TRANSCRIPT_WITH_SPEAKERS'],
            'SRT_CONTENT_SHA256': sha(r['SRT_CONTENT']),
            'SRT_WITH_SPEAKERS_SHA256': sha(r['SRT_WITH_SPEAKERS']) if r['SRT_WITH_SPEAKERS'] else None,
            'SRT_CONTENT_LEN': len(r['SRT_CONTENT']),
        } for r in rows]
        kb = write_jsonl(os.path.join(OUT_DIR, 'srt_full.local.jsonl.gz'), full, compress=True)
        print('  wrote %-20s %4d rows  %8.1f KB  (LOCAL ONLY, gitignored)'
              % ('srt_full.local.jsonl.gz', len(full), kb))

    meta = {
        'source_table': args.table,
        'srt_sample_size': args.srt_sample,
        'eligibility_at_extract': {k: stats[k] for k in stats},
        'srt_expected_as': 'sha256 of SRT_CONTENT and SRT_WITH_SPEAKERS',
        'era_cutoffs': {
            'srt_and_filename_from': SRT_ERA_START,
            'summary_from': SUMMARY_ERA_START,
            'filtered_on': 'TRANSCRIPTION_TIMESTAMP (processing date, NOT the meeting '
                           'date in FILE_NAME - those differ by weeks)',
            'why': ('The SRT generator stopped skipping empty-text segments between '
                    '2026-01 and 2026-02 (80 of 116 rows differ before, 0 of 377 after). '
                    'The summary prompt emitted "## Key Topics" markdown headings from '
                    '2026-02-10 to 2026-08-17, which parse_summary_sections cannot match '
                    'because it compares bare headers by exact string equality; the '
                    '2026-08-18 commit that added the missing `import re` also reverted '
                    'the prompt. Rows outside these windows were produced by different '
                    'code, so a mismatch against them is historical drift, not a port '
                    'bug - including them made 6 SRT rows and 72 summary rows fail.'),
        },
        'note': ('Known-good rows only, and current-code-era rows only. Rows with NULL '
                 'parsed fields are excluded deliberately: they are victims of the '
                 'missing `import re` (fixed 2026-08-18), not evidence that NULL is the '
                 'correct output. The SRT cohort is a stratified sample by segment count '
                 'and stores expected output as hashes, because a full dump is 49 MB.'),
    }
    with open(os.path.join(OUT_DIR, 'meta.json'), 'w') as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    print('  wrote meta.json')
    return 0


if __name__ == '__main__':
    sys.exit(main())
