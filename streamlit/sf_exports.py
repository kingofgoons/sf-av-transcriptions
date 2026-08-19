"""CSV and SRT builders plus download helpers.

NOTE: convert_speaker_segments_to_srt was deleted during the split - it was dead code,
never called, because the live path uses the pre-generated SRT_CONTENT column. Its
sibling convert_search_results_to_srt IS live, so search-result SRTs are still built
client-side, which is inconsistent with the pre-generate-at-transcription-time rule.
That inconsistency is tracked, not fixed here.
"""

import base64
import io
from datetime import datetime
import pandas as pd
import streamlit as st


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


def create_csv_download(df):
    """Serialize a DataFrame to a CSV string, or None if empty.

    The original signature took an unused `filename` argument; both call sites passed one
    and it was silently discarded. Dropped during the module split - callers name the file
    at the st.download_button, which is where it actually matters.
    """
    if df is None or df.empty:
        return None

    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    return csv_buffer.getvalue()


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
