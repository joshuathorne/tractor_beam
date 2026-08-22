@echo off
rem yt-dlp breaks when sites change their players. Run this when a download fails.
setlocal
cd /d "%~dp0"
.venv\Scripts\python.exe -m pip install --quiet --upgrade "yt-dlp[default,curl-cffi]"
for /f %%v in ('.venv\Scripts\python.exe -c "import yt_dlp;print(yt_dlp.version.__version__)"') do echo yt-dlp now at %%v
