#!/bin/bash

# Navigate to the script's directory so it can be run from anywhere
cd "$(dirname "$0")"

# Activate the virtual environment
source .venv/bin/activate

# Execute the downloader with auto and upgrade-dubs flags
exec python3 downloader.py --auto --upgrade-dubs "$@"
