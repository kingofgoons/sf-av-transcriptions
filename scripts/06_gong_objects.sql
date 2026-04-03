-- 06_gong_objects.sql
--
-- ACCOUNT:  DEMO  (connection: DEMO)
-- PURPOSE:  Create the Gong-integration layer on top of the transcription pipeline:
--             1. GONG_CALLS_MIRROR      — landing table for Gong calls synced by 05_sync_gong.sh
--             2. UNIFIED_MEETINGS_V     — UNION ALL view over local recordings + Gong calls
--             3. MEETING_SEARCH        — Cortex Search Service for full-text + semantic search
--             4. MEETINGS_SEMANTIC_VIEW — Semantic View for Cortex Analyst text-to-SQL
--             5. MEETING_INTELLIGENCE  — Cortex Agent (search + analyst tools)
--
-- NOTE: This script does NOT use session variables from 00_config.sql.
--       Object names are hardcoded to TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2,
--       consistent with how 05_sync_gong.sh references them.
--
-- DEPENDENCIES:  Run 01_setup.sql first (creates TRANSCRIPTION_RESULTS and TRANSCRIPTION_WH_V2).
-- SAFE TO RE-RUN: Yes. GONG_CALLS_MIRROR uses IF NOT EXISTS to preserve synced data.
--                 All other objects use CREATE OR REPLACE (derived/stateless).
--
-- Usage:
--   snow sql -f scripts/06_gong_objects.sql --connection DEMO
-- ---------------------------------------------------------------------------


-- ============================================================================
-- 1. GONG_CALLS_MIRROR
--    Landing table for Gong call records synced from Snowhouse by 05_sync_gong.sh.
--    Uses IF NOT EXISTS — re-running this script will NOT wipe existing sync data.
-- ============================================================================

CREATE TABLE IF NOT EXISTS TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.GONG_CALLS_MIRROR (
    GONG_ID                    VARCHAR(18)       NOT NULL,
    MEETING_TITLE              VARCHAR(765),
    CALL_START_TS              TIMESTAMP_TZ,
    MEETING_DATE               DATE,
    DURATION_SECONDS           FLOAT,
    LANGUAGE                   VARCHAR(765),
    DIRECTION                  VARCHAR(765),
    CALL_RESULT                VARCHAR(765),
    CALL_OUTCOME               VARCHAR(765),
    CALL_SCORE                 FLOAT,
    CALL_BRIEF                 TEXT,
    KEY_POINTS                 TEXT,
    NEXT_STEPS                 TEXT,
    PARTICIPANTS_EMAILS        VARCHAR(765),
    PARTICIPANTS_JSON          VARIANT,
    TOPICS_JSON                VARIANT,
    STATS_JSON                 VARIANT,
    TALK_TIME_US_SECONDS       FLOAT,
    TALK_TIME_THEM_SECONDS     FLOAT,
    ACCOUNT_NAME               VARCHAR(255),
    PRIMARY_ACCOUNT_ID         VARCHAR(18),
    PRIMARY_OPPORTUNITY_ID     VARCHAR(18),
    RELATED_ACCOUNTS_JSON      VARIANT,
    RELATED_OPPORTUNITIES_JSON VARIANT,
    RELATED_CONTACTS_JSON      VARIANT,
    SYNCED_AT                  TIMESTAMP_NTZ     DEFAULT CURRENT_TIMESTAMP()
);


-- ============================================================================
-- 2. UNIFIED_MEETINGS_V
--    UNION ALL of local recordings (TRANSCRIPTION_RESULTS) and Gong calls
--    (GONG_CALLS_MIRROR) with a common schema. Base table for objects 3–5.
-- ============================================================================

CREATE OR REPLACE VIEW TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.UNIFIED_MEETINGS_V
    COMMENT = 'Unified view of all of Bo Landsman''s meetings: local recordings with full transcripts (LOCAL) and Gong-synced calls (GONG). Used as the base for Cortex Search and Cortex Analyst.'
AS

-- ── Local recordings processed by the transcription pipeline ─────────────────
SELECT
    'LOCAL'                                                   AS SOURCE,
    FILE_NAME                                                 AS MEETING_ID,
    CALL_START_TS                                             AS CALL_START_TS,
    CALL_START_TS::DATE                                       AS MEETING_DATE,
    AUDIO_DURATION_SECONDS                                    AS DURATION_SECONDS,
    COALESCE(MEETING_TITLE, FILE_NAME)                        AS MEETING_TITLE,
    DETECTED_LANGUAGE                                         AS LANGUAGE,
    CALL_BRIEF                                                AS CALL_BRIEF,
    KEY_POINTS                                                AS KEY_POINTS,
    NEXT_STEPS                                                AS NEXT_STEPS,
    DECISIONS_MADE                                            AS DECISIONS_MADE,
    QUESTIONS_RAISED                                          AS QUESTIONS_RAISED,
    TRANSCRIPT                                                AS FULL_TRANSCRIPT,
    SUMMARY_MARKDOWN                                          AS SUMMARY_MARKDOWN,
    ACCOUNT_NAME                                              AS ACCOUNT_NAME,
    NULL::FLOAT                                               AS TALK_TIME_US_SECONDS,
    NULL::FLOAT                                               AS TALK_TIME_THEM_SECONDS,
    PARTICIPANTS_JSON                                         AS PARTICIPANTS_JSON,
    NULL::VARCHAR                                             AS DIRECTION,
    NULL::VARCHAR                                             AS CALL_RESULT,
    NULL::VARCHAR                                             AS CALL_OUTCOME,
    NULL::VARIANT                                             AS TOPICS_JSON,
    NULL::VARIANT                                             AS STATS_JSON,
    TRANSCRIPTION_TIMESTAMP                                   AS LOADED_AT
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS

UNION ALL

-- ── Gong calls synced from Snowhouse ─────────────────────────────────────────
SELECT
    'GONG'                                                    AS SOURCE,
    GONG_ID                                                   AS MEETING_ID,
    CALL_START_TS::TIMESTAMP_NTZ                              AS CALL_START_TS,
    MEETING_DATE                                              AS MEETING_DATE,
    DURATION_SECONDS                                          AS DURATION_SECONDS,
    MEETING_TITLE                                             AS MEETING_TITLE,
    LANGUAGE                                                  AS LANGUAGE,
    CALL_BRIEF                                                AS CALL_BRIEF,
    KEY_POINTS                                                AS KEY_POINTS,
    NEXT_STEPS                                                AS NEXT_STEPS,
    NULL::VARCHAR                                             AS DECISIONS_MADE,
    NULL::VARCHAR                                             AS QUESTIONS_RAISED,
    NULL::VARCHAR                                             AS FULL_TRANSCRIPT,
    NULL::VARCHAR                                             AS SUMMARY_MARKDOWN,
    ACCOUNT_NAME                                              AS ACCOUNT_NAME,
    TALK_TIME_US_SECONDS                                      AS TALK_TIME_US_SECONDS,
    TALK_TIME_THEM_SECONDS                                    AS TALK_TIME_THEM_SECONDS,
    PARTICIPANTS_JSON                                         AS PARTICIPANTS_JSON,
    DIRECTION                                                 AS DIRECTION,
    CALL_RESULT                                               AS CALL_RESULT,
    CALL_OUTCOME                                              AS CALL_OUTCOME,
    TOPICS_JSON                                               AS TOPICS_JSON,
    STATS_JSON                                                AS STATS_JSON,
    SYNCED_AT::TIMESTAMP_NTZ                                  AS LOADED_AT
FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.GONG_CALLS_MIRROR;


-- ============================================================================
-- 3. MEETING_SEARCH
--    Cortex Search Service for full-text and semantic search over all meetings.
--    Indexes SEARCH_TEXT (assembled from all text fields) with TARGET_LAG = 1 hour.
-- ============================================================================

CREATE OR REPLACE CORTEX SEARCH SERVICE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.MEETING_SEARCH
    ON SEARCH_TEXT
    ATTRIBUTES SOURCE, MEETING_ID, MEETING_TITLE, ACCOUNT_NAME, MEETING_DATE, DURATION_SECONDS, LANGUAGE
    WAREHOUSE = TRANSCRIPTION_WH_V2
    TARGET_LAG = '1 hour'
    AS
    SELECT
        SOURCE,
        MEETING_ID,
        MEETING_TITLE,
        ACCOUNT_NAME,
        MEETING_DATE::VARCHAR                                 AS MEETING_DATE,
        DURATION_SECONDS,
        LANGUAGE,
        -- Concatenate all text fields into a single searchable column
        TRIM(
            COALESCE('# ' || MEETING_TITLE || CHR(10) || CHR(10), '')
            || COALESCE('Account: ' || ACCOUNT_NAME || CHR(10), '')
            || COALESCE('Date: ' || MEETING_DATE::VARCHAR || CHR(10) || CHR(10), '')
            || COALESCE(CALL_BRIEF || CHR(10) || CHR(10), '')
            || COALESCE('Key Topics:' || CHR(10) || KEY_POINTS || CHR(10) || CHR(10), '')
            || COALESCE('Next Steps:' || CHR(10) || NEXT_STEPS || CHR(10) || CHR(10), '')
            || COALESCE('Decisions:' || CHR(10) || DECISIONS_MADE || CHR(10) || CHR(10), '')
            || COALESCE('Questions:' || CHR(10) || QUESTIONS_RAISED || CHR(10) || CHR(10), '')
            || COALESCE('Full Transcript:' || CHR(10) || FULL_TRANSCRIPT, '')
        )                                                     AS SEARCH_TEXT
    FROM TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.UNIFIED_MEETINGS_V
    WHERE SEARCH_TEXT IS NOT NULL
      AND TRIM(SEARCH_TEXT) != '';


-- ============================================================================
-- 4. MEETINGS_SEMANTIC_VIEW
--    Semantic View for Cortex Analyst text-to-SQL over meeting analytics:
--    frequency by account, duration, talk ratio, source breakdown.
-- ============================================================================

CREATE OR REPLACE SEMANTIC VIEW TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.MEETINGS_SEMANTIC_VIEW
    TABLES (
        MEETINGS AS TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.UNIFIED_MEETINGS_V
            PRIMARY KEY (MEETING_ID)
            WITH SYNONYMS = ('calls', 'conversations', 'recordings')
            COMMENT = 'All of Bo Landsman''s meetings: local recordings with full transcripts (LOCAL) and Gong-synced calls (GONG)'
    )
    DIMENSIONS (
        MEETINGS.SOURCE           AS SOURCE
            WITH SYNONYMS = ('origin', 'data source', 'pipeline')
            COMMENT = 'LOCAL for recordings from the transcription pipeline, GONG for calls synced from Gong',
        MEETINGS.MEETING_ID       AS MEETING_ID
            COMMENT = 'Unique identifier: file name for LOCAL, Gong call ID for GONG',
        MEETINGS.MEETING_TITLE    AS MEETING_TITLE
            WITH SYNONYMS = ('title', 'subject', 'call title', 'topic')
            COMMENT = 'Meeting title inferred by LLM (LOCAL) or set in Gong',
        MEETINGS.ACCOUNT_NAME     AS ACCOUNT_NAME
            WITH SYNONYMS = ('account', 'customer', 'company', 'client')
            COMMENT = 'Account or company name',
        MEETINGS.LANGUAGE         AS LANGUAGE
            WITH SYNONYMS = ('detected language', 'spoken language'),
        MEETINGS.DIRECTION        AS DIRECTION
            COMMENT = 'Gong call direction: Conference, Inbound, or Outbound (NULL for local recordings)',
        MEETINGS.CALL_RESULT      AS CALL_RESULT
            COMMENT = 'Gong call result (NULL for local recordings)',
        MEETINGS.MEETING_DATE     AS MEETING_DATE
            WITH SYNONYMS = ('date', 'call date', 'day', 'day of meeting'),
        MEETINGS.MEETING_YEAR     AS YEAR(CALL_START_TS)
            WITH SYNONYMS = ('year', 'call year'),
        MEETINGS.MEETING_MONTH    AS MONTH(CALL_START_TS)
            WITH SYNONYMS = ('month', 'call month'),
        MEETINGS.HAS_FULL_TRANSCRIPT AS IFF(FULL_TRANSCRIPT IS NOT NULL, TRUE, FALSE)
            COMMENT = 'TRUE for LOCAL recordings that have been transcribed'
    )
    METRICS (
        MEETINGS.TOTAL_MEETINGS               AS COUNT(MEETING_ID)
            WITH SYNONYMS = ('number of meetings', 'meeting count', 'call count', 'total calls'),
        MEETINGS.TOTAL_DURATION_HOURS         AS SUM(DURATION_SECONDS) / 3600.0
            WITH SYNONYMS = ('total hours', 'hours in meetings', 'total time'),
        MEETINGS.AVG_DURATION_MINUTES         AS AVG(DURATION_SECONDS) / 60.0
            WITH SYNONYMS = ('average meeting length', 'average call length', 'avg duration'),
        MEETINGS.TOTAL_TALK_TIME_US_MINUTES   AS SUM(TALK_TIME_US_SECONDS) / 60.0
            WITH SYNONYMS = ('snowflake talk time', 'our talk time', 'SE talk time'),
        MEETINGS.TOTAL_TALK_TIME_THEM_MINUTES AS SUM(TALK_TIME_THEM_SECONDS) / 60.0
            WITH SYNONYMS = ('customer talk time', 'their talk time'),
        MEETINGS.TALK_RATIO                   AS SUM(TALK_TIME_US_SECONDS) / NULLIF(SUM(TALK_TIME_US_SECONDS) + SUM(TALK_TIME_THEM_SECONDS), 0)
            WITH SYNONYMS = ('SE talk ratio', 'talk percentage', 'how much did I talk')
    )
    COMMENT = 'Semantic view for querying Bo Landsman''s meeting analytics: frequency by account, duration, talk ratio, and coverage across local recordings and Gong calls.';


-- ============================================================================
-- 5. MEETING_INTELLIGENCE
--    Cortex Agent with two tools:
--      search_meetings  → MEETING_SEARCH (Cortex Search, for content/context questions)
--      analyze_meetings → MEETINGS_SEMANTIC_VIEW (Cortex Analyst, for metrics/trends)
-- ============================================================================

CREATE OR REPLACE AGENT TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.MEETING_INTELLIGENCE
FROM SPECIFICATION
$$
models:
  orchestration: "claude-sonnet-4-6"
orchestration:
  budget:
    seconds: 120
    tokens: 200000
instructions:
  response: "Format responses for a command-line interface using markdown. Keep responses\
    \ concise. Lead with the answer, support with evidence. For meeting content results,\
    \ include: Meeting title, Account, Date, and the relevant excerpt."
  orchestration: "You are Meeting Intelligence, a personal AI assistant for Bo Landsman,\
    \ a Senior Solutions Engineer at Snowflake covering enterprise accounts in the\
    \ Northeast.\n\nYou have access to Bo's full meeting history: 108 locally-recorded\
    \ calls with full transcripts (source: LOCAL), and 12 Gong-recorded calls with\
    \ AI-generated summaries (source: GONG), covering accounts like Kargo, ANGI, Moody's,\
    \ Innovid, and Bridgewater Associates.\n\nROUTING RULES:\n- Use search_meetings\
    \ for content, context, and transcript questions: what was discussed, what did\
    \ someone say, what happened in a specific meeting, action items, decisions, follow-ups,\
    \ or questions about a specific account or topic.\n- Use analyze_meetings for\
    \ metrics, trends, and analytics questions: counts, durations, talk ratios, how\
    \ many meetings, which accounts have the most time, monthly trends, or comparisons.\n\
    - For questions that combine both, use both tools and synthesize the results.\n\
    \nBEHAVIOR:\n- Be concise and actionable. Bo is a busy SE, not a researcher.\n\
    - When surfacing meeting content, always include the meeting title, date, and\
    \ account.\n- For search results, quote specific text from the transcript or summary\
    \ when directly relevant.\n- Format action items and next steps as bullet lists.\n\
    - If a question is ambiguous, prefer search first."
tools:
  - tool_spec:
      type: "cortex_search"
      name: "search_meetings"
      description: "Search the full content of Bo Landsman's meetings including transcripts\
        \ (LOCAL recordings) and AI-generated summaries (GONG calls). Use this to\
        \ find what was discussed in specific meetings, surface action items and next\
        \ steps, recall decisions made, look up what was said about a topic or technology,\
        \ or find context about a specific account or person. Covers 120 meetings\
        \ from October 2025 to April 2026 across accounts including Kargo, ANGI, Moody's,\
        \ Innovid, and Bridgewater Associates."
  - tool_spec:
      type: "cortex_analyst_text_to_sql"
      name: "analyze_meetings"
      description: "Run analytics and metrics queries against Bo Landsman's meeting\
        \ data. Use this to answer questions about meeting counts by account or date,\
        \ total and average duration, monthly meeting trends, talk ratios for Gong\
        \ calls, meeting frequency by language or source, or comparisons across time\
        \ periods or accounts. Covers 120 meetings: 108 local recordings and 12 Gong\
        \ calls."
tool_resources:
  search_meetings:
    execution_environment:
      query_timeout: 30
      type: "warehouse"
      warehouse: "TRANSCRIPTION_WH_V2"
    search_service: "TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.MEETING_SEARCH"
  analyze_meetings:
    execution_environment:
      query_timeout: 299
      type: "warehouse"
      warehouse: "TRANSCRIPTION_WH_V2"
    semantic_view: "TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.MEETINGS_SEMANTIC_VIEW"
$$;
