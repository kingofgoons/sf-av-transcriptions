"""Speaker view: per-file speaker-segmented transcript.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.
"""

import streamlit as st
import pandas as pd
import re

from sf_data import get_speaker_segments
from sf_exports import convert_speaker_segments_to_csv, create_csv_download, srt_download_link
from sf_segments import display_speaker_transcript


def render(session, df):
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
                        csv_string = create_csv_download(csv_df)
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
