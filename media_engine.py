import asyncio
import os
import re
import subprocess
import uuid
from config import DOWNLOAD_DIR


def _clean_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)[:80].strip()


async def prepare_image_presentation(image_path: str, duration_sec: int = 30) -> str:
    """
    Tek görseli sesli sohbette belirtilen saniye boyunca 720p MP4 sunuma çevirir.
    """
    loop = asyncio.get_running_loop()
    out_path = os.path.splitext(image_path)[0] + "_sunum.mp4"
    safe_duration = max(5, min(int(duration_sec), 120))

    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-i", image_path,
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-preset", "veryfast",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-t", str(safe_duration),
        "-shortest",
        "-movflags", "+faststart",
        out_path,
    ]

    def _run():
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path

    return await loop.run_in_executor(None, _run)


async def prepare_slideshow_presentation(image_paths: list[str], seconds_per_slide: int = 6) -> str:
    """
    Birden fazla görseli sırayla birleştirerek 720p MP4 slayt yayını üretir.
    Render Free bellek/CPU sınırını korumak için en fazla 15 görsel işler.
    """
    if not image_paths:
        raise ValueError("Slayt oluşturmak için en az bir görsel gerekli.")

    if len(image_paths) == 1:
        return await prepare_image_presentation(image_paths[0], duration_sec=max(10, seconds_per_slide * 3))

    loop = asyncio.get_running_loop()
    uid = uuid.uuid4().hex[:8]
    list_file = os.path.join(DOWNLOAD_DIR, f"slides_{uid}.txt")
    out_path = os.path.join(DOWNLOAD_DIR, f"slayt_{uid}.mp4")
    per_slide = max(2, min(int(seconds_per_slide), 20))
    selected_images = image_paths[:15]

    def _run():
        with open(list_file, "w", encoding="utf-8") as f:
            for img in selected_images:
                safe_img = img.replace("'", "'\\''")
                f.write(f"file '{safe_img}'\n")
                f.write(f"duration {per_slide}\n")
            # concat demuxer kuralı: son görsel tekrar yazılır
            last_img = selected_images[-1].replace("'", "'\\''")
            f.write(f"file '{last_img}'\n")

        cmd = [
            "ffmpeg",
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", list_file,
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-c:a", "aac",
            "-b:a", "128k",
            "-pix_fmt", "yuv420p",
            "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
            "-shortest",
            "-movflags", "+faststart",
            out_path,
        ]
        try:
            subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        finally:
            if os.path.exists(list_file):
                os.remove(list_file)
        return out_path

    return await loop.run_in_executor(None, _run)
