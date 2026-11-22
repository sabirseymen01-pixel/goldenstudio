import asyncio
import os
import re
import subprocess
import uuid
import yt_dlp
from config import DOWNLOAD_DIR

COOKIES_FILE_PATH = "/tmp/yt_cookies.txt"
_COOKIE_LOGGED = False


def _get_cookiefile_path() -> str | None:
    global _COOKIE_LOGGED
    raw_cookies = (os.getenv("YT_COOKIES") or "").strip()
    if not raw_cookies:
        if not _COOKIE_LOGGED:
            print("[YT_COOKIES] Ortam degiskeni bos veya tanimli degil.", flush=True)
            _COOKIE_LOGGED = True
        return None

    normalized = raw_cookies.replace("\r\n", "\n").replace("\\n", "\n").strip() + "\n"
    with open(COOKIES_FILE_PATH, "w", encoding="utf-8") as f:
        f.write(normalized)

    line_count = len([ln for ln in normalized.splitlines() if ln.strip() and not ln.startswith("#")])
    if not _COOKIE_LOGGED:
        print(f"[YT_COOKIES] Dosya yazildi ({COOKIES_FILE_PATH}), gecerli satir sayisi: {line_count}", flush=True)
        _COOKIE_LOGGED = True
    return COOKIES_FILE_PATH


def _build_ydl_opts(use_cookies: bool = True) -> dict:
    opts: dict = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["mweb", "ios", "tv_embedded", "web"],
            }
        },
    }
    if use_cookies:
        cookie_path = _get_cookiefile_path()
        if cookie_path and os.path.exists(cookie_path):
            opts["cookiefile"] = cookie_path
    return opts


def _extract_with_cookie_fallback(search_query: str, extra_opts: dict, download: bool) -> tuple[dict, dict]:
    last_err: Exception | None = None
    for use_cookies in (True, False):
        ydl_opts = {
            **_build_ydl_opts(use_cookies=use_cookies),
            **extra_opts,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(search_query, download=download)
                if "entries" in info and info["entries"]:
                    info = info["entries"][0]
                return info, ydl_opts
        except Exception as exc:
            last_err = exc
            msg = str(exc)
            if "The page needs to be reloaded" not in msg and "Sign in to confirm you’re not a bot" not in msg:
                raise
    assert last_err is not None
    raise last_err


def _clean_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)[:80].strip()


async def get_stream_info(query: str, video: bool = False) -> dict:
    """
    Şarkı adı veya linkten ses/video stream bilgilerini ve doğrudan akış URL'sini çeker.
    """
    loop = asyncio.get_running_loop()
    search_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    extra_opts = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best" if video else "bestaudio/best",
        "extract_flat": False,
    }

    def _extract():
        info, _ = _extract_with_cookie_fallback(search_query, extra_opts, download=False)
        return {
            "title": info.get("title", "Bilinmeyen Parça"),
            "duration": info.get("duration", 0),
            "webpage_url": info.get("webpage_url", query),
            "stream_url": info.get("url"),
            "thumbnail": info.get("thumbnail"),
            "uploader": info.get("uploader", "Golden Studio"),
        }

    return await loop.run_in_executor(None, _extract)


async def prepare_image_presentation(image_path: str) -> str:
    """
    Görseli sesli sohbette kesintisiz video yayını olarak açmak için döngülü MP4 sunuma çevirir.
    """
    loop = asyncio.get_running_loop()
    out_path = os.path.splitext(image_path)[0] + "_sunum.mp4"

    cmd = [
        "ffmpeg",
        "-y",
        "-loop", "1",
        "-i", image_path,
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=48000",
        "-c:v", "libx264",
        "-tune", "stillimage",
        "-c:a", "aac",
        "-b:a", "128k",
        "-pix_fmt", "yuv420p",
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2",
        "-shortest",
        "-movflags", "+faststart",
        out_path,
    ]

    def _run():
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return out_path

    return await loop.run_in_executor(None, _run)


async def download_media(query: str, mode: str = "mp3") -> dict:
    """
    mode: 'mp3' veya 'mp4'
    Dosyayı indirir ve yolunu + başlığını döndürür.
    """
    loop = asyncio.get_running_loop()
    search_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"
    uid = uuid.uuid4().hex[:8]
    out_tmpl = os.path.join(DOWNLOAD_DIR, f"{uid}_%(title).60s.%(ext)s")

    if mode == "mp3":
        extra_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }
    else:
        extra_opts = {
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
        }

    def _download():
        info, used_opts = _extract_with_cookie_fallback(search_query, extra_opts, download=True)
        with yt_dlp.YoutubeDL(used_opts) as ydl:
            raw_path = ydl.prepare_filename(info)
        if mode == "mp3":
            file_path = os.path.splitext(raw_path)[0] + ".mp3"
        else:
            file_path = os.path.splitext(raw_path)[0] + ".mp4"
            if not os.path.exists(file_path) and os.path.exists(raw_path):
                file_path = raw_path
        return {
            "file_path": file_path,
            "title": _clean_filename(info.get("title", "Golden_Studio_Media")),
            "duration": int(info.get("duration") or 0),
            "performer": info.get("uploader", "Golden Studio"),
        }

    return await loop.run_in_executor(None, _download)
