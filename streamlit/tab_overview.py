"""Overview tab: key metrics and charts.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.
"""

import streamlit as st
import pandas as pd
import traceback




def render(df, stats):
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
            # Local Series rather than an in-place df['DATE'] column: tabs are now
            # separate function calls over the SAME frame, so a mutation would leak into
            # other tabs and into the Debug Mode column listing.
            dates = pd.to_datetime(df['TRANSCRIPTION_TIMESTAMP']).dt.normalize()
            timeline_data = (dates.groupby(dates).size()
                             .reset_index(name='Files Processed'))
            timeline_data.columns = ['DATE', 'Files Processed']
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
