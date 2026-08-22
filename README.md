# Tractor Beam

Fetch remote video or audio via url. `yt-dlp` extracts,
`ffmpeg` muxes, + small fastAPI server for UI.

## Run

```bash
./run.sh              # http://127.0.0.1:8877
./run.sh --lan        # reachable from your phone on the LAN
./run.sh --lan 9000   # custom port
```

Natively on Windows, same arguments:

```bat
run.cmd
run.cmd --lan
run.cmd --lan 9000
```

Both launchers build `.venv` on first run. Windows additionally needs `ffmpeg.exe`
on PATH (`winget install Gyan.FFmpeg`) — the page warns if it is missing. Running
natively is worth it for large files: downloads no longer cross WSL's `/mnt/c`
bridge, and Chrome/Edge cookies become readable because DPAPI unseals normally.

## Output

Files download to (`.../Downloads/tractor_beam`).

```bash
TRACTOR_BEAM_OUT=/mnt/f/media ./run.sh
```

## Sites that need a login

Some sites only serve the good formats to a signed-in session. If a browser profile
is detected on this machine, a **Sign in as** picker appears above Inspect and yt-dlp
reuses that browser's cookies — no passwords, no separate login.

Firefox is the one that works under WSL: its `cookies.sqlite` is readable from Linux.
Chrome/Edge profiles on the Windows side are skipped on purpose, because their cookies
are sealed with Windows DPAPI and cannot be unlocked from inside WSL. Chromium-family
profiles installed *in* Linux are offered normally.

This does not defeat DRM. Sites that encrypt their streams (Widevine and friends) stay
out of reach no matter whose session you borrow.

## Supported sites

The **Supported sites** section at the bottom of the page lists and filters every
extractor yt-dlp ships (1750 of them; greyed-out entries are flagged broken upstream).
Same list from the shell:

```bash
.venv/bin/yt-dlp --list-extractors
```

## When a download fails

Sites change their players and yt-dlp needs to catch up. This fixes most breakage:

```bash
./update.sh
```

Cloudflare's anti-bot check is the other common failure, and it shows up as
`HTTP Error 403`. That one is already handled: `curl_cffi` is installed and the
generic extractor presents a browser-like TLS fingerprint, so those URLs go
through without any extra flags.

## Notes

- `--lan` binds to every interface with no authentication. Trusted networks only.
- The output path and a finished job's **Show in folder** open your file browser.
  That window can only appear on the machine running the server, so over `--lan`
  a finished job offers a download link instead.
- Video downloads prefer already-mp4 streams so there's no re-encode — output is
  a byte-faithful copy of the source, not a recompressed version.
- `TRACTOR_BEAM_JOBS=3` controls how many downloads run at once.
