# Tractor Beam

Fetch remote video or audio via url. `yt-dlp` extracts,
`ffmpeg` muxes, + small fastAPI server for UI.

## Run

```bash
./run.sh              # http://127.0.0.1:877
./run.sh --lan        # reachable from your phone on the LAN
./run.sh --lan 9000   # custom port
```

## Output

Files download to (`.../Downloads/tractor_beam`).

```bash
TRACTOR_BEAM_OUT=/mnt/f/media ./run.sh
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
- `TRACTOR_BEAM_JOBS=3` controls how many downloads run at once.
