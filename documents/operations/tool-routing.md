# Tool Routing — Audio/Video Transcription Pipeline

Which tool to reach for, and why. `agents.md` carries a compact summary of the rules that
matter every session; this file has the full reasoning and the situational tools.

Ordered by how often each matters on this project.

---

## Rule 1 — Never assert Snowflake behavior from memory

Supported-statement lists, function signatures, and object limits change frequently, and
getting them wrong wastes a deploy cycle. If a claim about what Snowflake *can or cannot do*
is load-bearing, look it up.

| Order | Tool | Use for |
|---|---|---|
| 1 | `mcp_sfke-mcp-serv_Snowflake_Documentation_Agent` | **Source of truth for Snowflake docs.** "Is X supported", limits, error-code meanings. `verbose: true` for the reasoning trail. |
| 2 | `snowflake_product_docs` | Returns **full page contents**. Use when you need the whole page or exact syntax. |
| 3 | `web_search` | Last resort — release notes or behavior newer than the docs corpus. Cite sources. |

Anonymous blocks and stored procedures do **not** accept the same statement types — see the
`LS` / `LIST_FILES` worked example in [runbook.md](runbook.md) §6.

This project has repeatedly been bitten by skipping this rule. Documented cases: `LS` being
rejected inside a stored procedure, `ADD LIVE VERSION FROM LAST` reading committed versions
rather than staged files, `COMMIT` consuming the live version, and PAT-authenticated
connections rejecting `USE ROLE`. Every one was resolvable from the docs in a single query.

## Rule 2 — SFKE is docs; Glean is code

| Tool | Source of truth for |
|---|---|
| `mcp_sfke-mcp-serv_Snowflake_Documentation_Agent` | Snowflake **product documentation** — public, behavioral, syntax |
| `mcp_glean_code_search` | Snowflake **internal code** — repositories, implementations, actual behavior when docs are silent or ambiguous |

When docs and observed behavior disagree, the code search settles it.

## Rule 3 — Inspect the account before writing SQL against it

| Tool | Use for |
|---|---|
| `snowflake_sql_execute` | Diagnostics, DDL, queries. `only_compile: true` validates without running. Raise `timeout_seconds` for long ops. |
| `snowflake_object_search` | Discover tables/views/schemas. Required before writing SQL against any table whose columns you have not already seen. |
| `snowflake_semantic_view_search` | Locate semantic views and their joins/metrics. **This project has one** — `MEETINGS_SEMANTIC_VIEW`. |

Never assume a column exists from the table name — `DESCRIBE` or search first.

## Rule 4 — Notebooks use the notebook tools, never the file tools

Editing `audio_video_transcription.ipynb` with `edit`, `multi_edit`, or `write` corrupts
notebook JSON. Use `notebook_read`, `notebook_add_cell`, `notebook_edit_cell`,
`notebook_delete_cell`. Call `notebook_add_cell` sequentially, never in parallel. Call
`notebook_get_kernel_status` before executing anything.

`notebook_edit_cell` replaces a **substring**, not the whole cell — replacing only a header
leaves the old body appended below the new content. This happened on 2026-08-18 and produced
a 161-line duplicated cell. Verify the cell after editing, or delete and re-add it.

The local kernel is **not** the GPU Container Runtime, so local execution will not reproduce
Whisper or GPU behavior. To test real pipeline behavior, deploy with
`scripts/04_deploy_notebook.sh` and inspect telemetry via `scripts/08_telemetry_debug.sql`.

## Rule 5 — `meeting-intel` is THIS project's own MCP server, not an outside source

```
mcp_meeting-intel_meeting_intelligence
  → MEETING_INTELLIGENCE (Cortex Agent)
      → search_meetings   → MEETING_SEARCH (Cortex Search on UNIFIED_MEETINGS_V)
      → analyze_meetings  → Cortex Analyst on MEETINGS_SEMANTIC_VIEW
  → UNIFIED_MEETINGS_V = TRANSCRIPTION_RESULTS ∪ GONG_CALLS_MIRROR
```

**It reads the same tables this pipeline writes.** Treat it as a fast natural-language query
path over our own transcripts — never as corroboration. If it agrees with
`TRANSCRIPTION_RESULTS`, that is not validation; it is the same data. Independent
verification requires an outside source.

Useful when you want semantic or keyword search over transcript content without hand-writing
SQL. Note `MEETING_SEARCH` has `TARGET_LAG = 1 hour`, so freshly inserted transcripts are not
searchable immediately.

## Rule 6 — Situational and out-of-scope tools

| Tool | Status on this project |
|---|---|
| `mcp_glean_chat` / `mcp_glean_search` / `mcp_glean_read_document` | Situational — internal decisions, design docs, prior art not in this repo |
| `mcp_glean_employee_search` | Situational — finding owners of an internal system |
| `mcp_google-drive_google_drive` | Situational — specs and docs the user references |
| `mcp_snowhouse-rav_raven_sales_assistant` | Situational — `06_sync_gong.sh` pulls from Snowhouse, so this helps with Gong/Snowhouse-side questions. Not needed for the transcription pipeline itself. |
| `mcp_snowflake-inv_*` (portfolio-analyst, investment-knowledge-search) | **Not relevant.** Investment research. Do not use. |
| `call_cortex_analyst`, `evaluate_semantic_view`, `reflect_semantic_model` | **Relevant** — this project owns `MEETINGS_SEMANTIC_VIEW`, which backs the `analyze_meetings` tool. Use `reflect_semantic_model` before any Cortex Analyst call, and `evaluate_semantic_view` when text-to-SQL returns wrong answers. |
| `browser_*` / `browser` subagent | Only for Snowsight UI verification the SQL API cannot cover. Prefer SQL. |

## Rule 7 — Delegate broad exploration, keep narrow lookups in-context

Use `task` with `subagent_type: "explore"` for open-ended searches spanning many files. Do
**not** spawn a subagent to find one known function or read one known file.

Use `subagent_type: "sql-verify"` after writing non-trivial analytical SQL — it catches join
fanout, `NULL` comparison traps, and `UNION` mistakes that silently produce wrong results.
Monitoring queries in this project join `TRANSCRIPTION_RESULTS` against stage listings and
`ACCOUNT_USAGE` views, which is exactly where fanout hides.

## Rule 8 — Telemetry is already configured; do not "set it up"

The account emits notebook container telemetry to the Snowflake-provided default event table
`SNOWFLAKE.TELEMETRY.EVENTS`. `SHOW PARAMETERS LIKE 'EVENT_TABLE' IN ACCOUNT` returns it with
a **blank `level`**, meaning the default is in use and was never explicitly configured.

**Never run `ALTER ACCOUNT SET EVENT_TABLE`.** It is account-wide and would redirect
telemetry for every other object in the account.

Use `scripts/08_telemetry_debug.sql` (nine queries, including Q9 for all-thread stack dumps).
Expect 3–5 minutes of ingestion latency. Event table `TIMESTAMP` is **UTC**, while
`TASK_HISTORY` is session-local — convert before correlating.

Raw fd 2 output reaches the event table, which is why the `faulthandler` hang forensics work:
the interpreter's own `resource_tracker` warning arrives the same way. Note that
`faulthandler` needs a real file descriptor — Snowflake notebooks replace `sys.stderr` with a
capture proxy that has no `fileno()`, so resolve `sys.__stderr__` instead.
