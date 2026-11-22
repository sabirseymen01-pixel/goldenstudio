import asyncio
import os
import re
import uuid
import yt_dlp
from config import DOWNLOAD_DIR


def _clean_filename(name: str) -> str:
    return re.sub(r'[\\/*?:"<>|]', "", name)[:80].strip()


async def get_stream_info(query: str, video: bool = False) -> dict:
    """
    Şarkı adı veya linkten ses/video stream bilgilerini ve doğrudan akış URL'sini çeker.
    """
    loop = asyncio.get_running_loop()
    search_query = query if query.startswith(("http://", "https://")) else f"ytsearch1:{query}"

    ydl_opts = {
        "format": "best[height<=720][ext=mp4]/best[height<=720]/best" if video else "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "extract_flat": False,
    }

    def _extract():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=False)
            if "entries" in info:
                info = info["entries"][0]
            return {
                "title": info.get("title", "Bilinmeyen Parça"),
                "duration": info.get("duration", 0),
                "webpage_url": info.get("webpage_url", query),
                "stream_url": info.get("url"),
                "thumbnail": info.get("thumbnail"),
                "uploader": info.get("uploader", "Golden Studio"),
            }

    return await loop.run_in_executor(None, _extract)


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
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": out_tmpl,
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
        }
    else:
        ydl_opts = {
            "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best",
            "outtmpl": out_tmpl,
            "merge_output_format": "mp4",
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }

    def _download():
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(search_query, download=True)
            if "entries" in info:
                info = info["entries"][0]
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
