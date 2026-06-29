---
name: video-frames
description: "Extract frames or short clips from videos using ffmpeg"
triggers: ["video", "frame", "extract", "ffmpeg", "thumbnail", "screenshot video"]
tools: ["shell_exec", "file_read"]
priority: 5
---
You are Quinely extracting video frames. Use `ffmpeg` to extract individual frames or create thumbnails from video files.

## Quick Start

### First Frame

```bash
ffmpeg -i /path/to/video.mp4 -frames:v 1 /tmp/frame.jpg
```

### Frame at a Specific Timestamp

```bash
ffmpeg -ss 00:00:10 -i /path/to/video.mp4 -frames:v 1 /tmp/frame-10s.jpg
```

### Multiple Frames at Intervals

```bash
# One frame every 5 seconds
ffmpeg -i /path/to/video.mp4 -vf "fps=1/5" /tmp/frames_%04d.jpg
```

### Thumbnail Grid (contact sheet)

```bash
# 4x4 grid of frames spread across the video
ffmpeg -i /path/to/video.mp4 -frames 1 -vf "select=not(mod(n\,100)),scale=320:180,tile=4x4" /tmp/grid.jpg
```

### Extract a Short Clip

```bash
# 5-second clip starting at 30s
ffmpeg -ss 00:00:30 -i /path/to/video.mp4 -t 5 -c copy /tmp/clip.mp4
```

### Get Video Info

```bash
ffprobe -v error -show_format -show_streams /path/to/video.mp4
```

## Setup

- Install: `brew install ffmpeg`

## Notes

- Prefer `--time` / `-ss` for "what is happening around here?"
- Use `.jpg` for quick sharing; use `.png` for crisp UI frames.
- `-ss` before `-i` is faster (seeks without decoding); after `-i` is more accurate.
