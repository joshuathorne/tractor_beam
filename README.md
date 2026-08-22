# media-grab

A local stand-in for paste-a-URL downloader sites. `yt-dlp` does the extraction,
`ffmpeg` does the muxing, and a small FastAPI server wraps both in a web UI.
Nothing is uploaded anywhere; the only outbound request is the one yt-dlp makes
to the site you asked for.

## Run

```bash
./run.sh              # http://127.0.0.1:877
./run.sh --lan        # reachable from your phone on the LAN
./run.sh --lan 9000   # custom port
```

## Where files land

By default, your **Windows** Downloads folder (`.../Downloads/media-grab`), which
keeps large media out of the WSL virtual disk so it never swells again. Override:

```bash
MEDIA_GRAB_OUT=/mnt/f/media ./run.sh
```

## When a download fails

Sites change their players and yt-dlp needs to catch up. This fixes most breakage:

```bash
./update.sh
```

## Notes

- `--lan` binds to every interface with no authentication. Trusted networks only.
- Video downloads prefer already-mp4 streams so there's no re-encode — output is
  a byte-faithful copy of the source, not a recompressed version.
- `MEDIA_GRAB_JOBS=3` controls how many downloads run at once.
