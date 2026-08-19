"""Analytics tab: distributions and derived metrics.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.
"""

import streamlit as st




def render(df):
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
            # Local Series rather than df['FILE_SIZE_MB'] = ... : tabs are now separate
            # function calls over the SAME frame, so an in-place column would leak into
            # every other tab and into the Debug Mode column listing.
            size_mb = df['FILE_SIZE_BYTES'] / (1024 * 1024)
            st.bar_chart(size_mb.value_counts().head(20))

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
        # Local Series, not an in-place column - see the note on file size above.
        word_count = df['TRANSCRIPT'].astype(str).str.split().str.len()

        col1, col2 = st.columns(2)

        with col1:
            st.bar_chart(word_count.value_counts().head(20))

        with col2:
            avg_words_by_lang = (word_count.groupby(df['DETECTED_LANGUAGE'])
                                 .mean().sort_values(ascending=False).head(10))
            st.bar_chart(avg_words_by_lang)
