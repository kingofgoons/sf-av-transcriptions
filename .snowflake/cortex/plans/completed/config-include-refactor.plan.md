# Config Include Refactor

> ## ✅ COMPLETE — verified 2026-08-19. DO NOT EXECUTE.
>
> This refactor shipped. Verified by sweeping every consuming SQL file: **all 8 now load config
> via `EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql`**, with **zero
> inline `SET PROJECT_*` blocks** outside `00_config.sql` itself and **zero V1 object literals** —
> including `av.uploader/create_av_service_user.sql` and `cleanup_av_service_user.sql`, the two
> files this plan flagged as V1 drift.
>
> **The script names below are pre-renumbering and no longer exist.** Current names:
> `00_config.sql`, `01_bootstrap.sql`, `02_setup.sql`, `03_automate.sql`, `04_deploy_notebook.sh`,
> `05_gong_objects.sql`, `06_sync_gong.sh`, `07_reset.sql`, `08_telemetry_debug.sql`,
> `999_teardown.sql`, plus `publish_config.sh`.
>
> The config store is `TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql`, currently at
> `CONFIG_REVISION = '2026-08-19a'`. To change any object name: edit `00_config.sql`, bump
> `CONFIG_REVISION`, run `publish_config.sh`. Never paste a config block into a script.
>
> One known issue survives this refactor and is tracked in the port plan (task 5):
> `scripts/03_automate.sql` still has bare `DECLARE...END;` blocks that fail under `snow sql -f`
> and need wrapping in `EXECUTE IMMEDIATE $$ ... $$`.
>
> Procedure and diagram documentation now live in `documents/operations/runbook.md`.

---

<details>
<summary>Original plan as executed (historical — script names are stale)</summary>

## Why

The `SET PROJECT_*` block is duplicated across **7 SQL files**, and it has already drifted:

| File | Targets |
|---|---|
| `scripts/00_config.sql` | V2 ✅ |
| `scripts/01_setup.sql` | V2 ✅ |
| `scripts/02_automate.sql` | V2 ✅ |
| `scripts/98_reset.sql` | V2 ✅ |
| **`scripts/04_teardown.sql`** | **V1** ⚠️ |
| **`av.uploader/create_av_service_user.sql`** | **V1** ⚠️ |
| **`av.uploader/cleanup_av_service_user.sql`** | **V1** ⚠️ |

`scripts/99_telemetry_debug.sql` also carries its own 2-variable mini-block.

The service-user drift is already visible in the account: `AV_UPLOADER_SERVICE_ROLE` holds grants on **both** V1 (Nov 6) and V2 (Feb 10) objects, because the script was run once as-committed and once with hand-edited values.

## Mechanism

`EXECUTE IMMEDIATE FROM @stage/file.sql`. Docs confirm it "loads and runs a SQL file from a Snowflake stage **in the same session**", so `SET` session variables persist into the calling script. Config stays plain `SET` statements — no table, no stored procedure, and none of the discouraged "leave session variables set from a proc" pattern.

Every consuming script's header becomes:

```sql
EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;
```

### Bootstrap constraint

The config store cannot live in `TRANSCRIPTION_DB_V2`, because `01_setup.sql` is what creates that database — on a fresh deployment it would not exist yet. So config lives in a small deployment-independent database.

Note: `@~` (user stage) is **not** documented as supported for `EXECUTE IMMEDIATE FROM`, so a named internal stage is used instead.

**`scripts/000_bootstrap.sql`** — run once, ever:
```sql
CREATE DATABASE IF NOT EXISTS TRANSCRIPTION_DEPLOY;
CREATE SCHEMA IF NOT EXISTS TRANSCRIPTION_DEPLOY.PUBLIC;
CREATE STAGE IF NOT EXISTS TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS;
GRANT READ ON STAGE TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS TO ROLE SYSADMIN;
```

**`scripts/00_publish_config.sh`** — run whenever `00_config.sql` changes:
```bash
snow sql -q "PUT file://$(pwd)/00_config.sql @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS
             AUTO_COMPRESS=FALSE OVERWRITE=TRUE" --connection DEMO
```

### Guarding against a stale staged copy

The one real weakness of the include: the staged copy can lag the git copy. Mitigation — `00_config.sql` ends with a self-identifying echo:

```sql
SET CONFIG_REVISION = '2026-08-18a';   -- bump when editing

SELECT $CONFIG_REVISION AS CONFIG_REVISION,
       $PROJECT_DB AS DB, $PROJECT_SCHEMA AS SCHEMA,
       $PROJECT_WH AS WH, $PROJECT_COMPUTE_POOL AS POOL;
```

Every script therefore prints exactly which config it loaded as its first result.

---

## Teardown redesign (detailed)

**This is the part that needs care.** Today `04_teardown.sql` is stale on V1, so running it against the active deployment is accidentally harmless. After this refactor it resolves to the **active** deployment and actually works. The refactor converts a broken script into a loaded gun, so guards go in at the same time — not later.

Two structural problems with the current file compound this:
1. It is a flat list of `DROP` statements under level comments — nothing stops "Run All" from executing Level 4.
2. A guard that merely `RAISE`s as a separate statement can be bypassed if the client continues past errors.

### Design: one atomic guarded block

Destructive statements move **inside** a single anonymous block that validates first and then acts via `EXECUTE IMMEDIATE`. Guard and action become inseparable — you cannot skip the guard by running only part of the file.

```sql
EXECUTE IMMEDIATE FROM @TRANSCRIPTION_DEPLOY.PUBLIC.SCRIPTS/00_config.sql;

-- Operator must fill these in deliberately.
SET TEARDOWN_TARGET_DB    = '';      -- type the exact database name
SET TEARDOWN_LEVEL        = 0;       -- 1..4
SET TEARDOWN_ALLOW_ACTIVE = FALSE;   -- required if target is the active deployment
SET TEARDOWN_BACKUP_TABLE = '';      -- required for levels >= 3

DECLARE ... BEGIN  -- validates all guards, then performs exactly that level
```

### The five guards

**A. Typed-name confirmation.** `TEARDOWN_TARGET_DB` must be non-empty and exactly equal `$PROJECT_DB` as loaded from config. Forces the operator to name the victim, and catches "I thought config pointed somewhere else."

**B. Explicit level.** `TEARDOWN_LEVEL` must be 1–4; the default `0` refuses. Removes "run the whole file" as a failure mode. Levels keep current meanings:
- 1 — suspend tasks
- 2 — + notebook + GPU pool
- 3 — + tables + stages ← **destroys transcripts**
- 4 — + database + warehouse ← **destroys transcripts**

**C. Active-deployment lock.** If the target is the deployment currently marked active, refuse unless `TEARDOWN_ALLOW_ACTIVE = TRUE`. Tearing down production takes two independent acknowledgements.

**D. Verified zero-copy backup — for levels ≥ 3.** This is the guard that matters most given the standing instruction not to destroy existing transcripts (441 rows, 250 hours of audio).

Rather than a bare "are you sure", require a real backup and *verify* it:

```sql
CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_20260818
  CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
```

The block then asserts the named backup exists, is outside the database being dropped, and has a row count **equal to** the source. Any mismatch aborts. Zero-copy cloning is instant and adds no storage until divergence, so this costs essentially nothing and makes levels 3–4 fully reversible.

**E. In-flight work check — for levels ≥ 2.** `agents.md` already forbids dropping `TRANSCRIPTION_GPU_POOL_V2` while a transcription is running; this makes it an enforced assertion rather than a comment. Abort if either:
- the compute pool reports `NUM_JOBS > 0` or `ACTIVE_NODES > 0`, or
- an `EXECUTE NOTEBOOK` for the project notebook is currently RUNNING in query history.

### Failure output

Every abort names the specific variable to set and the value it expects, e.g.:

```
TEARDOWN ABORTED (guard D): level 3 destroys TRANSCRIPTION_RESULTS (441 rows).
Create a verified backup first:
  CREATE TABLE TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_<yyyymmdd>
    CLONE TRANSCRIPTION_DB_V2.TRANSCRIPTION_SCHEMA_V2.TRANSCRIPTION_RESULTS;
then SET TEARDOWN_BACKUP_TABLE = 'TRANSCRIPTION_DEPLOY.PUBLIC.TR_BACKUP_<yyyymmdd>';
```

---

## Files changed

| File | Change |
|---|---|
| `scripts/000_bootstrap.sql` | **new** — deploy DB + stage, run once |
| `scripts/00_publish_config.sh` | **new** — PUT config to stage |
| `scripts/00_config.sql` | single source of truth; adds `CONFIG_REVISION` + echo |
| `scripts/01_setup.sql` | block → include |
| `scripts/02_automate.sql` | block → include |
| `scripts/04_teardown.sql` | block → include, **+ guards**, fixes V1 drift |
| `scripts/98_reset.sql` | block → include |
| `scripts/99_telemetry_debug.sql` | mini-block → include |
| `av.uploader/create_av_service_user.sql` | block → include, fixes V1 drift |
| `av.uploader/cleanup_av_service_user.sql` | block → include, fixes V1 drift |
| `agents.md`, `README.md`, `DIARY.md` | docs |

## Verification

1. `grep -rn "^SET PROJECT_"` returns hits **only** in `00_config.sql`.
2. In a fresh session: run the include alone, confirm `SHOW VARIABLES` lists all keys and the echo prints the expected revision.
3. Run `99_telemetry_debug.sql` Q1 (read-only) to prove a consuming script works via the include.
4. Teardown dry runs — confirm each guard aborts as designed with the defaults, with a deliberately wrong `TEARDOWN_TARGET_DB`, at level 3 with no backup, and at level 3 with a row-count-mismatched backup. **No teardown level is actually executed.**

## Risks

- **Do not re-run `01_setup.sql` or `02_automate.sql` against the live deployment as part of this refactor.** They are `CREATE OR REPLACE` and would recreate the task and gate procedure — resetting the SYSADMIN ownership that was fixed earlier today and dropping the uploader's `OPERATE` grant. This refactor is script-text only; the sole live change is creating the bootstrap DB and stage.
- Stale staged config is the new failure mode, mitigated by the `CONFIG_REVISION` echo. If it proves annoying in practice, the table-based variant remains available.
- `av.uploader/config.json` still separately duplicates database/schema/warehouse/task for the Python client. Out of scope here; noted as a follow-up (either generate it from `00_config.sql` or have the publish script assert they agree).
- The pending Fix #1/#2 end-to-end test is unaffected — it needs a real recording and can happen before or after this refactor.

</details>
