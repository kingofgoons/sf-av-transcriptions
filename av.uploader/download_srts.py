"""
Download SRT subtitle files from TRANSCRIPTION_RESULTS for a given processing date range.
Writes <filename>.srt and <filename>_speakers.srt for each matching row.
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime

import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization


CONFIG_PATH = Path(__file__).parent / 'config.json'
PRIVATE_KEY_PATH = Path(__file__).parent.parent / 'rsa_key.p8'


def load_config(config_path):
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"Error: {config_path} not found.")
        print("Please copy config.template.json to config.json and fill in your credentials.")
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error parsing {config_path}: {e}")
        sys.exit(1)


def load_private_key(private_key_path):
    try:
        with open(private_key_path, 'rb') as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
                backend=default_backend()
            )
        return private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
    except FileNotFoundError:
        print(f"Error: Private key file not found at {private_key_path}")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading private key: {e}")
        sys.exit(1)


def connect_to_snowflake(config):
    print("Connecting to Snowflake...")
    private_key = load_private_key(PRIVATE_KEY_PATH)
    try:
        conn = snowflake.connector.connect(
            user=config['user'],
            account=config['account'],
            private_key=private_key,
            warehouse=config.get('warehouse'),
            database=config.get('database'),
            schema=config.get('schema'),
            role=config.get('role')
        )
        print(f"Connected as {config['user']}")
        return conn
    except Exception as e:
        print(f"Error connecting to Snowflake: {e}")
        sys.exit(1)


def fetch_transcripts(conn, config, start_date, end_date):
    db = config['database']
    schema = config['schema']
    query = f"""
        SELECT
            FILE_NAME,
            TRANSCRIPT_WITH_SPEAKERS,
            TRANSCRIPTION_TIMESTAMP
        FROM {db}.{schema}.TRANSCRIPTION_RESULTS
        WHERE TRANSCRIPTION_TIMESTAMP >= %(start)s
          AND TRANSCRIPTION_TIMESTAMP <  DATEADD('day', 1, %(end)s::DATE)
        ORDER BY TRANSCRIPTION_TIMESTAMP
    """
    cursor = conn.cursor()
    cursor.execute(query, {'start': start_date, 'end': end_date})
    rows = cursor.fetchall()
    cursor.close()
    return rows


def format_timestamp_srt(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def generate_srt_content(transcript_with_speakers):
    if not transcript_with_speakers or 'speakers' not in transcript_with_speakers:
        return None
    srt_lines = []
    for i, segment in enumerate(transcript_with_speakers['speakers'], 1):
        start = format_timestamp_srt(segment['start_time'])
        end = format_timestamp_srt(segment['end_time'])
        srt_lines.extend([str(i), f"{start} --> {end}", segment['text'].strip(), ''])
    return '\n'.join(srt_lines)


def generate_srt_with_speakers(transcript_with_speakers):
    if not transcript_with_speakers or 'speakers' not in transcript_with_speakers:
        return None
    srt_lines = []
    for i, segment in enumerate(transcript_with_speakers['speakers'], 1):
        start = format_timestamp_srt(segment['start_time'])
        end = format_timestamp_srt(segment['end_time'])
        speaker = segment.get('speaker', 'Unknown')
        srt_lines.extend([str(i), f"{start} --> {end}", f"[{speaker}] {segment['text'].strip()}", ''])
    return '\n'.join(srt_lines)


def srt_stem(file_name):
    """Return the filename stem (no extension) for naming output files."""
    return Path(file_name).stem


def main():
    parser = argparse.ArgumentParser(
        description="Download SRT files from TRANSCRIPTION_RESULTS by processing date range"
    )
    parser.add_argument('--start', required=True, help='Start date (YYYY-MM-DD), inclusive')
    parser.add_argument('--end',   required=True, help='End date (YYYY-MM-DD), inclusive')
    parser.add_argument('--output', default='srt_output', help='Output directory (default: srt_output)')
    parser.add_argument('--speakers', action='store_true',
                        help='Also write _speakers.srt files (default: write both)')
    parser.add_argument('--no-plain', action='store_true',
                        help='Skip plain SRT, write only _speakers.srt')
    args = parser.parse_args()

    # Validate dates
    for label, val in [('--start', args.start), ('--end', args.end)]:
        try:
            datetime.strptime(val, '%Y-%m-%d')
        except ValueError:
            print(f"Error: {label} must be in YYYY-MM-DD format, got: {val}")
            sys.exit(1)

    if args.start > args.end:
        print("Error: --start must be on or before --end")
        sys.exit(1)

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(CONFIG_PATH)
    conn = connect_to_snowflake(config)

    try:
        print(f"Querying transcriptions from {args.start} to {args.end}...")
        rows = fetch_transcripts(conn, config, args.start, args.end)
    finally:
        conn.close()

    if not rows:
        print("No transcriptions found in that date range.")
        sys.exit(0)

    print(f"Found {len(rows)} transcription(s). Writing to {output_dir}/\n")

    written = 0
    skipped = 0

    for file_name, transcript_with_speakers, ts in rows:
        stem = srt_stem(file_name)
        ts_str = ts.strftime('%Y-%m-%d %H:%M:%S') if ts else 'unknown'

        # VARIANT comes back as a dict or JSON string depending on driver version
        if isinstance(transcript_with_speakers, str):
            import json as _json
            try:
                transcript_with_speakers = _json.loads(transcript_with_speakers)
            except Exception:
                transcript_with_speakers = None

        if not transcript_with_speakers:
            print(f"  [{ts_str}] {file_name}: TRANSCRIPT_WITH_SPEAKERS is NULL, skipping")
            skipped += 1
            continue

        plain_written = False
        speakers_written = False

        if not args.no_plain:
            srt_content = generate_srt_content(transcript_with_speakers)
            if srt_content:
                out_path = output_dir / f"{stem}.srt"
                out_path.write_text(srt_content, encoding='utf-8')
                plain_written = True
            else:
                print(f"  [{ts_str}] {file_name}: no segments, skipping plain SRT")

        srt_speakers = generate_srt_with_speakers(transcript_with_speakers)
        if srt_speakers:
            out_path = output_dir / f"{stem}_speakers.srt"
            out_path.write_text(srt_speakers, encoding='utf-8')
            speakers_written = True
        else:
            print(f"  [{ts_str}] {file_name}: no segments, skipping speakers SRT")

        if plain_written or speakers_written:
            parts = []
            if plain_written:
                parts.append(f"{stem}.srt")
            if speakers_written:
                parts.append(f"{stem}_speakers.srt")
            print(f"  [{ts_str}] {file_name} -> {', '.join(parts)}")
            written += 1
        else:
            skipped += 1

    print(f"\nDone. {written} written, {skipped} skipped (no SRT content).")


if __name__ == '__main__':
    main()
