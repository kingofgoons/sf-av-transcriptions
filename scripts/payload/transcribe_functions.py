"""Transcription helper functions, extracted from notebook cell 19.

WHY THIS MODULE EXISTS

These functions are the portable core of the transcription pipeline. They live here so
that:

  1. They can be unit-tested OFFLINE, with no Snowflake connection and no GPU. Five of
     them are fully pure; the rest need only a file or a subprocess.
  2. The job-service payload (transcribe_job.py) and the notebook can share one
     implementation instead of diverging copies.
  3. Their output can be checked against the ~493 historical rows already in
     TRANSCRIPTION_RESULTS, which stores both the inputs and the outputs of most of
     these functions. See tests/test_payload_parity.py.

EXTRACTED VERBATIM, with exactly ONE deliberate change, documented at the function:
generate_summary_markdown() takes `session` as an explicit first parameter rather than
reading a notebook global. Everything else is byte-identical to cell 19 so that parity
against historical rows is provable. Resist "improving" these while porting - a parity
failure then becomes ambiguous between a port bug and an intended change.

KNOWN BRITTLENESS, deliberately preserved for now: parse_summary_sections() matches
section headers by EXACT string equality on stripped lines ('**Summary**', 'Key Topics',
'Follow-up Items'). Any drift in the LLM's markdown silently yields NULL for that
section - of 240 successfully parsed historical rows, only 39 contain the '**Summary**'
marker. Making it robust is worth doing, but as a SEPARATE change with its own tests,
after parity is banked.
"""

import os
import subprocess

# MUST be `from datetime import datetime`, NOT `import datetime`.
# parse_filename_metadata() calls `datetime.strptime(...)` directly, matching the
# notebook's cell 5 import. With a plain `import datetime` that call raises
# AttributeError, which the function's bare `except Exception: pass` SWALLOWS -
# so call_start_ts silently becomes None for every file, with no error anywhere.
# This was introduced and caught during extraction on 2026-09-24. It is the same
# failure shape as the missing `re` import below: a silent NULL, not a crash.
from datetime import datetime

# LOAD-BEARING IMPORT. parse_summary_sections() uses re.search with re.MULTILINE, and
# this import was missing from the notebook for roughly six months. Every title
# extraction raised NameError at runtime, so MEETING_TITLE was stored as NULL:
# 42.5% of rows before the 2026-08-18 fix have a title, versus 100% of the 55 rows
# after it. Section parsing uses plain string comparison and kept working throughout,
# which is why CALL_BRIEF (221 rows) outnumbers MEETING_TITLE (185) in the pre-fix
# cohort. Do not remove this import.
import re



def format_timestamp_srt(seconds):
    """Convert seconds to SRT timestamp format (HH:MM:SS,mmm)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def parse_filename_metadata(file_name):
    """Extract account name and call start timestamp from filename.
    Supports format: YYYY-MM-DD HH-MM-SS_AccountName[_rest].ext
    (date and time separated by a space, account name after first underscore)
    """
    result = {'account_name': None, 'call_start_ts': None}
    # Split on '_' to get [date+time, account, ...rest]
    parts = file_name.split('_')
    if len(parts) >= 2:
        result['account_name'] = parts[1]
    # First segment is "YYYY-MM-DD HH-MM-SS"
    if parts:
        try:
            dt_str = parts[0]  # e.g. "2026-03-04 14-03-35"
            space_idx = dt_str.index(' ')
            date_part = dt_str[:space_idx]
            time_raw = dt_str[space_idx + 1:]
            time_str = time_raw.replace('-', ':')
            result['call_start_ts'] = datetime.strptime(
                f"{date_part} {time_str}", '%Y-%m-%d %H:%M:%S'
            )
        except Exception:
            pass
    return result


def parse_summary_sections(summary_content):
    """Parse LLM summary content into structured fields matching Gong's schema."""
    result = {
        'meeting_title': None,
        'call_brief': None,
        'key_points': None,
        'next_steps': None,
        'decisions_made': None,
        'questions_raised': None
    }
    if not summary_content:
        return result

    title_match = re.search(r'^#\s*Meeting Summary:\s*(.+)$', summary_content, re.MULTILINE)
    if title_match:
        result['meeting_title'] = title_match.group(1).strip()

    sections = {}
    current_section = None
    current_lines = []

    for line in summary_content.split('\n'):
        stripped = line.strip()
        if stripped in ('**Summary**', '**Summary**  '):
            if current_section:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = 'summary'
            current_lines = []
        elif stripped == 'Key Topics':
            if current_section:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = 'key_topics'
            current_lines = []
        elif stripped == 'Follow-up Items':
            if current_section:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = 'follow_up'
            current_lines = []
        elif stripped == 'Decisions Made':
            if current_section:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = 'decisions'
            current_lines = []
        elif stripped == 'Questions Raised':
            if current_section:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = 'questions'
            current_lines = []
        elif current_section:
            current_lines.append(line)

    if current_section:
        sections[current_section] = '\n'.join(current_lines).strip()

    result['call_brief'] = sections.get('summary')
    result['key_points'] = sections.get('key_topics')
    result['next_steps'] = sections.get('follow_up')
    result['decisions_made'] = sections.get('decisions')
    result['questions_raised'] = sections.get('questions')
    return result


def generate_srt_content(transcript_with_speakers):
    """
    Generate SRT subtitle content from transcript segments.
    Returns plain SRT without speaker labels.
    """
    if not transcript_with_speakers or 'speakers' not in transcript_with_speakers:
        return None
    
    srt_lines = []
    for i, segment in enumerate(transcript_with_speakers['speakers'], 1):
        start_time = format_timestamp_srt(segment['start_time'])
        end_time = format_timestamp_srt(segment['end_time'])
        text = segment['text'].strip()
        
        srt_lines.append(str(i))
        srt_lines.append(f"{start_time} --> {end_time}")
        srt_lines.append(text)
        srt_lines.append('')  # Empty line between entries
    
    return '\n'.join(srt_lines)


def generate_srt_with_speakers(transcript_with_speakers):
    """
    Generate SRT subtitle content with speaker labels.
    """
    if not transcript_with_speakers or 'speakers' not in transcript_with_speakers:
        return None
    
    srt_lines = []
    for i, segment in enumerate(transcript_with_speakers['speakers'], 1):
        start_time = format_timestamp_srt(segment['start_time'])
        end_time = format_timestamp_srt(segment['end_time'])
        speaker = segment.get('speaker', 'Unknown')
        text = segment['text'].strip()
        
        srt_lines.append(str(i))
        srt_lines.append(f"{start_time} --> {end_time}")
        srt_lines.append(f"[{speaker}] {text}")
        srt_lines.append('')  # Empty line between entries
    
    return '\n'.join(srt_lines)


def get_file_size(file_path):
    """Get file size in bytes"""
    try:
        return os.path.getsize(file_path)
    except:
        return 0


def extract_audio_from_video(video_path, audio_path):
    """
    Extract audio from video file using FFmpeg CLI (more reliable in Container Runtime)
    """
    try:
        import subprocess
        
        # Get video duration using ffprobe
        duration_cmd = [
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            video_path
        ]
        
        try:
            duration_result = subprocess.run(duration_cmd, capture_output=True, text=True, check=True)
            duration = float(duration_result.stdout.strip())
        except:
            duration = 0
        
        # Extract audio using FFmpeg
        extract_cmd = [
            'ffmpeg',
            '-y',  # Overwrite output file
            '-i', video_path,  # Input file
            '-vn',  # No video
            '-acodec', 'pcm_s16le',  # Audio codec for WAV
            '-ar', '16000',  # Sample rate (Whisper compatible)
            '-ac', '1',  # Mono audio
            audio_path
        ]
        
        result = subprocess.run(extract_cmd, capture_output=True, text=True, check=False)
        
        if result.returncode == 0 and os.path.exists(audio_path):
            return True, duration
        else:
            print(f"FFmpeg extraction failed: {result.stderr[:200]}")
            return False, 0
            
    except Exception as e:
        print(f"Error extracting audio from {video_path}: {e}")
        return False, 0


# CHANGED FROM THE NOTEBOOK: `session` is an explicit parameter here.
# In cell 19 this function read the notebook's `session` global to call
# SNOWFLAKE.CORTEX.COMPLETE. An implicit global cannot be injected in a
# headless payload and cannot be mocked in a test, so it is passed in.
# This is the ONLY change from the notebook source in this module.
def generate_summary_markdown(session, file_name, transcript, detected_language, audio_duration):
    """
    Generate a markdown summary of the transcription using Snowflake Cortex LLM.
    Returns a dict with summary_markdown plus structured section fields (call_brief, key_points,
    next_steps, decisions_made, questions_raised, meeting_title), or None on failure.
    """
    if not transcript or len(transcript.strip()) == 0:
        return None
    
    try:
        # Truncate transcript if too long (Cortex has token limits)
        max_chars = 28000  # Leave room for prompt
        truncated_transcript = transcript[:max_chars] if len(transcript) > max_chars else transcript
        
        # Escape single quotes for SQL
        escaped_transcript = truncated_transcript.replace("'", "''")
        
        prompt = f"""Analyze this transcription and create a structured markdown summary.

TRANSCRIPTION:
{escaped_transcript}

Output a markdown document using EXACTLY this structure and formatting:

# Meeting Summary: {{descriptive meeting title inferred from the content}}

**Summary**  
2-3 paragraph summary of the key points discussed.

Key Topics

- bullet list of main topics covered

Follow-up Items

- **[SNOWFLAKE]** item (use for anything involving the Snowflake platform, technical work, or Snowflake products)
- **[BO LANDSMAN - SE]** item (use for follow-up actions specifically for Bo Landsman as SE)
- **[GENERAL]** item (use for all other follow-up items)

Decisions Made

- bullet list of decisions or conclusions reached

Questions Raised

- bullet list of open questions or items needing clarification

Rules:
- Do not number the sections
- Only **Summary** is bold; all other section headings are plain text
- Every follow-up item must have a **[SNOWFLAKE]**, **[BO LANDSMAN - SE]**, or **[GENERAL]** prefix
- Be thorough in identifying follow-up items"""
        
        # Call Snowflake Cortex LLM
        summary_query = f"""
        SELECT SNOWFLAKE.CORTEX.COMPLETE(
            'claude-sonnet-4-6',
            '{prompt}'
        ) as SUMMARY
        """
        
        result = session.sql(summary_query).collect()
        
        if result and len(result) > 0:
            summary_content = result[0]['SUMMARY']
            
            # Create full markdown document
            duration_min = audio_duration / 60 if audio_duration else 0
            
            markdown = f"""# Transcription Summary: {file_name}

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Language:** {detected_language}  
**Duration:** {duration_min:.1f} minutes

---

{summary_content}

---
*Generated by Snowflake Cortex AI*
"""
            parsed = parse_summary_sections(summary_content)
            return {
                'summary_markdown': markdown,
                'meeting_title': parsed['meeting_title'],
                'call_brief': parsed['call_brief'],
                'key_points': parsed['key_points'],
                'next_steps': parsed['next_steps'],
                'decisions_made': parsed['decisions_made'],
                'questions_raised': parsed['questions_raised']
            }
        else:
            return None
            
    except Exception as e:
        print(f"   ⚠️  Summary generation failed: {e}")
        return None

# NOTE: the notebook's cell 19 ends with `print("Helper functions defined!")`. That is
# deliberately NOT carried over - a library module must not print on import, and it
# would pollute every test run and every payload log. It is the only line from cell 19
# dropped here, and it has no behaviour.
