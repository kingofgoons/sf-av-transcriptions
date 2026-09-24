"""Golden-corpus parity tests for the ported transcription functions.

ZERO COST, FULLY OFFLINE. No Snowflake connection, no GPU, no LLM:

    pytest tests/test_payload_parity.py -v

WHY THIS EXISTS

TRANSCRIPTION_RESULTS stores both the INPUTS and the OUTPUTS of most functions in
`scripts/payload/transcribe_functions.py`:

    TRANSCRIPT_WITH_SPEAKERS  ->  SRT_CONTENT, SRT_WITH_SPEAKERS
    SUMMARY_MARKDOWN          ->  MEETING_TITLE, CALL_BRIEF, KEY_POINTS, NEXT_STEPS,
                                  DECISIONS_MADE, QUESTIONS_RAISED
    FILE_NAME                 ->  ACCOUNT_NAME, CALL_START_TS

So the historical table is a free regression corpus: run the ported function over the
stored input, assert it reproduces the stored output. The port plan originally validated
a 1,800-to-700 line rewrite only through expensive GPU end-to-end runs, checking a single
row by hand. This checks 239 real rows every time the suite runs.

Fixtures are snapshotted by `scripts/extract_golden_corpus.py` into
tests/fixtures/golden/. Re-run that only to widen the corpus deliberately.

ONLY CURRENT-CODE-ERA ROWS ARE IN THE CORPUS

This is the constraint that makes the whole file meaningful, and the first version of it
got this wrong. A stored value is evidence about the port only if the code that wrote it
is the code being ported. This table spans a year, and the pipeline changed twice in ways
that alter output:

  - The SRT generator stopped skipping empty-text segments between 2026-01 and 2026-02.
    Before the change, 80 of 116 rows have fewer SRT entries than transcript segments.
    After it, 0 of 377.
  - The summary prompt emitted '## Key Topics'-style markdown headings from 2026-02-10 to
    2026-08-17. parse_summary_sections matches bare headers by exact string equality, so
    it extracts nothing from those rows.

Running unscoped produced 6 SRT and 72 summary failures that were pure historical drift.
The extractor now filters on TRANSCRIPTION_TIMESTAMP - the PROCESSING date. Do not filter
on the date in FILE_NAME: that is the MEETING date, it trails processing by weeks, and
grouping by it makes the eras look interleaved and hides the boundaries entirely.

Two tests here assert the scoping held (test_srt_cohort_is_era_consistent and
test_summary_corpus_contains_no_off_era_rows), so a regression in the extractor fails
loudly at the cause instead of as hundreds of downstream mismatches.

WHAT "PARITY" MEANS HERE, AND WHY IT CONSTRAINS THE PORT

These tests assert the ported code reproduces what the NOTEBOOK produced, including its
quirks. That is the point: while porting, faithfulness is the property worth having,
because a mismatch then unambiguously means a port bug rather than an intended
improvement. Two known quirks are pinned deliberately:

  - `format_timestamp_srt` truncates milliseconds instead of rounding, so long
    recordings can land 1ms low. Every stored SRT was generated that way.
  - `parse_summary_sections` matches section headers by exact string equality, so any
    drift in the LLM's markdown silently yields NULL for that section. Widening it would
    also make the 2026-02..08 rows re-derivable.

Improving either is worthwhile, but as a separate change, with the stored values
regenerated and these fixtures re-extracted. Not as a drive-by tidy-up.

NULL-OUTPUT ROWS ARE ALSO EXCLUDED

Roughly 45% of rows have NULL parsed fields because the notebook was missing `import re`
for about six months. The NameError fires on the very first statement of
parse_summary_sections, so the caller's bare `except` swallowed it and ALL SIX fields came
back NULL - not just the title. Those rows are the BACKFILL population, not parity
evidence: a NULL output there means the parser never ran, not that NULL is correct.
"""

import gzip
import hashlib
import json
import os
import re
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'payload'))

import transcribe_functions as tf  # noqa: E402

GOLDEN = os.path.join(os.path.dirname(__file__), 'fixtures', 'golden')


# ---------------------------------------------------------------------------
# fixture loading
# ---------------------------------------------------------------------------

def _load(name):
    """Read a cohort, transparently handling the gzipped ones."""
    gz = os.path.join(GOLDEN, name + '.jsonl.gz')
    plain = os.path.join(GOLDEN, name + '.jsonl')
    if os.path.exists(gz):
        with gzip.open(gz, 'rt') as fh:
            return [json.loads(l) for l in fh if l.strip()]
    if os.path.exists(plain):
        with open(plain) as fh:
            return [json.loads(l) for l in fh if l.strip()]
    pytest.skip('golden corpus missing; run scripts/extract_golden_corpus.py')


def _segments(row):
    """TRANSCRIPT_WITH_SPEAKERS arrives as a JSON string from the CLI extract.

    Returns the whole structure, not a list: generate_srt_content takes the dict and
    reads its 'speakers' key. The dict also carries 'file_info' and 'full_transcript'.
    """
    raw = row['TRANSCRIPT_WITH_SPEAKERS']
    return json.loads(raw) if isinstance(raw, str) else raw


def sha(text):
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


# The footer and separator that generate_summary_markdown wraps around the LLM output.
_FOOTER = '\n---\n*Generated by Snowflake Cortex AI*'
_SEP = '\n---\n'


def inner_summary(markdown):
    """Recover the raw LLM text that parse_summary_sections was actually given.

    THIS MATTERS, and getting it wrong makes every row fail. generate_summary_markdown
    does NOT parse what it stores. It builds a wrapper document:

        # Transcription Summary: {file_name}
        **Generated:** ... **Language:** ... **Duration:** ...
        ---
        {summary_content}          <- only THIS is passed to the parser
        ---
        *Generated by Snowflake Cortex AI*

    then calls parse_summary_sections(summary_content) on the INNER text while storing
    the wrapper as SUMMARY_MARKDOWN. Feeding the stored wrapper straight back in sweeps
    the 40-character footer into the final section, so QUESTIONS_RAISED - which is last -
    mismatched on 240 of 240 rows on the first attempt.

    Uses rfind for the footer and find for the separator, because the LLM's own content
    can legitimately contain '---' horizontal rules.
    """
    body = markdown
    idx = body.rfind(_FOOTER)
    if idx != -1:
        body = body[:idx]
    idx = body.find(_SEP)
    if idx != -1:
        body = body[idx + len(_SEP):]
    return body.strip()


SRT_ROWS = _load('srt')
SUMMARY_ROWS = _load('summary')
FILENAME_ROWS = _load('filename')


# ---------------------------------------------------------------------------
# corpus integrity - guards the tests themselves
# ---------------------------------------------------------------------------

def test_corpus_is_present_and_non_trivial():
    assert len(SRT_ROWS) >= 15, 'SRT sample too small to be meaningful'
    assert len(SUMMARY_ROWS) >= 50, 'summary corpus unexpectedly small'
    assert len(FILENAME_ROWS) >= 120, 'filename corpus unexpectedly small'


def test_srt_cohort_is_era_consistent():
    """Every stored SRT must have one entry per transcript segment.

    The pre-2026-02 generator skipped segments whose text was empty, so its SRTs have
    fewer entries than the transcript has segments. That is the single cleanest signal
    that an out-of-era row slipped into the corpus, and it is exactly what produced the
    first run's 6 SRT failures (deltas of +1, +4, +5, +6 segments, each equal to that
    row's count of empty-text segments).
    """
    for row in SRT_ROWS:
        n_segments = len(_segments(row)['speakers'])
        assert row['SEGS'] == n_segments, (
            '%s: stored SRT has %d entries but the transcript has %d segments, so this row '
            'predates the 2026-02 generator change. Re-extract with SRT_ERA_START set.'
            % (row['FILE_NAME'], row['SEGS'], n_segments))


def test_srt_sample_spans_short_and_very_long_recordings():
    """The sample must reach the corpus maximum.

    Segment count tracks recording length, so it also tracks timestamp magnitude - and
    float precision in format_timestamp_srt degrades as the hour field grows. A sample
    that stops at mid-length recordings would not exercise that. An earlier version of
    the extractor topped out at 910 of 2869 segments for exactly this reason.
    """
    segs = sorted(r['SEGS'] for r in SRT_ROWS)
    assert segs[0] < 150, 'no short recording in the sample'
    assert segs[-1] > 1500, 'no very long recording in the sample: max is %d' % segs[-1]


def test_corpus_contains_no_null_output_rows():
    """A NULL output is never parity evidence; the extractor must have filtered them."""
    assert all(r['MEETING_TITLE'] for r in SUMMARY_ROWS)
    assert all(r['ACCOUNT_NAME'] for r in FILENAME_ROWS)


# ---------------------------------------------------------------------------
# SRT regeneration - transitively validates format_timestamp_srt
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", SRT_ROWS, ids=[r['FILE_NAME'][:40] for r in SRT_ROWS])
def test_srt_content_reproduces_stored_value(row):
    """Regenerate SRT_CONTENT from the stored segments and compare by hash.

    Expected output is stored as SHA-256 rather than full text: the two SRTs add ~86 KB
    per row and are fully derivable from the transcript, so a full dump was 49 MB. A hash
    mismatch fails just as loudly for 64 bytes.
    """
    produced = tf.generate_srt_content(_segments(row))
    assert produced is not None, 'generator returned None for a row that has stored SRT'
    assert len(produced) == row['SRT_CONTENT_LEN'], (
        'length differs: produced %d, stored %d' % (len(produced), row['SRT_CONTENT_LEN']))
    assert sha(produced) == row['SRT_CONTENT_SHA256'], (
        'SRT_CONTENT differs from the stored value for %s (%d segments)'
        % (row['FILE_NAME'], row['SEGS']))


@pytest.mark.parametrize("row", SRT_ROWS, ids=[r['FILE_NAME'][:40] for r in SRT_ROWS])
def test_srt_with_speakers_reproduces_stored_value(row):
    if not row.get('SRT_WITH_SPEAKERS_SHA256'):
        pytest.skip('no stored SRT_WITH_SPEAKERS for this row')
    produced = tf.generate_srt_with_speakers(_segments(row))
    assert produced is not None
    assert sha(produced) == row['SRT_WITH_SPEAKERS_SHA256'], (
        'SRT_WITH_SPEAKERS differs for %s' % row['FILE_NAME'])


def test_srt_segment_counts_match_stored():
    """A count mismatch means segments were dropped or duplicated, which a hash check
    alone would report without saying why."""
    for row in SRT_ROWS:
        produced = tf.generate_srt_content(_segments(row))
        assert produced.count(' --> ') == row['SEGS'], (
            '%s: produced %d segments, stored %d'
            % (row['FILE_NAME'], produced.count(' --> '), row['SEGS']))


# ---------------------------------------------------------------------------
# summary parser - the highest-risk function in the port
# ---------------------------------------------------------------------------

SUMMARY_FIELDS = [
    ('call_brief',       'CALL_BRIEF'),
    ('key_points',       'KEY_POINTS'),
    ('next_steps',       'NEXT_STEPS'),
    ('decisions_made',   'DECISIONS_MADE'),
    ('questions_raised', 'QUESTIONS_RAISED'),
]

# Header style the CURRENT prompt and parser agree on. Rows from the 2026-02-10 to
# 2026-08-17 window used '## Key Topics' markdown headings instead, which the parser
# cannot match because it compares bare headers by exact string equality. Those rows are
# excluded at extract time (see SUMMARY_ERA_START in scripts/extract_golden_corpus.py);
# this pattern is the assertion that the exclusion actually worked.
_OFF_ERA_HEADERS = re.compile(
    r'^##\s+(Summary|Key Topics|Follow-up Items|Decisions Made|Questions Raised)\s*$',
    re.MULTILINE)


def test_summary_corpus_contains_no_off_era_rows():
    """Guards the era filter, which is what makes the parity assertion meaningful.

    Without the filter this suite reported 72 summary rows and 6 SRT rows as failures that
    were really historical drift. If the extractor's cutoff regresses, fail here - with a
    pointer - rather than producing 360 confusing field mismatches downstream.
    """
    off = [r['FILE_NAME'] for r in SUMMARY_ROWS
           if _OFF_ERA_HEADERS.search(inner_summary(r['SUMMARY_MARKDOWN']))]
    assert not off, (
        '%d rows use the 2026-02..2026-08 "##" header format and cannot be reproduced by '
        'the current parser. Re-extract with SUMMARY_ERA_START set correctly. First: %s'
        % (len(off), off[:3]))


def test_summary_parser_reproduces_every_stored_field():
    """Run parse_summary_sections over every in-era LLM output, demanding exact equality.

    61 lines, 14 branches, feeding four user-visible columns. The port plan's only check
    was that these are "non-null and structurally correct", which a mis-ported branch can
    satisfy while returning the wrong text. This compares actual values.
    """
    mismatches = []
    for row in SUMMARY_ROWS:
        produced = tf.parse_summary_sections(inner_summary(row['SUMMARY_MARKDOWN']))
        for key, column in SUMMARY_FIELDS:
            want, got = row.get(column), produced[key]
            # Stored NULL against produced None is agreement.
            if (want or None) != (got or None):
                mismatches.append((row['FILE_NAME'], column, repr(want)[:70], repr(got)[:70]))
    if mismatches:
        lines = ['%s / %s\n    stored:   %s\n    produced: %s' % m for m in mismatches[:10]]
        pytest.fail('%d field mismatches across %d rows. First %d:\n\n%s'
                    % (len(mismatches), len(SUMMARY_ROWS),
                       min(10, len(mismatches)), '\n'.join(lines)))


def test_meeting_title_reproduces_for_every_row():
    """The title is regex-matched rather than compared for equality, so unlike the five
    section fields it is insensitive to the header-format change."""
    mismatches = []
    for row in SUMMARY_ROWS:
        got = tf.parse_summary_sections(inner_summary(row['SUMMARY_MARKDOWN']))['meeting_title']
        if (row.get('MEETING_TITLE') or None) != (got or None):
            mismatches.append((row['FILE_NAME'], row.get('MEETING_TITLE'), got))
    if mismatches:
        pytest.fail('%d meeting_title mismatches of %d rows. First 5: %s'
                    % (len(mismatches), len(SUMMARY_ROWS), mismatches[:5]))


def test_summary_parser_never_raises_across_the_corpus():
    """It runs per file inside the transcription loop; an exception loses the run."""
    for row in SUMMARY_ROWS:
        assert isinstance(
            tf.parse_summary_sections(inner_summary(row['SUMMARY_MARKDOWN'])), dict)
        # Also on the un-unwrapped wrapper, which is what a careless caller would pass.
        assert isinstance(tf.parse_summary_sections(row['SUMMARY_MARKDOWN']), dict)



# ---------------------------------------------------------------------------
# filename parser
# ---------------------------------------------------------------------------

def test_filename_parser_reproduces_account_name():
    mismatches = []
    for row in FILENAME_ROWS:
        got = tf.parse_filename_metadata(row['FILE_NAME'])['account_name']
        if (row['ACCOUNT_NAME'] or None) != (got or None):
            mismatches.append((row['FILE_NAME'], row['ACCOUNT_NAME'], got))
    if mismatches:
        pytest.fail('%d account_name mismatches. First 5: %s'
                    % (len(mismatches), mismatches[:5]))


def test_filename_parser_reproduces_call_start_ts():
    """Guards the `from datetime import datetime` bug directly.

    With a plain `import datetime`, strptime raises AttributeError into a bare except and
    every call_start_ts silently becomes None. Across 273 rows that would fail loudly
    here rather than shipping NULLs.
    """
    mismatches = []
    for row in FILENAME_ROWS:
        stored = row.get('CALL_START_TS')
        got = tf.parse_filename_metadata(row['FILE_NAME'])['call_start_ts']
        if stored is None:
            continue
        if got is None:
            mismatches.append((row['FILE_NAME'], stored, None))
            continue
        # Stored value arrives as a string from the extract; compare on the second.
        want = str(stored)[:19].replace('T', ' ')
        if got.strftime('%Y-%m-%d %H:%M:%S') != want:
            mismatches.append((row['FILE_NAME'], want, got.strftime('%Y-%m-%d %H:%M:%S')))
    if mismatches:
        pytest.fail('%d call_start_ts mismatches of %d rows. First 5: %s'
                    % (len(mismatches), len(FILENAME_ROWS), mismatches[:5]))


def test_filename_parser_resolves_a_timestamp_for_most_of_the_corpus():
    """Sanity floor. If a refactor silently broke timestamp parsing, the mismatch test
    above would catch rows that HAVE a stored value; this catches wholesale breakage."""
    resolved = sum(1 for r in FILENAME_ROWS
                   if tf.parse_filename_metadata(r['FILE_NAME'])['call_start_ts'])
    assert resolved > len(FILENAME_ROWS) * 0.8, (
        'only %d of %d filenames yielded a timestamp' % (resolved, len(FILENAME_ROWS)))
