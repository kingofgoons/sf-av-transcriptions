# audio-video-transcription-snowflake

End-to-end automated pipeline for transcribing audio/video files using Snowflake GPU Container Runtime, OpenAI Whisper, and Cortex LLM for AI summaries.

**Config**: all deployment-specific object names are session variables in `scripts/00_config.sql` (copy into setup/automate scripts). Uploader connection config lives in `av.uploader/config.json` (copy from `config.template.json`).

## Behavioral Rules

- Be direct. No sycophancy — no "great question!", no "you're absolutely right", no flattery.
- Don't over-engineer. Only make changes that are directly requested or clearly necessary.
- Don't create documentation files unless explicitly asked.
- Don't auto-commit. Only commit when explicitly asked.
- Don't add docstrings, comments, or type annotations to code you didn't change.
- When unsure, investigate first — don't guess and don't assume the user is correct.

## SQL Coding Standards (Snowflake)

**Idempotency**: all DDL must be re-runnable without error.
- Use `CREATE OR REPLACE` for views, stages, stored procedures, and notebooks.
- Use `CREATE TABLE IF NOT EXISTS` for `TRANSCRIPTION_RESULTS` and any accumulating tables.
- Use `CREATE WAREHOUSE IF NOT EXISTS` and `CREATE COMPUTE POOL IF NOT EXISTS` to avoid accidental resize/recreation.
- Never use bare `CREATE` — it fails on second run.

**Safety**:
- **NEVER** suspend or drop the GPU compute pool (`TRANSCRIPTION_GPU_POOL`) without confirming no transcription task is actively running.
- Always check `SYSTEM$STREAM_HAS_DATA('AV_STAGE_STREAM')` before manually triggering transcription to avoid duplicate runs.
- Before touching the stream, verify `DATA_RETENTION_TIME_IN_DAYS` on `TRANSCRIPTION_DB` is ≥ 14 days — lower values risk stream staleness.
- Do not truncate or replace `TRANSCRIPTION_RESULTS` without checking if `SKIP_ALREADY_TRANSCRIBED = True` is set in the notebook — it relies on this table for deduplication.

**Style**:
- Fully qualify object names: `TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.OBJECT` — never rely on session context alone.
- Use session `SET` variables (from `00_config.sql`) for all parameterized object references in setup and automation scripts.
- Uppercase SQL keywords. Snake_case for column aliases.
- Reference the four teardown levels in `04_teardown.sql` before writing any DROP statements — match the appropriate level.

## Notebook & Pipeline Standards

- Notebook config knobs live in Cell 4 of `audio_video_transcription.ipynb`: `WHISPER_MODEL`, `ENABLE_SPEAKER_DIARIZATION`, `SKIP_ALREADY_TRANSCRIBED`, `FORCE_RETRANSCRIBE`. Change these, not the logic cells.
- Whisper model selection has real cost impact — `base` is the default; `large` is ~10× slower on GPU_NV_S. Don't upsize the model without flagging the tradeoff.
- `EXECUTE NOTEBOOK` is fire-and-forget. The task exits immediately; transcription runs asynchronously. Monitor progress via `ACCOUNT_USAGE.NOTEBOOKS_CONTAINER_RUNTIME_HISTORY`, not task history.
- To deploy a notebook update: use `scripts/03_deploy_notebook.sh`. Pass `--safe` to automatically suspend/resume tasks around the deploy.
- SRT subtitles are pre-generated at transcription time and stored in `TRANSCRIPTION_RESULTS`. Do not attempt to generate them dynamically at query time.

## Python Coding Standards

- Keep the uploader (`av.uploader/upload_av_files.py`) runnable from the project root.
- RSA key-pair auth is required for the service account — do not switch to password auth.
- Local environment is minimal (`environment.yml`): Python 3.9 + `snowflake-snowpark-python`. Don't add heavy deps without a clear reason.
- `config.json` in `av.uploader/` is gitignored. Always edit `config.template.json` for any structural changes and keep them in sync.

## Cost Guardrails

- GPU_NV_S nodes are expensive at idle. The compute pool auto-suspends after 1 hour of inactivity — do not change `AUTO_SUSPEND_SECS` to a longer value without a reason.
- `SNOWFLAKE.CORTEX.COMPLETE` with `claude-sonnet-4-6` is called once per file. Cost is proportional to transcript length — long recordings (1+ hour) produce large prompts.
- The stage refresh task (`REFRESH_STAGE_DIRECTORY_TASK`) runs every 5 minutes and uses `TRANSCRIPTION_WH` (XS). It is lightweight but ongoing — confirm it is suspended when the pipeline is not in use.
- For bulk re-transcription runs, set `FORCE_RETRANSCRIBE = True` only with awareness that every file will consume GPU time and Cortex credits.
