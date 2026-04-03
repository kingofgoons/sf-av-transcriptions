-- 05_sync_gong.sql
--
-- PURPOSE: Extract Bo Landsman's Gong calls from Snowhouse for cross-account sync.
-- RUN ON:  Snowhouse (--connection snowhouse) — read-only SELECT, no writes here.
-- OUTPUT:  Rows to be merged into TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.GONG_CALLS_MIRROR
--          on the DEMO account.
--
-- Cross-account transfer is orchestrated by 05_sync_gong.sh, which:
--   1. Runs this query against Snowhouse and captures JSON output
--   2. Generates an idempotent MERGE statement and executes it against DEMO

SELECT
    g.ID                                                                AS GONG_ID,
    g.GONG_TITLE_C                                                      AS MEETING_TITLE,
    g.GONG_CALL_START_C                                                 AS CALL_START_TS,
    g.GONG_CALL_START_C::DATE                                           AS MEETING_DATE,
    g.GONG_CALL_DURATION_SEC_C                                          AS DURATION_SECONDS,
    g.GONG_LANGUAGE_C                                                   AS LANGUAGE,
    g.GONG_DIRECTION_C                                                  AS DIRECTION,
    g.GONG_CALL_RESULT_C                                                AS CALL_RESULT,
    g.GONG_CALL_OUTCOME_C                                               AS CALL_OUTCOME,
    g.GONG_CALL_SCORE_C                                                 AS CALL_SCORE,
    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        g.GONG_CALL_BRIEF_C,
        '&#39;', ''''),
        '&quot;', '"'),
        '&amp;', '&'),
        '<br>', CHR(10)),
        '<br/>', CHR(10))                                               AS CALL_BRIEF,
    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        g.GONG_CALL_KEY_POINTS_C,
        '&#39;', ''''),
        '&quot;', '"'),
        '&amp;', '&'),
        '<br>', CHR(10)),
        '<br/>', CHR(10))                                               AS KEY_POINTS,
    REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(
        g.GONG_CALL_HIGHLIGHTS_NEXT_STEPS_C,
        '&#39;', ''''),
        '&quot;', '"'),
        '&amp;', '&'),
        '<br>', CHR(10)),
        '<br/>', CHR(10))                                               AS NEXT_STEPS,
    g.GONG_PARTICIPANTS_EMAILS_C                                        AS PARTICIPANTS_EMAILS,
    TRY_PARSE_JSON(g.GONG_RELATED_PARTICIPANTS_JSON_C)                  AS PARTICIPANTS_JSON,
    TRY_PARSE_JSON(g.GONG_RELATED_TOPICS_JSON_C)                        AS TOPICS_JSON,
    TRY_PARSE_JSON(g.GONG_RELATED_STATS_JSON_C)                         AS STATS_JSON,
    g.GONG_TALK_TIME_US_C                                               AS TALK_TIME_US_SECONDS,
    g.GONG_TALK_TIME_THEM_C                                             AS TALK_TIME_THEM_SECONDS,
    a.NAME                                                              AS ACCOUNT_NAME,
    g.GONG_PRIMARY_ACCOUNT_C                                            AS PRIMARY_ACCOUNT_ID,
    g.GONG_PRIMARY_OPPORTUNITY_C                                        AS PRIMARY_OPPORTUNITY_ID,
    TRY_PARSE_JSON(g.GONG_RELATED_ACCOUNTS_JSON_C)                      AS RELATED_ACCOUNTS_JSON,
    TRY_PARSE_JSON(g.GONG_RELATED_OPPORTUNITIES_JSON_C)                 AS RELATED_OPPORTUNITIES_JSON,
    TRY_PARSE_JSON(g.GONG_RELATED_CONTACTS_JSON_C)                      AS RELATED_CONTACTS_JSON

FROM FIVETRAN.SALESFORCE.GONG_GONG_CALL_C g
LEFT JOIN FIVETRAN.SALESFORCE.ACCOUNT a
    ON g.GONG_PRIMARY_ACCOUNT_C = a.ID

WHERE CONTAINS(g.GONG_PARTICIPANTS_EMAILS_C, 'bo.landsman@snowflake.com')

ORDER BY g.GONG_CALL_START_C DESC
;
