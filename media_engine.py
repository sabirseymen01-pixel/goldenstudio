import asyncio
import os
import tempfile
from config import DOWNLOAD_DIR

STREAM_WIDTH = int(os.getenv("STREAM_WIDTH", "1920"))
STREAM_HEIGHT = int(os.getenv("STREAM_HEIGHT", "1080"))
STREAM_FPS = int(os.getenv("STREAM_FPS", "24"))


def _ensure_out_dir() -> str:
    out_dir = os.path.join(DOWNLOAD_DIR, "renders")
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


async def _run_ffmpeg(args: list[str], timeout_sec: int = 180) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise RuntimeError("FFmpeg render zaman asimina ugradi.")

    if proc.returncode != 0:
        err_tail = (stderr.decode("utf-8", errors="ignore") or "").strip().splitlines()[-5:]
        raise RuntimeError("FFmpeg hatasi: " + " | ".join(err_tail))


async def prepare_image_presentation(image_path: str, duration_sec: int = 30) -> str:
    """
    Tek gorselden akici MP4 sunum videosu uretir.
    """
    if not image_path or not os.path.exists(image_path):
        raise FileNotFoundError("Gorsel dosyasi bulunamadi.")

    dur = max(5, min(int(duration_sec or 30), 600))
    out_path = os.path.join(
        _ensure_out_dir(),
        f"pres_{os.path.splitext(os.path.basename(image_path))[0]}_{dur}s.mp4",
    )
    vf = (
        f"scale={STREAM_WIDTH}:{STREAM_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={STREAM_WIDTH}:{STREAM_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={STREAM_FPS},format=yuv420p"
    )
    await _run_ffmpeg(
        [
            "-loop", "1",
            "-i", image_path,
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-vf", vf,
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-tune", "stillimage",
            "-b:v", "2200k",
            "-maxrate", "2500k",
            "-bufsize", "4000k",
            "-c:a", "aac",
            "-b:a", "128k",
            "-t", str(dur),
            "-shortest",
            "-movflags", "+faststart",
            out_path,
        ]
    )
    return out_path


async def prepare_slideshow_presentation(image_paths: list[str], seconds_per_slide: int = 6) -> str:
    """
    Birden fazla gorselden concat demuxer ile akici slayt MP4 uretir.
    """
    valid = [p for p in (image_paths or []) if p and os.path.exists(p)]
    if not valid:
        raise FileNotFoundError("Slayt icin gecerli gorsel bulunamadi.")

    per_slide = max(2, min(int(seconds_per_slide or 6), 30))
    out_dir = _ensure_out_dir()
    out_path = os.path.join(out_dir, f"slide_{abs(hash(tuple(valid)))}_{len(valid)}p.mp4")

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as tf:
        concat_file = tf.name
        for img in valid[:25]:
            safe_img = img.replace("'", "'\\''")
            tf.write(f"file '{safe_img}'\n")
            tf.write(f"duration {per_slide}\n")
        # concat demuxer son kare icin bir kez daha dosya ister
        last_img = valid[:25][-1].replace("'", "'\\''")
        tf.write(f"file '{last_img}'\n")

    vf = (
        f"scale={STREAM_WIDTH}:{STREAM_HEIGHT}:force_original_aspect_ratio=decrease,"
        f"pad={STREAM_WIDTH}:{STREAM_HEIGHT}:(ow-iw)/2:(oh-ih)/2:black,"
        f"fps={STREAM_FPS},format=yuv420p"
    )
    try:
        await _run_ffmpeg(
            [
                "-f", "concat",
                "-safe", "0",
                "-i", concat_file,
                "-f", "lavfi",
                "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
                "-vf", vf,
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-b:v", "2200k",
                "-maxrate", "2500k",
                "-bufsize", "4000k",
                "-c:a", "aac",
                "-b:a", "128k",
                "-shortest",
                "-movflags", "+faststart",
                out_path,
            ],
            timeout_sec=240,
        )
    finally:
        try:
            if os.path.exists(concat_file):
                os.remove(concat_file)
        except OSError:
            pass

    return out_path
