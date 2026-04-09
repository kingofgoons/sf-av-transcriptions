# Audio/Video File Uploader - Service Account

Automated Python script to upload audio/video files from the local `AUDIO_VIDEO_STAGE_FILES/` directory to Snowflake stage using a service account with RSA key authentication.

## 🎯 Overview

This uploader provides:
- **Service Account Authentication**: Secure RSA key-pair authentication (no passwords)
- **Smart Deduplication**: Only uploads files not already in the stage
- **Batch Processing**: Uploads multiple files efficiently
- **Progress Tracking**: Shows upload status and statistics
- **Automatic Triggering**: Files trigger automated transcription pipeline once uploaded

## 🚀 Quick Start

### Step 1: Generate RSA Key Pair

Generate an RSA key pair in the **project root directory** (not in `av.uploader/`):

```bash
# Navigate to project root
cd /path/to/audio-video-transcription-snowflake

# Generate private key (will be in project root)
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt

# Generate public key (will be in project root)
openssl rsa -in rsa_key.p8 -pubout -out rsa_key.pub
```

**Important**: `rsa_key.p8` is already in `.gitignore` and will NOT be committed to git.

### Step 2: Create Service Account in Snowflake

1. Open `create_av_service_user.sql` in Snowsight or your SQL client
2. Copy the content of `rsa_key.pub` (without the BEGIN/END headers)
3. Replace `<RSA_PUBLIC_KEY>` in the SQL script with your public key
4. Run the entire script to create the service user and grant privileges

### Step 3: Configure the Uploader

```bash
# Navigate to av.uploader directory
cd av.uploader

# Copy the template configuration
cp config.template.json config.json

# Edit config.json with your Snowflake account identifier
# Replace "YOUR_ACCOUNT_IDENTIFIER" with your actual account identifier
```

Example `config.json`:
```json
{
  "account": "abc12345.us-east-1",
  "user": "AV_UPLOADER_SERVICE_USER",
  "warehouse": "TRANSCRIPTION_WH",
  "database": "TRANSCRIPTION_DB",
  "schema": "TRANSCRIPTION_SCHEMA",
  "role": "AV_UPLOADER_SERVICE_ROLE",
  "stage": "AUDIO_VIDEO_STAGE"
}
```

**Note**: `config.json` is already in `.gitignore` and will NOT be committed to git.

### Step 4: Install Python Dependencies

```bash
# Install required Python packages
pip install -r requirements.txt
```

### Step 5: Upload Files

Place your audio/video files in `../AUDIO_VIDEO_STAGE_FILES/` directory, then run:

```bash
python upload_av_files.py
```

## 📋 Usage

### Basic Upload

Upload all files from default directory (`../AUDIO_VIDEO_STAGE_FILES/`):

```bash
python upload_av_files.py
```

### Custom Directory

Upload from a different directory:

```bash
python upload_av_files.py -d /path/to/your/audio/files
```

### Help

View all options:

```bash
python upload_av_files.py --help
```

## 📂 Supported File Formats

### Audio Formats
- MP3, WAV, M4A, FLAC, AAC, OGG, WMA

### Video Formats
- MP4, AVI, MOV, MKV, WEBM, FLV, WMV, M4V

## 🔄 Workflow

1. **Place Files**: Put audio/video files in `AUDIO_VIDEO_STAGE_FILES/` directory
2. **Run Uploader**: Execute `python upload_av_files.py`
3. **Automatic Processing**: 
   - Script checks which files are already uploaded
   - Uploads only new files to Snowflake stage
   - Shows progress and summary
4. **Automated Transcription**: 
   - Stream detects new files within 5 minutes
   - Task triggers transcription notebook automatically
   - Results stored in `TRANSCRIPTION_RESULTS` table

## 📊 Example Output

```
================================================================================
Audio/Video File Uploader - Snowflake Transcription Stage
================================================================================
Loading configuration from config.json...
✓ Configuration loaded successfully

Connecting to Snowflake...
✓ Connected as AV_UPLOADER_SERVICE_USER using key-pair authentication

✓ Found 5 audio/video file(s) in local directory
  File types:
    .mp3: 2 file(s)
    .mp4: 3 file(s)

Checking files in stage @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE...
✓ Found 2 file(s) in stage

================================================================================
Upload Plan:
  Total local files:     5
  Already in stage:      2
  Files to upload:       3
================================================================================

Total upload size: 147.3 MB

[1/3] Uploading: meeting_recording.mp4 (52.1 MB)... ✓ UPLOADED
[2/3] Uploading: interview.mp3 (35.2 MB)... ✓ UPLOADED
[3/3] Uploading: presentation.mp4 (60.0 MB)... ✓ UPLOADED

================================================================================
Upload Summary:
  ✓ Uploaded: 3
  ⊘ Skipped:  0
  ✗ Failed:   0
  ━ Total:    3
================================================================================

Verifying stage contents...
✓ Total files in stage @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE: 5

ℹ️  The automated transcription pipeline will process these files within 5 minutes.

✓ Connection closed

Sync Gong calls from Snowhouse → DEMO? [y/N]
```

After the upload completes, the script prompts whether to sync Gong calls via `scripts/05_sync_gong.sh`. Enter `y` to run the sync or press Enter to skip.

## 🔒 Security Best Practices

1. **Private Key Storage**:
   - Keep `rsa_key.p8` in project root (already in `.gitignore`)
   - Never commit private keys to version control
   - Store securely with appropriate file permissions (chmod 600)

2. **Configuration File**:
   - Keep `config.json` private (already in `.gitignore`)
   - Never commit configuration files with credentials

3. **Key Rotation**:
   - Rotate keys periodically for security
   - Use `RSA_PUBLIC_KEY_2` for seamless key rotation
   - Update service account in Snowflake when rotating keys

## 🛠️ Troubleshooting

### Error: Private key file not found

```bash
# Make sure rsa_key.p8 exists in project root
ls -la ../rsa_key.p8

# If not, generate it:
cd ..
openssl genrsa 2048 | openssl pkcs8 -topk8 -inform PEM -out rsa_key.p8 -nocrypt
```

### Error: config.json not found

```bash
# Copy template and edit
cp config.template.json config.json
nano config.json  # or use your preferred editor
```

### Connection Error

1. Verify account identifier in `config.json`
2. Ensure public key is set correctly in Snowflake:
   ```sql
   DESC USER AV_UPLOADER_SERVICE_USER;
   -- Check RSA_PUBLIC_KEY_FP field
   ```
3. Verify service user has necessary privileges:
   ```sql
   SHOW GRANTS TO ROLE AV_UPLOADER_SERVICE_ROLE;
   ```

### Files Not Uploading

1. Check file extensions are supported
2. Verify stage privileges:
   ```sql
   SHOW GRANTS TO ROLE AV_UPLOADER_SERVICE_ROLE;
   ```
3. Check warehouse is running and accessible

## 📁 File Structure

```
av.uploader/
├── README.md                        # This file
├── create_av_service_user.sql       # SQL script to create service account
├── cleanup_av_service_user.sql      # SQL script to remove service account
├── config.template.json             # Template for configuration
├── config.json                      # Your configuration (git-ignored)
├── upload_av_files.py               # Main upload script
├── download_srts.py                 # Download SRT subtitle files from Snowflake
└── requirements.txt                 # Python dependencies
```

## 📥 Downloading SRT Subtitle Files

`download_srts.py` fetches pre-generated SRT subtitle files from `TRANSCRIPTION_RESULTS` and writes them to a local directory. It uses the same `config.json` and RSA key as the uploader.

### Usage

```bash
# Download SRTs for a date range (writes both plain and speaker-labeled SRTs)
python download_srts.py --start 2024-01-01 --end 2024-01-31

# Write to a custom output directory
python download_srts.py --start 2024-01-01 --end 2024-01-31 --output ./my_srts

# Skip plain SRT, write only speaker-labeled SRTs
python download_srts.py --start 2024-01-01 --end 2024-01-31 --no-plain
```

### Arguments

| Argument | Required | Description |
|---|---|---|
| `--start` | Yes | Start date, inclusive (`YYYY-MM-DD`) |
| `--end` | Yes | End date, inclusive (`YYYY-MM-DD`) |
| `--output` | No | Output directory (default: `srt_output`) |
| `--no-plain` | No | Skip plain SRT, write only `_speakers.srt` |

### Output Files

For each transcription, the script writes up to two files:
- `<filename>.srt` — plain subtitles (timestamps + text)
- `<filename>_speakers.srt` — subtitles with speaker labels (`[SPEAKER_00] text`)

Files with a `NULL` `TRANSCRIPT_WITH_SPEAKERS` column (e.g., diarization was disabled) are skipped with a warning.

## 🧹 Service Account Cleanup

To remove the service user and role from Snowflake, run `cleanup_av_service_user.sql` in Snowsight or your SQL client. This drops `AV_UPLOADER_SERVICE_USER` and `AV_UPLOADER_SERVICE_ROLE` and revokes all their grants.

The cleanup script does **not** remove the transcription database, schema, warehouse, or stage — those are managed by the main project teardown scripts (`scripts/04_teardown.sql`).

## 🔗 Related Documentation

- [Main Project README](../README.md)
- [Snowflake Key-Pair Authentication](https://docs.snowflake.com/en/user-guide/key-pair-auth)
- [Python Connector Documentation](https://docs.snowflake.com/en/user-guide/python-connector)

## 💡 Tips

1. **Batch Uploads**: Place multiple files in `AUDIO_VIDEO_STAGE_FILES/` and run once
2. **Incremental Updates**: Script only uploads new files, safe to run repeatedly
3. **Monitoring**: Check transcription progress in Snowflake:
   ```sql
   SELECT * FROM TRANSCRIPTION_RESULTS ORDER BY TRANSCRIPTION_TIMESTAMP DESC;
   ```
4. **Stage Verification**: View files in stage:
   ```sql
   LIST @TRANSCRIPTION_DB.TRANSCRIPTION_SCHEMA.AUDIO_VIDEO_STAGE;
   ```

## 🆘 Support

For issues or questions:
1. Check the troubleshooting section above
2. Review the main project [README](../README.md)
3. Open an issue on GitHub

