#!/bin/bash

# FFmpeg installation script for Snowflake Container Runtime
# This script installs ffmpeg which is required by OpenAI Whisper for audio/video processing

set -e  # Exit on any error

echo "Starting ffmpeg installation..."

# Update package manager
apt-get update

# Install ffmpeg and related packages
echo "Installing ffmpeg and dependencies..."
apt-get install -y ffmpeg

# Verify installation
echo "Verifying ffmpeg installation..."
ffmpeg -version | head -n 1

echo "FFmpeg installation completed successfully!"
echo "FFmpeg location: $(which ffmpeg)"

# Clean up package cache to save space
apt-get clean
rm -rf /var/lib/apt/lists/*

echo "Cleanup completed. FFmpeg is ready for use." 