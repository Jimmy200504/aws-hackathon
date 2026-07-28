# Reproducible five-minute demo video

The tracked HTML deck and `scenes.json` are the source of truth for the
Traditional Chinese judge video. Scene 4 captures the real local UI; the other
scenes use the release evidence deck.

On macOS with Google Chrome, `say`, FFmpeg, and FFprobe installed:

```bash
python3 scripts/render_demo_video.py
```

This produces:

- `dist/skillweave-demo-5min.mp4` — 1920×1080 H.264, AAC narration, embedded
  Traditional Chinese subtitle track.
- `dist/skillweave-demo-5min.zh-TW.srt` — subtitle sidecar.
- `reports/demo-video.json` — duration, stream, scene, size, and SHA-256
  verification.

The MP4 is a rebuildable release asset and is intentionally ignored by Git.
After uploading it to a public host, register the real URL:

```bash
python3 scripts/update_release_urls.py \
  --demo-video-url "https://VIDEO_HOST/VIDEO_ID"
```

Never replace the null external URL with a placeholder.
