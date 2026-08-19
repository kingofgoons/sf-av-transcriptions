# v2
import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from snowflake.snowpark.context import get_active_session
import re
import json
import io
import traceback
import base64

# Page configuration
st.set_page_config(
    page_title="Audio/Video Transcription Dashboard",
    page_icon="🎵",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for better styling
st.markdown("""
<style>
    .metric-container {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 0.25rem solid #1f77b4;
        margin: 0.5rem 0;
    }
    .search-result {
        background-color: #f8f9fa;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 0.5rem 0;
    }
    .transcript-box {
        background-color: #ffffff;
        padding: 1rem;
        border-radius: 0.5rem;
        border: 1px solid #e0e0e0;
        margin: 0.5rem 0;
    }
    .speaker-segment {
        background-color: #f9f9f9;
        padding: 0.75rem;
        border-radius: 0.25rem;
        border-left: 0.25rem solid #4CAF50;
        margin: 0.5rem 0;
    }
    .speaker-label {
        font-weight: bold;
        color: #2E7D32;
        font-size: 0.9rem;
        margin-bottom: 0.25rem;
    }
    .speaker-text {
        color: #333;
        line-height: 1.5;
    }
    .timestamp {
        color: #666;
        font-size: 0.8rem;
    }
    .info-box {
        background-color: #e7f3ff;
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 0.25rem solid #0066cc;
        margin: 1rem 0;
    }
</style>
""", unsafe_allow_html=True)

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


####################################
# OBJECT NAMES
####################################
#
# Resolved from the app's OWN session context rather than hardcoded. Warehouse-runtime
# Streamlit apps run with owner's rights and "use the database and schema that the app
# was created in", so this is guaranteed to point at the deployment the app belongs to
# and cannot drift from scripts/00_config.sql.
#
# WHY THIS EXISTS: every query in this file used to reference TRANSCRIPTION_RESULTS
# UNQUALIFIED, which worked only by accident of session context - nothing in the file
# proved it targeted V2 rather than the retired V1 deployment. The status panel also
# needs objects that will NOT resolve implicitly (TASK_HISTORY, DIRECTORY), so explicit
# names became a hard requirement.

_session_for_names = get_snowflake_connection()
if _session_for_names is not None:
    try:
        _DB = _session_for_names.get_current_database().replace('"', '')
        _SC = _session_for_names.get_current_schema().replace('"', '')
        FQ_SCHEMA = f"{_DB}.{_SC}"
    except Exception:
        _DB, _SC = 'TRANSCRIPTION_DB_V2', 'TRANSCRIPTION_SCHEMA_V2'
        FQ_SCHEMA = f"{_DB}.{_SC}"
else:
    _DB, _SC = 'TRANSCRIPTION_DB_V2', 'TRANSCRIPTION_SCHEMA_V2'
    FQ_SCHEMA = f"{_DB}.{_SC}"

T_RESULTS    = f"{FQ_SCHEMA}.TRANSCRIPTION_RESULTS"
T_RUN_EVENTS = f"{FQ_SCHEMA}.TRANSCRIPTION_RUN_EVENTS"
V_RUN_STATUS = f"{FQ_SCHEMA}.V_TRANSCRIPTION_RUN_STATUS"
STAGE_AV     = f"{FQ_SCHEMA}.AUDIO_VIDEO_STAGE"
TASK_NAME    = 'TRANSCRIBE_NEW_FILES_TASK_V2'
FQ_TASK      = f"{FQ_SCHEMA}.{TASK_NAME}"
FQ_GATE_PROC = f"{FQ_SCHEMA}.TRANSCRIBE_IF_NEW_FILES"

# Hard platform limit for st.file_uploader on a WAREHOUSE runtime. Not configurable -
# only container runtimes can raise it via server.maxUploadSize. Measured against this
# corpus, 18% of existing recordings (80 of 443) exceed it, up to 1.7 GB, so this is a
# convenience path for small files and NOT a replacement for av.uploader.
MAX_UPLOAD_MB = 200

# Media extensions the notebook actually globs for. Anything else is ignored by the
# pipeline, so uploading it would silently do nothing.
SUPPORTED_EXTS = ('mp3', 'wav', 'm4a', 'flac', 'aac', 'ogg',
                  'mp4', 'avi', 'mov', 'mkv', 'webm', 'flv')

# Progress model, mirroring scripts/02_setup.sql and the notebook emitter.
PHASE_TOTAL = 6

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
    FROM {T_RESULTS} 
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
        result = session.sql(f"SELECT COUNT(*) as count FROM {T_RESULTS}").to_pandas()
        stats['total_files'] = result.iloc[0, 0] if not result.empty else 0
        
        # Total duration in hours
        result = session.sql(f"SELECT SUM(AUDIO_DURATION_SECONDS)/3600 as hours FROM {T_RESULTS} WHERE AUDIO_DURATION_SECONDS IS NOT NULL").to_pandas()
        stats['total_duration'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0
        
        # Average processing time
        result = session.sql(f"SELECT AVG(PROCESSING_TIME_SECONDS) as avg_time FROM {T_RESULTS}").to_pandas()
        stats['avg_processing_time'] = result.iloc[0, 0] if not result.empty else 0
        
        # Number of languages
        result = session.sql(f"SELECT COUNT(DISTINCT DETECTED_LANGUAGE) as count FROM {T_RESULTS}").to_pandas()
        stats['languages'] = result.iloc[0, 0] if not result.empty else 0
        
        # Files with speaker data
        result = session.sql(f"SELECT COUNT(*) as count FROM {T_RESULTS} WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL").to_pandas()
        stats['files_with_speakers'] = result.iloc[0, 0] if not result.empty else 0
        
        # Average speakers per file
        result = session.sql(f"SELECT AVG(SPEAKER_COUNT) as avg_speakers FROM {T_RESULTS} WHERE SPEAKER_COUNT > 0").to_pandas()
        stats['avg_speakers'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0

        # Distinct accounts
        result = session.sql(f"SELECT COUNT(DISTINCT ACCOUNT_NAME) as count FROM {T_RESULTS} WHERE ACCOUNT_NAME IS NOT NULL").to_pandas()
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
    FROM {T_RESULTS} 
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
    FROM {T_RESULTS} 
    WHERE {where_clause}
    ORDER BY TRANSCRIPTION_TIMESTAMP DESC 
    LIMIT 50
    """
    
    try:
        return session.sql(query).to_pandas()
    except Exception as e:
        st.error(f"Search error: {str(e)}")
        return pd.DataFrame()

def find_matching_segments_with_context(speaker_segments, search_term, context_size=10):
    """Find speaker segments that match search term and return with context"""
    if not speaker_segments or not search_term:
        return []
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    # Find segments containing the search term
    matching_indices = []
    for i, segment in enumerate(sorted_segments):
        text = segment.get('text', '').lower()
        if search_term.lower() in text:
            matching_indices.append(i)
    
    if not matching_indices:
        return []
    
    # Extract context around matches
    context_segments = []
    added_indices = set()
    
    for match_idx in matching_indices:
        # Calculate context range
        start_idx = max(0, match_idx - context_size)
        end_idx = min(len(sorted_segments), match_idx + context_size + 1)
        
        # Add segments in context range
        for i in range(start_idx, end_idx):
            if i not in added_indices:
                segment = sorted_segments[i].copy()
                segment['is_match'] = (i == match_idx)
                segment['context_group'] = match_idx  # Group segments by their match
                context_segments.append(segment)
                added_indices.add(i)
    
    # Sort by start time to maintain chronological order
    return sorted(context_segments, key=lambda x: x.get('start_time', 0))

def display_search_result_with_speakers(speaker_segments, search_term, file_info=None):
    """Display search results with speaker segments and context"""
    if not speaker_segments:
        st.info("No speaker segments available for this search result.")
        return
    
    # Display file info if available
    if file_info:
        st.markdown(f"""
        **File:** {file_info.get('filename', 'Unknown')} | 
        **Language:** {file_info.get('language', 'Unknown')} | 
        **Duration:** {file_info.get('duration', 0):.1f}s |
        **Speakers:** {file_info.get('speaker_count', 'N/A')}
        """)
    
    # Group consecutive segments by speaker to reduce repetition (similar to display_speaker_transcript)
    grouped_segments = []
    current_speaker = None
    current_text = ""
    current_start = None
    current_end = None
    current_is_match = False
    current_context_group = None
    
    for segment in speaker_segments:
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        is_match = segment.get('is_match', False)
        context_group = segment.get('context_group')
        
        # Only group if same speaker, close timing, same match status, and same context group
        if (speaker == current_speaker and 
            current_end and abs(start_time - current_end) < 2 and
            is_match == current_is_match and
            context_group == current_context_group):
            # Same speaker, close timing, same match status - combine segments
            current_text += " " + text
            current_end = end_time
        else:
            # Different speaker, gap in time, or different match status - save previous and start new
            if current_speaker is not None:
                grouped_segments.append({
                    'speaker': current_speaker,
                    'text': current_text,
                    'start_time': current_start,
                    'end_time': current_end,
                    'is_match': current_is_match,
                    'context_group': current_context_group
                })
            
            current_speaker = speaker
            current_text = text
            current_start = start_time
            current_end = end_time
            current_is_match = is_match
            current_context_group = context_group
    
    # Don't forget the last segment
    if current_speaker is not None:
        grouped_segments.append({
            'speaker': current_speaker,
            'text': current_text,
            'start_time': current_start,
            'end_time': current_end,
            'is_match': current_is_match,
            'context_group': current_context_group
        })
    
    # Display the grouped segments with context
    current_context_group = None
    match_count = 0
    
    for i, segment in enumerate(grouped_segments):
        speaker = segment['speaker']
        text = segment['text']
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        is_match = segment.get('is_match', False)
        context_group = segment.get('context_group')
        
        # Add separator between different context groups
        if context_group != current_context_group and current_context_group is not None:
            st.markdown("---")
        
        if context_group != current_context_group:
            current_context_group = context_group
            if is_match:
                match_count += 1
                st.markdown(f"**🎯 Match {match_count}:**")
        
        # Format time as MM:SS
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        time_range = f"{start_mins:02d}:{start_secs:02d} - {end_mins:02d}:{end_secs:02d}"
        
        # Highlight search term in matching segments
        display_text = text
        if is_match:
            display_text = highlight_text(text, search_term)
        
        # Different styling for match vs context
        if is_match:
            # Highlight the matching segment
            st.markdown(f"""
            <div class="speaker-segment" style="border-left-color: #FF5722; background-color: #FFF3E0;">
                <div class="speaker-label" style="color: #E65100;">{speaker} <span class="timestamp">({time_range}) 🎯 MATCH</span></div>
                <div class="speaker-text" style="font-weight: 500;">{display_text}</div>
            </div>
            """, unsafe_allow_html=True)
        else:
            # Regular context segment
            st.markdown(f"""
            <div class="speaker-segment" style="border-left-color: #9E9E9E; background-color: #FAFAFA;">
                <div class="speaker-label" style="color: #616161;">{speaker} <span class="timestamp">({time_range})</span></div>
                <div class="speaker-text" style="color: #757575;">{display_text}</div>
            </div>
            """, unsafe_allow_html=True)

def highlight_text(text, search_term):
    """Highlight search term in text"""
    if not search_term or not text:
        return text
    
    # Simple highlighting by making search term bold
    pattern = re.compile(re.escape(search_term), re.IGNORECASE)
    return pattern.sub(f"**{search_term.upper()}**", str(text))

def display_speaker_transcript(speaker_segments, file_info=None):
    """Display transcript with speaker segments line by line"""
    if not speaker_segments:
        st.info("No speaker segments available for this file.")
        return
    
    # Display file info if available
    if file_info:
        st.markdown(f"""
        **File:** {file_info.get('filename', 'Unknown')} | 
        **Language:** {file_info.get('language', 'Unknown')} | 
        **Duration:** {file_info.get('duration', 0):.1f}s
        """)
        st.divider()
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    # Group consecutive segments by speaker to reduce repetition
    grouped_segments = []
    current_speaker = None
    current_text = ""
    current_start = None
    current_end = None
    
    for segment in sorted_segments:
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        
        if speaker == current_speaker and current_end and abs(start_time - current_end) < 2:
            # Same speaker, close timing - combine segments
            current_text += " " + text
            current_end = end_time
        else:
            # Different speaker or gap in time - save previous and start new
            if current_speaker is not None:
                grouped_segments.append({
                    'speaker': current_speaker,
                    'text': current_text,
                    'start_time': current_start,
                    'end_time': current_end
                })
            
            current_speaker = speaker
            current_text = text
            current_start = start_time
            current_end = end_time
    
    # Don't forget the last segment
    if current_speaker is not None:
        grouped_segments.append({
            'speaker': current_speaker,
            'text': current_text,
            'start_time': current_start,
            'end_time': current_end
        })
    
    # Display the grouped segments
    for i, segment in enumerate(grouped_segments):
        speaker = segment['speaker']
        text = segment['text']
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        
        # Format time as MM:SS
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        time_range = f"{start_mins:02d}:{start_secs:02d} - {end_mins:02d}:{end_secs:02d}"
        
        # Display the segment
        st.markdown(f"""
        <div class="speaker-segment">
            <div class="speaker-label">{speaker} <span class="timestamp">({time_range})</span></div>
            <div class="speaker-text">{text}</div>
        </div>
        """, unsafe_allow_html=True)

def convert_speaker_segments_to_csv(speaker_segments, file_info=None):
    """Convert speaker segments to CSV format"""
    if not speaker_segments:
        return None
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    # Prepare data for CSV
    csv_data = []
    
    for i, segment in enumerate(sorted_segments, 1):
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        duration = segment.get('duration', end_time - start_time)
        
        # Format timestamps
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        start_time_formatted = f"{start_mins:02d}:{start_secs:02d}"
        end_time_formatted = f"{end_mins:02d}:{end_secs:02d}"
        
        csv_data.append({
            'Segment': i,
            'Speaker': speaker,
            'Start_Time': start_time_formatted,
            'End_Time': end_time_formatted,
            'Start_Seconds': start_time,
            'End_Seconds': end_time,
            'Duration_Seconds': round(duration, 2),
            'Text': text
        })
    
    # Create DataFrame
    df = pd.DataFrame(csv_data)
    
    # Add metadata at the top if available
    if file_info:
        metadata_rows = []
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'File',
            'Start_Time': file_info.get('filename', 'Unknown'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Language',
            'Start_Time': file_info.get('language', 'Unknown'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Duration',
            'Start_Time': f"{file_info.get('duration', 0):.1f}s",
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Export_Date',
            'Start_Time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': '---',
            'Speaker': '---',
            'Start_Time': '---',
            'End_Time': '---',
            'Start_Seconds': '---',
            'End_Seconds': '---',
            'Duration_Seconds': '---',
            'Text': '---'
        })
        
        # Combine metadata and data
        metadata_df = pd.DataFrame(metadata_rows)
        df = pd.concat([metadata_df, df], ignore_index=True)
    
    return df

def create_csv_download(df, filename):
    """Create CSV file for download"""
    if df is None or df.empty:
        return None
    
    # Convert DataFrame to CSV
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_string = csv_buffer.getvalue()
    
    return csv_string

def convert_search_results_to_csv(context_segments, file_info=None, search_term=""):
    """Convert search results with context to CSV format"""
    if not context_segments:
        return None
    
    # Sort segments by start time
    sorted_segments = sorted(context_segments, key=lambda x: x.get('start_time', 0))
    
    # Prepare data for CSV
    csv_data = []
    
    for i, segment in enumerate(sorted_segments, 1):
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        duration = segment.get('duration', end_time - start_time)
        is_match = segment.get('is_match', False)
        context_group = segment.get('context_group', 0)
        
        # Format timestamps
        start_mins, start_secs = divmod(int(start_time), 60)
        end_mins, end_secs = divmod(int(end_time), 60)
        start_time_formatted = f"{start_mins:02d}:{start_secs:02d}"
        end_time_formatted = f"{end_mins:02d}:{end_secs:02d}"
        
        csv_data.append({
            'Segment': i,
            'Speaker': speaker,
            'Start_Time': start_time_formatted,
            'End_Time': end_time_formatted,
            'Start_Seconds': start_time,
            'End_Seconds': end_time,
            'Duration_Seconds': round(duration, 2),
            'Is_Match': 'YES' if is_match else 'CONTEXT',
            'Match_Group': context_group + 1,
            'Text': text
        })
    
    # Create DataFrame
    df = pd.DataFrame(csv_data)
    
    # Add metadata at the top if available
    if file_info:
        metadata_rows = []
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Search_Term',
            'Start_Time': search_term,
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Is_Match': '',
            'Match_Group': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'File',
            'Start_Time': file_info.get('filename', 'Unknown'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Is_Match': '',
            'Match_Group': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Language',
            'Start_Time': file_info.get('language', 'Unknown'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Is_Match': '',
            'Match_Group': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Duration',
            'Start_Time': f"{file_info.get('duration', 0):.1f}s",
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Is_Match': '',
            'Match_Group': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': 'METADATA',
            'Speaker': 'Export_Date',
            'Start_Time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'End_Time': '',
            'Start_Seconds': '',
            'End_Seconds': '',
            'Duration_Seconds': '',
            'Is_Match': '',
            'Match_Group': '',
            'Text': ''
        })
        metadata_rows.append({
            'Segment': '---',
            'Speaker': '---',
            'Start_Time': '---',
            'End_Time': '---',
            'Start_Seconds': '---',
            'End_Seconds': '---',
            'Duration_Seconds': '---',
            'Is_Match': '---',
            'Match_Group': '---',
            'Text': '---'
        })
        
        # Combine metadata and data
        metadata_df = pd.DataFrame(metadata_rows)
        df = pd.concat([metadata_df, df], ignore_index=True)
    
    return df

def convert_speaker_segments_to_srt(speaker_segments, file_info=None, include_speakers=True):
    """Convert speaker segments to SRT subtitle format"""
    if not speaker_segments:
        return None
    
    # Sort segments by start time
    sorted_segments = sorted(speaker_segments, key=lambda x: x.get('start_time', 0))
    
    srt_content = []
    
    for i, segment in enumerate(sorted_segments, 1):
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        
        if not text:  # Skip empty segments
            continue
        
        # Format timestamps for SRT (HH:MM:SS,mmm)
        start_hours, start_remainder = divmod(int(start_time), 3600)
        start_minutes, start_seconds = divmod(start_remainder, 60)
        start_milliseconds = int((start_time - int(start_time)) * 1000)
        
        end_hours, end_remainder = divmod(int(end_time), 3600)
        end_minutes, end_seconds = divmod(end_remainder, 60)
        end_milliseconds = int((end_time - int(end_time)) * 1000)
        
        start_timestamp = f"{start_hours:02d}:{start_minutes:02d}:{start_seconds:02d},{start_milliseconds:03d}"
        end_timestamp = f"{end_hours:02d}:{end_minutes:02d}:{end_seconds:02d},{end_milliseconds:03d}"
        
        # Format subtitle text
        if include_speakers and speaker != 'Unknown':
            subtitle_text = f"{speaker}: {text}"
        else:
            subtitle_text = text
        
        # Create SRT entry
        srt_entry = f"{i}\n{start_timestamp} --> {end_timestamp}\n{subtitle_text}\n"
        srt_content.append(srt_entry)
    
    return "\n".join(srt_content)

def convert_search_results_to_srt(context_segments, file_info=None, search_term="", include_speakers=True):
    """Convert search results with context to SRT subtitle format"""
    if not context_segments:
        return None
    
    # Sort segments by start time
    sorted_segments = sorted(context_segments, key=lambda x: x.get('start_time', 0))
    
    srt_content = []
    subtitle_number = 1
    
    for segment in sorted_segments:
        speaker = segment.get('speaker', 'Unknown')
        text = segment.get('text', '').strip()
        start_time = segment.get('start_time', 0)
        end_time = segment.get('end_time', 0)
        is_match = segment.get('is_match', False)
        
        if not text:  # Skip empty segments
            continue
        
        # Format timestamps for SRT (HH:MM:SS,mmm)
        start_hours, start_remainder = divmod(int(start_time), 3600)
        start_minutes, start_seconds = divmod(start_remainder, 60)
        start_milliseconds = int((start_time - int(start_time)) * 1000)
        
        end_hours, end_remainder = divmod(int(end_time), 3600)
        end_minutes, end_seconds = divmod(end_remainder, 60)
        end_milliseconds = int((end_time - int(end_time)) * 1000)
        
        start_timestamp = f"{start_hours:02d}:{start_minutes:02d}:{start_seconds:02d},{start_milliseconds:03d}"
        end_timestamp = f"{end_hours:02d}:{end_minutes:02d}:{end_seconds:02d},{end_milliseconds:03d}"
        
        # Format subtitle text with match indication
        if include_speakers and speaker != 'Unknown':
            if is_match:
                subtitle_text = f"{speaker} [MATCH]: {text}"
            else:
                subtitle_text = f"{speaker}: {text}"
        else:
            if is_match:
                subtitle_text = f"[MATCH]: {text}"
            else:
                subtitle_text = text
        
        # Create SRT entry
        srt_entry = f"{subtitle_number}\n{start_timestamp} --> {end_timestamp}\n{subtitle_text}\n"
        srt_content.append(srt_entry)
        subtitle_number += 1
    
    return "\n".join(srt_content)

def srt_download_link(content, filename, label):
    """Data URI download link for SRT files.

    st.download_button uses S3 presigned URLs in SiS. Snowsight appends
    ?title=... to those URLs, corrupting the AWS signature. Embedding
    the content as a data URI bypasses S3 entirely.
    """
    b64 = base64.b64encode(content.encode('utf-8')).decode()
    href = (
        f'<a href="data:text/plain;charset=utf-8;base64,{b64}" '
        f'download="{filename}" '
        f'style="display:inline-block;padding:5px 10px;background:#f0f2f6;'
        f'color:#31333F;border:1px solid #d1d5db;border-radius:4px;'
        f'text-decoration:none;font-size:14px;">{label}</a>'
    )
    st.markdown(href, unsafe_allow_html=True)


####################################
# PIPELINE STATUS
####################################
#
# These are the only functions in this file that reach outside TRANSCRIPTION_RESULTS.
# They must NOT be cached: the whole point is a live view. (This file has no
# @st.cache_data anywhere, so there is nothing to clear.)

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
            FROM {V_RUN_STATUS}
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
            FROM TABLE({_DB}.INFORMATION_SCHEMA.TASK_HISTORY(
                TASK_NAME => '{TASK_NAME}',
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
            session.sql(f"ALTER STAGE {STAGE_AV} REFRESH").collect()
        return session.sql(f"""
            SELECT d.RELATIVE_PATH AS FILE_NAME,
                   ROUND(d.SIZE / 1048576.0, 1) AS SIZE_MB,
                   TO_CHAR(d.LAST_MODIFIED, 'YYYY-MM-DD HH24:MI') AS LAST_MODIFIED
            FROM DIRECTORY(@{STAGE_AV}) d
            LEFT JOIN {T_RESULTS} t ON d.RELATIVE_PATH = t.FILE_NAME
            WHERE t.FILE_NAME IS NULL
            ORDER BY d.LAST_MODIFIED DESC
        """).to_pandas()
    except Exception as e:
        st.warning(f"Could not read stage backlog: {e}")
        return pd.DataFrame()


# Visual treatment per state. Follows the existing house idiom in this file: a left
# accent bar plus tinted background, as used for match/context highlighting.
STATE_STYLE = {
    'RUNNING':                  ('#1f77b4', '#eaf2fb', 'RUNNING',           'Transcription in progress'),
    'FINISHING':                ('#1f77b4', '#eaf2fb', 'FINISHING',         'Work committed, container winding down'),
    'CELLS_COMPLETE':           ('#4CAF50', '#eaf7ea', 'COMPLETE',          'All notebook cells finished'),
    'SUCCEEDED':                ('#4CAF50', '#eaf7ea', 'SUCCEEDED',         'Run completed cleanly'),
    'WORK_COMPLETE_NOT_EXITED': ('#FF9800', '#fff4e5', 'HUNG (work saved)', 'Transcripts were written but the container has not exited. Known snowbook shutdown hang - the data is safe.'),
    'STALLED':                  ('#FF5722', '#ffece7', 'STALLED',           'No heartbeat for over 10 minutes'),
    'FAILED':                   ('#9E9E9E', '#f5f5f5', 'FAILED',            'Run reported a failure'),
}


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
        accent, bg, label, blurb = '#9E9E9E', '#f5f5f5', 'IDLE', 'No pipeline runs recorded yet'
        state = 'IDLE'
    else:
        state = run.get('DERIVED_STATE') or 'RUNNING'
        accent, bg, label, blurb = STATE_STYLE.get(state, ('#9E9E9E', '#f5f5f5', state, ''))

        # Cross-check the task. A CELLS_COMPLETE run whose task is still EXECUTING means
        # the container has not exited - that is the hang, and only TASK_HISTORY knows.
        if state == 'CELLS_COMPLETE':
            task = get_task_state(session)
            if task and task.get('STATE') == 'EXECUTING':
                accent, bg, label = '#FF9800', '#fff4e5', 'HUNG (work saved)'
                blurb = (f"All cells finished but the task is still EXECUTING after "
                         f"{task.get('ELAPSED_SEC')}s. Known snowbook shutdown hang - "
                         f"transcripts are already saved.")
                state = 'WORK_COMPLETE_NOT_EXITED'

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

    st.markdown(
        f"""<div style="background-color:{bg}; padding:1rem; border-radius:0.5rem;
                    border-left:0.25rem solid {accent}; margin:0.5rem 0;">
            <div style="font-weight:bold; color:{accent}; font-size:1.05rem;">&#9679; {label}</div>
            <div style="color:#555; font-size:0.85rem; margin-bottom:0.4rem;">{blurb}</div>
            {''.join(f"<div style='color:#333; line-height:1.5;'>{d}</div>" for d in detail_lines)}
        </div>""",
        unsafe_allow_html=True)

    return run, backlog, n_backlog, state


def main():
    st.title("🎵 Audio/Video Transcription Dashboard")
    st.markdown("Explore and analyze your transcribed audio and video files")
    
    # Initialize session
    session = get_snowflake_connection()
    
    if session is None:
        st.stop()
    
    # Sidebar controls
    st.sidebar.title("📊 Dashboard Controls")
    
    # Load data controls
    with st.sidebar:
        st.subheader("Data Loading")
        data_limit = st.selectbox("Number of records to load:", [100, 500, 1000, 2000], index=2)
        if st.button("🔄 Refresh Data"):
            # Clear any cached data that depends on the session
            st.cache_data.clear()
            st.rerun() if hasattr(st, 'rerun') else st.experimental_rerun()

        st.divider()
        debug_mode = st.checkbox("Debug Mode", value=False, help="Show DataFrame dtypes, sample values, and full error tracebacks")

        st.divider()
        st.subheader("Pipeline Status")
        # Default OFF deliberately. Each poll runs a status query and a backlog query, so
        # leaving this on idles the warehouse awake for no benefit. Turn it on while
        # watching a run.
        auto_refresh = st.checkbox(
            "Auto-refresh (5s)", value=False,
            help="Polls run status every 5 seconds. Only useful while a transcription is "
                 "running; costs a warehouse query per poll.")
        deep_refresh = st.button(
            "🔃 Rescan stage",
            help="Runs ALTER STAGE REFRESH so newly uploaded files become visible to the "
                 "backlog count. Not done on every poll because it walks the whole stage.")

    # ---- Pipeline status and controls ---------------------------------------------
    # Rendered ABOVE the empty-data guard on purpose: this block is most useful exactly
    # when TRANSCRIPTION_RESULTS is empty (a fresh deployment, or watching the first run).
    #
    # Wrapped in st.fragment when available so auto-refresh re-runs ONLY this block.
    # That matters for cost: this file has no @st.cache_data, so all 10 dashboard queries
    # re-execute on every full rerun, and an unscoped 5-second loop would hammer the
    # warehouse. st.fragment needs Streamlit >= 1.37; warehouse runtimes pin an older
    # version on some accounts, so fall back to a static render rather than crashing.
    st.subheader("⚙️ Pipeline Status")

    def _status_block():
        render_status_panel(session, refresh_stage=deep_refresh)

    if auto_refresh and hasattr(st, 'fragment'):
        st.fragment(run_every="5s")(_status_block)()
    else:
        _status_block()
        if auto_refresh:
            st.caption("Auto-refresh needs Streamlit 1.37+ (st.fragment). "
                       "Use 🔃 Rescan stage or reload to update.")

    st.divider()

    # Load main dataset
    df = load_transcription_data(session, data_limit)

    # TRANSCRIPTION_TIMESTAMP was cast to VARCHAR in SQL to avoid PyArrow timezone
    # serialization issues. Re-parse here as timezone-naive datetime64[ns].
    if not df.empty and 'TRANSCRIPTION_TIMESTAMP' in df.columns:
        df['TRANSCRIPTION_TIMESTAMP'] = pd.to_datetime(df['TRANSCRIPTION_TIMESTAMP'])

    # Debug panel — shown immediately after load so types are visible even if
    # a later rendering call crashes before any output reaches the screen
    if debug_mode and not df.empty:
        with st.expander("Debug Info", expanded=True):
            st.write(f"**Shape:** {df.shape[0]} rows × {df.shape[1]} columns")

            dtype_df = pd.DataFrame({
                "Column": df.dtypes.index,
                "dtype": df.dtypes.astype(str).values,
                "nulls": df.isnull().sum().values,
                "sample": [repr(df[c].iloc[0])[:120] if not df.empty else "" for c in df.columns],
            })
            st.dataframe(dtype_df, use_container_width=True)

            st.write("**Full first-row values** (to spot unexpected Python types):")
            for col in df.columns:
                try:
                    val = df[col].iloc[0]
                    st.write(f"`{col}` `({type(val).__name__})` → `{repr(val)[:200]}`")
                except Exception as col_err:
                    st.error(f"`{col}`: ERROR reading — {col_err}")

    if df.empty:
        st.markdown("""
        <div class="info-box">
            <h4>⚠️ No transcription data found</h4>
            <p>Nothing in TRANSCRIPTION_RESULTS yet. Upload media and start a run using the
            controls above &mdash; the data tabs will appear once a transcription completes.</p>
        </div>
        """, unsafe_allow_html=True)
        # `return`, NOT st.stop(). Both stop the data tabs from rendering, but the status
        # panel and controls above have already been drawn by this point, which is the
        # whole reason they were moved above this guard. st.stop() here would be
        # equivalent, but return makes the intent explicit and cannot be mistaken for the
        # old behaviour of aborting before the panel existed.
        #
        # Do NOT "fix" this by guarding each tab with st.stop() instead: st.stop() halts
        # the ENTIRE script, so a guard inside `with tab1:` silently prevents tabs 2-5
        # from rendering at all.
        return
    
    # Get summary stats
    stats = get_summary_stats(session)
    
    # Main dashboard tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs(["📈 Overview", "🔍 Search", "👥 Speaker View", "📊 Analytics", "📋 Browse Data"])
    
    with tab1:
        st.header("Overview")
        
        # Key metrics
        col1, col2, col3, col4, col5, col6 = st.columns(6)
        
        with col1:
            st.metric("Total Files", f"{stats.get('total_files', 0):,}")
        
        with col2:
            hours = stats.get('total_duration', 0)
            st.metric("Total Audio Hours", f"{hours:.1f}")
        
        with col3:
            st.metric("Accounts", f"{stats.get('account_count', 0)}")
            
        with col4:
            st.metric("Languages Detected", f"{stats.get('languages', 0)}")
        
        with col5:
            st.metric("Files with Speakers", f"{stats.get('files_with_speakers', 0)}")
        
        with col6:
            avg_speakers = stats.get('avg_speakers', 0)
            st.metric("Avg Speakers", f"{avg_speakers:.1f}")
        
        st.divider()
        
        # Top accounts chart + file types side by side
        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Meetings by Account")
            if not df.empty:
                account_counts = df[df['ACCOUNT_NAME'].notna()]['ACCOUNT_NAME'].value_counts().head(10)
                if not account_counts.empty:
                    try:
                        st.bar_chart(account_counts)
                    except Exception as _chart_e:
                        st.error(f"[bar_chart: Meetings by Account] {type(_chart_e).__name__}: {_chart_e}")
                        st.code(traceback.format_exc())
                else:
                    st.info("No account data available yet")

        with col2:
            st.subheader("File Types")
            if not df.empty:
                try:
                    st.bar_chart(df['FILE_TYPE'].value_counts())
                except Exception as _chart_e:
                    st.error(f"[bar_chart: File Types] {type(_chart_e).__name__}: {_chart_e}")
                    st.code(traceback.format_exc())

        st.divider()

        # Charts using Streamlit native charting
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.subheader("Processing Timeline")
            if not df.empty:
                # Create timeline chart
                df['DATE'] = pd.to_datetime(df['TRANSCRIPTION_TIMESTAMP']).dt.normalize()
                timeline_data = df.groupby('DATE').size().reset_index(name='Files Processed')
                try:
                    st.line_chart(timeline_data.set_index('DATE')['Files Processed'])
                except Exception as _chart_e:
                    st.error(f"[line_chart: Processing Timeline] {type(_chart_e).__name__}: {_chart_e}")
                    st.code(traceback.format_exc())
        
        # Language distribution
        st.subheader("Language Distribution")
        if not df.empty:
            lang_counts = df['DETECTED_LANGUAGE'].value_counts().head(10)
            try:
                st.bar_chart(lang_counts)
            except Exception as _chart_e:
                st.error(f"[bar_chart: Language Distribution] {type(_chart_e).__name__}: {_chart_e}")
                st.code(traceback.format_exc())
        
        # Recent files table
        st.subheader("Recent Transcriptions")
        if not df.empty:
            recent_df = df.head(5).copy()
            recent_df['DISPLAY_TITLE'] = recent_df['MEETING_TITLE'].where(recent_df['MEETING_TITLE'].notna(), recent_df['FILE_NAME'])
            try:
                st.dataframe(
                    recent_df[['DISPLAY_TITLE', 'ACCOUNT_NAME', 'CALL_START_TS', 'DETECTED_LANGUAGE', 'SPEAKER_COUNT']].rename(columns={
                        'DISPLAY_TITLE': 'Meeting',
                        'ACCOUNT_NAME': 'Account',
                        'CALL_START_TS': 'Date',
                        'DETECTED_LANGUAGE': 'Language',
                        'SPEAKER_COUNT': 'Speakers',
                    }),
                    use_container_width=True
                )
            except Exception as _chart_e:
                st.error(f"[st.dataframe: Recent Transcriptions] {type(_chart_e).__name__}: {_chart_e}")
                st.code(traceback.format_exc())
    
    with tab2:
        st.header("🔍 Search Transcriptions")
        
        # Search controls
        col1, col2, col3, col4 = st.columns([2, 1, 1, 1])
        
        with col1:
            search_term = st.text_input("Search in transcripts:", placeholder="Enter keywords to search...")
        
        with col2:
            file_types = ["All"] + sorted(df['FILE_TYPE'].unique().tolist())
            selected_file_type = st.selectbox("File Type:", file_types)
        
        with col3:
            languages = ["All"] + sorted(df['DETECTED_LANGUAGE'].dropna().unique().tolist())
            selected_language = st.selectbox("Language:", languages)

        with col4:
            accounts = ["All"] + sorted(df['ACCOUNT_NAME'].dropna().unique().tolist())
            selected_account = st.selectbox("Account:", accounts)
        
        # Additional search options
        col1, col2, col3 = st.columns(3)
        with col1:
            start_date = st.date_input("From date:", value=datetime.now().date() - timedelta(days=30))
        with col2:
            end_date = st.date_input("To date:", value=datetime.now().date())
        with col3:
            context_size = st.number_input(
                "Context messages:", 
                min_value=0, 
                max_value=50, 
                value=10,
                help="Number of messages before and after each match to show for context"
            )
        
        # Search options
        col1, col2 = st.columns([1, 3])
        with col1:
            show_speaker_view = st.checkbox("Show speaker segments", value=True, help="Display results with speaker-by-speaker breakdown")
        with col2:
            st.markdown("") # Spacer
        
        # Search execution
        if st.button("🔍 Search", type="primary") and search_term:
            with st.spinner("Searching transcriptions..."):
                search_results = search_transcriptions(
                    session, search_term, 
                    selected_file_type if selected_file_type != "All" else None,
                    selected_language if selected_language != "All" else None,
                    (start_date, end_date),
                    selected_account if selected_account != "All" else None
                )
            
            st.subheader(f"Search Results ({len(search_results)} found)")
            
            if not search_results.empty:
                for idx, row in search_results.iterrows():
                    display_title = row.get('MEETING_TITLE') if pd.notna(row.get('MEETING_TITLE')) else row['FILE_NAME']
                    account_str = f" · {row['ACCOUNT_NAME']}" if pd.notna(row.get('ACCOUNT_NAME')) else ""
                    date_str = str(row['CALL_START_TS'])[:10] if pd.notna(row.get('CALL_START_TS')) else str(row['TRANSCRIPTION_TIMESTAMP'])[:10]
                    with st.container():
                        st.markdown(f"""
                        <div class="search-result">
                            <h4>📄 {display_title}{account_str}</h4>
                            <p><strong>Date:</strong> {date_str} | 
                               <strong>Type:</strong> {row['FILE_TYPE']} | 
                               <strong>Language:</strong> {row['DETECTED_LANGUAGE']} | 
                               <strong>Speakers:</strong> {row.get('SPEAKER_COUNT', 'N/A')} |
                               <strong>Duration:</strong> {row['AUDIO_DURATION_SECONDS']:.1f}s</p>
                        </div>
                        """, unsafe_allow_html=True)
                        
                        if show_speaker_view and row.get('TRANSCRIPT_WITH_SPEAKERS') is not None:
                            # Show speaker-based results with context
                            try:
                                # Parse speaker data
                                transcript_data = row['TRANSCRIPT_WITH_SPEAKERS']
                                if isinstance(transcript_data, str):
                                    transcript_data = json.loads(transcript_data)
                                
                                speaker_segments = transcript_data.get('speakers', [])
                                
                                if speaker_segments:
                                    # Find matching segments with context
                                    context_segments = find_matching_segments_with_context(
                                        speaker_segments, search_term, context_size
                                    )
                                    
                                    if context_segments:
                                        file_info = {
                                            'filename': row['FILE_NAME'],
                                            'language': row['DETECTED_LANGUAGE'],
                                            'duration': row['AUDIO_DURATION_SECONDS'],
                                            'speaker_count': row.get('SPEAKER_COUNT', 'N/A')
                                        }
                                        
                                        # Create export button for search results
                                        col1, col2 = st.columns([3, 1])
                                        
                                        with col1:
                                            expander_label = "🎯 View Matches with Context"
                                        
                                        with col2:
                                            # Create CSV for search results
                                            search_csv_df = convert_search_results_to_csv(context_segments, file_info, search_term)
                                            search_srt_content_with_speakers = convert_search_results_to_srt(context_segments, file_info, search_term, include_speakers=True)
                                            search_srt_content_no_speakers = convert_search_results_to_srt(context_segments, file_info, search_term, include_speakers=False)
                                            
                                            # Clean filename for downloads
                                            clean_filename = re.sub(r'[^\w\-_\.]', '_', row['FILE_NAME'])
                                            clean_search_term = re.sub(r'[^\w\-_]', '_', search_term)
                                            
                                            # Export buttons in mini columns
                                            btn_col1, btn_col2, btn_col3 = st.columns(3)
                                            
                                            with btn_col1:
                                                if search_csv_df is not None:
                                                    search_csv_string = create_csv_download(search_csv_df, row['FILE_NAME'])
                                                    search_csv_filename = f"search_{clean_search_term}_{clean_filename}.csv"
                                                    
                                                    st.download_button(
                                                        label="📊 CSV",
                                                        data=search_csv_string,
                                                        file_name=search_csv_filename,
                                                        mime="text/csv",
                                                        help="Export as CSV",
                                                        key=f"csv_{idx}_{row['FILE_NAME']}"
                                                    )
                                            
                                            with btn_col2:
                                                if search_srt_content_with_speakers:
                                                    search_srt_filename = row['FILE_NAME'].rsplit('.', 1)[0] + '.srt'
                                                    srt_download_link(search_srt_content_with_speakers, search_srt_filename, "🎬 SRT (w/ Speakers)")
                                            
                                            with btn_col3:
                                                if search_srt_content_no_speakers:
                                                    search_srt_filename_no_speakers = row['FILE_NAME'].rsplit('.', 1)[0] + '_no_speakers.srt'
                                                    srt_download_link(search_srt_content_no_speakers, search_srt_filename_no_speakers, "🎬 SRT (no Speakers)")
                                        
                                        with st.expander(expander_label, expanded=True):
                                            display_search_result_with_speakers(context_segments, search_term, file_info)
                                    else:
                                        # Fallback to regular transcript if no speaker matches found
                                        transcript = str(row['TRANSCRIPT'])[:500] + "..." if len(str(row['TRANSCRIPT'])) > 500 else str(row['TRANSCRIPT'])
                                        highlighted_transcript = highlight_text(transcript, search_term)
                                        
                                        with st.expander("View Transcript"):
                                            st.markdown(highlighted_transcript)
                                else:
                                    # No speaker segments available
                                    transcript = str(row['TRANSCRIPT'])[:500] + "..." if len(str(row['TRANSCRIPT'])) > 500 else str(row['TRANSCRIPT'])
                                    highlighted_transcript = highlight_text(transcript, search_term)
                                    
                                    with st.expander("View Transcript"):
                                        st.markdown(highlighted_transcript)
                                        
                            except Exception as e:
                                st.error(f"Error processing speaker data: {e}")
                                # Fallback to regular transcript
                                transcript = str(row['TRANSCRIPT'])[:500] + "..." if len(str(row['TRANSCRIPT'])) > 500 else str(row['TRANSCRIPT'])
                                highlighted_transcript = highlight_text(transcript, search_term)
                                
                                with st.expander("View Transcript"):
                                    st.markdown(highlighted_transcript)
                        else:
                            # Show regular transcript view
                            transcript = str(row['TRANSCRIPT'])[:500] + "..." if len(str(row['TRANSCRIPT'])) > 500 else str(row['TRANSCRIPT'])
                            highlighted_transcript = highlight_text(transcript, search_term)
                            
                            with st.expander("View Transcript"):
                                st.markdown(highlighted_transcript)
                        
                        st.divider()
            else:
                st.info("No results found for your search criteria.")
        elif search_term == "":
            st.warning("Please enter a search term.")
    
    with tab3:
        st.header("👥 Speaker-by-Speaker Transcripts")
        
        # File selection — show meeting title when available, fall back to file name
        files_with_speakers = df[df['SPEAKER_COUNT'] > 0].copy()
        
        if files_with_speakers.empty:
            st.markdown("""
            <div class="info-box">
                <h4>ℹ️ No files with speaker data found</h4>
                <p>Speaker diarization data is not available for the current dataset. 
                Files will still show structured segments based on timing.</p>
            </div>
            """, unsafe_allow_html=True)
            files_with_speakers = df.head(10).copy()

        file_names = files_with_speakers['FILE_NAME'].tolist()

        selected_file = st.selectbox(
            "Select a meeting to view:",
            options=file_names,
            index=0 if file_names else None
        )
        
        if selected_file:
            # Get file metadata
            file_row = df[df['FILE_NAME'] == selected_file].iloc[0]
            
            # Display file info and export controls
            col1, col2, col3, col4, col5, col6 = st.columns([1, 1, 1, 1, 1, 2])
            with col1:
                st.metric("File Type", file_row['FILE_TYPE'])
            with col2:
                st.metric("Language", file_row['DETECTED_LANGUAGE'])
            with col3:
                duration = file_row['AUDIO_DURATION_SECONDS']
                st.metric("Duration", f"{duration:.1f}s" if pd.notna(duration) else "N/A")
            with col4:
                speakers = file_row.get('SPEAKER_COUNT', 0)
                st.metric("Speakers", f"{speakers}" if (speakers or 0) > 0 else "N/A")
            with col5:
                account_val = file_row.get('ACCOUNT_NAME')
                st.metric("Account", account_val if pd.notna(account_val) else "—")
            with col6:
                # Export button
                st.markdown("**📥 Export Options:**")
                
                # Clean filename for download
                clean_filename = re.sub(r'[^\w\-_\.]', '_', selected_file)
                
                # Get pre-computed content from database
                srt_content = file_row.get('SRT_CONTENT')
                srt_with_speakers = file_row.get('SRT_WITH_SPEAKERS')
                summary_markdown = file_row.get('SUMMARY_MARKDOWN')
                
                # Export buttons in columns - now with 4 columns for markdown
                export_col1, export_col2, export_col3, export_col4 = st.columns(4)
                
                with export_col1:
                    # Use pre-computed SRT without speakers
                    if srt_content and pd.notna(srt_content):
                        srt_filename = selected_file.rsplit('.', 1)[0] + '.srt'
                        srt_download_link(srt_content, srt_filename, "📥 SRT")
                    else:
                        st.caption("SRT N/A")
                
                with export_col2:
                    # Use pre-computed SRT with speakers
                    if srt_with_speakers and pd.notna(srt_with_speakers):
                        srt_filename_speakers = selected_file.rsplit('.', 1)[0] + '_speakers.srt'
                        srt_download_link(srt_with_speakers, srt_filename_speakers, "📥 SRT+")
                    else:
                        st.caption("SRT+ N/A")
                
                with export_col3:
                    # Markdown summary download
                    if summary_markdown and pd.notna(summary_markdown):
                        md_filename = f"summary_{clean_filename}.md"
                        st.download_button(
                            label="📥 Summary",
                            data=summary_markdown,
                            file_name=md_filename,
                            mime="text/markdown",
                            help="Download AI-generated summary",
                            key=f"summary_{clean_filename}"
                        )
                    else:
                        st.caption("Summary N/A")
                
                with export_col4:
                    # CSV export (still dynamically generated from speaker segments)
                    speaker_segments = get_speaker_segments(session, selected_file)
                    if speaker_segments:
                        file_info = {
                            'filename': selected_file,
                            'duration': file_row['AUDIO_DURATION_SECONDS'],
                            'language': file_row['DETECTED_LANGUAGE']
                        }
                        csv_df = convert_speaker_segments_to_csv(speaker_segments, file_info)
                        if csv_df is not None:
                            csv_string = create_csv_download(csv_df, selected_file)
                            download_filename = f"transcript_{clean_filename}.csv"
                            st.download_button(
                                label="📥 CSV",
                                data=csv_string,
                                file_name=download_filename,
                                mime="text/csv",
                                help="Download as CSV spreadsheet",
                                key=f"csv_{clean_filename}"
                            )
                        else:
                            st.caption("CSV N/A")
                    else:
                        st.caption("CSV N/A")
            
            # Summary / structured insights section
            has_summary = summary_markdown and pd.notna(summary_markdown)
            key_points = file_row.get('KEY_POINTS')
            next_steps = file_row.get('NEXT_STEPS')
            decisions = file_row.get('DECISIONS_MADE')
            questions = file_row.get('QUESTIONS_RAISED')
            has_structured = any(pd.notna(v) and v for v in [key_points, next_steps, decisions, questions])

            if has_summary or has_structured:
                with st.expander("📋 Meeting Summary & Insights", expanded=False):
                    if has_structured:
                        scol1, scol2 = st.columns(2)
                        with scol1:
                            if pd.notna(key_points) and key_points:
                                st.markdown("**Key Points**")
                                st.markdown(key_points if isinstance(key_points, str) else str(key_points))
                            if pd.notna(decisions) and decisions:
                                st.markdown("**Decisions Made**")
                                st.markdown(decisions if isinstance(decisions, str) else str(decisions))
                        with scol2:
                            if pd.notna(next_steps) and next_steps:
                                st.markdown("**Next Steps**")
                                st.markdown(next_steps if isinstance(next_steps, str) else str(next_steps))
                            if pd.notna(questions) and questions:
                                st.markdown("**Questions Raised**")
                                st.markdown(questions if isinstance(questions, str) else str(questions))
                        if has_summary:
                            st.divider()
                    if has_summary:
                        st.markdown("**Full Summary**")
                        st.markdown(summary_markdown)
            
            st.divider()
            
            # Load and display speaker segments
            speaker_segments = get_speaker_segments(session, selected_file)
            
            if speaker_segments:
                st.subheader("📝 Transcript with Speaker Segments")
                
                # Get file info from first segment if available
                file_info = None
                if speaker_segments and isinstance(speaker_segments, list) and len(speaker_segments) > 0:
                    # If we have speaker data, the file_info might be in the original JSON
                    # For now, we'll create it from our DataFrame
                    file_info = {
                        'filename': selected_file,
                        'duration': file_row['AUDIO_DURATION_SECONDS'],
                        'language': file_row['DETECTED_LANGUAGE']
                    }
                
                display_speaker_transcript(speaker_segments, file_info)
                
            else:
                # Fallback: show basic transcript
                st.subheader("📝 Basic Transcript")
                st.info("Speaker segments not available. Showing full transcript:")
                
                transcript = file_row['TRANSCRIPT']
                st.markdown(f"""
                <div class="transcript-box">
                    <p>{transcript}</p>
                </div>
                """, unsafe_allow_html=True)
    
    with tab4:
        st.header("📊 Analytics")
        
        if not df.empty:
            # Processing performance analysis
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Processing Time vs Duration")
                valid_data = df.dropna(subset=['PROCESSING_TIME_SECONDS', 'AUDIO_DURATION_SECONDS'])
                if not valid_data.empty:
                    chart_data = valid_data[['AUDIO_DURATION_SECONDS', 'PROCESSING_TIME_SECONDS']]
                    if hasattr(st, 'scatter_chart'):
                        st.scatter_chart(chart_data.set_index('AUDIO_DURATION_SECONDS'))
                    else:
                        st.line_chart(chart_data.set_index('AUDIO_DURATION_SECONDS'))
            
            with col2:
                st.subheader("File Size Distribution")
                df['FILE_SIZE_MB'] = df['FILE_SIZE_BYTES'] / (1024 * 1024)
                st.bar_chart(df['FILE_SIZE_MB'].value_counts().head(20))
            
            # Speaker analysis
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Speaker Count Distribution")
                speaker_counts = df[df['SPEAKER_COUNT'] > 0]['SPEAKER_COUNT'].value_counts().sort_index()
                if not speaker_counts.empty:
                    st.bar_chart(speaker_counts)
                else:
                    st.info("No speaker data available")
            
            with col2:
                st.subheader("Files with Speaker Data by Language")
                speaker_by_lang = df[df['SPEAKER_COUNT'] > 0].groupby('DETECTED_LANGUAGE').size()
                if not speaker_by_lang.empty:
                    st.bar_chart(speaker_by_lang)
                else:
                    st.info("No speaker data available")
            
            # Processing efficiency by file type
            st.subheader("Processing Efficiency by File Type")
            efficiency_data = df.groupby('FILE_TYPE').agg({
                'PROCESSING_TIME_SECONDS': 'mean',
                'AUDIO_DURATION_SECONDS': 'mean'
            }).reset_index()
            
            efficiency_data['PROCESSING_RATIO'] = (
                efficiency_data['PROCESSING_TIME_SECONDS'] / 
                efficiency_data['AUDIO_DURATION_SECONDS']
            )
            
            st.bar_chart(efficiency_data.set_index('FILE_TYPE')['PROCESSING_RATIO'])
            st.info("Lower ratios indicate better efficiency (faster than real-time processing)")
            
            # Word count analysis
            st.subheader("Transcript Length Analysis")
            df['WORD_COUNT'] = df['TRANSCRIPT'].astype(str).str.split().str.len()
            
            col1, col2 = st.columns(2)
            
            with col1:
                st.bar_chart(df['WORD_COUNT'].value_counts().head(20))
            
            with col2:
                avg_words_by_lang = df.groupby('DETECTED_LANGUAGE')['WORD_COUNT'].mean().sort_values(ascending=False).head(10)
                st.bar_chart(avg_words_by_lang)
    
    with tab5:
        st.header("📋 Browse All Data")
        
        # Filters
        col1, col2, col3, col4 = st.columns(4)
        
        with col1:
            filter_file_type = st.selectbox("Filter by File Type:", ["All"] + sorted(df['FILE_TYPE'].unique()))
        
        with col2:
            filter_language = st.selectbox("Filter by Language:", ["All"] + sorted(df['DETECTED_LANGUAGE'].dropna().unique()))

        with col3:
            filter_accounts = ["All"] + sorted(df['ACCOUNT_NAME'].dropna().unique().tolist())
            filter_account = st.selectbox("Filter by Account:", filter_accounts)
        
        with col4:
            sort_by = st.selectbox("Sort by:", ["TRANSCRIPTION_TIMESTAMP", "FILE_NAME", "SPEAKER_COUNT", "PROCESSING_TIME_SECONDS", "AUDIO_DURATION_SECONDS"])
        
        # Apply filters
        filtered_df = df.copy()
        
        if filter_file_type != "All":
            filtered_df = filtered_df[filtered_df['FILE_TYPE'] == filter_file_type]
        
        if filter_language != "All":
            filtered_df = filtered_df[filtered_df['DETECTED_LANGUAGE'] == filter_language]

        if filter_account != "All":
            filtered_df = filtered_df[filtered_df['ACCOUNT_NAME'] == filter_account]
        
        # Sort
        filtered_df = filtered_df.sort_values(sort_by, ascending=False)
        
        st.subheader(f"Showing {len(filtered_df)} records")
        
        # Display data with expandable transcripts
        for idx, row in filtered_df.head(20).iterrows():  # Limit to 20 for performance
            with st.container():
                col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
                
                with col1:
                    display_title = row['MEETING_TITLE'] if pd.notna(row.get('MEETING_TITLE')) else row['FILE_NAME']
                    account_str = f"  ·  {row['ACCOUNT_NAME']}" if pd.notna(row.get('ACCOUNT_NAME')) else ""
                    date_str = f"  ·  {str(row['CALL_START_TS'])[:10]}" if pd.notna(row.get('CALL_START_TS')) else ""
                    st.markdown(f"**{display_title}**{account_str}{date_str}")
                
                with col2:
                    st.text(f"{row['FILE_TYPE']}")
                
                with col3:
                    st.text(f"{row['DETECTED_LANGUAGE']}")
                
                with col4:
                    speakers = row.get('SPEAKER_COUNT', 0)
                    st.text(f"{speakers}" if (speakers or 0) > 0 else "N/A")
                
                with col5:
                    duration = row['AUDIO_DURATION_SECONDS']
                    st.text(f"{duration:.1f}s" if pd.notna(duration) else "N/A")
                
                # Transcript preview
                transcript = str(row['TRANSCRIPT'])
                transcript_preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
                st.text(transcript_preview)
                
                # Full transcript in expander
                with st.expander("View Full Transcript"):
                    if (row.get('SPEAKER_COUNT') or 0) > 0:
                        # Show speaker segments if available
                        speaker_segments = get_speaker_segments(session, row['FILE_NAME'])
                        if speaker_segments:
                            st.markdown("**Speaker-separated transcript:**")
                            display_speaker_transcript(speaker_segments)
                        else:
                            # Fallback to regular transcript
                            st.markdown(f"""
                            <div class="transcript-box">
                                <p>{transcript}</p>
                                <hr>
                                <small>
                                Processing time: {row['PROCESSING_TIME_SECONDS']:.2f}s | 
                                Timestamp: {row['TRANSCRIPTION_TIMESTAMP']}
                                </small>
                            </div>
                            """, unsafe_allow_html=True)
                    else:
                        # Regular transcript
                        st.markdown(f"""
                        <div class="transcript-box">
                            <p>{transcript}</p>
                            <hr>
                            <small>
                            Processing time: {row['PROCESSING_TIME_SECONDS']:.2f}s | 
                            Timestamp: {row['TRANSCRIPTION_TIMESTAMP']}
                            </small>
                        </div>
                        """, unsafe_allow_html=True)
                
                st.divider()
        
        if len(filtered_df) > 20:
            st.info(f"Showing first 20 records. Total matching records: {len(filtered_df)}")
    
    # Footer
    st.markdown("---")
    st.markdown("Built with ❤️ using Streamlit in Snowflake")

try:
    main()
except Exception as _e:
    st.error(f"**Fatal app error — `{type(_e).__name__}: {_e}`**")
    st.code(traceback.format_exc(), language="python")
    st.warning(
        "Enable **Debug Mode** in the sidebar for column-level dtype details "
        "that help pinpoint PyArrow serialization failures."
    )