"""Snowflake brand palette, CSS, and pipeline-status presentation.

The base colour theme (primary, backgrounds, text, sidebar) lives in
.streamlit/config.toml and is applied natively by Streamlit. This module holds only what
theming cannot express: the custom card/segment classes the dashboard already used, and
the status-state colour map.

RETINT NOTE: the CSS below is the original dashboard stylesheet with arbitrary colours
replaced by brand equivalents. Colours that carry MEANING were deliberately left alone -
speaker green, and the status green/orange/red in STATE_STYLE. Making those blue for brand
purity would defeat their purpose; the orange "container wedged" state in particular exists
to catch the eye.
"""

import streamlit as st


####################################
# BRAND PALETTE
####################################

BRAND = {
    'SF_BLUE':    '#29B5E8',   # Snowflake Blue - primary accent
    'MID_BLUE':   '#11567F',   # links, subheadings
    'DARK_BLUE':  '#003545',   # strong emphasis
    'NAVY':       '#003D73',   # gradient start (unused: no banner)
    'MED_BLUE':   '#0055A5',   # gradient end (unused: no banner)
    'STAR_BLUE':  '#71D3DC',   # highlight labels
    'LIGHT_TINT': '#F0F7FB',   # card backgrounds
    'NEAR_WHITE': '#F8FBFC',   # sidebar, footer
    'BORDER':     '#E0E8ED',   # borders
    'TEXT':       '#333333',   # body text
    'TEXT_MUTED': '#555555',   # secondary text
}


def inject_css():
    """Inject brand typography and the dashboard's custom card classes.

    WHY HEADINGS ARE STYLED HERE RATHER THAN VIA [theme]:
    the first branding attempt used ONLY .streamlit/config.toml. It applied correctly but
    was imperceptible - Streamlit's default light theme is already very close to the brand
    palette, so nothing visibly changed. Heading colour and the blue underline are what
    actually make the app read as Snowflake, and they are only reachable through CSS.
    Newer theme options (headingFont, borderColor, chartCategoricalColors) would help but
    warehouse runtime resolves an unpinned Streamlit, so relying on them is a risk.
    """
    st.markdown(f"""
<style>
    /* ---- Brand typography (per Snowflake brand guidelines) ---------------- */
    /* H1: page title */
    h1 {{
        color: {BRAND['DARK_BLUE']} !important;
        font-weight: 900 !important;
        letter-spacing: -0.5px;
    }}
    /* H2: section headings - dark blue with a Snowflake Blue rule underneath */
    h2 {{
        color: {BRAND['DARK_BLUE']} !important;
        font-weight: 700 !important;
        border-bottom: 2px solid {BRAND['SF_BLUE']};
        padding-bottom: 0.3rem;
    }}
    /* H3: subheadings */
    h3 {{
        color: {BRAND['MID_BLUE']} !important;
        font-weight: 700 !important;
    }}
    a, a:visited {{ color: {BRAND['MID_BLUE']}; }}
    a:hover {{ color: {BRAND['SF_BLUE']}; }}

    /* Metric tiles: tinted card with a brand accent bar */
    div[data-testid="stMetric"] {{
        background-color: {BRAND['LIGHT_TINT']};
        border-left: 0.25rem solid {BRAND['SF_BLUE']};
        border-radius: 0.4rem;
        padding: 0.75rem 1rem;
    }}
    div[data-testid="stMetricLabel"] {{ color: {BRAND['MID_BLUE']}; }}

    /* Active tab picks up the brand accent */
    button[data-baseweb="tab"][aria-selected="true"] {{
        color: {BRAND['DARK_BLUE']} !important;
        font-weight: 700;
    }}

    /* ---- Dashboard card classes ------------------------------------------- */
    .metric-container {{
        background-color: {BRAND['LIGHT_TINT']};
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 0.25rem solid {BRAND['SF_BLUE']};
        margin: 0.5rem 0;
    }}
    .search-result {{
        background-color: {BRAND['NEAR_WHITE']};
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 0.5rem 0;
    }}
    .transcript-box {{
        background-color: #ffffff;
        padding: 1rem;
        border-radius: 0.5rem;
        border: 1px solid {BRAND['BORDER']};
        margin: 0.5rem 0;
    }}
    /* Speaker green is SEMANTIC (speaker identity), not decorative - left unbranded. */
    .speaker-segment {{
        background-color: #f9f9f9;
        padding: 0.75rem;
        border-radius: 0.25rem;
        border-left: 0.25rem solid #4CAF50;
        margin: 0.5rem 0;
    }}
    .speaker-label {{
        font-weight: bold;
        color: #2E7D32;
        font-size: 0.9rem;
        margin-bottom: 0.25rem;
    }}
    .speaker-text {{
        color: {BRAND['TEXT']};
        line-height: 1.5;
    }}
    .timestamp {{
        color: #666;
        font-size: 0.8rem;
    }}
    .info-box {{
        background-color: {BRAND['LIGHT_TINT']};
        padding: 1rem;
        border-radius: 0.5rem;
        border-left: 0.25rem solid {BRAND['MID_BLUE']};
        margin: 1rem 0;
    }}
</style>
""", unsafe_allow_html=True)


def brand_header(title, subtitle=None):
    """Compact branded title row.

    Not the gradient banner from the brand skill - that was declined as too tall for a
    data-dense dashboard. This is a single row: the snowflake mark and wordmark in
    Snowflake Blue, the title in Dark Blue, over a brand rule.
    """
    sub = (f"<div style='color:{BRAND['TEXT_MUTED']}; font-size:0.9rem; "
           f"margin-top:0.15rem;'>{subtitle}</div>") if subtitle else ''
    st.markdown(
        f"""<div style="border-bottom:3px solid {BRAND['SF_BLUE']};
                    padding-bottom:0.6rem; margin-bottom:1.2rem;">
            <span style="color:{BRAND['SF_BLUE']}; font-weight:700; font-size:0.8rem;
                         letter-spacing:2px; text-transform:uppercase;">
                &#10052; Snowflake
            </span>
            <div style="color:{BRAND['DARK_BLUE']}; font-weight:900; font-size:1.9rem;
                        letter-spacing:-0.5px; line-height:1.15;">{title}</div>
            {sub}
        </div>""",
        unsafe_allow_html=True)


####################################
# PIPELINE STATUS PRESENTATION
####################################

# accent, background, label, blurb
#
# These colours are SEMANTIC and intentionally not brand-blue. A wedged container must be
# visually distinct from a healthy run at a glance - that is the entire point of the hang
# detector. Do not "brand" this map.
STATE_STYLE = {
    'STARTING':                 ('#1f77b4', '#eaf2fb', 'STARTING',          'Container starting - no progress reported yet'),
    'RUNNING':                  ('#1f77b4', '#eaf2fb', 'RUNNING',           'Transcription in progress'),
    'FINISHING':                ('#1f77b4', '#eaf2fb', 'FINISHING',         'Work committed, container winding down'),
    'CELLS_COMPLETE':           ('#4CAF50', '#eaf7ea', 'COMPLETE',          'All notebook cells finished'),
    'SUCCEEDED':                ('#4CAF50', '#eaf7ea', 'SUCCEEDED',         'Run completed cleanly'),
    'WORK_COMPLETE_NOT_EXITED': ('#FF9800', '#fff4e5', 'HUNG (work saved)', 'Transcripts were written but the container has not exited. Known snowbook shutdown hang - the data is safe.'),
    'STALLED':                  ('#FF5722', '#ffece7', 'STALLED',           'No heartbeat for over 10 minutes'),
    'FAILED':                   ('#9E9E9E', '#f5f5f5', 'FAILED',            'Run reported a failure'),
    'IDLE':                     ('#9E9E9E', '#f5f5f5', 'IDLE',              'No pipeline runs recorded yet'),
}


def status_card(accent, bg, label, blurb, detail_lines):
    """Render the status card. Follows the house idiom used elsewhere in this app:
    a left accent bar plus a tinted background."""
    st.markdown(
        f"""<div style="background-color:{bg}; padding:1rem; border-radius:0.5rem;
                    border-left:0.25rem solid {accent}; margin:0.5rem 0;">
            <div style="font-weight:bold; color:{accent}; font-size:1.05rem;">&#9679; {label}</div>
            <div style="color:{BRAND['TEXT_MUTED']}; font-size:0.85rem; margin-bottom:0.4rem;">{blurb}</div>
            {''.join(f"<div style='color:{BRAND['TEXT']}; line-height:1.5;'>{d}</div>" for d in detail_lines)}
        </div>""",
        unsafe_allow_html=True)
