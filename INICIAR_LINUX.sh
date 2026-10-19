#!/bin/bash
cd "$(dirname "$0")"
python3 -m pip install -U --disable-pip-version-check --prefer-binary yt-dlp curl_cffi imageio-ffmpeg
python3 app.py
