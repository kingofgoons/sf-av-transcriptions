"""Offline unit tests for scripts/payload/transcribe_job.py.

    pytest tests/test_payload_job.py -v

No Snowflake, no GPU, no network. Covers the parts of the payload that are pure logic and
that would fail silently rather than loudly if broken.

WHAT IS AND IS NOT TESTED HERE

The payload's risky surface is not its arithmetic - it is the contracts it shares with
other components that nothing else checks:

  - UNITS_TOTAL arithmetic, because the dashboard's completeness percentage is derived
    from it and a wrong value shows a permanently-stalled run that actually finished.
  - The 23-column INSERT order, because a transposition writes plausible values into the
    wrong columns and no error is raised.
  - finish_file() snapping, because a skipped or failed file otherwise leaves the
    percentage short of 100% forever.
  - emit() never raising, because losing a transcription run to a failed telemetry INSERT
    would be absurd.
  - The ledger's None-vs-empty-list distinction, which is the specific false negative that
    made the original hang investigation wrong.

End-to-end behaviour (GPU, Cortex, the real INSERT) is covered by the port plan's Tier
2-5, not here.
"""

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts', 'payload'))

import transcribe_job as tj  # noqa: E402


# ---------------------------------------------------------------------------
# the INSERT contract
# ---------------------------------------------------------------------------

NOTEBOOK_COLUMN_ORDER = [
    'FILE_PATH', 'FILE_NAME', 'FILE_TYPE', 'DETECTED_LANGUAGE', 'TRANSCRIPT',
    'TRANSCRIPT_WITH_SPEAKERS', 'PROCESSING_TIME_SECONDS', 'FILE_SIZE_BYTES',
    'AUDIO_DURATION_SECONDS', 'SPEAKER_COUNT', 'SRT_CONTENT', 'SRT_WITH_SPEAKERS',
    'SUMMARY_MARKDOWN', 'MEETING_TITLE', 'CALL_BRIEF', 'KEY_POINTS', 'NEXT_STEPS',
    'DECISIONS_MADE', 'QUESTIONS_RAISED', 'ACCOUNT_NAME', 'CALL_START_TS',
    'PARTICIPANTS_JSON', 'TRANSCRIPTION_TIMESTAMP',
]


def test_column_order_matches_the_notebook_exactly():
    """Transposed columns do not raise - they write plausible values into wrong fields.

    Both lists are all-VARCHAR-ish at the SQL layer, so swapping e.g. KEY_POINTS and
    NEXT_STEPS inserts successfully and corrupts data silently. This list is copied from
    notebook cell 28; keep it hard-coded rather than importing, so the test is an
    independent statement of the contract.
    """
    assert tj.COLUMNS == NOTEBOOK_COLUMN_ORDER
    assert len(tj.COLUMNS) == 23


def test_build_record_populates_every_insert_column():
    """A missing key would raise KeyError in persist()'s row comprehension, but only at
    the very end of a GPU run - after all the expensive work is done."""
    rec = tj.build_record(
        file_path='/tmp/2026-09-08 13-01-21_Kargo_topic_TEST01.mp4',
        file_name='2026-09-08 13-01-21_Kargo_topic_TEST01.mp4',
        transcript='hello', language='en', processing_time=1.5, audio_duration=60.0,
        tws={'speakers': [], 'file_info': {}, 'full_transcript': 'hello'},
        speaker_count=2, srt='1\n', srt_speakers='1\n', summary=None,
        include_file_path=False)
    for col in tj.COLUMNS:
        assert col in rec, 'build_record omitted %s' % col
    assert len(rec) == 23


def test_build_record_parses_account_and_timestamp_from_filename():
    rec = tj.build_record(
        file_path='/tmp/x.mp4',
        file_name='2026-09-08 13-01-21_Kargo_sync.on.AIOps_TEST01.mp4',
        transcript='t', language='en', processing_time=1.0, audio_duration=1.0,
        tws=None, speaker_count=0, srt=None, srt_speakers=None, summary=None,
        include_file_path=False)
    assert rec['ACCOUNT_NAME'] == 'Kargo'
    assert rec['CALL_START_TS'] == '2026-09-08 13:01:21'
    assert rec['FILE_TYPE'] == 'MP4'


def test_build_record_summary_none_yields_nulls_not_crash():
    """Cortex can return nothing. The notebook tolerates it; so must this."""
    rec = tj.build_record('/tmp/x.mp4', 'x.mp4', 't', 'en', 1.0, 1.0, None, 0,
                          None, None, None, False)
    for col in ('SUMMARY_MARKDOWN', 'MEETING_TITLE', 'CALL_BRIEF', 'KEY_POINTS',
                'NEXT_STEPS', 'DECISIONS_MADE', 'QUESTIONS_RAISED'):
        assert rec[col] is None


def test_build_record_serialises_speakers_to_json_text():
    """The INSERT wraps this column in PARSE_JSON, so it must arrive as a string."""
    tws = {'file_info': {'filename': 'x'}, 'speakers': [{'text': 'hi'}],
           'full_transcript': 'hi'}
    rec = tj.build_record('/tmp/x.mp4', 'x.mp4', 't', 'en', 1.0, 1.0, tws, 1,
                          None, None, None, False)
    assert isinstance(rec['TRANSCRIPT_WITH_SPEAKERS'], str)
    assert json.loads(rec['TRANSCRIPT_WITH_SPEAKERS'])['speakers'][0]['text'] == 'hi'


def test_file_path_is_populated_by_default_matching_the_notebook():
    """The notebook sets INCLUDE_FILE_PATH = True, so the column is populated in all 499
    existing rows. Defaulting this to False would flip a column from populated to empty
    across the port - a diff a reviewer has to chase down for no benefit.

    The CONTENT legitimately differs (notebook: 'media_files/<name>'; payload: the mkdtemp
    work dir) and nothing reads the column, so only the populated/empty shape is matched.
    """
    default_on = tj.build_record('/tmp/transcribe_x/y.mp4', 'y.mp4', 't', 'en', 1.0, 1.0,
                                 None, 0, None, None, None,
                                 tj.parse_args([]).include_file_path)
    assert default_on['FILE_PATH'] == '/tmp/transcribe_x/y.mp4'
    assert tj.parse_args([]).include_file_path is True
    assert tj.parse_args(['--no-file-path']).include_file_path is False


def test_no_file_path_flag_stores_empty_string_not_null():
    """Empty string, not NULL - the column is VARCHAR(500) NOT NULL-less but the notebook
    convention is '' and parity comparisons diff it."""
    rec = tj.build_record('/abs/x.mp4', 'x.mp4', 't', 'en', 1.0, 1.0, None, 0,
                          None, None, None, False)
    assert rec['FILE_PATH'] == ''
    assert rec['FILE_PATH'] is not None


# ---------------------------------------------------------------------------
# RunProgress — the dashboard's completeness percentage depends on this
# ---------------------------------------------------------------------------

class FakeSession:
    """Records emitted SQL. `params` is what matters, not the text."""

    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def sql(self, text, params=None):
        if self.fail:
            raise RuntimeError('simulated telemetry outage')
        self.calls.append((text, params))
        return self

    def collect(self):
        return []


def make_prog(**kw):
    return tj.RunProgress(FakeSession(), 'RUN_EVENTS', **kw)


def test_units_total_is_four_globals_plus_four_per_file():
    """sf_config and V_TRANSCRIPTION_RUN_STATUS both assume this shape."""
    p = make_prog()
    assert p.units_total == 4, 'before set_file_total, only the global units exist'
    p.set_file_total(3)
    assert p.units_total == 16
    p.set_file_total(1)
    assert p.units_total == 8
    p.set_file_total(10)
    assert p.units_total == 44


def test_phase_and_step_vocabulary_is_unchanged():
    """sf_config.PHASE_TOTAL hardcodes 6; the four file steps drive the per-file units."""
    assert tj.RunProgress.PHASE_TOTAL == 6
    assert tj.RunProgress.PHASES == ['STARTUP', 'DISCOVER', 'DOWNLOAD', 'TRANSCRIBE',
                                     'PERSIST', 'COMPLETE']
    assert tj.RunProgress.FILE_STEPS == ['EXTRACT_AUDIO', 'TRANSCRIBE', 'GENERATE_SRT',
                                         'GENERATE_SUMMARY']
    assert tj.RunProgress.GLOBAL_UNITS == 4


def test_finish_file_snaps_to_baseline_plus_four_even_when_steps_were_skipped():
    """THE bug this guards: a skipped or failed file that completed 0 of its 4 units
    leaves the run permanently short of 100%, and the dashboard shows a stalled run that
    actually finished."""
    p = make_prog()
    p.set_file_total(2)
    p.complete_unit(4)          # the four global units

    p.start_file()              # file 1 completes nothing at all (e.g. skipped)
    p.finish_file()
    assert p.units_done == 8

    p.start_file()              # file 2 completes only 1 of 4 then fails
    p.complete_unit()
    p.finish_file()
    assert p.units_done == 12


def test_a_full_three_file_run_lands_exactly_on_one_hundred_percent():
    """End-to-end arithmetic. Anything other than units_done == units_total shows as a
    percentage that never reaches 100."""
    p = make_prog()
    p.set_file_total(3)
    p.complete_unit()                       # STARTUP
    p.complete_unit()                       # DISCOVER
    p.complete_unit()                       # DOWNLOAD
    for _ in range(3):
        p.start_file()
        for _ in range(4):                  # the four file steps
            p.complete_unit()
        p.finish_file()
    p.complete_unit()                       # PERSIST
    assert p.units_done == p.units_total == 16


def test_emit_never_raises_even_when_the_insert_fails():
    """Losing a whole GPU transcription run because a telemetry INSERT failed would be
    absurd. emit() swallows everything."""
    p = tj.RunProgress(FakeSession(fail=True), 'RUN_EVENTS')
    p.emit('RUNNING', 'STARTUP', message='should not raise')      # must not raise


def test_emit_records_run_source_job_service_and_increments_seq():
    """RUN_SOURCE distinguishes ported runs from notebook runs in history; SEQ orders
    events within a run."""
    p = make_prog()
    p.emit('RUNNING', 'STARTUP')
    p.emit('RUNNING', 'DISCOVER')
    params = [c[1] for c in p.session.calls]
    assert len(params) == 2
    assert params[0][2] == 'JOB_SERVICE'
    assert params[0][1] == 1 and params[1][1] == 2


# Bound-parameter order in emit()'s INSERT. EVENT_TS is absent because SQL supplies it
# via CURRENT_TIMESTAMP(), so 18 columns take 17 bound values. Declared as a map rather
# than bare indices because hand-counted offsets are exactly how a column transposition
# slips through - the first draft of this test asserted index 11 for FILE_STEP, which is
# really FILE_STEP_NUM.
EMIT_PARAM_INDEX = {
    'RUN_ID': 0, 'SEQ': 1, 'RUN_SOURCE': 2, 'STATUS': 3, 'PHASE': 4, 'PHASE_NUM': 5,
    'PHASE_TOTAL': 6, 'FILE_INDEX': 7, 'FILE_TOTAL': 8, 'CURRENT_FILE': 9,
    'FILE_STEP': 10, 'FILE_STEP_NUM': 11, 'FILE_STEP_TOTAL': 12, 'UNITS_DONE': 13,
    'UNITS_TOTAL': 14, 'MESSAGE': 15, 'ERROR_MESSAGE': 16,
}


def emitted(prog, call=0):
    """Return one emitted event as a name -> value dict."""
    params = prog.session.calls[call][1]
    return {name: params[i] for name, i in EMIT_PARAM_INDEX.items()}


def test_emit_param_map_covers_every_bound_value():
    """Guards the map above against drifting from the INSERT."""
    p = make_prog()
    p.emit('RUNNING', 'STARTUP')
    assert len(EMIT_PARAM_INDEX) == len(p.session.calls[0][1]) == 17


def test_emit_resolves_phase_and_step_numbers_and_tolerates_unknown_names():
    p = make_prog()
    p.emit('RUNNING', 'TRANSCRIBE', file_step='GENERATE_SRT')
    ev = emitted(p)
    assert ev['PHASE'] == 'TRANSCRIBE' and ev['PHASE_NUM'] == 4
    assert ev['FILE_STEP'] == 'GENERATE_SRT' and ev['FILE_STEP_NUM'] == 3
    assert ev['FILE_STEP_TOTAL'] == 4 and ev['PHASE_TOTAL'] == 6

    p.emit('RUNNING', 'NOT_A_PHASE', file_step='NOT_A_STEP')
    ev = emitted(p, 1)
    assert ev['PHASE_NUM'] is None and ev['FILE_STEP_NUM'] is None, (
        'unknown names must be NULL, not an error - a typo in a phase name should not '
        'abort a GPU run')


def test_emit_carries_file_position_and_units():
    p = make_prog()
    p.set_file_total(3)
    p.complete_unit(5)
    p.emit('RUNNING', 'TRANSCRIBE', file_index=2, file_total=3, current_file='x.mp4',
           message='hello', error_message=None)
    ev = emitted(p)
    assert (ev['FILE_INDEX'], ev['FILE_TOTAL'], ev['CURRENT_FILE']) == (2, 3, 'x.mp4')
    assert (ev['UNITS_DONE'], ev['UNITS_TOTAL']) == (5, 16)
    assert ev['MESSAGE'] == 'hello' and ev['ERROR_MESSAGE'] is None


def test_emit_writes_eighteen_column_values():
    """The RUN_EVENTS schema is 18 columns and append-only. EVENT_TS is set by
    CURRENT_TIMESTAMP() in SQL, so 17 values are bound."""
    p = make_prog()
    p.emit('RUNNING', 'STARTUP')
    assert len(p.session.calls[0][1]) == 17


def test_progress_can_be_disabled_without_side_effects():
    p = make_prog(enabled=False)
    p.emit('RUNNING', 'STARTUP')
    assert p.session.calls == []


def test_run_id_is_a_distinct_uuid_per_instance():
    a, b = make_prog(), make_prog()
    assert a.run_id != b.run_id
    assert len(a.run_id) == 36


# ---------------------------------------------------------------------------
# resource ledger
# ---------------------------------------------------------------------------

def test_os_children_returns_none_not_empty_list_when_unmeasurable(monkeypatch):
    """THE distinction that made the original investigation wrong.

    An empty list is indistinguishable from "genuinely zero children", which is how
    multiprocessing.active_children() produced a confident false negative while an
    ffmpeg subprocess.Popen child was in fact present. None means "unknown"; the
    snapshot renders it as -1.
    """
    monkeypatch.setattr(os.path, 'isdir', lambda p: False)
    assert tj._os_children() is None


def test_ledger_reconcile_reports_ok_when_counts_pair(tmp_path):
    tj.LEDGER['wav_created'] = 3
    tj.LEDGER['wav_removed'] = 3
    assert tj.ledger_reconcile(3, str(tmp_path)) == 'OK'


def test_ledger_reconcile_reports_leak_on_unpaired_counts(tmp_path):
    tj.LEDGER['wav_created'] = 3
    tj.LEDGER['wav_removed'] = 2
    assert tj.ledger_reconcile(3, str(tmp_path)) == 'LEAK'


def test_ledger_reconcile_reports_leak_on_leftover_wav_on_disk(tmp_path):
    """Paired counters can both be right while a file survives - e.g. a WAV written by a
    previous crashed run. on_disk catches that independently."""
    tj.LEDGER['wav_created'] = 1
    tj.LEDGER['wav_removed'] = 1
    (tmp_path / 'orphan_temp_audio.wav').write_bytes(b'')
    assert tj.ledger_reconcile(1, str(tmp_path)) == 'LEAK'


def test_ledger_snapshot_never_raises_without_proc(tmp_path):
    """It runs at every per-file boundary; an exception here would kill a run for the sake
    of telemetry."""
    tj.ledger_snapshot('unit test', str(tmp_path))
    tj.ledger_snapshot('unit test, no work dir', None)


def test_tmp_wavs_counts_only_temp_audio_files(tmp_path):
    (tmp_path / 'a_temp_audio.wav').write_bytes(b'')
    (tmp_path / 'b_temp_audio.wav').write_bytes(b'')
    (tmp_path / 'real_recording.mp4').write_bytes(b'')
    (tmp_path / 'unrelated.wav').write_bytes(b'')
    assert tj._tmp_wavs(str(tmp_path)) == 2


# ---------------------------------------------------------------------------
# argument handling
# ---------------------------------------------------------------------------

def test_defaults_target_the_real_deployment():
    a = tj.parse_args([])
    assert a.database == 'TRANSCRIPTION_DB_V2'
    assert a.schema == 'TRANSCRIPTION_SCHEMA_V2'
    assert a.results_table == 'TRANSCRIPTION_RESULTS'
    assert a.whisper_model == 'base', "'large' is ~10x slower on GPU_NV_S"
    assert a.force_retranscribe is False, 'must never default to re-consuming GPU credits'
    assert a.dry_run is False


def test_results_table_can_be_redirected_to_a_clone():
    """Tier 2 of the test plan depends on this: validate against a clone, never the real
    table."""
    a = tj.parse_args(['--results-table', 'TRANSCRIPTION_RESULTS_PORTTEST'])
    assert a.results_table == 'TRANSCRIPTION_RESULTS_PORTTEST'


def test_env_vars_supply_defaults_for_the_container(monkeypatch):
    """The service spec passes names as env vars rather than argv."""
    monkeypatch.setenv('PROJECT_DB', 'OTHER_DB')
    monkeypatch.setenv('WHISPER_MODEL', 'small')
    import importlib
    importlib.reload(tj)
    a = tj.parse_args([])
    assert a.database == 'OTHER_DB'
    assert a.whisper_model == 'small'
    monkeypatch.delenv('PROJECT_DB')
    monkeypatch.delenv('WHISPER_MODEL')
    importlib.reload(tj)


def test_media_extensions_cover_the_gate_procedures_list():
    """The SQL gate counts these extensions when deciding whether to launch. If the
    payload's list is narrower, the gate launches a GPU for work the payload then skips -
    an idle launch that is not free, because Whisper loads unconditionally."""
    gate = {'mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg',
            'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv'}
    payload = {e.lstrip('.') for e in tj.MEDIA_EXT}
    assert gate == payload, 'gate and payload disagree about what counts as media'


def test_video_extensions_are_a_subset_of_media_extensions():
    assert set(tj.VIDEO_EXT).issubset(set(tj.MEDIA_EXT))


# ---------------------------------------------------------------------------
# the import that silently nulls a column
# ---------------------------------------------------------------------------

def test_datetime_is_the_class_not_the_module():
    """`import datetime` instead of `from datetime import datetime` makes
    datetime.strptime raise AttributeError inside a bare except, so CALL_START_TS becomes
    NULL for every file with no error anywhere. Proven and fixed 2026-09-24."""
    assert callable(tj.datetime)
    assert hasattr(tj.datetime, 'strptime')
    assert tj.datetime.__name__ == 'datetime'
