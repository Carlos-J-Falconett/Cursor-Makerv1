"""Turn a video clip into animation frames with the system ffmpeg.

Frames are written as PNGs to ~/.cache/cursor-maker/video/<hash>/ and stay
there, because saved projects reference them. The hash covers the video path,
its modification time and the extraction settings, so re-importing the same
clip with the same settings reuses the frames.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess

CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
                         "cursor-maker", "video")
MAX_SIDE = 1280     # frames are scaled down to this long side (keeps memory in check)


def available() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def probe(path: str) -> tuple[float, float]:
    """(duration in seconds, frames per second) of a video file."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "format=duration:stream=r_frame_rate,duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "ffprobe failed")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No video stream found")
    duration = float(data.get("format", {}).get("duration") or
                     streams[0].get("duration") or 0)
    num, _, den = (streams[0].get("r_frame_rate") or "0/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 0.0
    return duration, fps


def extract_frames(path: str, fps: float, start: float, length: float, max_frames: int,
                   progress_cb=None) -> list[str]:
    """Extract up to max_frames PNG frames sampled at `fps` from start..start+length.

    progress_cb(frames_done, frames_expected) is called while ffmpeg runs.
    Returns the frame paths in order.
    """
    key = f"{os.path.abspath(path)}|{os.path.getmtime(path)}|{fps}|{start}|{length}|{max_frames}"
    out_dir = os.path.join(CACHE_DIR, hashlib.sha1(key.encode()).hexdigest()[:16])
    done_flag = os.path.join(out_dir, ".complete")
    if not os.path.exists(done_flag):
        shutil.rmtree(out_dir, ignore_errors=True)
        os.makedirs(out_dir)
        expected = max(1, min(max_frames, round(fps * length)))
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
               "-ss", str(start), "-t", str(length), "-i", path,
               # sample at the chosen rate, shrink big videos, keep alpha if any
               "-vf", f"fps={fps},scale='min({MAX_SIDE},iw)':'min({MAX_SIDE},ih)'"
                      ":force_original_aspect_ratio=decrease",
               "-frames:v", str(max_frames), "-progress", "pipe:1",
               os.path.join(out_dir, "%05d.png")]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        for line in proc.stdout:
            m = re.match(r"frame=(\d+)", line)
            if m and progress_cb:
                progress_cb(int(m.group(1)), expected)
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(err.strip() or "ffmpeg failed")
        open(done_flag, "w").close()
    frames = sorted(f for f in os.listdir(out_dir) if f.endswith(".png"))
    if not frames:
        raise RuntimeError("No frames were extracted (check start time and length)")
    return [os.path.join(out_dir, f) for f in frames]
