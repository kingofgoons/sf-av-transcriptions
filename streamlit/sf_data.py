"""Queries against TRANSCRIPTION_RESULTS.

Moved verbatim from the monolithic dashboard; only the table references changed, from
unqualified names to NAMES.* so they are explicit and cannot resolve to retired V1
objects by accident of session context.
"""

import pandas as pd
import streamlit as st
import json

from sf_config import NAMES


def load_transcription_data(session, limit=1000):
    """Load transcription data from Snowflake"""
    if session is None:
        return pd.DataFrame()
    
    query = f"""
    SELECT 
        FILE_NAME,
        FILE_TYPE,
        DETECTED_LANGUAGE,
        TRANSCRIPT,
        TRANSCRIPT_WITH_SPEAKERS,
        SPEAKER_COUNT,
        PROCESSING_TIME_SECONDS,
        FILE_SIZE_BYTES,
        AUDIO_DURATION_SECONDS,
        SRT_CONTENT,
        SRT_WITH_SPEAKERS,
        SUMMARY_MARKDOWN,
        TO_CHAR(TRANSCRIPTION_TIMESTAMP, 'YYYY-MM-DD HH24:MI:SS') AS TRANSCRIPTION_TIMESTAMP,
        MEETING_TITLE,
        ACCOUNT_NAME,
        TO_CHAR(CALL_START_TS, 'YYYY-MM-DD') AS CALL_START_TS,
        TO_VARCHAR(KEY_POINTS) AS KEY_POINTS,
        TO_VARCHAR(NEXT_STEPS) AS NEXT_STEPS,
        TO_VARCHAR(DECISIONS_MADE) AS DECISIONS_MADE,
        TO_VARCHAR(QUESTIONS_RAISED) AS QUESTIONS_RAISED
    FROM {NAMES.T_RESULTS} 
    ORDER BY TRANSCRIPTION_TIMESTAMP DESC 
    LIMIT {limit}
    """
    
    try:
        return session.sql(query).to_pandas()
    except Exception as e:
        st.error(f"Error loading data: {str(e)}")
        return pd.DataFrame()


def get_summary_stats(session):
    """Get summary statistics"""
    if session is None:
        return {}
    
    stats = {}
    
    try:
        # Total files
        result = session.sql(f"SELECT COUNT(*) as count FROM {NAMES.T_RESULTS}").to_pandas()
        stats['total_files'] = result.iloc[0, 0] if not result.empty else 0
        
        # Total duration in hours
        result = session.sql(f"SELECT SUM(AUDIO_DURATION_SECONDS)/3600 as hours FROM {NAMES.T_RESULTS} WHERE AUDIO_DURATION_SECONDS IS NOT NULL").to_pandas()
        stats['total_duration'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0
        
        # Average processing time
        result = session.sql(f"SELECT AVG(PROCESSING_TIME_SECONDS) as avg_time FROM {NAMES.T_RESULTS}").to_pandas()
        stats['avg_processing_time'] = result.iloc[0, 0] if not result.empty else 0
        
        # Number of languages
        result = session.sql(f"SELECT COUNT(DISTINCT DETECTED_LANGUAGE) as count FROM {NAMES.T_RESULTS}").to_pandas()
        stats['languages'] = result.iloc[0, 0] if not result.empty else 0
        
        # Files with speaker data
        result = session.sql(f"SELECT COUNT(*) as count FROM {NAMES.T_RESULTS} WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL").to_pandas()
        stats['files_with_speakers'] = result.iloc[0, 0] if not result.empty else 0
        
        # Average speakers per file
        result = session.sql(f"SELECT AVG(SPEAKER_COUNT) as avg_speakers FROM {NAMES.T_RESULTS} WHERE SPEAKER_COUNT > 0").to_pandas()
        stats['avg_speakers'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0

        # Distinct accounts
        result = session.sql(f"SELECT COUNT(DISTINCT ACCOUNT_NAME) as count FROM {NAMES.T_RESULTS} WHERE ACCOUNT_NAME IS NOT NULL").to_pandas()
        stats['account_count'] = result.iloc[0, 0] if not result.empty else 0
        
    except Exception as e:
        st.warning(f"Error getting statistics: {str(e)}")
        stats = {'total_files': 0, 'total_duration': 0, 'avg_processing_time': 0, 'languages': 0, 'files_with_speakers': 0, 'avg_speakers': 0, 'account_count': 0}
    
    return stats


def get_speaker_segments(session, file_name):
    """Get speaker segments for a specific file"""
    if session is None:
        return []
    
    query = f"""
    SELECT 
        FILE_NAME,
        TRANSCRIPT_WITH_SPEAKERS,
        SPEAKER_COUNT,
        DETECTED_LANGUAGE,
        AUDIO_DURATION_SECONDS
    FROM {NAMES.T_RESULTS} 
    WHERE FILE_NAME = '{file_name}' 
    AND TRANSCRIPT_WITH_SPEAKERS IS NOT NULL
    """
    
    try:
        result = session.sql(query).to_pandas()
        if result.empty:
            return []
        
        # Parse the JSON data
        transcript_data = result.iloc[0]['TRANSCRIPT_WITH_SPEAKERS']
        if transcript_data is None:
            return []
        
        # If it's a string, parse it as JSON
        if isinstance(transcript_data, str):
            transcript_data = json.loads(transcript_data)
        
        return transcript_data.get('speakers', [])
        
    except Exception as e:
        st.error(f"Error loading speaker segments: {str(e)}")
        return []


def search_transcriptions(session, search_term, file_type=None, language=None, date_range=None, account_name=None):
    """Search transcriptions"""
    if session is None:
        return pd.DataFrame()
    
    where_conditions = [f"(TRANSCRIPT ILIKE '%{search_term}%' OR MEETING_TITLE ILIKE '%{search_term}%')"]
    
    if file_type and file_type != "All":
        where_conditions.append(f"FILE_TYPE = '{file_type}'")
    
    if language and language != "All":
        where_conditions.append(f"DETECTED_LANGUAGE = '{language}'")
    
    if account_name and account_name != "All":
        where_conditions.append(f"ACCOUNT_NAME = '{account_name}'")
    
    if date_range:
        start_date, end_date = date_range
        where_conditions.append(f"DATE(TRANSCRIPTION_TIMESTAMP) BETWEEN '{start_date}' AND '{end_date}'")
    
    where_clause = " AND ".join(where_conditions)
    
    query = f"""
    SELECT 
        FILE_NAME,
        FILE_TYPE,
        DETECTED_LANGUAGE,
        TRANSCRIPT,
        TRANSCRIPT_WITH_SPEAKERS,
        SPEAKER_COUNT,
        TRANSCRIPTION_TIMESTAMP,
        AUDIO_DURATION_SECONDS,
        MEETING_TITLE,
        ACCOUNT_NAME,
        TO_CHAR(CALL_START_TS, 'YYYY-MM-DD') AS CALL_START_TS
    FROM {NAMES.T_RESULTS} 
    WHERE {where_clause}
    ORDER BY TRANSCRIPTION_TIMESTAMP DESC 
    LIMIT 50
    """
    
    try:
        return session.sql(query).to_pandas()
    except Exception as e:
        st.error(f"Search error: {str(e)}")
        return pd.DataFrame()
