#!/usr/bin/env python3
"""Headless transcription payload — the job-service replacement for EXECUTE NOTEBOOK.

Runs the same work as notebooks/audio_video_transcription.ipynb with no notebook runtime
involved. That absence is the entire point: the hang this replaces lives in `snowbook`'s
shutdown path, in four threads that only exist because a notebook hosts the code
(`on_scriptrunner_ready`, the gRPC `run_forever` loop, `stage_file_watcher`,
`status_updater`). A plain Python process has none of them.

    # locally, against a clone - writes nothing to the real table
    python transcribe_job.py --connection DEMO \
        --results-table TRANSCRIPTION_RESULTS_PORTTEST --force-retranscribe --limit 1

    # in the container, OAuth from the mounted token
    python transcribe_job.py

CRITICAL: NEVER invoke this as `python -m snowbook.web.cli`. That module IS present in the
Container Runtime image (snowbooks 1.76.10rc1 ships in it), and it is the top of the hang
stack. The service `command` must call this script directly.

ENVIRONMENT, measured 2026-09-24 on gpu_x86_64:2.9.0

  python 3.10.19 · ffmpeg 6.1.1 at /usr/bin/ffmpeg · torch 2.9.1+cu129 · NVIDIA A10G
  whisper 'base' loads in 4.7s using 279 MB · warm start 18s, cold 105s

  `pip install` FAILS in this image with PEP 668 "externally managed ... managed by uv".
  Install with `uv pip install --system --break-system-packages openai-whisper`.

  torch here is 2.9.1 versus 2.6.0 in the notebook runtime. Transcript text is therefore
  NOT guaranteed byte-identical to the notebook's output; compare structure, language
  detection and segment counts, not exact strings.

WHAT IS DELIBERATELY PRESERVED, EVEN THOUGH IT LOOKS WRONG

  - `from datetime import datetime, timezone`, never `import datetime`. parse_filename_metadata
    calls datetime.strptime directly and swallows AttributeError, so the wrong import
    nulls CALL_START_TS on every file with no error anywhere. Guarded by a test.
  - The 23-column INSERT order, matched to the notebook exactly.
  - The dedup contract: SELECT DISTINCT FILE_NAME, matched on BARE filename. The SQL gate
    procedure uses the same signal; any drift makes the gate and the payload disagree
    about what work exists.
  - Millisecond truncation in format_timestamp_srt, and exact-string header matching in
    parse_summary_sections. Both are quirks; both are what produced every stored row.

WHAT IS DELIBERATELY DIFFERENT

  - Emits a terminal SUCCEEDED event. The notebook cannot - its hang is after the last
    cell - so its terminal state is CELLS_COMPLETE and the dashboard has to cross-check
    TASK_HISTORY to tell "finished" from "wedged". A headless process can report its own
    exit, which makes WORK_COMPLETE_NOT_EXITED genuinely diagnostic instead of routine.
  - Cell 28's `except` branch is NOT ported. It supplies 14 positional values against a
    23-column table, so it could only ever have raised. A real error path replaces it.
  - Absolute temp dir via tempfile.mkdtemp(), not the notebook's cwd-relative
    'media_files'.
  - logging, not print.
  - TRANSCRIPTION_TIMESTAMP uses datetime.now(timezone.utc), where the notebook uses a
    bare datetime.now(). The stored string is identical - this runtime is UTC, so both
    produce the same value - but the notebook's version is UTC only by accident, and
    TIMESTAMP_NTZ carries no offset to say so. See the comment at the assignment.
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import transcribe_functions as tf

LOG = logging.getLogger('transcribe_job')

MEDIA_EXT = ('.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg',
             '.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv')
VIDEO_EXT = ('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv')

# Column order for the INSERT. Must match the notebook's cell 28 exactly.
COLUMNS = [
    'FILE_PATH', 'FILE_NAME', 'FILE_TYPE', 'DETECTED_LANGUAGE', 'TRANSCRIPT',
    'TRANSCRIPT_WITH_SPEAKERS', 'PROCESSING_TIME_SECONDS', 'FILE_SIZE_BYTES',
    'AUDIO_DURATION_SECONDS', 'SPEAKER_COUNT', 'SRT_CONTENT', 'SRT_WITH_SPEAKERS',
    'SUMMARY_MARKDOWN', 'MEETING_TITLE', 'CALL_BRIEF', 'KEY_POINTS', 'NEXT_STEPS',
    'DECISIONS_MADE', 'QUESTIONS_RAISED', 'ACCOUNT_NAME', 'CALL_START_TS',
    'PARTICIPANTS_JSON', 'TRANSCRIPTION_TIMESTAMP',
]


# ---------------------------------------------------------------------------
# progress emitter — ported from notebook cell 5, schema unchanged
# ---------------------------------------------------------------------------

class RunProgress:
    """Append-only progress into TRANSCRIPTION_RUN_EVENTS.

    The 18-column schema, the six phases, PHASE_TOTAL = 6 and the
    UNITS_TOTAL = 4 + files*4 arithmetic are all load-bearing: the dashboard's
    V_TRANSCRIPTION_RUN_STATUS view, its derived-state logic and sf_config.PHASE_TOTAL
    all depend on them. Do not "improve" any of it here.

    RUN_SOURCE is 'JOB_SERVICE' so old and new runs stay distinguishable in history.

    emit() must never raise. Losing a whole transcription run because a telemetry INSERT
    failed would be absurd, so every failure is swallowed and logged.
    """

    PHASES = ['STARTUP', 'DISCOVER', 'DOWNLOAD', 'TRANSCRIBE', 'PERSIST', 'COMPLETE']
    FILE_STEPS = ['EXTRACT_AUDIO', 'TRANSCRIBE', 'GENERATE_SRT', 'GENERATE_SUMMARY']
    GLOBAL_UNITS = 4          # STARTUP, DISCOVER, DOWNLOAD, PERSIST
    PHASE_TOTAL = 6

    def __init__(self, session, table, run_source='JOB_SERVICE', enabled=True):
        self.session = session
        self.table = table
        self.run_source = run_source
        self.enabled = enabled
        self.run_id = str(uuid.uuid4())
        self.seq = 0
        self.units_done = 0
        self.units_total = self.GLOBAL_UNITS
        self.file_baseline = 0

    def set_file_total(self, n_files):
        self.units_total = self.GLOBAL_UNITS + n_files * len(self.FILE_STEPS)

    def complete_unit(self, n=1):
        self.units_done += n

    def start_file(self):
        self.file_baseline = self.units_done

    def finish_file(self):
        """Snap to baseline + 4 regardless of outcome.

        Without this a skipped or failed file leaves the percentage permanently short of
        100%, and the dashboard shows an apparently stalled run that actually finished.
        """
        self.units_done = self.file_baseline + len(self.FILE_STEPS)

    def emit(self, status, phase, file_index=None, file_total=None, current_file=None,
             file_step=None, message=None, error_message=None):
        if not self.enabled:
            return
        self.seq += 1
        try:
            phase_num = self.PHASES.index(phase) + 1 if phase in self.PHASES else None
            step_num = (self.FILE_STEPS.index(file_step) + 1
                        if file_step in self.FILE_STEPS else None)
            self.session.sql(
                'INSERT INTO %s (RUN_ID, SEQ, EVENT_TS, RUN_SOURCE, STATUS, PHASE, '
                'PHASE_NUM, PHASE_TOTAL, FILE_INDEX, FILE_TOTAL, CURRENT_FILE, FILE_STEP, '
                'FILE_STEP_NUM, FILE_STEP_TOTAL, UNITS_DONE, UNITS_TOTAL, MESSAGE, '
                'ERROR_MESSAGE) SELECT ?, ?, CURRENT_TIMESTAMP(), ?, ?, ?, ?, ?, ?, ?, ?, '
                '?, ?, ?, ?, ?, ?, ?' % self.table,
                params=[self.run_id, self.seq, self.run_source, status, phase, phase_num,
                        self.PHASE_TOTAL, file_index, file_total, current_file, file_step,
                        step_num, len(self.FILE_STEPS), self.units_done, self.units_total,
                        message, error_message],
            ).collect()
        except Exception as exc:
            LOG.warning('progress emit failed (ignored): %s: %s', type(exc).__name__, exc)


# ---------------------------------------------------------------------------
# resource ledger — ported from notebook cell 5 (port plan task 4c)
# ---------------------------------------------------------------------------

LEDGER = {'wav_created': 0, 'wav_removed': 0}


def _fd_count():
    try:
        return len(os.listdir('/proc/%d/fd' % os.getpid()))
    except Exception:
        return -1


def _os_children():
    """DIRECT OS children, including subprocess.Popen. Returns None if unmeasurable.

    Returning None rather than [] when /proc is unavailable is deliberate and is the
    whole reason this exists. An empty list is indistinguishable from "genuinely zero
    children" - which is exactly the false negative that made the original hang
    investigation wrong. It concluded "zero children" from
    multiprocessing.active_children(), which structurally cannot see the
    subprocess.Popen child that ffmpeg actually is. Measured truth: os_children is
    persistently 1.
    """
    pid = os.getpid()
    if not os.path.isdir('/proc/%d/task' % pid):
        return None
    try:
        kids, read_any = [], False
        for tid in os.listdir('/proc/%d/task' % pid):
            try:
                with open('/proc/%d/task/%s/children' % (pid, tid)) as fh:
                    kids += fh.read().split()
                read_any = True
            except Exception:
                pass
        return kids if read_any else None
    except Exception:
        return None


def _cuda_mb():
    try:
        import torch
        if torch.cuda.is_available():
            return (round(torch.cuda.memory_allocated() / 1048576, 1),
                    round(torch.cuda.memory_reserved() / 1048576, 1))
    except Exception:
        pass
    return (-1, -1)


def _tmp_wavs(work_dir):
    try:
        return len([f for f in os.listdir(work_dir) if f.endswith('_temp_audio.wav')])
    except Exception:
        return -1


def ledger_snapshot(label, work_dir=None):
    """One line per boundary. Comparing START across files is how a per-file leak shows
    up as a growth curve - the most direct test of whether multi-file runs accumulate
    anything, which is what separates them from the never-hanging single-file case."""
    import threading
    kids = _os_children()
    alloc, reserved = _cuda_mb()
    LOG.info('LEDGER %s | fd=%s threads=%d nondaemon=%d os_children=%s '
             'cuda_alloc_mb=%s cuda_reserved_mb=%s tmp_wav=%s',
             label, _fd_count(), threading.active_count(),
             sum(1 for t in threading.enumerate() if not t.daemon),
             -1 if kids is None else len(kids), alloc, reserved,
             _tmp_wavs(work_dir) if work_dir else -1)


def ledger_reconcile(n_files, work_dir=None):
    """Paired counts: created vs removed. MUST run BEFORE the work dir is deleted -
    afterwards on_disk is always 0 and the check is vacuous."""
    on_disk = _tmp_wavs(work_dir) if work_dir else -1
    unaccounted = LEDGER['wav_created'] - LEDGER['wav_removed']
    verdict = 'OK' if (unaccounted == 0 and on_disk in (0, -1)) else 'LEAK'
    LOG.info('LEDGER RECONCILE files=%d temp wav created=%d removed=%d '
             'unaccounted=%d on_disk=%s %s',
             n_files, LEDGER['wav_created'], LEDGER['wav_removed'],
             unaccounted, on_disk, verdict)
    return verdict


# ---------------------------------------------------------------------------
# session
# ---------------------------------------------------------------------------

def build_session(args):
    """OAuth inside SPCS, or a named connection locally.

    The container's token file is refreshed every few minutes, but the connection is not
    bound to the token's 1-hour validity once established.
    """
    from snowflake.snowpark import Session

    token_path = '/snowflake/session/token'
    if os.path.isfile(token_path) and not args.connection:
        with open(token_path) as fh:
            token = fh.read()
        cfg = {
            'host': os.environ['SNOWFLAKE_HOST'],
            'account': os.environ['SNOWFLAKE_ACCOUNT'],
            'token': token,
            'authenticator': 'oauth',
            'warehouse': args.warehouse,
            'database': args.database,
            'schema': args.schema,
        }
        LOG.info('connecting with the container OAuth token')
        return Session.builder.configs(cfg).create()

    LOG.info('connecting with named connection %r', args.connection)
    sess = Session.builder.config('connection_name', args.connection).create()
    for stmt in ('USE WAREHOUSE %s' % args.warehouse,
                 'USE DATABASE %s' % args.database,
                 'USE SCHEMA %s' % args.schema):
        sess.sql(stmt).collect()
    return sess


def preflight(require_gpu=True):
    """Fail fast and loudly rather than halfway through a GPU run.

    require_gpu=False is for --dry-run, which only lists the stage and reports what it
    would do. Keeping the GPU and whisper checks mandatory there would make the cheap,
    free validation step impossible to run anywhere but a GPU container - and that step
    exists precisely so the dedup contract and session wiring can be checked before any
    compute is provisioned. ffmpeg is still checked either way: it is cheap, and its
    absence is the one environment failure that silently produces no audio.
    """
    problems = []
    for binary in ('ffmpeg', 'ffprobe'):
        if not shutil.which(binary):
            problems.append('%s not found on PATH' % binary)
    if require_gpu:
        try:
            import torch
            if not torch.cuda.is_available():
                problems.append('torch.cuda.is_available() is False - a misconfigured pool '
                                'would otherwise silently transcribe on CPU, ~20x slower')
            else:
                LOG.info('GPU: %s, torch %s',
                         torch.cuda.get_device_name(0), torch.__version__)
        except ImportError:
            problems.append('torch is not importable')
        try:
            import whisper  # noqa: F401
        except ImportError:
            problems.append("whisper not importable - install with 'uv pip install "
                            "--system --break-system-packages openai-whisper' "
                            '(plain pip fails PEP 668 in the Container Runtime image)')
    if problems:
        for p in problems:
            LOG.error('PREFLIGHT: %s', p)
        raise SystemExit('preflight failed; refusing to start')
    LOG.info('preflight OK%s', '' if require_gpu else ' (dry run: GPU checks skipped)')


# ---------------------------------------------------------------------------
# discovery — dedup contract must match the SQL gate exactly
# ---------------------------------------------------------------------------

def discover(session, args):
    """Stage files with no row in the results table, matched on BARE filename.

    Uses LIST, not DIRECTORY(). The directory table goes stale after PUT and has
    previously reported a months-old view of this very stage - it once showed a February
    notebook at 64,816 bytes while LIST showed the current 109,920.
    """
    rows = session.sql('LIST @%s' % args.stage).collect()
    staged = {}
    for r in rows:
        name = r['name'].rsplit('/', 1)[-1]
        if name.lower().endswith(MEDIA_EXT):
            staged[name] = r['size']
    LOG.info('stage holds %d media file(s)', len(staged))

    if args.force_retranscribe:
        LOG.warning('--force-retranscribe: dedup bypassed, every staged file will '
                    're-consume GPU time and Cortex credits')
        todo = sorted(staged)
    else:
        done = {r['FILE_NAME'] for r in session.sql(
            'SELECT DISTINCT FILE_NAME FROM %s' % args.results_table).collect()}
        todo = sorted(n for n in staged if n not in done)
        LOG.info('%d already transcribed, %d new', len(staged) - len(todo), len(todo))

    if args.limit:
        todo = todo[:args.limit]
        LOG.info('--limit %d applied', args.limit)
    return todo, staged


def download(session, names, stage, work_dir):
    """GET each file to the local work dir."""
    local = []
    for name in names:
        session.sql("GET '@%s/%s' 'file://%s'" % (stage, name, work_dir)).collect()
        path = os.path.join(work_dir, name)
        if os.path.exists(path):
            local.append(path)
        else:
            LOG.error('GET reported success but %s is not on disk', path)
    LOG.info('downloaded %d/%d file(s)', len(local), len(names))
    return local


# ---------------------------------------------------------------------------
# transcription — ported from cell 19's transcribe_media_file
# ---------------------------------------------------------------------------

def transcribe_media_file(model, whisper_mod, file_path, diarization_pipeline=None,
                          prog=None, file_index=None, file_total=None):
    """Returns (transcript, language, processing_time, audio_duration,
    transcript_with_speakers, speaker_count).

    `model` and `whisper_mod` are explicit parameters rather than the notebook's implicit
    globals. Behaviour is otherwise unchanged, including the fallback that fabricates
    alternating Speaker_0/Speaker_1 labels when diarization is unavailable - that is what
    generated every stored TRANSCRIPT_WITH_SPEAKERS, so changing it would break parity.
    """
    start_time = time.time()

    # Bound BEFORE the try so `finally` can always reference it. Assigning only inside
    # the try left a NameError path when anything raised early.
    audio_path = file_path
    work_dir = os.path.dirname(file_path) or '.'
    base = os.path.basename(file_path)

    def _emit(step, msg=None, err=None):
        if prog is not None:
            prog.emit('RUNNING', 'TRANSCRIBE', file_index=file_index, file_total=file_total,
                      current_file=base, file_step=step, message=msg, error_message=err)

    ledger_snapshot('file %s/%s START %s' % (file_index, file_total, base[:40]), work_dir)

    try:
        ext = os.path.splitext(file_path)[1].lower()
        audio_duration = 0

        if ext in VIDEO_EXT:
            _emit('EXTRACT_AUDIO', 'extracting audio with ffmpeg')
            audio_path = file_path.rsplit('.', 1)[0] + '_temp_audio.wav'
            LEDGER['wav_created'] += 1
            ok, duration = tf.extract_audio_from_video(file_path, audio_path)
            if not ok:
                _emit('EXTRACT_AUDIO', err='ffmpeg audio extraction failed')
                return None, None, 0, 0, None, 0
            audio_duration = duration
            if prog is not None:
                prog.complete_unit()
        elif ext in MEDIA_EXT:
            _emit('EXTRACT_AUDIO', 'audio input - no extraction needed')
            try:
                audio = whisper_mod.load_audio(file_path)
                audio_duration = len(audio) / whisper_mod.audio.SAMPLE_RATE
            except Exception:
                audio_duration = 0
            # Count the skipped EXTRACT_AUDIO unit anyway, or an audio-only run can
            # never reach 100%.
            if prog is not None:
                prog.complete_unit()
        else:
            _emit(None, err='unsupported format %s' % ext)
            return None, None, 0, 0, None, 0

        _emit('TRANSCRIBE', 'whisper on %.0fs of audio' % audio_duration)
        result = model.transcribe(audio_path, word_timestamps=True)

        transcript_with_speakers = None
        speaker_count = 0

        if diarization_pipeline is not None:
            try:
                diarization = diarization_pipeline(audio_path)
                current = {}
                for segment in result['segments']:
                    for w in segment.get('words', []):
                        label = 'Unknown'
                        for turn, _, spk in diarization.itertracks(yield_label=True):
                            if w['start'] >= turn.start and w['end'] <= turn.end:
                                label = 'Speaker_%s' % spk
                                break
                        if label not in current:
                            current[label] = {'speaker': label, 'start_time': w['start'],
                                              'end_time': w['end'], 'text': w['word']}
                        else:
                            current[label]['end_time'] = w['end']
                            current[label]['text'] += w['word']
                segs = [{'speaker': s['speaker'],
                         'start_time': round(s['start_time'], 2),
                         'end_time': round(s['end_time'], 2),
                         'duration': round(s['end_time'] - s['start_time'], 2),
                         'text': s['text'].strip()} for s in current.values()]
                segs.sort(key=lambda x: x['start_time'])
                transcript_with_speakers = {
                    'file_info': {'filename': base,
                                  'duration': round(audio_duration, 2),
                                  'language': result.get('language', 'unknown')},
                    'speakers': segs,
                    'full_transcript': result['text'].strip()}
                speaker_count = len({s['speaker'] for s in segs})
            except Exception as exc:
                LOG.warning('diarization failed, continuing without it: %s', exc)

        if transcript_with_speakers is None and result.get('segments'):
            # Fallback: alternating demo speaker labels. Preserved verbatim - every
            # stored TRANSCRIPT_WITH_SPEAKERS was produced this way.
            segs = [{'speaker': 'Speaker_%d' % (i % 2),
                     'start_time': round(s['start'], 2),
                     'end_time': round(s['end'], 2),
                     'duration': round(s['end'] - s['start'], 2),
                     'text': s['text'].strip()}
                    for i, s in enumerate(result['segments'])]
            transcript_with_speakers = {
                'file_info': {'filename': base,
                              'duration': round(audio_duration, 2),
                              'language': result.get('language', 'unknown')},
                'speakers': segs,
                'full_transcript': result['text'].strip()}
            speaker_count = 2

        processing_time = time.time() - start_time
        LOG.info('transcribed %s: %.0fs audio in %.0fs, language=%s, speakers=%d',
                 base, audio_duration, processing_time,
                 result.get('language', 'unknown'), speaker_count)
        if prog is not None:
            prog.complete_unit()
            ratio = ('ratio %.4f' % (processing_time / audio_duration)) if audio_duration else ''
            _emit('TRANSCRIBE', 'transcribed %.0fs in %.0fs %s'
                  % (audio_duration, processing_time, ratio))

        return (result['text'].strip(), result.get('language', 'unknown'),
                processing_time, audio_duration, transcript_with_speakers, speaker_count)

    except Exception as exc:
        processing_time = time.time() - start_time
        LOG.exception('error transcribing %s', file_path)
        _emit('TRANSCRIBE', err='%s: %s' % (type(exc).__name__, exc))
        return None, None, processing_time, 0, None, 0

    finally:
        # Runs on EVERY exit path: success, early return, exception. The paired LEDGER
        # counter is what makes the cleanup verifiable rather than assumed. This used to
        # sit inline in the try, which leaked a 16 kHz WAV on every ffmpeg failure.
        try:
            if audio_path != file_path and os.path.exists(audio_path):
                os.remove(audio_path)
                LEDGER['wav_removed'] += 1
        except Exception as exc:
            LOG.warning('temp audio cleanup failed: %s: %s', type(exc).__name__, exc)
        ledger_snapshot('file %s/%s END   %s' % (file_index, file_total, base[:40]), work_dir)


# ---------------------------------------------------------------------------
# persistence — 23 columns, same order as cell 28
# ---------------------------------------------------------------------------

def persist(session, records, results_table):
    """Insert via a temp view, converting JSON and timestamps in SQL.

    Cell 28's `except` branch is intentionally absent: it passed 14 positional values to
    a 23-column table, so it could only ever have raised. Failure here propagates.
    """
    if not records:
        LOG.info('nothing to persist')
        return 0

    from snowflake.snowpark.types import (StructType, StructField, StringType,
                                          FloatType, IntegerType)
    schema = StructType([
        StructField('FILE_PATH', StringType()), StructField('FILE_NAME', StringType()),
        StructField('FILE_TYPE', StringType()), StructField('DETECTED_LANGUAGE', StringType()),
        StructField('TRANSCRIPT', StringType()),
        StructField('TRANSCRIPT_WITH_SPEAKERS', StringType()),
        StructField('PROCESSING_TIME_SECONDS', FloatType()),
        StructField('FILE_SIZE_BYTES', IntegerType()),
        StructField('AUDIO_DURATION_SECONDS', FloatType()),
        StructField('SPEAKER_COUNT', IntegerType()),
        StructField('SRT_CONTENT', StringType()),
        StructField('SRT_WITH_SPEAKERS', StringType()),
        StructField('SUMMARY_MARKDOWN', StringType()),
        StructField('MEETING_TITLE', StringType()), StructField('CALL_BRIEF', StringType()),
        StructField('KEY_POINTS', StringType()), StructField('NEXT_STEPS', StringType()),
        StructField('DECISIONS_MADE', StringType()),
        StructField('QUESTIONS_RAISED', StringType()),
        StructField('ACCOUNT_NAME', StringType()), StructField('CALL_START_TS', StringType()),
        StructField('PARTICIPANTS_JSON', StringType()),
        StructField('TRANSCRIPTION_TIMESTAMP', StringType()),
    ])

    rows = [[r[c] for c in COLUMNS] for r in records]
    df = session.create_dataframe(rows, schema=schema)
    view = 'temp_transcription_data'
    df.create_or_replace_temp_view(view)

    session.sql("""
        INSERT INTO %s (%s)
        SELECT FILE_PATH, FILE_NAME, FILE_TYPE, DETECTED_LANGUAGE, TRANSCRIPT,
               CASE WHEN TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
                    THEN PARSE_JSON(TRANSCRIPT_WITH_SPEAKERS) ELSE NULL END,
               PROCESSING_TIME_SECONDS, FILE_SIZE_BYTES, AUDIO_DURATION_SECONDS,
               SPEAKER_COUNT, SRT_CONTENT, SRT_WITH_SPEAKERS, SUMMARY_MARKDOWN,
               MEETING_TITLE, CALL_BRIEF, KEY_POINTS, NEXT_STEPS, DECISIONS_MADE,
               QUESTIONS_RAISED, ACCOUNT_NAME,
               TRY_TO_TIMESTAMP_NTZ(CALL_START_TS, 'YYYY-MM-DD HH24:MI:SS'),
               CASE WHEN PARTICIPANTS_JSON IS NOT NULL
                    THEN PARSE_JSON(PARTICIPANTS_JSON) ELSE NULL END,
               TO_TIMESTAMP_NTZ(TRANSCRIPTION_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS.FF6')
        FROM %s
    """ % (results_table, ', '.join(COLUMNS), view)).collect()

    LOG.info('inserted %d record(s) into %s', len(records), results_table)
    return len(records)


def build_record(file_path, file_name, transcript, language, processing_time,
                 audio_duration, tws, speaker_count, srt, srt_speakers, summary,
                 include_file_path):
    """Assemble one row. Keys must cover COLUMNS exactly.

    FILE_PATH is populated by default because the notebook sets INCLUDE_FILE_PATH = True,
    and a column that silently changes from populated to empty across the port is the kind
    of diff that costs a reviewer an hour. Be clear about what it is worth, though:

      - Nothing reads it. It appears exactly once in the repo, as a column definition in
        02_setup.sql - not in the dashboard, not in any script, not in the uploader.
      - The stored value has no provenance value either way. The notebook writes
        'media_files/<name>', a cwd-relative path to a directory it deletes in cell 34;
        this writes the mkdtemp work dir, which is gone when the container exits. Both
        point at nothing by the time anyone could look.
      - So the CONTENT differs between notebook and payload rows and that is expected.
        Only the populated/empty shape is matched. Use --no-file-path to store ''.
    """
    meta = tf.parse_filename_metadata(file_name)
    participants = json.dumps([{'name': 'Bo Landsman',
                                'email': 'bo.landsman@snowflake.com',
                                'title': 'Solutions Engineer',
                                'affiliation': 'Internal'}])
    return {
        'FILE_PATH': file_path if include_file_path else '',
        'FILE_NAME': file_name,
        'FILE_TYPE': os.path.splitext(file_name)[1][1:].upper(),
        'DETECTED_LANGUAGE': language,
        'TRANSCRIPT': transcript,
        'TRANSCRIPT_WITH_SPEAKERS': json.dumps(tws) if tws is not None else None,
        'PROCESSING_TIME_SECONDS': float(processing_time),
        'FILE_SIZE_BYTES': int(tf.get_file_size(file_path) or 0),
        'AUDIO_DURATION_SECONDS': float(audio_duration or 0),
        'SPEAKER_COUNT': int(speaker_count or 0),
        'SRT_CONTENT': srt,
        'SRT_WITH_SPEAKERS': srt_speakers,
        'SUMMARY_MARKDOWN': summary['summary_markdown'] if summary else None,
        'MEETING_TITLE': summary['meeting_title'] if summary else None,
        'CALL_BRIEF': summary['call_brief'] if summary else None,
        'KEY_POINTS': summary['key_points'] if summary else None,
        'NEXT_STEPS': summary['next_steps'] if summary else None,
        'DECISIONS_MADE': summary['decisions_made'] if summary else None,
        'QUESTIONS_RAISED': summary['questions_raised'] if summary else None,
        'ACCOUNT_NAME': meta['account_name'],
        'CALL_START_TS': (meta['call_start_ts'].strftime('%Y-%m-%d %H:%M:%S')
                          if meta['call_start_ts'] else None),
        'PARTICIPANTS_JSON': participants,
        # UTC, explicitly. The column is TIMESTAMP_NTZ, which cannot express a zone, so
        # this call is the only place the contract exists - every reader has to convert.
        # A bare datetime.now() would return the container's wall clock, which is UTC
        # today but would silently change the column's meaning mid-table if the runtime's
        # timezone ever moved, with no offset stored to tell the eras apart.
        # Note TRANSCRIPTION_RUN_EVENTS.EVENT_TS is populated by CURRENT_TIMESTAMP() and
        # is TIMESTAMP_LTZ, so it reads as session-local. The two columns are not
        # comparable without a conversion.
        'TRANSCRIPTION_TIMESTAMP': datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f'),
    }


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    env = os.environ.get
    p.add_argument('--database', default=env('PROJECT_DB', 'TRANSCRIPTION_DB_V2'))
    p.add_argument('--schema', default=env('PROJECT_SCHEMA', 'TRANSCRIPTION_SCHEMA_V2'))
    p.add_argument('--warehouse', default=env('PROJECT_WH', 'TRANSCRIPTION_WH_V2'))
    p.add_argument('--stage', default=env('PROJECT_STAGE_AV', 'AUDIO_VIDEO_STAGE'))
    p.add_argument('--results-table', default=env('PROJECT_RESULTS_TABLE',
                                                  'TRANSCRIPTION_RESULTS'))
    p.add_argument('--run-events-table', default=env('PROJECT_RUN_EVENTS_TABLE',
                                                     'TRANSCRIPTION_RUN_EVENTS'))
    p.add_argument('--whisper-model', default=env('WHISPER_MODEL', 'base'),
                   help="default 'base'; 'large' is ~10x slower on GPU_NV_S")
    p.add_argument('--connection', default=None,
                   help='named Snowflake CLI connection for local runs; omit in-container')
    p.add_argument('--force-retranscribe', action='store_true')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--work-dir', default=None)
    p.add_argument('--no-file-path', dest='include_file_path', action='store_false',
                   default=True,
                   help='store empty string in FILE_PATH instead of the work-dir path')
    p.add_argument('--no-progress', action='store_true',
                   help='disable run-event emission. NOT for production: the dashboard '
                        'shows IDLE for the whole run without it.')
    p.add_argument('--dry-run', action='store_true',
                   help='discover and report, transcribe nothing')
    return p.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format='%(asctime)s %(levelname)-7s %(message)s')
    args = parse_args(argv)

    session = build_session(args)
    prog = RunProgress(session, args.run_events_table, enabled=not args.no_progress)
    LOG.info('run_id=%s results_table=%s', prog.run_id, args.results_table)

    work_dir = args.work_dir or tempfile.mkdtemp(prefix='transcribe_')
    owns_work_dir = args.work_dir is None
    exit_code = 0
    n_written = 0

    try:
        prog.emit('RUNNING', 'STARTUP', message='job service starting')
        preflight(require_gpu=not args.dry_run)
        prog.complete_unit()

        prog.emit('RUNNING', 'DISCOVER', message='listing stage')
        todo, _ = discover(session, args)
        prog.set_file_total(len(todo))
        prog.complete_unit()

        if not todo:
            prog.emit('SKIPPED', 'COMPLETE', message='no untranscribed media in stage')
            LOG.info('nothing to do')
            return 0
        if args.dry_run:
            LOG.info('DRY RUN. Would transcribe %d file(s): %s', len(todo), todo)
            prog.emit('SKIPPED', 'COMPLETE', message='dry run')
            return 0

        prog.emit('RUNNING', 'DOWNLOAD', file_total=len(todo),
                  message='downloading %d file(s)' % len(todo))
        local = download(session, todo, args.stage, work_dir)
        prog.complete_unit()

        import whisper
        import torch
        LOG.info('loading whisper %r', args.whisper_model)
        t0 = time.time()
        model = whisper.load_model(args.whisper_model,
                                   device='cuda' if torch.cuda.is_available() else 'cpu')
        LOG.info('whisper loaded in %.1fs', time.time() - t0)
        ledger_snapshot('after model load', work_dir)

        records = []
        total = len(local)
        prog.emit('RUNNING', 'TRANSCRIBE', file_total=total,
                  message='starting %d file(s)' % total)

        for idx, path in enumerate(local, 1):
            name = os.path.basename(path)
            prog.start_file()
            (transcript, language, ptime, duration,
             tws, speakers) = transcribe_media_file(
                model, whisper, path, prog=prog, file_index=idx, file_total=total)

            if transcript is None:
                LOG.error('no transcript for %s', name)
                prog.finish_file()
                continue

            prog.emit('RUNNING', 'TRANSCRIBE', file_index=idx, file_total=total,
                      current_file=name, file_step='GENERATE_SRT', message='generating SRT')
            srt = tf.generate_srt_content(tws)
            srt_speakers = tf.generate_srt_with_speakers(tws)
            prog.complete_unit()

            prog.emit('RUNNING', 'TRANSCRIBE', file_index=idx, file_total=total,
                      current_file=name, file_step='GENERATE_SUMMARY',
                      message='calling Cortex COMPLETE')
            summary = tf.generate_summary_markdown(session, name, transcript,
                                                   language, duration)
            prog.complete_unit()
            prog.emit('RUNNING', 'TRANSCRIBE', file_index=idx, file_total=total,
                      current_file=name, file_step='GENERATE_SUMMARY',
                      message='summary ok' if summary else None,
                      error_message=None if summary else 'summary generation returned None')

            records.append(build_record(path, name, transcript, language, ptime, duration,
                                        tws, speakers, srt, srt_speakers, summary,
                                        args.include_file_path))
            prog.finish_file()

        prog.emit('RUNNING', 'PERSIST', message='inserting %d record(s)' % len(records))
        n_written = persist(session, records, args.results_table)
        prog.complete_unit()

        # BEFORE the work dir is removed, or on_disk is always 0 and the check is vacuous.
        verdict = ledger_reconcile(total, work_dir)

        prog.emit('WORK_COMPLETE', 'COMPLETE',
                  message='%d/%d transcribed, ledger %s' % (n_written, total, verdict))

    except Exception as exc:
        exit_code = 1
        LOG.exception('run failed')
        prog.emit('FAILED', 'COMPLETE',
                  error_message='%s: %s' % (type(exc).__name__, exc))
    finally:
        if owns_work_dir:
            shutil.rmtree(work_dir, ignore_errors=True)
        ledger_snapshot('final post-cleanup', None)

        # The one thing the notebook can never do: report its own clean exit. Emitting
        # SUCCEEDED here is what turns WORK_COMPLETE_NOT_EXITED from a routine state into
        # a genuine alarm - after the port it would mean the job service has its own exit
        # problem rather than the known snowbook hang.
        if exit_code == 0:
            prog.emit('SUCCEEDED', 'COMPLETE',
                      message='job service exiting cleanly, %d record(s) written' % n_written)
        try:
            session.close()
        except Exception:
            pass

    LOG.info('exit %d', exit_code)
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
