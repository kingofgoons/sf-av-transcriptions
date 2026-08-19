"""Session access and object naming for the transcription dashboard.

WHY THE `NAMES` HOLDER EXISTS (do not "simplify" this to module constants):

Object names are derived from the LIVE session, not hardcoded, so they cannot be
plain module-level constants. If this module exposed

    T_RESULTS = f"{FQ_SCHEMA}.TRANSCRIPTION_RESULTS"

then a consumer doing `from sf_config import T_RESULTS` would bind whatever the value
was AT IMPORT TIME - which is before the session exists. Every query would silently
target the fallback names, and nothing would raise.

Instead `NAMES` is a mutable holder populated once by `init(session)` from the
entrypoint. Consumers do `from sf_config import NAMES` and read `NAMES.T_RESULTS` at
CALL time, which is order-independent and always correct.
"""

import streamlit as st
from snowflake.snowpark.context import get_active_session


# Fallback used only if the session context cannot be read. Matches the active V2
# deployment so a context failure degrades rather than pointing at retired V1 objects.
_FALLBACK_DB = 'TRANSCRIPTION_DB_V2'
_FALLBACK_SCHEMA = 'TRANSCRIPTION_SCHEMA_V2'


@st.cache_resource
def get_snowflake_connection():
    """Initialize Snowflake connection - using cache_resource for unserializable objects"""
    try:
        session = get_active_session()
        return session
    except Exception as e:
        st.error(f"Failed to connect to Snowflake: {str(e)}")
        st.info("Make sure you're running this in Snowflake's Streamlit environment.")
        return None


class _Names:
    """Fully-qualified object names, resolved at runtime by init()."""

    SOURCE = 'fallback'          # 'config_view' | 'session_context' | 'fallback'
    CONFIG_REVISION = 'unknown'
    DB = _FALLBACK_DB
    SCHEMA = _FALLBACK_SCHEMA
    FQ_SCHEMA = f"{_FALLBACK_DB}.{_FALLBACK_SCHEMA}"
    T_RESULTS = f"{FQ_SCHEMA}.TRANSCRIPTION_RESULTS"
    T_RUN_EVENTS = f"{FQ_SCHEMA}.TRANSCRIPTION_RUN_EVENTS"
    V_RUN_STATUS = f"{FQ_SCHEMA}.V_TRANSCRIPTION_RUN_STATUS"
    V_SUMMARY = f"{FQ_SCHEMA}.TRANSCRIPTION_SUMMARY"
    STAGE_AV = f"{FQ_SCHEMA}.AUDIO_VIDEO_STAGE"
    TASK_NAME = 'TRANSCRIBE_NEW_FILES_TASK_V2'
    FQ_TASK = f"{FQ_SCHEMA}.TRANSCRIBE_NEW_FILES_TASK_V2"
    FQ_GATE_PROC = f"{FQ_SCHEMA}.TRANSCRIBE_IF_NEW_FILES"
    RUN_STALE_SECS = 600


NAMES = _Names()

# The deployment config view. Single source of truth, projected from
# scripts/00_config.sql on every config load.
CONFIG_VIEW = 'TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG'


def init(session):
    """Resolve object names, preferring the shared config view.

    RESOLUTION ORDER
      1. TRANSCRIPTION_DEPLOY.PUBLIC.V_PROJECT_CONFIG - the authoritative projection of
         scripts/00_config.sql. Using it means the dashboard, the notebook and the SQL
         scripts all derive names from ONE authored source instead of hardcoding them in
         three places.
      2. Session context - the app's own database/schema. Correct for location, but object
         names within the schema fall back to defaults.
      3. Hardcoded V2 fallback, so a config outage degrades rather than breaking the app.

    WHY NOT `EXECUTE IMMEDIATE FROM .../00_config.sql`?
    Because it cannot work here. Warehouse-runtime Streamlit runs as an owner's-rights
    stored procedure, and those reject session variables outright:

        090244 (42601): Use of session variable '$PROJECT_DB' is not allowed in
                        owners rights stored procedure

    The script fails on its first SET. Verified empirically 2026-08-19. Reading the view is
    the supported way to share config with a Streamlit app.
    """
    if session is None:
        return NAMES

    # --- 1. Config view ---------------------------------------------------------
    try:
        row = session.sql(f"""
            SELECT CONFIG_REVISION, PROJECT_DB, PROJECT_SCHEMA, FQ_SCHEMA,
                   FQ_RESULTS, FQ_RUN_EVENTS, FQ_RUN_STATUS, FQ_STAGE_AV,
                   FQ_TASK, FQ_GATE_PROC, PROJECT_TASK_TRANSCRIBE, RUN_STALE_SECS
            FROM {CONFIG_VIEW}
        """).collect()[0]

        NAMES.SOURCE = 'config_view'
        NAMES.CONFIG_REVISION = row['CONFIG_REVISION']
        NAMES.DB = row['PROJECT_DB']
        NAMES.SCHEMA = row['PROJECT_SCHEMA']
        NAMES.FQ_SCHEMA = row['FQ_SCHEMA']
        NAMES.T_RESULTS = row['FQ_RESULTS']
        NAMES.T_RUN_EVENTS = row['FQ_RUN_EVENTS']
        NAMES.V_RUN_STATUS = row['FQ_RUN_STATUS']
        NAMES.V_SUMMARY = f"{row['FQ_SCHEMA']}.TRANSCRIPTION_SUMMARY"
        NAMES.STAGE_AV = row['FQ_STAGE_AV']
        NAMES.TASK_NAME = row['PROJECT_TASK_TRANSCRIBE']
        NAMES.FQ_TASK = row['FQ_TASK']
        NAMES.FQ_GATE_PROC = row['FQ_GATE_PROC']
        NAMES.RUN_STALE_SECS = int(row['RUN_STALE_SECS'])
        return NAMES
    except Exception:
        pass  # fall through to session context

    # --- 2. Session context -----------------------------------------------------
    try:
        db = session.get_current_database().replace('"', '')
        sc = session.get_current_schema().replace('"', '')
    except Exception:
        return NAMES

    fq = f"{db}.{sc}"
    NAMES.SOURCE = 'session_context'
    NAMES.DB = db
    NAMES.SCHEMA = sc
    NAMES.FQ_SCHEMA = fq
    NAMES.T_RESULTS = f"{fq}.TRANSCRIPTION_RESULTS"
    NAMES.T_RUN_EVENTS = f"{fq}.TRANSCRIPTION_RUN_EVENTS"
    NAMES.V_RUN_STATUS = f"{fq}.V_TRANSCRIPTION_RUN_STATUS"
    NAMES.V_SUMMARY = f"{fq}.TRANSCRIPTION_SUMMARY"
    NAMES.STAGE_AV = f"{fq}.AUDIO_VIDEO_STAGE"
    NAMES.FQ_TASK = f"{fq}.{NAMES.TASK_NAME}"
    NAMES.FQ_GATE_PROC = f"{fq}.TRANSCRIBE_IF_NEW_FILES"
    return NAMES


####################################
# CONSTANTS
####################################

# Hard platform limit for st.file_uploader on a WAREHOUSE runtime. NOT configurable -
# only container runtimes can raise it via server.maxUploadSize. Measured against this
# corpus, 18% of existing recordings (80 of 443) exceed it, the largest being 1.7 GB, so
# in-app upload is a convenience path for small files and NOT a replacement for
# av.uploader/upload_av_files.py.
MAX_UPLOAD_MB = 200

# Media extensions the notebook actually globs for. Anything else sits on the stage and is
# silently ignored by the pipeline, so uploading it would appear to work and then do nothing.
SUPPORTED_EXTS = ('mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg',
                  'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv')

# Progress model, mirroring scripts/02_setup.sql and the notebook's RunProgress emitter.
PHASE_TOTAL = 6

# Filename contract enforced by parse_filename_metadata() in the notebook:
#   YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext
# A SPACE separates date from time; fields are underscore-delimited and the account is
# taken from parts[1]. A file named anything else still transcribes fine but lands with
# ACCOUNT_NAME and CALL_START_TS NULL.
FILENAME_HINT = 'YYYY-MM-DD HH-MM-SS_AccountName_description.mp4'
