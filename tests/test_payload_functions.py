"""Offline unit tests for the ported transcription helper functions.

ZERO COST. No Snowflake connection, no GPU, no LLM calls:

    pytest tests/test_payload_functions.py -v

WHY THIS EXISTS

`scripts/payload/transcribe_functions.py` is the portable core being extracted from
notebook cell 19 for the job-service port. The port rewrites ~1,800 notebook lines into
~700 lines of headless Python, and the port plan originally validated that only through
expensive GPU end-to-end runs. These functions are pure or near-pure, so they can be
checked here for free instead.

`parse_summary_sections` is the highest-risk function in the port: 61 lines with 14
branches, producing MEETING_TITLE, CALL_BRIEF, KEY_POINTS, NEXT_STEPS. The port plan's
only assertion on it was that those fields are "non-null and structurally correct",
which a mis-ported branch can satisfy while returning the wrong text.

TWO REAL BUGS WERE CAUGHT BY WRITING THESE, both silent-NULL failures rather than
crashes, which is why they survived so long:

  1. The notebook was missing `import re` for roughly six months. Every title
     extraction raised NameError inside a bare `except`, so MEETING_TITLE was stored as
     NULL. 42.5% of rows before the 2026-08-18 fix have a title, versus 100% of the 55
     rows after. Section parsing needs no regex, which is why CALL_BRIEF (221 rows)
     outnumbers MEETING_TITLE (185) in the pre-fix cohort.
  2. During extraction, `import datetime` was written where the notebook has
     `from datetime import datetime`. `datetime.strptime` then raises AttributeError,
     swallowed by the same kind of bare except, making call_start_ts None for EVERY
     file. Caught here before it reached the payload.

Both are guarded by explicit regression tests below. Neither would have been caught by
the port plan's 23-column row diff, because a NULL column looks like a legitimately
empty one.

COMPANION: tests/test_payload_parity.py checks these same functions against ~493
historical rows in TRANSCRIPTION_RESULTS, which stores both their inputs and outputs.
"""

import os
import subprocess
import sys
import tempfile
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'payload'))

import transcribe_functions as tf  # noqa: E402


# ---------------------------------------------------------------------------
# regression guards for the two silent-NULL bugs
# ---------------------------------------------------------------------------

def test_re_module_is_importable_in_the_module_namespace():
    """Guards the bug that cost six months of MEETING_TITLE values.

    parse_summary_sections uses re.search inside a try/except that swallows NameError,
    so a missing import degrades silently instead of failing. Assert the name is bound.
    """
    assert hasattr(tf, 're'), "the `re` import is missing again; titles will be NULL"
    assert tf.re.search(r'^#\s*Meeting Summary:\s*(.+)$',
                        '# Meeting Summary: Test', tf.re.MULTILINE) is not None


def test_datetime_is_the_class_not_the_module():
    """Guards the bug introduced during extraction.

    parse_filename_metadata calls `datetime.strptime` directly. With `import datetime`
    that is an AttributeError, swallowed by a bare except, so call_start_ts becomes
    None for every file with no error anywhere.
    """
    assert hasattr(tf.datetime, 'strptime'), (
        "datetime must be the CLASS (from datetime import datetime), not the module"
    )


def test_importing_the_module_prints_nothing():
    """A library must not print on import. Cell 19 ends with a print; it is dropped."""
    result = subprocess.run(
        [sys.executable, '-c',
         'import sys; sys.path.insert(0, %r); import transcribe_functions'
         % os.path.join(os.path.dirname(__file__), '..', 'scripts', 'payload')],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout == '', "module prints on import: %r" % result.stdout


# ---------------------------------------------------------------------------
# format_timestamp_srt - feeds every SRT file, so an error here ships broken subtitles
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("seconds,expected", [
    (0,        '00:00:00,000'),
    (0.5,      '00:00:00,500'),
    (1,        '00:00:01,000'),
    (59.999,   '00:00:59,999'),
    (60,       '00:01:00,000'),
    (3600,     '01:00:00,000'),
    (3661.5,   '01:01:01,500'),
    (36000,    '10:00:00,000'),
])
def test_format_timestamp_srt(seconds, expected):
    assert tf.format_timestamp_srt(seconds) == expected


def test_format_timestamp_srt_truncates_milliseconds_and_must_keep_doing_so():
    """Documents a real 1ms truncation artifact. DO NOT "fix" this.

    The function computes `int((seconds % 1) * 1000)`, which TRUNCATES. Because a float
    keeps less precision for its fraction as the integer part grows, large timestamps
    can land 1ms low:

        3599.999 % 1  ->  0.998999999999796...   int(*1000) -> 998   (not 999)
          59.999 % 1  ->  0.999000000000002...   int(*1000) -> 999

    So `format_timestamp_srt(3599.999)` is '00:59:59,998'. Switching to round() would
    give 999 and look more correct in isolation.

    It must not be changed, because all ~493 SRT_CONTENT values already stored in
    TRANSCRIPTION_RESULTS were generated with truncation. Rounding would make
    tests/test_payload_parity.py fail on every long recording, and would silently
    rewrite subtitle timings for the whole historical corpus. A 1ms subtitle offset is
    inaudible; a broken parity guard is not.

    If the rounding behaviour is ever genuinely wanted, change it as a separate,
    deliberate migration with the stored SRTs regenerated, not as a drive-by tidy-up.
    """
    assert tf.format_timestamp_srt(3599.999) == '00:59:59,998'
    assert tf.format_timestamp_srt(59.999) == '00:00:59,999'


def test_format_timestamp_srt_is_zero_padded():
    """SRT requires HH:MM:SS,mmm. Unpadded values are silently rejected by players."""
    out = tf.format_timestamp_srt(1.001)
    assert len(out) == len('00:00:00,000')
    assert out.count(':') == 2 and out.count(',') == 1


# ---------------------------------------------------------------------------
# parse_filename_metadata
# ---------------------------------------------------------------------------

def test_parse_filename_metadata_happy_path():
    r = tf.parse_filename_metadata('2026-03-04 14-03-35_Moodys_call.mp4')
    assert r['account_name'] == 'Moodys'
    assert r['call_start_ts'] == datetime(2026, 3, 4, 14, 3, 35)


def test_parse_filename_metadata_account_only_no_rest():
    r = tf.parse_filename_metadata('2026-03-04 14-03-35_Moodys.mp4')
    assert r['account_name'] == 'Moodys.mp4'
    assert r['call_start_ts'] == datetime(2026, 3, 4, 14, 3, 35)


@pytest.mark.parametrize("name", [
    'no_timestamp_here.mp4',
    'garbage',
    '',
    '2026-13-45 99-99-99_Bad.mp4',
])
def test_parse_filename_metadata_degrades_without_raising(name):
    """Must never raise: it runs per file inside the transcription loop."""
    r = tf.parse_filename_metadata(name)
    assert set(r) == {'account_name', 'call_start_ts'}


def test_parse_filename_metadata_unparseable_time_yields_none_ts():
    r = tf.parse_filename_metadata('notadate_Acme_call.mp4')
    assert r['account_name'] == 'Acme'
    assert r['call_start_ts'] is None


# ---------------------------------------------------------------------------
# parse_summary_sections - 14 branches, the highest port risk
# ---------------------------------------------------------------------------

FULL_SUMMARY = """# Meeting Summary: Quarterly Review with Acme

**Summary**
Discussed renewal terms and the migration timeline.

Key Topics
- Pricing
- Migration

Follow-up Items
- Send revised quote

Decisions Made
- Proceed with phase one

Questions Raised
- What is the data residency requirement?
"""


def test_parse_summary_sections_full():
    r = tf.parse_summary_sections(FULL_SUMMARY)
    assert r['meeting_title'] == 'Quarterly Review with Acme'
    assert 'renewal terms' in r['call_brief']
    assert 'Pricing' in r['key_points']
    assert 'revised quote' in r['next_steps']
    assert 'phase one' in r['decisions_made']
    assert 'data residency' in r['questions_raised']


@pytest.mark.parametrize("content", [None, '', '   '])
def test_parse_summary_sections_empty_input(content):
    r = tf.parse_summary_sections(content)
    assert set(r) == {'meeting_title', 'call_brief', 'key_points', 'next_steps',
                      'decisions_made', 'questions_raised'}
    assert all(v is None for v in r.values())


def test_parse_summary_sections_title_only():
    r = tf.parse_summary_sections('# Meeting Summary: Just A Title\n\nsome prose\n')
    assert r['meeting_title'] == 'Just A Title'
    assert r['call_brief'] is None


def test_parse_summary_sections_sections_without_title():
    """Real cohort: 36 historical rows have sections but no title, because section
    parsing needs no regex while the title does."""
    r = tf.parse_summary_sections('Key Topics\n- A\n\nFollow-up Items\n- B\n')
    assert r['meeting_title'] is None
    assert 'A' in r['key_points']
    assert 'B' in r['next_steps']


def test_parse_summary_sections_reordered_sections():
    r = tf.parse_summary_sections(
        'Follow-up Items\n- first\n\nKey Topics\n- second\n')
    assert 'first' in r['next_steps']
    assert 'second' in r['key_points']


def test_parse_summary_sections_no_recognised_markers():
    """Documents the KNOWN brittleness rather than asserting it is good.

    Headers are matched by exact string equality on stripped lines, so '## Key Topics'
    does not match 'Key Topics'. This is why only 39 of 240 parsed historical rows have
    a '**Summary**' marker. Preserved verbatim for the port; fix separately.
    """
    r = tf.parse_summary_sections('## Key Topics\n- A\n\n## Follow-up Items\n- B\n')
    assert r['key_points'] is None, "brittleness changed; parity vs history will break"
    assert r['next_steps'] is None


def test_parse_summary_sections_last_section_is_captured():
    """Guards the trailing-section flush after the loop ends."""
    r = tf.parse_summary_sections('Questions Raised\n- only section\n')
    assert 'only section' in r['questions_raised']


def test_parse_summary_sections_never_raises_on_odd_input():
    for junk in ['\n\n\n', '**Summary**', 'Key Topics', '# Meeting Summary:',
                 'x' * 10000]:
        assert isinstance(tf.parse_summary_sections(junk), dict)


# ---------------------------------------------------------------------------
# SRT generation
# ---------------------------------------------------------------------------

SEGMENTS = {'speakers': [
    {'start_time': 0.0, 'end_time': 2.5, 'text': '  hello  ', 'speaker': 'Speaker 1'},
    {'start_time': 2.5, 'end_time': 5.0, 'text': 'world',     'speaker': 'Speaker 2'},
]}


def test_generate_srt_content_structure():
    out = tf.generate_srt_content(SEGMENTS)
    lines = out.split('\n')
    assert lines[0] == '1', "SRT indices must start at 1, not 0"
    assert lines[1] == '00:00:00,000 --> 00:00:02,500'
    assert lines[2] == 'hello', "segment text must be stripped"
    assert lines[3] == ''
    assert lines[4] == '2'


@pytest.mark.parametrize("bad", [None, {}, {'no_speakers_key': []}])
def test_generate_srt_content_returns_none_without_segments(bad):
    assert tf.generate_srt_content(bad) is None


def test_generate_srt_content_empty_segment_list():
    assert tf.generate_srt_content({'speakers': []}) == ''


def test_generate_srt_with_speakers_includes_labels():
    out = tf.generate_srt_with_speakers(SEGMENTS)
    assert 'Speaker 1' in out and 'Speaker 2' in out


def test_srt_variants_have_the_same_segment_count():
    """Both feed columns on the same row; a divergence means one dropped a segment."""
    plain = tf.generate_srt_content(SEGMENTS)
    spk = tf.generate_srt_with_speakers(SEGMENTS)
    assert plain.count(' --> ') == spk.count(' --> ') == len(SEGMENTS['speakers'])


# ---------------------------------------------------------------------------
# filesystem-touching helpers
# ---------------------------------------------------------------------------

def test_get_file_size():
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b'x' * 1234)
        path = fh.name
    try:
        assert tf.get_file_size(path) == 1234
    finally:
        os.remove(path)


def test_get_file_size_missing_file_does_not_raise():
    assert tf.get_file_size('/tmp/definitely-not-here-%d' % os.getpid()) in (0, None)


@pytest.mark.skipif(not __import__('shutil').which('ffmpeg'),
                    reason="ffmpeg not installed locally")
def test_extract_audio_from_video_reports_failure_on_a_non_video():
    """Must return a falsey success rather than raising, since the caller branches on it."""
    with tempfile.TemporaryDirectory() as d:
        bogus = os.path.join(d, 'not-a-video.mp4')
        with open(bogus, 'wb') as fh:
            fh.write(b'this is not video data')
        success, duration = tf.extract_audio_from_video(
            bogus, os.path.join(d, 'out.wav'))
        assert success is False or success == 0


# ---------------------------------------------------------------------------
# the port's contract with the SQL gate
# ---------------------------------------------------------------------------

def test_generate_summary_markdown_requires_an_explicit_session():
    """The ONE intended deviation from cell 19.

    The notebook read a `session` global, which cannot be injected in a headless payload
    or mocked in a test. Assert the parameter exists so nobody silently reverts it.
    """
    import inspect
    params = list(inspect.signature(tf.generate_summary_markdown).parameters)
    assert params[0] == 'session', (
        "generate_summary_markdown must take session explicitly; got %s" % params)
