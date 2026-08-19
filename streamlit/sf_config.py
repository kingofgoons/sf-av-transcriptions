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


NAMES = _Names()


def init(session):
    """Resolve object names from the app's own session context.

    Warehouse-runtime Streamlit apps run with owner's rights and "use the database and
    schema that the app was created in", so this is guaranteed to point at the deployment
    the app belongs to and cannot drift from scripts/00_config.sql.

    Historical note: every query in this dashboard used to reference TRANSCRIPTION_RESULTS
    UNQUALIFIED, which worked only by accident of session context - nothing in the code
    proved it targeted V2 rather than the retired V1 deployment.
    """
    if session is None:
        return NAMES
    try:
        db = session.get_current_database().replace('"', '')
        sc = session.get_current_schema().replace('"', '')
    except Exception:
        return NAMES

    fq = f"{db}.{sc}"
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
