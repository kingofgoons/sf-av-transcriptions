"""Browse tab: filterable table of all records.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.
"""

import streamlit as st
import pandas as pd

from sf_data import get_speaker_segments
from sf_segments import display_speaker_transcript


def render(df):
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
