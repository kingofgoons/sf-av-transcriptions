---
name: "webapp-app-runtime-parallel-build"
created: "2026-08-19T21:15:00.000Z"
status: pending
---

# Parallel App Runtime web app in `webapp/`

## Context

### Why, and what this is not

A **parallel** Snowflake App Runtime application in `webapp/`, alongside the existing Streamlit
app. The Streamlit app **stays on warehouse runtime and remains the primary tool** (decision
confirmed 2026-08-19). This is additive: two front ends over one pipeline, not a migration.

**This is a rewrite, not a port, and the plan must not pretend otherwise.** App Runtime builds
**Node.js apps (Next.js focus)** during public preview; Python support is only *planned*. None of
the 12 Python modules in `streamlit/` can be reused — not `sf_data.py`, not `sf_pipeline.py`, not
the export builders. What ports is the **SQL and the semantics**, not the code:

- the queries in `sf_data.py` and `sf_pipeline.py` (SQL text is reusable verbatim)
- the derived-state logic in `V_TRANSCRIPTION_RUN_STATUS` (already server-side — free)
- the unit arithmetic `4 + (files × 4)` and `PHASE_TOTAL = 6`
- the hang cross-check rule: terminal run + task `EXECUTING` = hung
- the `STARTING` age comparison (heartbeat age > task elapsed)
- the filename metadata contract `YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext`

Estimate the UI work as new front-end development, not as a translation exercise. `sf_exports.py`
alone is ~325 lines of Python whose CSV/SRT logic has no JavaScript equivalent to copy.

### What App Runtime actually buys us

These are the constraints in `documents/architecture/dashboard.md` §4 and §6 that it removes:

| Streamlit warehouse runtime | App Runtime |
|---|---|
| 200 MB upload cap, not configurable | Own upload handling; no platform cap |
| `REMOVE` blocked → no file deletion, ever | Likely available — **spike it** |
| Session variables rejected (`090244`) | Not a stored-procedure context |
| Lato impossible (CSP blocks external fonts) | **Google Fonts allowed at build time** → real Lato |
| Owner's rights: every viewer runs as one role | **Queries can run as the calling user** |
| No cross-session caching | Persistent shared service |

The caller's-rights capability is the most interesting one architecturally. The Streamlit app
gives every viewer `TRANSCRIPTION_APP_ROLE`'s privileges; App Runtime can enforce per-user access.
Worth deciding deliberately rather than inheriting.

### Verified platform constraints (from docs, 2026-08-19)

- **Node.js / Next.js only.** Python "planned", not available.
- **Not available on trial accounts.** This is a paid account, so fine.
- **`CREATE OR REPLACE APPLICATION SERVICE` is not supported.** Deploys *upgrade* in place, and
  the URL is stable — the opposite of the Streamlit app, where every deploy assigns a new
  `url_id` and 404s bookmarks. Do not carry over that warning.
- **`UNDROP` is not supported.** A dropped Application Service is unrecoverable.
- **Ownership transfer is not supported during preview.** Same trap as
  `GRANT OWNERSHIP ON STREAMLIT`: get the owning role right at creation. Deploy **as the intended
  role from the start.**
- **Standard SPCS `CREATE SERVICE` / `ALTER SERVICE` do not work** — use the
  `APPLICATION SERVICE` variants.
- **Deploy to a standard database and schema, not a personal database.** Privileges cannot be
  granted on an app in a personal DB, so a PDB deploy cannot be shared.
- **The build job always runs in the personal database** (`USER$<login_name>`), regardless of
  deploy destination. Expect that split.
- **Build egress ≠ runtime egress.** Build reaches `registry.npmjs.org`,
  `fonts.googleapis.com`, `fonts.gstatic.com` by default. The running service has **no automatic
  internet access** — outbound needs an EAI. Bundle fonts at build time; do not fetch at runtime.
- Container listens on **8080**; served over HTTPS at a `*.snowflakecomputing.app` URL.
- Artifact repository defaults to `<app-name>_REPO` in the deploy destination.

### The `snowflake.yml` collision — resolve before starting

This repo **already has a `snowflake.yml` at the root** declaring the `streamlit` entity
(`transcription_dashboard`). App Runtime also uses `snowflake.yml`, with
`type: snowflake-app`, and `snow app setup` **generates** one.

Definition version 2 supports multiple entities in one file, so both *can* coexist — but
`snow app setup` may overwrite the existing file and silently destroy the Streamlit entity
definition and its extensive explanatory comments. **Back up `snowflake.yml` and diff after
running `snow app setup`.** If the tool insists on owning the file, prefer keeping the app entity
in the root `snowflake.yml` manually over letting it clobber the Streamlit entry.

Note also that the `snowflake-apps` skill auto-loads when a directory contains `app.yml` or a
`snowflake.yml` with `type: snowflake-app` — expect that to activate once this lands.

## Decisions taken

| Question | Decision |
|---|---|
| Does Streamlit go away? | **No.** It stays primary on warehouse runtime |
| Location | `webapp/` at the repo root, parallel to `streamlit/` |
| Framework | Next.js — the only supported option in preview |
| Build approach | Use the `snowflake-apps` skill rather than hand-rolling scaffolding |
| Scope of v1 | **Read-only parity first.** No kickoff, no upload until read paths are proven |

## Open questions to settle before building

1. **Caller's rights or service role?** Per-user access is a genuine capability change. If
   caller's rights, every viewer needs `SELECT` on `TRANSCRIPTION_RESULTS` — decide whether that
   is acceptable or whether a service role is still preferable.
2. **Does `REMOVE` work?** If yes, this app can offer file deletion, which the Streamlit app can
   never do. That is a real differentiator worth confirming early.
3. **Is the 200 MB ceiling actually gone?** The platform cap is, but Next.js body-size limits and
   the documented **32 MB message size** constraint may bite. Test with a >200 MB file before
   advertising it.
4. **Which name and deploy destination?** Must be a standard DB/schema. Probably
   `TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2`, and add the names to `00_config.sql` plus the
   `V_PROJECT_CONFIG` emitter so this app resolves names the same drift-free way.

## Implementation steps

### 1. Spike the four open questions

Scaffold the smallest possible app and answer, in order: identity model (`CURRENT_USER()` /
`CURRENT_ROLE()` under caller's rights), `REMOVE` on the AV stage, an upload above 200 MB, and
whether `snow app setup` preserved the Streamlit entity in `snowflake.yml`. **Do not build UI
until these are answered** — questions 1 and 2 change the app's shape.

### 2. Read-only parity

Port the SQL, not the Python. Five views matching the Streamlit tabs (Overview, Search, Speakers,
Analytics, Browse) plus the Pipeline Status panel. Reuse `V_TRANSCRIPTION_RUN_STATUS` for state so
the two front ends cannot disagree.

Carry over the corrections already learned the hard way, rather than rediscovering them:

- **Never `value_counts()` a continuous column** — bin it. That bug shipped twice in Streamlit
  (file size, transcript length) and produced charts where every bar was height 1.
- **Guard NULL numerics.** The Python failure was `NaN` being truthy; JavaScript has the
  equivalent trap with `null`/`0`/`NaN` in truthiness checks. `PHASE_NUM`, `FILE_INDEX`,
  `FILE_TOTAL`, `UNITS_*` and `PCT_COMPLETE` are all nullable on terminal events.
- **Duration-weighted ratios** (`sum/sum`), not means of per-file ratios.
- **Tables beat charts** for few-category data (file types, languages, per-type efficiency).

### 3. Real Snowflake branding

The one place this app can exceed the Streamlit version visually. Self-host **Lato** — fetched at
build time, bundled into the package, never requested at runtime. Use the palette in
`.cortex/skills/snowflake-web-page`: Snowflake Blue `#29B5E8`, Mid Blue `#11567F`, Dark Blue
`#003545`, Navy `#003D73`, Light Tint `#F0F7FB`, Near-White `#F8FBFC`, Border `#E0E8ED`, Star Blue
`#71D3DC`.

Keep **semantic status colours unbranded** (green/orange/red). A wedged container must stay
visually distinct from a healthy run — the same rule the Streamlit theme follows.

### 4. Write paths, only after step 2 is proven

Kickoff (`EXECUTE TASK`, async, guarded on `IS_ACTIVE` **and** `STARTING`) and upload
(`auto_compress=False` equivalent, then a **mandatory** `ALTER STAGE REFRESH` — without it neither
the backlog nor the task gate can see the file). If the `REMOVE` spike passed, add deletion with a
confirmation step, since it is genuinely destructive.

### 5. Config, deploy, and docs

Add `PROJECT_WEBAPP*` names to `scripts/00_config.sql`, extend the `V_PROJECT_CONFIG` emitter, bump
`CONFIG_REVISION`, republish. Write `documents/architecture/webapp.md` and add a section to
`architecture.md` distinguishing the two front ends and when to use which. Regenerate the draw.io
diagrams with round-trip validation.

## Verification

**Spike gates (step 1) — do not build past these:**

- Identity model confirmed, and the decision recorded in the plan
- `REMOVE` result known either way
- A >200 MB upload either works or its real ceiling is measured
- `snowflake.yml` still contains a working `streamlit` entity; `snow streamlit get-url
  transcription_dashboard` still resolves

**Parity (step 2):**

- Row counts match the Streamlit app exactly for the same filters
- Pipeline Status shows the same `DERIVED_STATE` as Streamlit for the same run
- A hung run renders as hung, not as complete — this is the single most important behaviour to
  get right, and it needs a real multi-file run to test
- No chart binned by `value_counts` on a continuous column
- Terminal events render without `null`/`NaN` leaking into the UI

**Deploy:**

- Owner is the intended role **at creation** — ownership cannot be transferred
- Deployed to a standard DB/schema, not a personal database
- App URL is stable across a redeploy (unlike the Streamlit app)
- **Open the app after deploying.** A green deploy is not a working app; that lesson cost a
  debugging session on the Streamlit side when a valid-looking package failed at render time.

**Data safety, non-negotiable:** `TRANSCRIPTION_RESULTS` must not lose rows. 447 as of
2026-08-19 — check before and after. This app has write access to the AV stage and, if the spike
passes, delete access. Treat both carefully.

## Out of scope

- Retiring or degrading the Streamlit app. It stays.
- Migrating the transcription pipeline itself — that is
  `port-transcription-to-job-service.plan.md`, which should land first since it changes what the
  status panel reads.
- Python App Runtime. Not available in preview; revisit if that changes, since it would make
  genuine code sharing with `streamlit/` possible.

## Critical files

- `webapp/` — new, does not exist yet
- `snowflake.yml` — **shared with the Streamlit entity; back up before `snow app setup`**
- `streamlit/sf_data.py`, `streamlit/sf_pipeline.py` — source of the SQL to port (not the code)
- `documents/architecture/dashboard.md` — the constraint list this app is designed to escape
- `scripts/00_config.sql` — add webapp names and extend the `V_PROJECT_CONFIG` emitter
- `.cortex/skills/snowflake-web-page` — brand palette and typography
