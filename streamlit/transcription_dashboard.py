import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
from snowflake.snowpark.context import get_active_session
import re
import json
import io

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
        TRANSCRIPTION_TIMESTAMP
    FROM TRANSCRIPTION_RESULTS 
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
        result = session.sql("SELECT COUNT(*) as count FROM TRANSCRIPTION_RESULTS").to_pandas()
        stats['total_files'] = result.iloc[0, 0] if not result.empty else 0
        
        # Total duration in hours
        result = session.sql("SELECT SUM(AUDIO_DURATION_SECONDS)/3600 as hours FROM TRANSCRIPTION_RESULTS WHERE AUDIO_DURATION_SECONDS IS NOT NULL").to_pandas()
        stats['total_duration'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0
        
        # Average processing time
        result = session.sql("SELECT AVG(PROCESSING_TIME_SECONDS) as avg_time FROM TRANSCRIPTION_RESULTS").to_pandas()
        stats['avg_processing_time'] = result.iloc[0, 0] if not result.empty else 0
        
        # Number of languages
        result = session.sql("SELECT COUNT(DISTINCT DETECTED_LANGUAGE) as count FROM TRANSCRIPTION_RESULTS").to_pandas()
        stats['languages'] = result.iloc[0, 0] if not result.empty else 0
        
        # Files with speaker data
        result = session.sql("SELECT COUNT(*) as count FROM TRANSCRIPTION_RESULTS WHERE TRANSCRIPT_WITH_SPEAKERS IS NOT NULL").to_pandas()
        stats['files_with_speakers'] = result.iloc[0, 0] if not result.empty else 0
        
        # Average speakers per file
        result = session.sql("SELECT AVG(SPEAKER_COUNT) as avg_speakers FROM TRANSCRIPTION_RESULTS WHERE SPEAKER_COUNT > 0").to_pandas()
        stats['avg_speakers'] = result.iloc[0, 0] if not result.empty and result.iloc[0, 0] is not None else 0
        
    except Exception as e:
        st.warning(f"Error getting statistics: {str(e)}")
        stats = {'total_files': 0, 'total_duration': 0, 'avg_processing_time': 0, 'languages': 0, 'files_with_speakers': 0, 'avg_speakers': 0}
    
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
    FROM TRANSCRIPTION_RESULTS 
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

def search_transcriptions(session, search_term, file_type=None, language=None, date_range=None):
    """Search transcriptions"""
    if session is None:
        return pd.DataFrame()
    
    where_conditions = [f"TRANSCRIPT ILIKE '%{search_term}%'"]
    
    if file_type and file_type != "All":
        where_conditions.append(f"FILE_TYPE = '{file_type}'")
    
    if language and language != "All":
        where_conditions.append(f"DETECTED_LANGUAGE = '{language}'")
    
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
        AUDIO_DURATION_SECONDS
    FROM TRANSCRIPTION_RESULTS 
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
            st.rerun()
    
    # Load main dataset
    df = load_transcription_data(session, data_limit)
    
    if df.empty:
        st.markdown("""
        <div class="info-box">
            <h4>⚠️ No transcription data found</h4>
            <p>Make sure you've run the transcription notebook first and have data in the TRANSCRIPTION_RESULTS table.</p>
        </div>
        """, unsafe_allow_html=True)
        st.stop()
    
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
            avg_time = stats.get('avg_processing_time', 0)
            st.metric("Avg Processing Time", f"{avg_time:.1f}s")
            
        with col4:
            st.metric("Languages Detected", f"{stats.get('languages', 0)}")
        
        with col5:
            st.metric("Files with Speakers", f"{stats.get('files_with_speakers', 0)}")
        
        with col6:
            avg_speakers = stats.get('avg_speakers', 0)
            st.metric("Avg Speakers", f"{avg_speakers:.1f}")
        
        st.divider()
        
        # Charts using Streamlit native charting
        col1, col2 = st.columns([2, 1])
        
        with col1:
            st.subheader("Processing Timeline")
            if not df.empty:
                # Create timeline chart
                df['DATE'] = pd.to_datetime(df['TRANSCRIPTION_TIMESTAMP']).dt.date
                timeline_data = df.groupby('DATE').size().reset_index(name='Files Processed')
                
                # Use Streamlit's built-in line chart
                st.line_chart(timeline_data.set_index('DATE')['Files Processed'])
        
        with col2:
            st.subheader("File Types")
            if not df.empty:
                file_type_counts = df['FILE_TYPE'].value_counts()
                # Display as bar chart
                st.bar_chart(file_type_counts)
        
        # Language distribution
        st.subheader("Language Distribution")
        if not df.empty:
            lang_counts = df['DETECTED_LANGUAGE'].value_counts().head(10)
            st.bar_chart(lang_counts)
        
        # Recent files table
        st.subheader("Recent Transcriptions")
        if not df.empty:
            recent_df = df.head(5)[['FILE_NAME', 'FILE_TYPE', 'DETECTED_LANGUAGE', 'SPEAKER_COUNT', 'TRANSCRIPTION_TIMESTAMP']]
            st.dataframe(recent_df, use_container_width=True)
    
    with tab2:
        st.header("🔍 Search Transcriptions")
        
        # Search controls
        col1, col2, col3 = st.columns([2, 1, 1])
        
        with col1:
            search_term = st.text_input("Search in transcripts:", placeholder="Enter keywords to search...")
        
        with col2:
            file_types = ["All"] + sorted(df['FILE_TYPE'].unique().tolist())
            selected_file_type = st.selectbox("File Type:", file_types)
        
        with col3:
            languages = ["All"] + sorted(df['DETECTED_LANGUAGE'].unique().tolist())
            selected_language = st.selectbox("Language:", languages)
        
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
                    (start_date, end_date)
                )
            
            st.subheader(f"Search Results ({len(search_results)} found)")
            
            if not search_results.empty:
                for idx, row in search_results.iterrows():
                    with st.container():
                        st.markdown(f"""
                        <div class="search-result">
                            <h4>📄 {row['FILE_NAME']}</h4>
                            <p><strong>Type:</strong> {row['FILE_TYPE']} | 
                               <strong>Language:</strong> {row['DETECTED_LANGUAGE']} | 
                               <strong>Speakers:</strong> {row.get('SPEAKER_COUNT', 'N/A')} |
                               <strong>Duration:</strong> {row['AUDIO_DURATION_SECONDS']:.1f}s</p>
                            <p><strong>Date:</strong> {row['TRANSCRIPTION_TIMESTAMP']}</p>
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
                                            search_srt_content = convert_search_results_to_srt(context_segments, file_info, search_term)
                                            
                                            # Clean filename for downloads
                                            clean_filename = re.sub(r'[^\w\-_\.]', '_', row['FILE_NAME'])
                                            clean_search_term = re.sub(r'[^\w\-_]', '_', search_term)
                                            
                                            # Export buttons in mini columns
                                            btn_col1, btn_col2 = st.columns(2)
                                            
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
                                                if search_srt_content:
                                                    search_srt_filename = f"search_{clean_search_term}_{clean_filename}.srt"
                                                    
                                                    st.download_button(
                                                        label="🎬 SRT",
                                                        data=search_srt_content,
                                                        file_name=search_srt_filename,
                                                        mime="application/x-subrip",
                                                        help="Export as SRT subtitles",
                                                        key=f"srt_{idx}_{row['FILE_NAME']}"
                                                    )
                                        
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
        
        # File selection
        files_with_speakers = df[df['SPEAKER_COUNT'] > 0]['FILE_NAME'].tolist()
        
        if not files_with_speakers:
            st.markdown("""
            <div class="info-box">
                <h4>ℹ️ No files with speaker data found</h4>
                <p>Speaker diarization data is not available for the current dataset. 
                Files will still show structured segments based on timing.</p>
            </div>
            """, unsafe_allow_html=True)
            files_with_speakers = df['FILE_NAME'].tolist()[:10]  # Show first 10 files as fallback
        
        selected_file = st.selectbox(
            "Select a file to view speaker segments:",
            options=files_with_speakers,
            index=0 if files_with_speakers else None
        )
        
        if selected_file:
            # Get file metadata
            file_row = df[df['FILE_NAME'] == selected_file].iloc[0]
            
            # Display file info and export controls
            col1, col2, col3, col4, col5 = st.columns([1, 1, 1, 1, 2])
            with col1:
                st.metric("File Type", file_row['FILE_TYPE'])
            with col2:
                st.metric("Language", file_row['DETECTED_LANGUAGE'])
            with col3:
                duration = file_row['AUDIO_DURATION_SECONDS']
                st.metric("Duration", f"{duration:.1f}s" if pd.notna(duration) else "N/A")
            with col4:
                speakers = file_row.get('SPEAKER_COUNT', 0)
                st.metric("Speakers", f"{speakers}" if speakers > 0 else "N/A")
            with col5:
                # Export button
                st.markdown("**📥 Export Options:**")
                
                # Load speaker segments for export
                speaker_segments = get_speaker_segments(session, selected_file)
                
                if speaker_segments:
                    file_info = {
                        'filename': selected_file,
                        'duration': file_row['AUDIO_DURATION_SECONDS'],
                        'language': file_row['DETECTED_LANGUAGE']
                    }
                    
                    # Create export data
                    csv_df = convert_speaker_segments_to_csv(speaker_segments, file_info)
                    srt_content = convert_speaker_segments_to_srt(speaker_segments, file_info)
                    
                    # Clean filename for download
                    clean_filename = re.sub(r'[^\w\-_\.]', '_', selected_file)
                    
                    # Export buttons in columns
                    export_col1, export_col2 = st.columns(2)
                    
                    with export_col1:
                        if csv_df is not None:
                            csv_string = create_csv_download(csv_df, selected_file)
                            download_filename = f"transcript_{clean_filename}.csv"
                            
                            st.download_button(
                                label="📥 CSV",
                                data=csv_string,
                                file_name=download_filename,
                                mime="text/csv",
                                help="Download as CSV spreadsheet"
                            )
                        else:
                            st.warning("CSV unavailable")
                    
                    with export_col2:
                        if srt_content:
                            srt_filename = f"transcript_{clean_filename}.srt"
                            
                            st.download_button(
                                label="📥 SRT",
                                data=srt_content,
                                file_name=srt_filename,
                                mime="application/x-subrip",
                                help="Download as SRT subtitle file"
                            )
                        else:
                            st.warning("SRT unavailable")
                    
                    # Export options
                    with st.expander("⚙️ Export Settings"):
                        include_speaker_names = st.checkbox(
                            "Include speaker names in SRT", 
                            value=True, 
                            help="Add speaker identification to subtitle text"
                        )
                        
                        # Regenerate SRT with updated settings if changed
                        if not include_speaker_names:
                            srt_content = convert_speaker_segments_to_srt(speaker_segments, file_info, include_speakers=False)
                            srt_filename = f"transcript_{clean_filename}.srt"
                            
                            st.download_button(
                                label="📥 SRT (No Speakers)",
                                data=srt_content,
                                file_name=srt_filename,
                                mime="application/x-subrip",
                                help="Download SRT without speaker names",
                                key="srt_no_speakers"
                            )
                        
                        st.markdown("**📋 Format Preview:**")
                        if include_speaker_names:
                            st.code("""1
00:00:15,200 --> 00:00:32,100
Speaker_0: Welcome to today's meeting...

2
00:00:33,000 --> 00:00:45,200
Speaker_1: Thank you for having me here...""")
                        else:
                            st.code("""1
00:00:15,200 --> 00:00:32,100
Welcome to today's meeting...

2
00:00:33,000 --> 00:00:45,200
Thank you for having me here...""")
                        
                        # Show CSV preview
                        st.markdown("**📊 CSV Preview:**")
                        st.markdown("Columns: Segment, Speaker, Start_Time, End_Time, Start_Seconds, End_Seconds, Duration_Seconds, Text")
                        
                        if csv_df is not None and len(csv_df) > 5:
                            # Skip metadata rows for preview
                            data_rows = csv_df[csv_df['Segment'] != 'METADATA'].head(3)
                            if not data_rows.empty:
                                st.dataframe(data_rows[['Segment', 'Speaker', 'Start_Time', 'End_Time', 'Text']])
                else:
                    st.info("No speaker segments available for export")
            
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
                    st.scatter_chart(chart_data.set_index('AUDIO_DURATION_SECONDS'))
            
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
        col1, col2, col3 = st.columns(3)
        
        with col1:
            filter_file_type = st.selectbox("Filter by File Type:", ["All"] + sorted(df['FILE_TYPE'].unique()))
        
        with col2:
            filter_language = st.selectbox("Filter by Language:", ["All"] + sorted(df['DETECTED_LANGUAGE'].unique()))
        
        with col3:
            sort_by = st.selectbox("Sort by:", ["TRANSCRIPTION_TIMESTAMP", "FILE_NAME", "SPEAKER_COUNT", "PROCESSING_TIME_SECONDS", "AUDIO_DURATION_SECONDS"])
        
        # Apply filters
        filtered_df = df.copy()
        
        if filter_file_type != "All":
            filtered_df = filtered_df[filtered_df['FILE_TYPE'] == filter_file_type]
        
        if filter_language != "All":
            filtered_df = filtered_df[filtered_df['DETECTED_LANGUAGE'] == filter_language]
        
        # Sort
        filtered_df = filtered_df.sort_values(sort_by, ascending=False)
        
        st.subheader(f"Showing {len(filtered_df)} records")
        
        # Display data with expandable transcripts
        for idx, row in filtered_df.head(20).iterrows():  # Limit to 20 for performance
            with st.container():
                col1, col2, col3, col4, col5 = st.columns([3, 1, 1, 1, 1])
                
                with col1:
                    st.markdown(f"**{row['FILE_NAME']}**")
                
                with col2:
                    st.text(f"{row['FILE_TYPE']}")
                
                with col3:
                    st.text(f"{row['DETECTED_LANGUAGE']}")
                
                with col4:
                    speakers = row.get('SPEAKER_COUNT', 0)
                    st.text(f"{speakers}" if speakers > 0 else "N/A")
                
                with col5:
                    duration = row['AUDIO_DURATION_SECONDS']
                    st.text(f"{duration:.1f}s" if pd.notna(duration) else "N/A")
                
                # Transcript preview
                transcript = str(row['TRANSCRIPT'])
                transcript_preview = transcript[:200] + "..." if len(transcript) > 200 else transcript
                st.text(transcript_preview)
                
                # Full transcript in expander
                with st.expander("View Full Transcript"):
                    if row.get('SPEAKER_COUNT', 0) > 0:
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

if __name__ == "__main__":
    main() 