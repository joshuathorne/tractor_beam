#!/usr/bin/env bash
# yt-dlp breaks when sites change their players. Run this when a download fails.
set -euo pipefail
cd "$(dirname "$0")"
.venv/bin/pip install --quiet --upgrade "yt-dlp[default,curl-cffi]"
echo "yt-dlp now at $(.venv/bin/python -c 'import yt_dlp;print(yt_dlp.version.__version__)')"
