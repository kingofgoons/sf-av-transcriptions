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
