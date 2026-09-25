"""
Download SRT subtitle files from TRANSCRIPTION_RESULTS for a range of processing dates.
Writes <filename>.srt and <filename>_speakers.srt for each matching row.

DATES ARE LOCAL; THE COLUMN IS UTC

  TRANSCRIPTION_RESULTS.TRANSCRIPTION_TIMESTAMP is TIMESTAMP_NTZ holding UTC. NTZ carries
  no offset, so nothing in the schema says so and a naive date comparison is wrong by the
  local offset. The dates you pass are interpreted in THIS MACHINE'S timezone and converted
  to UTC bounds before the query runs, so `--today` means your calendar day, not UTC's.

  BEHAVIOUR CHANGE: before this conversion existed, `--start X --end X` compared local-intent
  dates directly against the UTC column, which in US Eastern actually returned 20:00 the
  previous day through 20:00 on X - silently missing that day's evening transcripts and
  including the previous evening's. The same invocation now returns a different, correct set
  of rows. Exports taken before and after this change are not directly comparable.
"""
import sys
import json
import argparse
from pathlib import Path
from datetime import datetime, date, time, timedelta, timezone

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


def parse_ymd(value, label):
    """Parse a YYYY-MM-DD string to a date, raising ValueError that names the flag."""
    try:
        return datetime.strptime(value, '%Y-%m-%d').date()
    except (ValueError, TypeError):
        raise ValueError(f"{label} must be in YYYY-MM-DD format, got: {value}")


def resolve_dates(args, today=None):
    """Resolve the date flags to (start_date, end_date, label) as inclusive LOCAL dates.

    `today` is injectable so tests do not depend on the calendar. Raises ValueError naming
    the flag at fault; the caller turns that into an exit status.
    """
    if today is None:
        today = date.today()

    days = getattr(args, 'days', None)
    shortcuts = sum([bool(getattr(args, 'today', False)),
                     bool(getattr(args, 'yesterday', False)),
                     days is not None])
    start_raw = getattr(args, 'start', None)
    end_raw = getattr(args, 'end', None)
    explicit = start_raw is not None or end_raw is not None

    if shortcuts and explicit:
        raise ValueError(
            "--today/--yesterday/--days cannot be combined with --start/--end. "
            "Use one or the other.")
    if not shortcuts and not explicit:
        raise ValueError(
            "No date range given. Use --today, --yesterday, --days N, "
            "or --start and --end together.")

    if getattr(args, 'today', False):
        return today, today, 'today'
    if getattr(args, 'yesterday', False):
        y = today - timedelta(days=1)
        return y, y, 'yesterday'
    if days is not None:
        if days < 1:
            raise ValueError(f"--days must be 1 or greater, got: {days}")
        # Inclusive of today, so --days 1 is exactly --today.
        return today - timedelta(days=days - 1), today, f'last {days} day(s) including today'

    if start_raw is None:
        raise ValueError("--end requires --start")
    if end_raw is None:
        raise ValueError("--start requires --end")

    start = parse_ymd(start_raw, '--start')
    end = parse_ymd(end_raw, '--end')
    if start > end:
        raise ValueError(f"--start ({start}) must be on or before --end ({end})")
    return start, end, 'explicit range'


def local_day_bounds_utc(start_date, end_date):
    """Half-open UTC bounds covering local [start_date 00:00, end_date+1day 00:00).

    Returns naive datetimes, because the target column is TIMESTAMP_NTZ and the connector
    would otherwise bind an offset the column cannot hold.

    .astimezone() on a naive datetime attaches the machine's real offset FOR THAT DATE, so
    DST is handled without naming a zone: in US Eastern, 2026-03-08 spans 23 hours and
    2026-11-01 spans 25. Hardcoding a 4- or 5-hour shift would be wrong twice a year.
    """
    start_local = datetime.combine(start_date, time.min).astimezone()
    end_local = datetime.combine(end_date + timedelta(days=1), time.min).astimezone()
    return (start_local.astimezone(timezone.utc).replace(tzinfo=None),
            end_local.astimezone(timezone.utc).replace(tzinfo=None))


def fetch_transcripts(conn, config, start_utc, end_utc):
    """Fetch rows whose TRANSCRIPTION_TIMESTAMP falls in [start_utc, end_utc).

    Both bounds are naive UTC datetimes from local_day_bounds_utc. The comparison is a
    plain range scan so Snowflake can prune micro-partitions; wrapping the column in
    CONVERT_TIMEZONE would force a per-row function call and defeat that.
    """
    db = config['database']
    schema = config['schema']
    query = f"""
        SELECT
            FILE_NAME,
            TRANSCRIPT_WITH_SPEAKERS,
            TRANSCRIPTION_TIMESTAMP
        FROM {db}.{schema}.TRANSCRIPTION_RESULTS
        WHERE TRANSCRIPTION_TIMESTAMP >= %(start_utc)s
          AND TRANSCRIPTION_TIMESTAMP <  %(end_utc)s
        ORDER BY TRANSCRIPTION_TIMESTAMP
    """
    cursor = conn.cursor()
    cursor.execute(query, {'start_utc': start_utc, 'end_utc': end_utc})
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
        description="Download SRT files from TRANSCRIPTION_RESULTS by processing date range",
        epilog="Dates are interpreted in this machine's local timezone and converted to UTC, "
               "because TRANSCRIPTION_TIMESTAMP stores UTC in a TIMESTAMP_NTZ column."
    )
    # argparse can express "at most one shortcut", but not "a shortcut OR the complete
    # --start/--end pair". resolve_dates() carries the rest of that rule.
    window = parser.add_mutually_exclusive_group()
    window.add_argument('--today', action='store_true',
                        help='Transcripts processed today (local)')
    window.add_argument('--yesterday', action='store_true',
                        help='Transcripts processed yesterday (local)')
    window.add_argument('--days', type=int, metavar='N',
                        help='Last N local days including today (--days 1 == --today)')
    parser.add_argument('--start', help='Start date (YYYY-MM-DD), local, inclusive')
    parser.add_argument('--end', help='End date (YYYY-MM-DD), local, inclusive')
    parser.add_argument('--output', default='srt_output', help='Output directory (default: srt_output)')
    parser.add_argument('--speakers', action='store_true',
                        help='Also write _speakers.srt files (default: write both)')
    parser.add_argument('--no-plain', action='store_true',
                        help='Skip plain SRT, write only _speakers.srt')
    args = parser.parse_args()

    try:
        start_date, end_date, label = resolve_dates(args)
    except ValueError as e:
        print(f"Error: {e}")
        sys.exit(1)

    start_utc, end_utc = local_day_bounds_utc(start_date, end_date)

    # Print the conversion before connecting, so a wrong window costs no round trip. An
    # unexplained local->UTC shift is what made the old single-day query return wrong rows.
    if start_date == end_date:
        print(f"Querying local {start_date} ({label})")
    else:
        print(f"Querying local {start_date} to {end_date} inclusive ({label})")
    print(f"  -> TRANSCRIPTION_TIMESTAMP >= {start_utc} UTC")
    print(f"                             <  {end_utc} UTC")

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    config = load_config(CONFIG_PATH)
    conn = connect_to_snowflake(config)

    try:
        rows = fetch_transcripts(conn, config, start_utc, end_utc)
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
