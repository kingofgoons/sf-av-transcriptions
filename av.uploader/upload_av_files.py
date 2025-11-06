"""
Upload audio/video files from local AUDIO_VIDEO_STAGE_FILES/ folder to Snowflake stage.
Only uploads files that haven't been uploaded yet.
"""
import os
import sys
import json
import glob
from pathlib import Path
import snowflake.connector
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization


# Path to the config file (relative to this script)
CONFIG_PATH = Path(__file__).parent / 'config.json'

# Path to the private key file (in project root)
PRIVATE_KEY_PATH = Path(__file__).parent.parent / 'rsa_key.p8'

# Supported audio/video file extensions
AV_EXTENSIONS = {
    # Audio formats
    '.mp3', '.wav', '.m4a', '.flac', '.aac', '.ogg', '.wma',
    # Video formats
    '.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv', '.wmv', '.m4v'
}


def load_config(config_path):
    """Load configuration from config.json."""
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
    """Load and parse the private key file."""
    try:
        with open(private_key_path, 'rb') as key_file:
            private_key = serialization.load_pem_private_key(
                key_file.read(),
                password=None,
                backend=default_backend()
            )
        
        # Serialize the private key to DER format for Snowflake
        pkb = private_key.private_bytes(
            encoding=serialization.Encoding.DER,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption()
        )
        return pkb
    except FileNotFoundError:
        print(f"Error: Private key file not found at {private_key_path}")
        print("\nTo generate RSA key pair:")
        print("  openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt")
        print("  openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub")
        sys.exit(1)
    except Exception as e:
        print(f"Error loading private key: {e}")
        sys.exit(1)


def connect_to_snowflake(config):
    """Connect to Snowflake using service account with key-pair authentication."""
    print("Connecting to Snowflake...")
    
    # Load private key
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
        print(f"✓ Connected as {config['user']} using key-pair authentication")
        return conn
    except Exception as e:
        print(f"Error connecting to Snowflake: {e}")
        sys.exit(1)


def get_stage_files(conn, stage_name):
    """Get list of files already in the Snowflake stage."""
    print(f"\nChecking files in stage @{stage_name}...")
    
    try:
        cursor = conn.cursor()
        # List files in the stage
        cursor.execute(f"LIST @{stage_name}")
        
        stage_files = set()
        for row in cursor.fetchall():
            # Row format: (name, size, md5, last_modified)
            file_path = row[0]  # Full path in stage
            # Extract just the filename
            filename = file_path.split('/')[-1]
            stage_files.add(filename)
        
        cursor.close()
        print(f"✓ Found {len(stage_files)} file(s) in stage")
        return stage_files
    except Exception as e:
        print(f"Warning: Could not list stage files: {e}")
        print("Assuming stage is empty...")
        return set()


def get_local_av_files(av_dir='../AUDIO_VIDEO_STAGE_FILES'):
    """Get list of audio/video files in the local directory."""
    av_path = Path(av_dir)
    
    if not av_path.exists():
        print(f"Error: Audio/Video directory not found: {av_dir}")
        return []
    
    # Find all audio/video files
    av_files = []
    for ext in AV_EXTENSIONS:
        av_files.extend(av_path.glob(f'*{ext}'))
    
    # Sort by name
    av_files.sort()
    
    print(f"\n✓ Found {len(av_files)} audio/video file(s) in local directory")
    
    # Display file types summary
    if av_files:
        ext_counts = {}
        for f in av_files:
            ext = f.suffix.lower()
            ext_counts[ext] = ext_counts.get(ext, 0) + 1
        
        print("  File types:")
        for ext, count in sorted(ext_counts.items()):
            print(f"    {ext}: {count} file(s)")
    
    return av_files


def format_size(size_bytes):
    """Format file size in human-readable format."""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if size_bytes < 1024.0:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024.0
    return f"{size_bytes:.1f} TB"


def upload_file(conn, local_file, stage_name):
    """Upload a single file to the Snowflake stage."""
    try:
        cursor = conn.cursor()
        
        # Convert to absolute path for PUT command
        abs_path = os.path.abspath(local_file)
        
        # PUT command to upload file (no compression for media files)
        put_sql = f"PUT file://{abs_path} @{stage_name} AUTO_COMPRESS=FALSE OVERWRITE=FALSE"
        
        cursor.execute(put_sql)
        result = cursor.fetchone()
        cursor.close()
        
        # Check result status
        if result and 'UPLOADED' in str(result[6]).upper():
            return True
        elif result and 'SKIPPED' in str(result[6]).upper():
            return 'SKIPPED'
        else:
            return False
            
    except Exception as e:
        print(f"  Error uploading {local_file.name}: {e}")
        return False


def upload_av_files(config, av_dir='../AUDIO_VIDEO_STAGE_FILES'):
    """Main function to upload audio/video files to Snowflake stage."""
    print("=" * 80)
    print("Audio/Video File Uploader - Snowflake Transcription Stage")
    print("=" * 80)
    
    # Build stage name
    stage_name = f"{config['database']}.{config['schema']}.{config['stage']}"
    
    # Connect to Snowflake
    conn = connect_to_snowflake(config)
    
    try:
        # Get local AV files
        local_files = get_local_av_files(av_dir)
        
        if not local_files:
            print("\nNo audio/video files found to upload.")
            return
        
        # Get files already in stage
        stage_files = get_stage_files(conn, stage_name)
        
        # Determine which files need to be uploaded
        files_to_upload = []
        already_uploaded = []
        
        for local_file in local_files:
            if local_file.name not in stage_files:
                files_to_upload.append(local_file)
            else:
                already_uploaded.append(local_file)
        
        print(f"\n{'=' * 80}")
        print(f"Upload Plan:")
        print(f"  Total local files:     {len(local_files)}")
        print(f"  Already in stage:      {len(already_uploaded)}")
        print(f"  Files to upload:       {len(files_to_upload)}")
        print(f"{'=' * 80}\n")
        
        if not files_to_upload:
            print("✓ All files are already uploaded to the stage!")
            return
        
        # Show files to upload with sizes
        total_size = 0
        for file_path in files_to_upload:
            file_size = file_path.stat().st_size
            total_size += file_size
        
        print(f"Total upload size: {format_size(total_size)}\n")
        
        # Upload files
        uploaded_count = 0
        skipped_count = 0
        failed_count = 0
        
        for i, file_path in enumerate(files_to_upload, 1):
            file_size = format_size(file_path.stat().st_size)
            print(f"[{i}/{len(files_to_upload)}] Uploading: {file_path.name} ({file_size})...", end=' ')
            
            result = upload_file(conn, file_path, stage_name)
            
            if result is True:
                print("✓ UPLOADED")
                uploaded_count += 1
            elif result == 'SKIPPED':
                print("⊘ SKIPPED (already exists)")
                skipped_count += 1
            else:
                print("✗ FAILED")
                failed_count += 1
        
        # Summary
        print(f"\n{'=' * 80}")
        print("Upload Summary:")
        print(f"  ✓ Uploaded: {uploaded_count}")
        print(f"  ⊘ Skipped:  {skipped_count}")
        print(f"  ✗ Failed:   {failed_count}")
        print(f"  ━ Total:    {len(files_to_upload)}")
        print(f"{'=' * 80}")
        
        # Verify upload
        if uploaded_count > 0:
            print("\nVerifying stage contents...")
            cursor = conn.cursor()
            cursor.execute(f"LIST @{stage_name}")
            total_in_stage = len(cursor.fetchall())
            cursor.close()
            print(f"✓ Total files in stage @{stage_name}: {total_in_stage}")
            print(f"\nℹ️  The automated transcription pipeline will process these files within 5 minutes.")
        
    finally:
        conn.close()
        print("\n✓ Connection closed")


def main():
    """Main entry point."""
    import argparse
    
    parser = argparse.ArgumentParser(
        description="Upload audio/video files from local folder to Snowflake stage for transcription"
    )
    parser.add_argument(
        '-d', '--directory',
        type=str,
        default='../AUDIO_VIDEO_STAGE_FILES',
        help='Directory containing audio/video files (default: ../AUDIO_VIDEO_STAGE_FILES)'
    )
    
    args = parser.parse_args()
    
    # Check if private key file exists
    if not PRIVATE_KEY_PATH.exists():
        print(f"Error: Private key file not found at {PRIVATE_KEY_PATH}")
        print("\nTo generate RSA key pair:")
        print("  openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt")
        print("  openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub")
        print("\nThen configure the public key in Snowflake using create_av_service_user.sql")
        sys.exit(1)
    
    # Load configuration
    print("Loading configuration from config.json...")
    config = load_config(CONFIG_PATH)
    
    # Check if account is configured
    if config.get('account') == 'YOUR_ACCOUNT_IDENTIFIER':
        print("Error: Please update the 'account' value in config.json")
        print("Replace 'YOUR_ACCOUNT_IDENTIFIER' with your actual Snowflake account identifier")
        sys.exit(1)
    
    print("✓ Configuration loaded successfully\n")
    
    # Upload AV files
    upload_av_files(config, args.directory)


if __name__ == "__main__":
    main()

