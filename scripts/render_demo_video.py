#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SCENES = ROOT / "video" / "scenes.json"
DECK = ROOT / "video" / "pitch-deck.html"
OUTPUT = ROOT / "dist" / "skillweave-demo-5min.mp4"
SUBTITLES = ROOT / "dist" / "skillweave-demo-5min.zh-TW.srt"
REPORT = ROOT / "reports" / "demo-video.json"
CHROME = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")


def command(args: list[str], *, capture: bool = False) -> str:
    result = subprocess.run(
        args,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout if capture else ""


def media_duration(path: Path) -> float:
    return float(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            capture=True,
        ).strip()
    )


def timestamp(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    hours, milliseconds = divmod(milliseconds, 3_600_000)
    minutes, milliseconds = divmod(milliseconds, 60_000)
    secs, milliseconds = divmod(milliseconds, 1000)
    return f"{hours:02}:{minutes:02}:{secs:02},{milliseconds:03}"


def ensure_demo() -> subprocess.Popen[str] | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=2):
            return None
    except Exception:
        process = subprocess.Popen(
            ["python3", "-m", "app.server", "--port", "8080"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for _ in range(30):
            try:
                with urllib.request.urlopen(
                    "http://127.0.0.1:8080/health", timeout=2
                ):
                    return process
            except Exception:
                time.sleep(0.25)
        process.terminate()
        raise RuntimeError("local demo did not become healthy")


def chrome_screenshot(url: str, output: Path) -> None:
    command(
        [
            str(CHROME),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            "--allow-file-access-from-files",
            "--window-size=1920,1080",
            "--force-device-scale-factor=1",
            "--virtual-time-budget=5000",
            f"--screenshot={output}",
            url,
        ]
    )


def main() -> None:
    scenes: list[dict[str, Any]] = json.loads(SCENES.read_text(encoding="utf-8"))
    if round(sum(float(scene["duration"]) for scene in scenes)) != 300:
        raise ValueError("scene durations must total exactly 300 seconds")
    if not CHROME.is_file():
        raise FileNotFoundError(f"Chrome not found at {CHROME}")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    started_demo = ensure_demo()
    try:
        with tempfile.TemporaryDirectory(prefix="skillweave-video-") as directory:
            build = Path(directory)
            scene_videos: list[Path] = []
            subtitle_rows: list[str] = []
            elapsed = 0.0
            for index, scene in enumerate(scenes, 1):
                duration = float(scene["duration"])
                image = build / f"scene-{index:02}.png"
                if int(scene["slide"]) == 4:
                    chrome_screenshot("http://127.0.0.1:8080", image)
                else:
                    chrome_screenshot(
                        DECK.as_uri() + f"?slide={int(scene['slide'])}",
                        image,
                    )

                narration = build / f"scene-{index:02}.aiff"
                command(
                    [
                        "say",
                        "-v",
                        "Meijia",
                        "-r",
                        "190",
                        "-o",
                        str(narration),
                        str(scene["narration"]),
                    ]
                )
                narration_duration = media_duration(narration)
                audio_filter = (
                    f"atempo={narration_duration / (duration - 1):.6f},"
                    if narration_duration > duration - 1
                    else ""
                )
                audio_filter += (
                    f"apad,atrim=0:{duration},"
                    f"afade=t=in:st=0:d=0.35,"
                    f"afade=t=out:st={duration - 0.5}:d=0.5"
                )
                video = build / f"scene-{index:02}.mp4"
                command(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-loop",
                        "1",
                        "-framerate",
                        "30",
                        "-i",
                        str(image),
                        "-i",
                        str(narration),
                        "-vf",
                        (
                            f"fade=t=in:st=0:d=0.35,"
                            f"fade=t=out:st={duration - 0.5}:d=0.5,"
                            "format=yuv420p"
                        ),
                        "-af",
                        audio_filter,
                        "-t",
                        str(duration),
                        "-r",
                        "30",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-crf",
                        "20",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "160k",
                        "-shortest",
                        str(video),
                    ]
                )
                scene_videos.append(video)
                subtitle_rows.extend(
                    [
                        str(index),
                        f"{timestamp(elapsed)} --> {timestamp(elapsed + duration - 0.1)}",
                        str(scene["caption"]),
                        "",
                    ]
                )
                elapsed += duration

            concat_file = build / "concat.txt"
            concat_file.write_text(
                "".join(f"file '{video}'\n" for video in scene_videos),
                encoding="utf-8",
            )
            joined = build / "joined.mp4"
            command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-f",
                    "concat",
                    "-safe",
                    "0",
                    "-i",
                    str(concat_file),
                    "-c",
                    "copy",
                    str(joined),
                ]
            )
            SUBTITLES.write_text("\n".join(subtitle_rows), encoding="utf-8")
            command(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(joined),
                    "-i",
                    str(SUBTITLES),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a:0",
                    "-map",
                    "1:0",
                    "-c:v",
                    "copy",
                    "-c:a",
                    "copy",
                    "-c:s",
                    "mov_text",
                    "-metadata:s:s:0",
                    "language=zho",
                    "-metadata",
                    "title=SkillWeave AWS x 1111 Hackathon Demo",
                    str(OUTPUT),
                ]
            )
    finally:
        if started_demo is not None:
            started_demo.terminate()
            started_demo.wait(timeout=5)

    probe = json.loads(
        command(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(OUTPUT),
            ],
            capture=True,
        )
    )
    duration = float(probe["format"]["duration"])
    streams = probe["streams"]
    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
    checks = {
        "duration_five_minutes": 299.0 <= duration <= 301.0,
        "full_hd": (
            int(video_stream["width"]) == 1920
            and int(video_stream["height"]) == 1080
        ),
        "h264_video": video_stream["codec_name"] == "h264",
        "audio_present": any(
            stream["codec_type"] == "audio" for stream in streams
        ),
        "embedded_zh_subtitles": any(
            stream["codec_type"] == "subtitle"
            and stream.get("tags", {}).get("language") == "zho"
            for stream in streams
        ),
        "eight_scenes": len(scenes) == 8,
    }
    report = {
        "metadata": {
            "schema": "skillweave-demo-video-v1",
            "generated_at_utc": datetime.now(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "language": "zh-TW",
            "release": "skillweave-2026.07.28-rc5",
        },
        "passed": all(checks.values()),
        "checks": checks,
        "artifact": {
            "path": "dist/skillweave-demo-5min.mp4",
            "subtitle_sidecar": "dist/skillweave-demo-5min.zh-TW.srt",
            "duration_seconds": duration,
            "size_bytes": OUTPUT.stat().st_size,
            "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest(),
        },
        "external_url_registered": False,
    }
    REPORT.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
