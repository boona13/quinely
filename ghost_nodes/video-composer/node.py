"""
Video Composer Node — stitch clips + audio into a complete video.

The final assembly step for video production pipelines:
1. Concatenate video clips (MP4) and still images (PNG/JPG) in sequence
2. Overlay multiple audio tracks (narration, music, SFX) at different volumes
3. Add transitions between clips (crossfade, fade-to-black)
4. Output a single MP4 with everything merged

Uses moviepy (pip-installable, wraps ffmpeg internally).
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.video_composer")

RESOLUTIONS = {
    "480p": (854, 480),
    "720p": (1280, 720),
    "1080p": (1920, 1080),
    "4k": (3840, 2160),
}


def _parse_resolution(res_str):
    if res_str in RESOLUTIONS:
        return RESOLUTIONS[res_str]
    if "x" in str(res_str):
        parts = str(res_str).split("x")
        try:
            return (int(parts[0]), int(parts[1]))
        except ValueError:
            pass
    return RESOLUTIONS["720p"]


def register(api):

    def execute_compose(clips="[]", audio_tracks="[]",
                        transition="none", transition_duration=0.5,
                        output_fps=24, output_resolution="720p",
                        filename="", **_kw):
        try:
            from moviepy import (
                VideoFileClip, ImageClip, AudioFileClip,
                CompositeAudioClip, concatenate_videoclips,
            )
        except ImportError:
            try:
                from moviepy.editor import (
                    VideoFileClip, ImageClip, AudioFileClip,
                    CompositeAudioClip, concatenate_videoclips,
                )
            except ImportError:
                return json.dumps({
                    "status": "error",
                    "error": "Required: pip install moviepy",
                })

        import numpy as np

        if isinstance(clips, str):
            try:
                clips = json.loads(clips)
            except json.JSONDecodeError:
                return json.dumps({"status": "error", "error": "clips must be a JSON array"})
        if isinstance(audio_tracks, str):
            try:
                audio_tracks = json.loads(audio_tracks)
            except json.JSONDecodeError:
                return json.dumps({"status": "error", "error": "audio_tracks must be a JSON array"})

        if not clips:
            return json.dumps({"status": "error", "error": "At least one clip is required"})

        target_w, target_h = _parse_resolution(output_resolution)
        fps = max(1, min(int(output_fps), 60))
        t0 = time.time()

        video_clips = []
        clip_objects = []

        try:
            api.log(f"Composing video: {len(clips)} clips, {len(audio_tracks)} audio tracks...")

            for i, clip_def in enumerate(clips):
                clip_path = clip_def.get("path", "")
                if not clip_path or not Path(clip_path).exists():
                    return json.dumps({
                        "status": "error",
                        "error": f"Clip {i}: file not found: {clip_path}",
                    })

                ext = Path(clip_path).suffix.lower()
                api.log(f"Loading clip {i + 1}/{len(clips)}: {Path(clip_path).name}")

                if ext in (".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"):
                    duration = float(clip_def.get("duration", 3))
                    clip = ImageClip(clip_path).with_duration(duration)
                else:
                    clip = VideoFileClip(clip_path)

                trim_start = clip_def.get("trim_start")
                trim_end = clip_def.get("trim_end")
                if trim_start is not None or trim_end is not None:
                    start = float(trim_start) if trim_start is not None else 0
                    end = float(trim_end) if trim_end is not None else clip.duration
                    clip = clip.subclipped(start, min(end, clip.duration))

                speed = clip_def.get("speed")
                if speed and float(speed) != 1.0:
                    clip = clip.with_speed_scaled(float(speed))

                clip = clip.resized((target_w, target_h))
                clip = clip.with_fps(fps)

                clip_objects.append(clip)
                video_clips.append(clip)

            if not video_clips:
                return json.dumps({"status": "error", "error": "No valid clips loaded"})

            api.log(f"Concatenating {len(video_clips)} clips...")
            trans = str(transition).lower()
            if trans in ("crossfade", "fade_black") and len(video_clips) > 1:
                td = max(0.1, min(float(transition_duration), 2.0))
                try:
                    from moviepy.video.fx import CrossFadeIn, CrossFadeOut, FadeIn, FadeOut
                    if trans == "crossfade":
                        from moviepy import CompositeVideoClip
                        for i in range(1, len(video_clips)):
                            start_t = sum(c.duration for c in video_clips[:i]) - td * i
                            video_clips[i] = video_clips[i].with_start(start_t).with_effects([CrossFadeIn(td)])
                            video_clips[i - 1] = video_clips[i - 1].with_effects([CrossFadeOut(td)])
                        final_video = CompositeVideoClip(video_clips)
                    else:
                        faded = []
                        for c in video_clips:
                            faded.append(c.with_effects([FadeIn(td), FadeOut(td)]))
                        final_video = concatenate_videoclips(faded, method="compose")
                except (ImportError, AttributeError):
                    api.log("Transitions unavailable, concatenating without effects")
                    final_video = concatenate_videoclips(video_clips, method="compose")
            else:
                final_video = concatenate_videoclips(video_clips, method="compose")

            total_duration = final_video.duration
            api.log(f"Video duration: {total_duration:.1f}s")

            if audio_tracks:
                api.log(f"Mixing {len(audio_tracks)} audio tracks...")
                audio_layers = []

                for j, track_def in enumerate(audio_tracks):
                    track_path = track_def.get("path", "")
                    if not track_path or not Path(track_path).exists():
                        api.log(f"Warning: audio track {j} not found: {track_path}, skipping")
                        continue

                    aclip = AudioFileClip(track_path)
                    clip_objects.append(aclip)

                    volume = float(track_def.get("volume", 1.0))
                    start_at = float(track_def.get("start_at", 0))
                    should_loop = track_def.get("loop", False)

                    if should_loop and aclip.duration < total_duration:
                        repeats = int(total_duration / aclip.duration) + 1
                        from moviepy import concatenate_audioclips
                        aclip = concatenate_audioclips([aclip] * repeats)
                        clip_objects.append(aclip)

                    if aclip.duration > total_duration - start_at:
                        aclip = aclip.subclipped(0, total_duration - start_at)

                    if volume != 1.0:
                        aclip = aclip.with_volume_scaled(volume)

                    if start_at > 0:
                        aclip = aclip.with_start(start_at)

                    audio_layers.append(aclip)

                if audio_layers:
                    mixed = CompositeAudioClip(audio_layers)
                    final_video = final_video.with_audio(mixed)
                    api.log("Audio tracks merged")

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"composed_{ts}.mp4"
            out_path = Path(api.models_dir).parent / "media" / "video" / fname
            out_path.parent.mkdir(parents=True, exist_ok=True)

            api.log(f"Encoding final video ({target_w}x{target_h}, {fps}fps)...")
            final_video.write_videofile(
                str(out_path),
                fps=fps,
                codec="libx264",
                audio_codec="aac",
                logger=None,
                threads=4,
            )

            elapsed = time.time() - t0
            file_size_mb = round(out_path.stat().st_size / (1024 * 1024), 2)

            video_bytes = out_path.read_bytes()
            saved = api.save_media(
                data=video_bytes, filename=fname, media_type="video",
                prompt=f"Composed video: {len(clips)} clips, {len(audio_tracks)} audio tracks",
                params={
                    "clips": len(clips), "audio_tracks": len(audio_tracks),
                    "transition": transition, "resolution": output_resolution,
                },
                metadata={
                    "clips": len(clips), "audio_tracks": len(audio_tracks),
                    "duration_secs": round(total_duration, 2),
                    "resolution": f"{target_w}x{target_h}", "fps": fps,
                    "transition": transition, "file_size_mb": file_size_mb,
                    "elapsed_secs": round(elapsed, 2),
                },
            )

            return json.dumps({
                "status": "ok",
                "path": saved,
                "duration_secs": round(total_duration, 2),
                "resolution": f"{target_w}x{target_h}",
                "fps": fps,
                "clips_count": len(clips),
                "audio_tracks_count": len(audio_tracks),
                "file_size_mb": file_size_mb,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Video compose error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

        finally:
            for c in clip_objects:
                try:
                    c.close()
                except Exception:
                    pass

    api.register_tool({
        "name": "compose_video",
        "description": (
            "Stitch video clips and images together into a complete video with "
            "audio tracks (local). Concatenate MP4 clips and still images in "
            "sequence, overlay multiple audio layers (narration, background music, "
            "sound effects) at different volumes, add transitions (crossfade, "
            "fade_black), and export a single final MP4. No API key needed.\n\n"
            "Example: compose 3 animated clips with background music at 30% volume "
            "and narration at full volume, crossfade transitions between scenes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "clips": {
                    "type": "array",
                    "description": (
                        "List of clips in sequence. Each clip: "
                        '{\"path\": \"clip.mp4\", \"trim_start\": 0, \"trim_end\": 5, '
                        '\"speed\": 1.0, \"duration\": 3} '
                        "(duration only for still images)."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path to video (MP4) or image (PNG/JPG)."},
                            "trim_start": {"type": "number", "description": "Start time in seconds (optional)."},
                            "trim_end": {"type": "number", "description": "End time in seconds (optional)."},
                            "speed": {"type": "number", "description": "Playback speed multiplier (optional, default 1.0)."},
                            "duration": {"type": "number", "description": "Duration for still images in seconds (default 3)."},
                        },
                        "required": ["path"],
                    },
                },
                "audio_tracks": {
                    "type": "array",
                    "description": (
                        "Audio layers to mix onto the video. Each track: "
                        '{\"path\": \"audio.wav\", \"volume\": 1.0, \"start_at\": 0, \"loop\": false}'
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path to audio file (WAV/MP3)."},
                            "volume": {"type": "number", "description": "Volume multiplier 0-1 (default 1.0)."},
                            "start_at": {"type": "number", "description": "When to start this track in seconds (default 0)."},
                            "loop": {"type": "boolean", "description": "Loop audio to fill video duration (default false)."},
                        },
                        "required": ["path"],
                    },
                },
                "transition": {
                    "type": "string",
                    "enum": ["none", "crossfade", "fade_black"],
                    "description": "Transition between clips. Default: none.",
                    "default": "none",
                },
                "transition_duration": {
                    "type": "number",
                    "description": "Transition duration in seconds (default 0.5, max 2.0).",
                    "default": 0.5,
                },
                "output_fps": {"type": "integer", "description": "Output frames per second (default 24).", "default": 24},
                "output_resolution": {
                    "type": "string",
                    "description": "Output resolution: 480p, 720p, 1080p, 4k, or WxH. Default: 720p.",
                    "default": "720p",
                },
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["clips"],
        },
        "execute": execute_compose,
    })
