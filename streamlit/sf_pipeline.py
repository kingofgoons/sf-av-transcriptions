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

    if run is None:
        accent, bg, label, blurb = STATE_STYLE['IDLE']
        state = 'IDLE'
    else:
        state = run.get('DERIVED_STATE') or 'RUNNING'
        accent, bg, label, blurb = STATE_STYLE.get(state, STATE_STYLE['IDLE'])

        # Cross-check the task. A CELLS_COMPLETE run whose task is still EXECUTING means
        # the container has not exited - that is the hang, and only TASK_HISTORY knows.
        if state == 'CELLS_COMPLETE':
            task = get_task_state(session)
            if task and task.get('STATE') == 'EXECUTING':
                state = 'WORK_COMPLETE_NOT_EXITED'
                accent, bg, label, _ = STATE_STYLE[state]
                blurb = (f"All cells finished but the task is still EXECUTING after "
                         f"{task.get('ELAPSED_SEC')}s. Known snowbook shutdown hang - "
                         f"transcripts are already saved.")

    detail_lines = []
    if run is not None:
        if run.get('PHASE'):
            detail_lines.append(f"Phase {run.get('PHASE_NUM')} of {run.get('PHASE_TOTAL')} "
                                f"&mdash; <b>{run.get('PHASE')}</b>")
        if run.get('FILE_TOTAL'):
            fi = run.get('FILE_INDEX')
            fname = run.get('CURRENT_FILE') or ''
            pos = (f"File {fi} of {run.get('FILE_TOTAL')}" if fi
                   else f"{run.get('FILE_TOTAL')} file(s)")
            detail_lines.append(f"{pos}{(' &mdash; ' + fname) if fname else ''}")
        if run.get('FILE_STEP'):
            detail_lines.append(f"Step {run.get('FILE_STEP_NUM')} of "
                                f"{run.get('FILE_STEP_TOTAL')} &mdash; {run.get('FILE_STEP')}")
        if run.get('UNITS_TOTAL'):
            pct = run.get('PCT_COMPLETE')
            detail_lines.append(
                f"{run.get('UNITS_DONE')} of {run.get('UNITS_TOTAL')} units complete"
                + (f" ({pct}%)" if pct is not None else ""))
        if run.get('MESSAGE'):
            detail_lines.append(f"<i>{run.get('MESSAGE')}</i>")
        if run.get('ERROR_MESSAGE'):
            detail_lines.append(f"<b style='color:#c62828'>{run.get('ERROR_MESSAGE')}</b>")
        hb = run.get('SECONDS_SINCE_HEARTBEAT')
        if hb is not None:
            detail_lines.append(
                f"<span class='timestamp'>last heartbeat {int(hb)}s ago &middot; "
                f"run {str(run.get('RUN_ID'))[:8]} &middot; {run.get('RUN_SOURCE')}</span>")

    status_card(accent, bg, label, blurb, detail_lines)

    return run, backlog, n_backlog, state
