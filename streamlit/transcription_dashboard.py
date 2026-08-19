"""Audio/Video Transcription Dashboard - entrypoint.

MUST stay in the root of the source directory: warehouse-runtime Streamlit apps require
the entrypoint file at the root, and this filename is what MAIN_FILE points at.

Module layout (deliberately FLAT - see documents/architecture/dashboard.md):
    sf_config    session, object names, constants
    sf_theme     brand palette, CSS, status-card renderer
    sf_data      queries against TRANSCRIPTION_RESULTS
    sf_pipeline  run status, task state, stage backlog
    sf_segments  speaker-segment matching and rendering
    sf_exports   CSV/SRT builders and download helpers
    tab_*        one render() per tab

Colours come from .streamlit/config.toml ([theme] / [theme.sidebar]), not from CSS here.
"""

import pandas as pd
import streamlit as st

import sf_config
from sf_config import get_snowflake_connection
from sf_data import load_transcription_data, get_summary_stats
from sf_pipeline import render_status_panel
from sf_theme import inject_css, brand_header

import tab_overview
import tab_search
import tab_speakers
import tab_analytics
import tab_browse

# page_title and page_icon are accepted but IGNORED by Streamlit in Snowflake. Kept for
# parity with local execution.
st.set_page_config(
    page_title="Audio/Video Transcription Dashboard",
    page_icon="🎵",
    layout="wide",
    initial_sidebar_state="expanded"
)

inject_css()


def main():
    brand_header("Audio/Video Transcription",
                 "Search, review and export meeting transcripts")

    session = get_snowflake_connection()
    if session is None:
        st.stop()

    # Resolve fully-qualified object names. Prefers the shared config view in
    # TRANSCRIPTION_DEPLOY.PUBLIC so the dashboard, notebook and SQL scripts all derive
    # names from one authored source. Must happen before any module reads NAMES.*
    sf_config.init(session)

    # ---- Sidebar ---------------------------------------------------------------
    st.sidebar.title("📊 Dashboard Controls")

    with st.sidebar:
        st.subheader("Data Loading")
        data_limit = st.selectbox("Number of records to load:", [100, 500, 1000, 2000], index=2)
        if st.button("🔄 Refresh Data"):
            st.rerun()

        st.divider()
        debug_mode = st.checkbox(
            "Debug Mode", value=False,
            help="Show DataFrame dtypes, sample values, and full error tracebacks")

        # Diagnostic footer. Confirms which Streamlit build is actually running (theme
        # options vary by version) and where object names came from - config_view means
        # the shared config in TRANSCRIPTION_DEPLOY.PUBLIC was readable, session_context
        # or fallback means it was not.
        #
        # Plain text, no unsafe_allow_html: that argument is not available on every
        # Streamlit build the warehouse runtime may resolve.
        st.divider()
        st.caption(
            f"streamlit {st.__version__}  ·  config: {sf_config.NAMES.SOURCE} "
            f"({sf_config.NAMES.CONFIG_REVISION})  ·  {sf_config.NAMES.FQ_SCHEMA}")

        st.divider()
        st.subheader("Pipeline Status")
        # Default OFF deliberately. Each poll costs a status query plus a backlog query, so
        # leaving it on keeps the warehouse awake for no benefit. Turn it on while watching
        # a run.
        auto_refresh = st.checkbox(
            "Auto-refresh (5s)", value=False,
            help="Polls run status every 5 seconds. Only useful while a transcription is "
                 "running; costs a warehouse query per poll.")
        deep_refresh = st.button(
            "🔃 Rescan stage",
            help="Runs ALTER STAGE REFRESH so newly uploaded files become visible to the "
                 "backlog count. Not done on every poll because it walks the whole stage.")

    # ---- Pipeline status -------------------------------------------------------
    # Rendered ABOVE the empty-data guard on purpose: this block is most useful exactly
    # when TRANSCRIPTION_RESULTS is empty (fresh deployment, or watching the first run).
    #
    # Wrapped in st.fragment when available so auto-refresh re-runs ONLY this block. That
    # matters for cost: there is no @st.cache_data anywhere in this app, so a full rerun
    # re-executes every dashboard query. st.fragment needs Streamlit >= 1.37; warehouse
    # runtimes pin older versions on some accounts, so fall back rather than crash.
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

    # ---- Data -----------------------------------------------------------------
    df = load_transcription_data(session, data_limit)

    # TRANSCRIPTION_TIMESTAMP was cast to VARCHAR in SQL to avoid PyArrow timezone
    # serialization issues. Re-parse here as timezone-naive datetime64[ns].
    if not df.empty and 'TRANSCRIPTION_TIMESTAMP' in df.columns:
        df['TRANSCRIPTION_TIMESTAMP'] = pd.to_datetime(df['TRANSCRIPTION_TIMESTAMP'])

    # Debug panel - shown immediately after load so types are visible even if a later
    # rendering call crashes before any output reaches the screen.
    if debug_mode and not df.empty:
        with st.expander("Debug Info", expanded=True):
            st.write(f"**Shape:** {df.shape[0]} rows × {df.shape[1]} columns")
            dtype_df = pd.DataFrame({
                "Column": df.dtypes.index,
                "dtype": df.dtypes.astype(str).values,
                "nulls": df.isnull().sum().values,
                "sample": [repr(df[c].iloc[0])[:120] for c in df.columns],
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
            <p>Nothing in TRANSCRIPTION_RESULTS yet. Upload media and start a run &mdash;
            the data tabs will appear once a transcription completes.</p>
        </div>
        """, unsafe_allow_html=True)
        # `return`, not st.stop(). The status panel above has already rendered, which is the
        # whole reason it was moved above this guard.
        #
        # Do NOT "fix" this by guarding each tab with st.stop() instead: st.stop() halts the
        # ENTIRE script, so a guard inside a `with tab1:` block silently prevents tabs 2-5
        # from rendering at all.
        return

    stats = get_summary_stats(session)

    # ---- Tabs -----------------------------------------------------------------
    tab1, tab2, tab3, tab4, tab5 = st.tabs(
        ["📈 Overview", "🔍 Search", "👥 Speaker View", "📊 Analytics", "📋 Browse Data"])

    with tab1:
        tab_overview.render(df, stats)
    with tab2:
        tab_search.render(session, df)
    with tab3:
        tab_speakers.render(session, df)
    with tab4:
        tab_analytics.render(df)
    with tab5:
        tab_browse.render(session, df)

    # Footer
    st.markdown("---")
    st.markdown("Built with ❤️ using Streamlit in Snowflake")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        st.error(f"Application error: {str(e)}")
        st.info("Please check your Snowflake connection and try again.")
