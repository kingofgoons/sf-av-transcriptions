"""Search tab: keyword search across transcripts.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.
"""

import streamlit as st
import pandas as pd
from datetime import datetime, timedelta
import json
import re

from sf_data import search_transcriptions
from sf_exports import convert_search_results_to_csv, convert_search_results_to_srt, create_csv_download, srt_download_link
from sf_segments import display_search_result_with_speakers, find_matching_segments_with_context, highlight_text


def render(session, df):
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
                                    # NOTE: expander_label is assigned HERE, not inside the
                                    # `with col1:` block below. In the original code it was set
                                    # inside that block and consumed further down, outside it -
                                    # which worked only because the branch always ran. Hoisted
                                    # during the module split to remove the latent scope bug.
                                    expander_label = "🎯 View Matches with Context"
                                    col1, col2 = st.columns([3, 1])

                                    with col1:
                                        st.empty()

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
                                                search_csv_string = create_csv_download(search_csv_df)
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
