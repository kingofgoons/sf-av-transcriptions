"""Pipeline run status: progress events, task state, and stage backlog.

These are the only functions that reach outside TRANSCRIPTION_RESULTS. None of them is
cached - the whole point is a live view.
"""

import pandas as pd
import streamlit as st

from sf_config import NAMES
from sf_theme import STATE_STYLE, status_card


def get_run_status(session):
    """Latest run state from V_TRANSCRIPTION_RUN_STATUS. Returns dict or None."""
    if session is None:
        return None
    try:
        df = session.sql(f"""
            SELECT RUN_ID, RUN_SOURCE, STATUS, DERIVED_STATE, IS_ACTIVE,
                   PHASE, PHASE_NUM, PHASE_TOTAL,
                   FILE_INDEX, FILE_TOTAL, CURRENT_FILE,
                   FILE_STEP, FILE_STEP_NUM, FILE_STEP_TOTAL,
                   UNITS_DONE, UNITS_TOTAL, PCT_COMPLETE,
                   SECONDS_SINCE_HEARTBEAT, MESSAGE, ERROR_MESSAGE,
                   TO_CHAR(LAST_HEARTBEAT_AT, 'YYYY-MM-DD HH24:MI:SS') AS LAST_HEARTBEAT_AT
            FROM {NAMES.V_RUN_STATUS}
            ORDER BY LAST_HEARTBEAT_AT DESC
            LIMIT 1
        """).to_pandas()
        return None if df.empty else df.iloc[0].to_dict()
    except Exception as e:
        st.warning(f"Could not read run status: {e}")
        return None


def get_task_state(session):
    """Most recent task run state, straight from TASK_HISTORY.

    This is the AUTHORITATIVE answer to "is the container still up?". The notebook
    cannot report its own clean exit - the snowbook shutdown hang happens after the
    last cell runs, so a hung run still emits CELLS_COMPLETE. Only the task knows
    whether EXECUTE NOTEBOOK actually returned.
    """
    if session is None:
        return None
    try:
        df = session.sql(f"""
            SELECT STATE,
                   TO_CHAR(SCHEDULED_TIME, 'YYYY-MM-DD HH24:MI:SS') AS SCHEDULED_TIME,
                   DATEDIFF('second', QUERY_START_TIME,
                            COALESCE(COMPLETED_TIME, CURRENT_TIMESTAMP())) AS ELAPSED_SEC,
                   ERROR_CODE,
                   LEFT(COALESCE(ERROR_MESSAGE, ''), 300) AS ERROR_MESSAGE,
                   LEFT(COALESCE(RETURN_VALUE, ''), 200)  AS RETURN_VALUE
            FROM TABLE({NAMES.DB}.INFORMATION_SCHEMA.TASK_HISTORY(
                TASK_NAME => '{NAMES.TASK_NAME}',
                SCHEDULED_TIME_RANGE_START => DATEADD('hour', -6, CURRENT_TIMESTAMP())))
            ORDER BY SCHEDULED_TIME DESC
            LIMIT 1
        """).to_pandas()
        return None if df.empty else df.iloc[0].to_dict()
    except Exception as e:
        st.warning(f"Could not read task history: {e}")
        return None


def _n(v):
    """Return v as an int, or None if it is NULL/NaN.

    REQUIRED, not decorative. Snowflake NUMBER columns arrive from to_pandas() as float64
    whenever the result contains a NULL, so a NULL becomes float('nan') - and **NaN is
    truthy in Python**. `if run.get('FILE_TOTAL'):` therefore PASSES on a NULL and renders
    the literal string "File nan of nan", which is exactly what the terminal COMPLETE event
    (which carries no per-file context) displayed until 2026-08-19.

    A float64 column also turns 6 into "6.0", so this casts to int as well.
    """
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def get_backlog(session, refresh=True):
    """Untranscribed files on the stage.

    Uses ALTER STAGE REFRESH + a DIRECTORY join on FILE_NAME - deliberately the SAME
    logic the task gate uses, so the UI and the gate can never disagree.

    Do NOT substitute SYSTEM$STREAM_HAS_DATA here: nothing consumes AV_STAGE_STREAM_V2,
    so it reports TRUE permanently.

    The refresh is REQUIRED, not cosmetic - PUT and put_stream do not register files in
    the directory table, so a freshly uploaded file is invisible without it.
    """
    if session is None:
        return pd.DataFrame()
    try:
        if refresh:
            session.sql(f"ALTER STAGE {NAMES.STAGE_AV} REFRESH").collect()
        return session.sql(f"""
            SELECT d.RELATIVE_PATH AS FILE_NAME,
                   ROUND(d.SIZE / 1048576.0, 1) AS SIZE_MB,
                   TO_CHAR(d.LAST_MODIFIED, 'YYYY-MM-DD HH24:MI') AS LAST_MODIFIED
            FROM DIRECTORY(@{NAMES.STAGE_AV}) d
            LEFT JOIN {NAMES.T_RESULTS} t ON d.RELATIVE_PATH = t.FILE_NAME
            WHERE t.FILE_NAME IS NULL
            ORDER BY d.LAST_MODIFIED DESC
        """).to_pandas()
    except Exception as e:
        st.warning(f"Could not read stage backlog: {e}")
        return pd.DataFrame()


def render_status_panel(session, refresh_stage=False):
    """Live pipeline status: phase N of M, file N of M, step N of M, plus an exact
    discrete completeness percentage.

    That percentage is a MEASUREMENT, not an estimate - the notebook counts a work unit
    only when it actually finishes, with no time-based interpolation.

    refresh_stage=False by default ON PURPOSE. ALTER STAGE REFRESH walks the whole stage
    (300+ files here), so doing it on every 5-second poll would be real, pointless
    warehouse spend. It only needs to run when files may have changed: on explicit
    refresh, and immediately after an upload.

    Returns (run, backlog_df, n_backlog, state) so the controls below reuse exactly the
    same state and cannot disagree with the panel about whether a run is active.
    """
    run = get_run_status(session)
    backlog = get_backlog(session, refresh=refresh_stage)
    n_backlog = 0 if backlog is None or backlog.empty else len(backlog)

    # TASK_HISTORY is read on EVERY poll, not just when a run looks complete. It is the
    # only source that knows a task is executing before the notebook has emitted anything,
    # which is a 60-180s window (measured: 62s to first event on a warm pool; a cold GPU
    # pool resume adds to that). Without this the panel shows the PREVIOUS run's terminal
    # card for minutes after a kickoff, which reads as "nothing happened".
    # Cost: one INFORMATION_SCHEMA table function call per refresh. Cheap, but not free -
    # this is why auto-refresh defaults to OFF.
    task = get_task_state(session)
    task_running = bool(task and task.get('STATE') == 'EXECUTING')

    if run is None:
        state = 'STARTING' if task_running else 'IDLE'
        accent, bg, label, blurb = STATE_STYLE[state]
    else:
        state = run.get('DERIVED_STATE') or 'RUNNING'
        accent, bg, label, blurb = STATE_STYLE.get(state, STATE_STYLE['IDLE'])

        # The task is up but the newest run we know about is already terminal. Two very
        # different situations produce that, and they must not be confused:
        #
        #   STARTING  - a NEW execution has begun and has not emitted yet. The terminal
        #               run belongs to a PREVIOUS execution.
        #   HUNG      - the terminal run belongs to THIS execution, which means all cells
        #               finished but the container never exited.
        #
        # Distinguish by age, not by state: if the last heartbeat is OLDER than the task's
        # own elapsed time, that heartbeat predates this execution and cannot belong to it.
        # Comparing the two elapsed counters avoids parsing timestamps across timezones.
        if task_running and not run.get('IS_ACTIVE'):
            hb = _n(run.get('SECONDS_SINCE_HEARTBEAT'))
            elapsed = _n(task.get('ELAPSED_SEC'))
            if hb is not None and elapsed is not None and hb > elapsed:
                state = 'STARTING'
                accent, bg, label, _ = STATE_STYLE[state]
                blurb = (f"A run started {elapsed}s ago. The notebook installs Whisper "
                         f"before it can report progress, so the first update takes "
                         f"roughly 60-180s (longer on a cold GPU pool).")
            elif state == 'CELLS_COMPLETE':
                state = 'WORK_COMPLETE_NOT_EXITED'
                accent, bg, label, _ = STATE_STYLE[state]
                blurb = (f"All cells finished but the task is still EXECUTING after "
                         f"{elapsed}s. Known snowbook shutdown hang - "
                         f"transcripts are already saved.")

    detail_lines = []
    # In STARTING, `run` is the PREVIOUS execution. Showing its phase and "8 of 8 units
    # (100%)" here would actively mislead - it would look like the new run finished
    # instantly. Suppress the stale detail and say plainly that nothing is reported yet.
    if state == 'STARTING':
        if n_backlog:
            detail_lines.append(f"{n_backlog} file(s) queued for transcription")
        detail_lines.append("<i>waiting for the first progress event&hellip;</i>")
        if run is not None:
            detail_lines.append(
                f"<span class='timestamp'>previous run {str(run.get('RUN_ID'))[:8]} "
                f"&middot; {run.get('DERIVED_STATE')}</span>")
    elif run is not None:
        phase_num, phase_total = _n(run.get('PHASE_NUM')), _n(run.get('PHASE_TOTAL'))
        if run.get('PHASE') and phase_num and phase_total:
            detail_lines.append(f"Phase {phase_num} of {phase_total} "
                                f"&mdash; <b>{run.get('PHASE')}</b>")

        # _n() throughout: NaN is truthy, so a bare `if run.get('FILE_TOTAL')` rendered
        # "File nan of nan" on the terminal event.
        file_total, file_index = _n(run.get('FILE_TOTAL')), _n(run.get('FILE_INDEX'))
        if file_total:
            fname = run.get('CURRENT_FILE') or ''
            pos = (f"File {file_index} of {file_total}" if file_index
                   else f"{file_total} file(s)")
            detail_lines.append(f"{pos}{(' &mdash; ' + fname) if fname else ''}")

        step_num, step_total = _n(run.get('FILE_STEP_NUM')), _n(run.get('FILE_STEP_TOTAL'))
        if run.get('FILE_STEP') and step_num and step_total:
            detail_lines.append(f"Step {step_num} of {step_total} "
                                f"&mdash; {run.get('FILE_STEP')}")

        units_done, units_total = _n(run.get('UNITS_DONE')), _n(run.get('UNITS_TOTAL'))
        if units_total:
            pct = run.get('PCT_COMPLETE')
            pct_txt = ''
            try:
                if pct is not None and not pd.isna(pct):
                    pct_txt = f" ({float(pct):.1f}%)"
            except (TypeError, ValueError):
                pass
            detail_lines.append(
                f"{units_done if units_done is not None else '?'} of {units_total} "
                f"units complete{pct_txt}")

        if run.get('MESSAGE'):
            detail_lines.append(f"<i>{run.get('MESSAGE')}</i>")
        if run.get('ERROR_MESSAGE'):
            detail_lines.append(f"<b style='color:#c62828'>{run.get('ERROR_MESSAGE')}</b>")
        hb = _n(run.get('SECONDS_SINCE_HEARTBEAT'))
        if hb is not None:
            detail_lines.append(
                f"<span class='timestamp'>last heartbeat {hb}s ago &middot; "
                f"run {str(run.get('RUN_ID'))[:8]} &middot; {run.get('RUN_SOURCE')}</span>")

    status_card(accent, bg, label, blurb, detail_lines)

    return run, backlog, n_backlog, state


####################################
# CONTROLS: trigger and upload
####################################

def trigger_transcription(session):
    """Fire the transcription task. Returns (ok, message).

    EXECUTE TASK, not CALL. Two reasons:
      - It is ASYNCHRONOUS: it queues the run and returns immediately, so the app does not
        block. A synchronous CALL of the gate would run EXECUTE NOTEBOOK inline and hold
        the app's session for the whole transcription, bounded only by the warehouse's
        4-hour STATEMENT_TIMEOUT rather than the task's 30-minute cap.
      - It routes through the task, which owns the real concurrency guard.

    EXECUTE TASK is absent from the documented list of statements permitted in an
    owner's-rights context, so this was spiked before being built: it works, and returns
    "Task ... is scheduled to run immediately." Verified 2026-08-19 against a no-op task
    and then against the real one.

    CONCURRENCY: the authority is the task's ALLOW_OVERLAPPING_EXECUTION = FALSE, not this
    app. The UI check in render_controls is advisory and inherently racy - two viewers can
    pass it simultaneously. The platform is what actually prevents a second concurrent run.
    """
    try:
        rows = session.sql(f"EXECUTE TASK {NAMES.FQ_TASK}").collect()
        msg = rows[0][0] if rows and len(rows[0]) else 'Task triggered'
        return True, str(msg)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def upload_to_stage(session, uploaded_file):
    """Upload one file to the media stage. Returns (ok, message).

    put_stream, not PUT: PUT reads from the client filesystem, which does not exist here -
    the bytes arrive in memory from the browser.

    auto_compress=False is REQUIRED. Gzipping media would break ffprobe duration detection
    and the extension-based format check in the notebook, and the file would sit on the
    stage looking fine while never transcribing.

    overwrite=False protects existing recordings; a collision is reported rather than
    silently replacing a transcript's source.
    """
    import io
    try:
        target = f"@{NAMES.STAGE_AV}/{uploaded_file.name}"
        session.file.put_stream(
            io.BytesIO(uploaded_file.getbuffer()),
            target,
            auto_compress=False,
            overwrite=False,
        )
        # REQUIRED, not cosmetic: put_stream does not register the file in the directory
        # table, so without this the backlog count and the task gate cannot see it.
        session.sql(f"ALTER STAGE {NAMES.STAGE_AV} REFRESH").collect()
        return True, f"Uploaded {uploaded_file.name}"
    except Exception as e:
        detail = str(e)
        if 'already exists' in detail.lower() or 'SUBMITTED' in detail:
            return False, f"{uploaded_file.name} already exists on the stage."
        return False, f"{type(e).__name__}: {detail[:300]}"


def validate_filename(name):
    """Check a filename against the pipeline's two real contracts.

    Returns (extension_ok, metadata_ok, hint).

    The metadata contract is parse_filename_metadata() in the notebook:
        YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext
    a SPACE between date and time, underscore-delimited, account taken from parts[1].
    A non-conforming name still transcribes fine but lands with ACCOUNT_NAME and
    CALL_START_TS NULL - so this warns rather than blocks.
    """
    import re
    from sf_config import SUPPORTED_EXTS, FILENAME_HINT

    ext = name.rsplit('.', 1)[-1].lower() if '.' in name else ''
    ext_ok = ext in SUPPORTED_EXTS
    meta_ok = bool(re.match(r'^\d{4}-\d{2}-\d{2} \d{2}-\d{2}-\d{2}_[^_]+', name))
    return ext_ok, meta_ok, FILENAME_HINT


def render_controls(session, run, n_backlog, state):
    """Trigger and upload controls. Takes the state the status panel already computed so
    the two cannot disagree about whether a run is active."""
    from sf_config import MAX_UPLOAD_MB, SUPPORTED_EXTS

    # IS_ACTIVE comes from the view: false once a run reaches CELLS_COMPLETE, SUCCEEDED or
    # FAILED. A wedged container still counts as active, because it is still holding a GPU.
    #
    # STARTING must block too. IS_ACTIVE is false during the 60-180s before the notebook
    # emits its first event, so keying only on IS_ACTIVE left the button live immediately
    # after a kickoff. The task's ALLOW_OVERLAPPING_EXECUTION = FALSE would reject the
    # second run, so it was never dangerous - but the button appeared to do nothing, which
    # is a poor way to discover that.
    is_active = (bool(run.get('IS_ACTIVE')) if run else False) or state == 'STARTING'

    col_run, col_up = st.columns([1, 2])

    # ---- Trigger -----------------------------------------------------------------
    with col_run:
        st.markdown("**Run transcription**")
        if is_active:
            st.button("▶ Start transcription", disabled=True, use_container_width=True)
            st.caption(f"Blocked: a run is {state}. Wait for it to finish.")
        elif n_backlog == 0:
            st.button("▶ Start transcription", disabled=True, use_container_width=True)
            st.caption("Nothing to do: no untranscribed files on the stage.")
            with st.expander("Force a run anyway"):
                st.caption(
                    "Launches a GPU container even though the gate will find no new "
                    "files. It will start the pool, decide there is nothing to do, and "
                    "stop. Costs credits for no result.")
                if st.button("Force run", key="force_run"):
                    ok, msg = trigger_transcription(session)
                    (st.success if ok else st.error)(msg)
                    if ok:
                        st.rerun()
        else:
            if st.button(f"▶ Start transcription ({n_backlog} file(s))",
                         type="primary", use_container_width=True):
                ok, msg = trigger_transcription(session)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
            st.caption("First run after idle includes a GPU cold start of roughly a "
                       "minute or two before transcription begins.")

    # ---- Upload ------------------------------------------------------------------
    with col_up:
        st.markdown("**Add media**")
        # State the cap BEFORE the picker, not as a post-failure error. This is a hard
        # warehouse-runtime limit, not a preference.
        st.caption(
            f"Max {MAX_UPLOAD_MB} MB per file — a hard Streamlit-in-Snowflake limit on "
            f"warehouse runtime, not configurable. Larger recordings must go through "
            f"`av.uploader/upload_av_files.py`; roughly 1 in 5 recordings in this corpus "
            f"exceeds it.")

        files = st.file_uploader(
            "Media files", type=list(SUPPORTED_EXTS), accept_multiple_files=True,
            label_visibility="collapsed")

        if files:
            oversize = [f for f in files if f.size > MAX_UPLOAD_MB * 1024 * 1024]
            for f in oversize:
                st.error(f"{f.name} is {f.size / 1048576:.0f} MB, over the "
                         f"{MAX_UPLOAD_MB} MB limit. Use av.uploader for this one.")

            ok_files = [f for f in files if f not in oversize]
            for f in ok_files:
                ext_ok, meta_ok, hint = validate_filename(f.name)
                if not meta_ok:
                    st.warning(
                        f"`{f.name}` does not match the naming convention, so "
                        f"ACCOUNT_NAME and the call timestamp will be empty. The "
                        f"transcript itself will still be fine.\n\nExpected: `{hint}`")

            if ok_files and st.button(f"⬆ Upload {len(ok_files)} file(s) to stage",
                                      use_container_width=True):
                results = [upload_to_stage(session, f) for f in ok_files]
                for (ok, msg) in results:
                    (st.success if ok else st.error)(msg)
                if any(ok for ok, _ in results):
                    st.info("Uploaded. Use **Start transcription** above when ready — "
                            "upload does not trigger a run on its own.")
                    st.rerun()
