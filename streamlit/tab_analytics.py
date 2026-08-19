"""Analytics tab: distributions and derived metrics.

Extracted from the monolithic dashboard's main(). State is passed in
explicitly rather than closed over.

A note on what was removed, so it does not get "restored" as a regression:

  Speaker Count Distribution / Files with Speaker Data by Language - dropped 2026-08-19.
  SPEAKER_COUNT comes from a duration-and-gap heuristic, not real diarization, so charting
  its distribution presented a guess with the authority of a measurement. Revisit once
  video runs can attribute speech to a speaker per frame (AI_MULTI_EMBED over the video
  alongside the audio transcript); until then there is nothing trustworthy to plot.

  value_counts() histograms of file size and word count - dropped 2026-08-19. Both are
  effectively continuous, so nearly every value was unique and every bar was height 1.
  A distribution over continuous data needs BINNING (pd.cut), not value_counts. If you
  find yourself reaching for value_counts on a float column, that is the bug.
"""

import pandas as pd
import streamlit as st


# Bin edges for transcript length, in words. Open-ended at the top because a handful of
# multi-hour recordings would otherwise stretch the axis and flatten everything else.
_WORD_BINS = [0, 500, 1000, 2500, 5000, 10000, float('inf')]
_WORD_LABELS = ['0-500', '500-1k', '1k-2.5k', '2.5k-5k', '5k-10k', '10k+']


def render(df):
    st.header("📊 Analytics")

    if df.empty:
        st.info("No transcriptions yet.")
        return

    # ---- Processing time vs duration -------------------------------------------------
    st.subheader("Processing Time vs Audio Duration")
    valid = df.dropna(subset=['PROCESSING_TIME_SECONDS', 'AUDIO_DURATION_SECONDS'])
    if valid.empty:
        st.info("No timing data available.")
    else:
        # Minutes rather than seconds on both axes: recordings run to hours, and a
        # four-digit second count on an axis label is not readable.
        plot = pd.DataFrame({
            'Audio duration (minutes)': valid['AUDIO_DURATION_SECONDS'] / 60,
            'Processing time (minutes)': valid['PROCESSING_TIME_SECONDS'] / 60,
        })
        st.scatter_chart(
            plot, x='Audio duration (minutes)', y='Processing time (minutes)',
            x_label='Audio duration (minutes)', y_label='Processing time (minutes)',
        )

        # State the relationship in words too. The scatter shows the shape; this gives the
        # number that actually matters for capacity planning.
        ratio = (valid['PROCESSING_TIME_SECONDS'].sum()
                 / max(valid['AUDIO_DURATION_SECONDS'].sum(), 1))
        st.caption(
            f"Each point is one file: how long it ran (y) against how long the recording "
            f"was (x). Overall the pipeline processes at **{ratio:.3f}x realtime** "
            f"(~{1 / ratio:.0f}x faster than listening). Points well above the trend are "
            f"files where the Cortex summary, not transcription, dominated the runtime."
        )

    st.divider()

    # ---- Cumulative storage growth ----------------------------------------------------
    st.subheader("Cumulative Media Transcribed")
    sized = df.dropna(subset=['TRANSCRIPTION_TIMESTAMP', 'FILE_SIZE_BYTES'])
    if sized.empty:
        st.info("No file size data available.")
    else:
        # Sort by when it was transcribed, then run a cumulative sum - this is corpus
        # growth over time, which is the storage-planning question. (The previous chart
        # here counted identical file sizes, so every bar was 1.)
        by_day = (pd.to_datetime(sized['TRANSCRIPTION_TIMESTAMP']).dt.normalize()
                  .to_frame('DATE')
                  .assign(GB=sized['FILE_SIZE_BYTES'].values / (1024 ** 3))
                  .groupby('DATE', as_index=True)['GB'].sum()
                  .sort_index()
                  .cumsum())
        st.area_chart(by_day, x_label='Date transcribed', y_label='Cumulative GB')
        st.caption(
            f"Total media transcribed to date: **{by_day.iloc[-1]:.1f} GB** across "
            f"{len(sized):,} files, mean {sized['FILE_SIZE_BYTES'].mean() / 1048576:.0f} MB "
            f"per file. Steps are batch runs; flat stretches are idle periods."
        )

    st.divider()

    # ---- Processing efficiency by file type -------------------------------------------
    st.subheader("Processing Efficiency by File Type")
    eff = df.dropna(subset=['PROCESSING_TIME_SECONDS', 'AUDIO_DURATION_SECONDS'])
    if eff.empty:
        st.info("No timing data available.")
    else:
        # A table, not a bar chart. There are two or three file types, and the useful
        # output is several numbers per type (n, mean duration, mean runtime, ratio) which
        # a single-series bar chart cannot show at all.
        g = eff.groupby('FILE_TYPE')
        agg = pd.DataFrame({
            'Type': g.size().index,
            'Files': g.size().values,
            'Mean duration (min)': (g['AUDIO_DURATION_SECONDS'].mean() / 60).values,
            'Mean runtime (min)': (g['PROCESSING_TIME_SECONDS'].mean() / 60).values,
            # Duration-weighted, not a mean of per-file ratios: one 30-second clip should
            # not carry the same weight as a two-hour call.
            'Ratio (x realtime)': (g['PROCESSING_TIME_SECONDS'].sum()
                                   / g['AUDIO_DURATION_SECONDS'].sum().clip(lower=1)).values,
        }).sort_values('Files', ascending=False)

        st.dataframe(
            agg, use_container_width=True, hide_index=True,
            column_config={
                'Files': st.column_config.NumberColumn(format="%d"),
                'Mean duration (min)': st.column_config.NumberColumn(format="%.1f"),
                'Mean runtime (min)': st.column_config.NumberColumn(format="%.1f"),
                'Ratio (x realtime)': st.column_config.NumberColumn(format="%.3f"),
            },
        )
        st.caption(
            "Ratio is processing time ÷ audio duration, weighted by duration. Lower is "
            "faster; ~0.035 is the healthy band for Whisper `base` on GPU_NV_S. Video and "
            "audio should land close together — video only adds an ffmpeg audio-extract "
            "step, so a large gap means extraction is the bottleneck, not transcription."
        )

    st.divider()

    # ---- Transcript length ------------------------------------------------------------
    st.subheader("Transcript Length")
    words = df['TRANSCRIPT'].astype(str).str.split().str.len()

    col1, col2 = st.columns([2, 1])

    with col1:
        # Binned, so the bars mean "how many transcripts fall in this range". The old
        # version charted value_counts of the raw word count, where almost every transcript
        # had a unique length and so every bar was height 1.
        binned = pd.cut(words, bins=_WORD_BINS, labels=_WORD_LABELS, right=False)
        counts = binned.value_counts().reindex(_WORD_LABELS).fillna(0).astype(int)
        # Cast the CategoricalIndex to plain strings before charting. Arrow serialization
        # of a categorical index is inconsistent across Streamlit versions and can render
        # the bins out of order or blank.
        counts.index = counts.index.astype(str)
        st.bar_chart(counts, x_label='Words per transcript', y_label='Files')

    with col2:
        st.metric("Median", f"{int(words.median()):,} words")
        st.metric("Shortest", f"{int(words.min()):,} words")
        st.metric("Longest", f"{int(words.max()):,} words")

    # Very short transcripts are the actionable signal here - they usually mean silence,
    # a failed extract, or a recording that is not speech, rather than a brief meeting.
    n_short = int((words < 100).sum())
    if n_short:
        st.caption(
            f"⚠️ **{n_short} transcript(s) under 100 words.** These are usually silence, a "
            f"failed audio extract, or non-speech audio rather than genuinely short "
            f"meetings — worth spot-checking in the Browse tab."
        )
    else:
        st.caption("No suspiciously short transcripts (all over 100 words).")
