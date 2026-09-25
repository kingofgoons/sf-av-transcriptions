"""Tests for the av.uploader scripts, and for the contract between the transcription
payload and the SRT consumer.

WHY THIS FILE EXISTS

`upload_av_files.py` is the PRIMARY production trigger - its whole job is firing the task
that the 2026-09-25 job-service port rewired - and `download_srts.py` consumes
TRANSCRIPT_WITH_SPEAKERS, which the payload writes. Neither had a single test. The port
was validated end-to-end through the dashboard and through the uploader's own service
role, but "we exercised it once" is not a regression guard.

THE REAL RISK THIS GUARDS

`generate_srt_content`, `generate_srt_with_speakers` and `format_timestamp_srt` exist
TWICE, independently implemented:

    scripts/payload/transcribe_functions.py   writes SRT_CONTENT into the table
    av.uploader/download_srts.py              regenerates SRTs at download time

They agree today. Nothing makes them keep agreeing. If they drift, a downloaded .srt
silently stops matching the stored SRT_CONTENT for the same recording, and there is no
error anywhere - which is the same failure shape as the 2026-01/02 SRT generator era that
took a corpus-wide comparison to discover.

These tests compare the two implementations directly rather than asserting a golden
string, so they stay honest if the shared format is deliberately changed in both places.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


def _load(path, name):
    """Import a module from a file path.

    Needed because 'av.uploader' is not a valid package name - the dot makes it
    unimportable by the normal machinery.
    """
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope='module')
def dl():
    p = REPO / 'av.uploader' / 'download_srts.py'
    if not p.exists():
        pytest.skip('download_srts.py not present')
    try:
        return _load(p, '_dl_srts')
    except ImportError as e:
        pytest.skip(f'download_srts.py dependencies unavailable: {e}')


@pytest.fixture(scope='module')
def up():
    p = REPO / 'av.uploader' / 'upload_av_files.py'
    if not p.exists():
        pytest.skip('upload_av_files.py not present')
    try:
        return _load(p, '_up_av')
    except ImportError as e:
        pytest.skip(f'upload_av_files.py dependencies unavailable: {e}')


@pytest.fixture(scope='module')
def tf():
    p = REPO / 'scripts' / 'payload' / 'transcribe_functions.py'
    if not p.exists():
        pytest.skip('transcribe_functions.py not present')
    return _load(p, '_tf_payload')


# A segment list shaped exactly as the payload writes it. Verified 2026-09-25 against a
# real job-service-written row: TRANSCRIPT_WITH_SPEAKERS has top-level keys
# {file_info, full_transcript, speakers} and each segment carries
# {duration, end_time, speaker, start_time, text}.
PAYLOAD_SHAPED = {
    'file_info': {'file_name': 'x.mp4'},
    'full_transcript': "I'll do this. Oh, there you are.",
    'speakers': [
        {'start_time': 3.22, 'end_time': 4.0, 'speaker': 'Speaker_0',
         'text': " I'll do this. ", 'duration': 0.78},
        {'start_time': 5.16, 'end_time': 5.96, 'speaker': 'Speaker_1',
         'text': 'Oh, there you are.', 'duration': 0.8},
        # Crosses an hour boundary, which is where a naive divmod implementation breaks.
        {'start_time': 3661.5, 'end_time': 3665.25, 'speaker': 'Speaker_0',
         'text': 'Later in the call.', 'duration': 3.75},
    ],
}


# ---------------------------------------------------------------------------
# The contract that actually matters: writer and reader must agree
# ---------------------------------------------------------------------------

def test_srt_generators_agree_between_payload_and_downloader(tf, dl):
    """The stored SRT_CONTENT and a downloaded .srt must be byte-identical.

    Comparing the two implementations, not a golden string: if the format is changed
    deliberately in both places this still passes, but a change in only one fails.
    """
    assert tf.generate_srt_content(PAYLOAD_SHAPED) == dl.generate_srt_content(PAYLOAD_SHAPED)


def test_speaker_srt_generators_agree(tf, dl):
    assert (tf.generate_srt_with_speakers(PAYLOAD_SHAPED)
            == dl.generate_srt_with_speakers(PAYLOAD_SHAPED))


def test_timestamp_formatters_agree(tf, dl):
    """Includes an hour rollover and a sub-millisecond value.

    3661.5 -> 01:01:01,500 is the case that catches an implementation that forgot to
    take minutes modulo 60.
    """
    for secs in (0.0, 0.001, 3.22, 59.999, 60.0, 3599.9, 3661.5, 7322.25):
        assert tf.format_timestamp_srt(secs) == dl.format_timestamp_srt(secs), secs


def test_timestamp_format_is_srt_compliant(dl):
    assert dl.format_timestamp_srt(0.0) == '00:00:00,000'
    assert dl.format_timestamp_srt(3.22) == '00:00:03,220'
    assert dl.format_timestamp_srt(3661.5) == '01:01:01,500'


# ---------------------------------------------------------------------------
# download_srts.py behaviour
# ---------------------------------------------------------------------------

def test_downloader_reads_the_key_the_payload_writes(dl):
    """The payload stores segments under 'speakers'. A reader looking for 'segments'
    would return None for every row and report "0 written" with no error at all."""
    out = dl.generate_srt_content(PAYLOAD_SHAPED)
    assert out is not None, "downloader could not read payload-shaped data"
    assert out.count(' --> ') == len(PAYLOAD_SHAPED['speakers'])


def test_downloader_emits_one_cue_per_segment_numbered_from_one(dl):
    lines = dl.generate_srt_content(PAYLOAD_SHAPED).split('\n')
    assert lines[0] == '1', "SRT cues must be 1-indexed"
    assert lines[1] == '00:00:03,220 --> 00:00:04,000'
    assert lines[2] == "I'll do this.", "leading/trailing whitespace must be stripped"


def test_downloader_labels_speakers_and_defaults_unknown(dl):
    out = dl.generate_srt_with_speakers(PAYLOAD_SHAPED)
    assert '[Speaker_0] ' in out and '[Speaker_1] ' in out

    no_speaker = {'speakers': [{'start_time': 0.0, 'end_time': 1.0, 'text': 'hi'}]}
    assert '[Unknown] hi' in dl.generate_srt_with_speakers(no_speaker)


@pytest.mark.parametrize('bad', [None, {}, {'full_transcript': 'x'}, {'segments': []}])
def test_downloader_returns_none_rather_than_raising(dl, bad):
    """A row with no usable segments must be SKIPPED, not crash the whole batch.

    'segments' is included deliberately: it is the plausible-but-wrong key, and the one
    a future refactor is most likely to introduce.
    """
    assert dl.generate_srt_content(bad) is None
    assert dl.generate_srt_with_speakers(bad) is None


def test_payload_generators_are_equally_defensive(tf):
    assert tf.generate_srt_content(None) is None
    assert tf.generate_srt_content({'segments': []}) is None


def test_srt_stem_drops_the_media_extension(dl):
    assert dl.srt_stem('2026-03-17 10-22-35_Acme_sync.mp4') == '2026-03-17 10-22-35_Acme_sync'
    assert dl.srt_stem('a.b.c.mp3') == 'a.b.c'


# ---------------------------------------------------------------------------
# upload_av_files.py — the production trigger
# ---------------------------------------------------------------------------

def test_task_name_is_fully_qualified(up):
    """The uploader calls EXECUTE TASK on this string. An unqualified name resolves
    against whatever schema the session happens to be in."""
    name = up.resolve_task_name({'database': 'DB1', 'schema': 'SCH1'})
    assert name == 'DB1.SCH1.TRANSCRIBE_NEW_FILES_TASK_V2'


def test_task_name_override_key_is_transcription_task(up):
    """The override key is `transcription_task`, and it is returned VERBATIM - the
    database/schema are not prepended. Worth pinning: a config carrying a bare task name
    under this key would produce an unqualified EXECUTE TASK, and the name looks
    plausible enough that `task_name` is the natural wrong guess (it is ignored).
    """
    name = up.resolve_task_name({
        'database': 'DB1', 'schema': 'SCH1',
        'transcription_task': 'OTHER_DB.OTHER_SCH.T'})
    assert name == 'OTHER_DB.OTHER_SCH.T'

    # A key that merely looks right has no effect, and the default still wins.
    ignored = up.resolve_task_name({
        'database': 'DB1', 'schema': 'SCH1', 'task_name': 'NOT_USED'})
    assert ignored == 'DB1.SCH1.TRANSCRIBE_NEW_FILES_TASK_V2'


def test_uploader_does_not_reference_execute_notebook():
    """The launch mechanism moved to EXECUTE JOB SERVICE on 2026-09-25. The uploader's
    trigger contract is unchanged, but its documentation described the old mechanism,
    and stale operational docs in this project have already cost real money once - see
    the CALL TRANSCRIBE_IF_NEW_FILES() claim that said it did not launch a container.
    """
    text = (REPO / 'av.uploader' / 'upload_av_files.py').read_text()
    offending = [ln.strip() for ln in text.splitlines()
                 if 'EXECUTE NOTEBOOK' in ln and 'rather than' not in ln]
    assert not offending, f"stale EXECUTE NOTEBOOK references: {offending}"


def test_format_size_is_human_readable(up):
    assert up.format_size(0).startswith('0')
    assert 'KB' in up.format_size(2048)
    assert 'MB' in up.format_size(5 * 1024 * 1024)


# ---------------------------------------------------------------------------
# Date-window resolution and the local -> UTC conversion
#
# TRANSCRIPTION_TIMESTAMP is TIMESTAMP_NTZ holding UTC. Before this conversion existed,
# `--start X --end X` compared a local-intent date straight against that UTC column, so in
# US Eastern it actually returned 20:00 the previous day through 20:00 on X - missing that
# evening's transcripts. These tests pin TZ so they assert the arithmetic, not the machine.
# ---------------------------------------------------------------------------

import os
import time as _time
from argparse import Namespace
from datetime import date, datetime, timedelta


@pytest.fixture
def eastern():
    """Pin the process timezone to America/New_York for one test, then restore it.

    time.tzset() is process-global, so the restore matters - without it a later test
    inherits Eastern and passes or fails for the wrong reason.
    """
    prior = os.environ.get('TZ')
    os.environ['TZ'] = 'America/New_York'
    _time.tzset()
    yield
    if prior is None:
        os.environ.pop('TZ', None)
    else:
        os.environ['TZ'] = prior
    _time.tzset()


def _args(**kw):
    """An argparse-shaped stub. resolve_dates uses getattr defaults, so omitted flags are
    absent rather than None, which is also how a partially-built Namespace behaves."""
    base = {'today': False, 'yesterday': False, 'days': None, 'start': None, 'end': None}
    base.update(kw)
    return Namespace(**base)


TODAY = date(2026, 9, 25)


def test_today_resolves_to_a_single_local_day(dl):
    start, end, label = dl.resolve_dates(_args(today=True), today=TODAY)
    assert (start, end) == (TODAY, TODAY)
    assert label == 'today'


def test_yesterday_resolves_to_the_prior_local_day(dl):
    start, end, _ = dl.resolve_dates(_args(yesterday=True), today=TODAY)
    assert (start, end) == (date(2026, 9, 24), date(2026, 9, 24))


def test_days_counts_back_inclusive_of_today(dl):
    start, end, _ = dl.resolve_dates(_args(days=3), today=TODAY)
    assert (start, end) == (date(2026, 9, 23), TODAY), "3 days = today + 2 back"


def test_days_one_is_exactly_today(dl):
    """Documented in --help, so it is a contract: --days 1 must not be off by one."""
    assert (dl.resolve_dates(_args(days=1), today=TODAY)[:2]
            == dl.resolve_dates(_args(today=True), today=TODAY)[:2])


def test_explicit_range_parses_both_ends(dl):
    start, end, label = dl.resolve_dates(
        _args(start='2026-09-01', end='2026-09-25'), today=TODAY)
    assert (start, end) == (date(2026, 9, 1), date(2026, 9, 25))
    assert label == 'explicit range'


def test_explicit_single_date_is_allowed(dl):
    start, end, _ = dl.resolve_dates(
        _args(start='2026-09-25', end='2026-09-25'), today=TODAY)
    assert start == end == TODAY


@pytest.mark.parametrize('kwargs,expect', [
    ({},                                              'No date range'),
    ({'start': '2026-09-25'},                         '--start requires --end'),
    ({'end': '2026-09-25'},                           '--end requires --start'),
    ({'today': True, 'start': '2026-09-25', 'end': '2026-09-25'}, 'cannot be combined'),
    ({'days': 0},                                     '--days must be 1 or greater'),
    ({'days': -1},                                    '--days must be 1 or greater'),
    ({'start': '2026-09-26', 'end': '2026-09-25'},    'must be on or before'),
    ({'start': 'not-a-date', 'end': '2026-09-25'},    'YYYY-MM-DD'),
    ({'start': '2026-09-25', 'end': '09/25/2026'},    'YYYY-MM-DD'),
    ({'yesterday': True, 'start': '2026-09-25', 'end': '2026-09-25'}, 'cannot be combined'),
])
def test_bad_flag_combinations_raise_naming_the_flag(dl, kwargs, expect):
    """Every rejection has to say which flag is wrong - 'invalid arguments' sends the
    operator back to the source."""
    with pytest.raises(ValueError) as e:
        dl.resolve_dates(_args(**kwargs), today=TODAY)
    assert expect.lower() in str(e.value).lower(), f"unhelpful message: {e.value}"


def test_bounds_shift_by_the_local_offset(dl, eastern):
    """2026-09-25 is EDT (UTC-4), so the local day is 04:00 UTC to 04:00 UTC next day.

    The old code sent '2026-09-25' and DATEADD'd a day, giving 00:00-00:00 UTC - four hours
    early at both ends.
    """
    start_utc, end_utc = dl.local_day_bounds_utc(TODAY, TODAY)
    assert start_utc == datetime(2026, 9, 25, 4, 0, 0)
    assert end_utc == datetime(2026, 9, 26, 4, 0, 0)


def test_winter_dates_use_the_five_hour_offset(dl, eastern):
    """EST, not EDT. A hardcoded -4 would be wrong for half the year."""
    start_utc, _ = dl.local_day_bounds_utc(date(2026, 1, 15), date(2026, 1, 15))
    assert start_utc == datetime(2026, 1, 15, 5, 0, 0)


def test_bounds_are_naive_so_the_connector_cannot_bind_an_offset(dl, eastern):
    """The column is TIMESTAMP_NTZ. An aware datetime would carry an offset the column
    cannot hold, and the driver's coercion is not something to rely on."""
    start_utc, end_utc = dl.local_day_bounds_utc(TODAY, TODAY)
    assert start_utc.tzinfo is None and end_utc.tzinfo is None


def test_consecutive_days_abut_exactly_without_overlap(dl, eastern):
    """Half-open [start, end). Yesterday's upper bound must equal today's lower bound, or
    rows land in both windows or neither."""
    _, yday_end = dl.local_day_bounds_utc(date(2026, 9, 24), date(2026, 9, 24))
    today_start, _ = dl.local_day_bounds_utc(TODAY, TODAY)
    assert yday_end == today_start


def test_spring_forward_day_spans_twenty_three_hours(dl, eastern):
    """2026-03-08 loses an hour. Proves the offset is computed per date, not assumed."""
    start_utc, end_utc = dl.local_day_bounds_utc(date(2026, 3, 8), date(2026, 3, 8))
    assert (end_utc - start_utc) == timedelta(hours=23)


def test_fall_back_day_spans_twenty_five_hours(dl, eastern):
    """2026-11-01 gains an hour."""
    start_utc, end_utc = dl.local_day_bounds_utc(date(2026, 11, 1), date(2026, 11, 1))
    assert (end_utc - start_utc) == timedelta(hours=25)


def test_multi_day_range_covers_every_day_inclusive(dl, eastern):
    """--start/--end are both inclusive, so a 3-day range spans 3 local days - 72 hours
    here, since no DST boundary falls inside it."""
    start_utc, end_utc = dl.local_day_bounds_utc(date(2026, 9, 23), date(2026, 9, 25))
    assert (end_utc - start_utc) == timedelta(hours=72)


def test_today_and_the_equivalent_explicit_range_produce_identical_bounds(dl, eastern):
    """--today is an alias, not a second code path. If these diverge, one of them is wrong
    and only the explicit form gets exercised."""
    via_flag = dl.local_day_bounds_utc(*dl.resolve_dates(_args(today=True), today=TODAY)[:2])
    via_dates = dl.local_day_bounds_utc(
        *dl.resolve_dates(_args(start='2026-09-25', end='2026-09-25'), today=TODAY)[:2])
    assert via_flag == via_dates


class _FakeCursor:
    """Captures the SQL and bound parameters instead of executing them."""
    def __init__(self):
        self.sql = None
        self.params = None

    def execute(self, sql, params=None):
        self.sql, self.params = sql, params

    def fetchall(self):
        return []

    def close(self):
        pass


class _FakeConn:
    def __init__(self):
        self.cur = _FakeCursor()

    def cursor(self):
        return self.cur


def test_query_binds_utc_bounds_and_no_longer_dateadds_a_local_date(dl, eastern):
    """Asserts the SQL actually sent, not the file's text - the source also mentions
    DATEADD and CONVERT_TIMEZONE in comments explaining why they are not used.

    DATEADD('day', 1, %(end)s::DATE) was the old upper bound: it added a day to a LOCAL
    date and compared the result to a UTC column, which is the bug being fixed.
    """
    conn = _FakeConn()
    start_utc, end_utc = dl.local_day_bounds_utc(TODAY, TODAY)
    dl.fetch_transcripts(conn, {'database': 'DB1', 'schema': 'SCH1'}, start_utc, end_utc)

    sql = conn.cur.sql
    assert 'DATEADD' not in sql, "local-date arithmetic is back in the query"
    assert 'CONVERT_TIMEZONE' not in sql, \
        "per-row conversion defeats partition pruning; convert client-side instead"
    assert '%(start_utc)s' in sql and '%(end_utc)s' in sql
    assert '>=' in sql and '<' in sql, 'bounds must be half-open, not BETWEEN'
    assert 'BETWEEN' not in sql.upper(), 'BETWEEN is inclusive and would double-count'

    # the values bound are the UTC datetimes, not date strings
    assert conn.cur.params == {'start_utc': datetime(2026, 9, 25, 4, 0),
                               'end_utc': datetime(2026, 9, 26, 4, 0)}
    assert all(isinstance(v, datetime) for v in conn.cur.params.values()), \
        'binding strings re-introduces the implicit-cast ambiguity'
