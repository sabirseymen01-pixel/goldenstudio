import asyncio
import os
from pyrogram import Client
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from config import API_ID, API_HASH, SESSION_STRING
from media_engine import get_stream_info, prepare_image_presentation
from playlist_manager import (
    LISTS,
    NOW_PLAYING,
    add_to_list,
    pop_next,
    clear_list,
    resolve_input_items,
)

assistant = Client(
    name="golden_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
    in_memory=True,
)

call_py = PyTgCalls(assistant)
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}


def _get_lock(chat_id: int) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[chat_id] = lock
    return lock


def _build_stream(source_url: str, is_video: bool) -> MediaStream:
    if not source_url:
        raise ValueError("Geçerli bir yayın kaynağı (URL veya dosya yolu) bulunamadı.")
    if is_video:
        return MediaStream(
            source_url,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
        )
    return MediaStream(
        source_url,
        audio_parameters=AudioQuality.STUDIO,
        video_flags=MediaStream.Flags.IGNORE,
    )


async def _resolve_playable_source(item: dict) -> tuple[str, bool, str, int]:
    """
    Dönüş: (playable_url_or_path, is_video, title, duration)
    """
    kind = item.get("kind", "audio")
    src = item.get("source", "")
    title = item.get("title", "Bilinmeyen Parça")
    duration = int(item.get("duration") or 0)

    if kind == "image":
        if not src or not os.path.exists(src):
            raise FileNotFoundError("Sunum için görsel dosyası bulunamadı.")
        mp4_path = await prepare_image_presentation(src)
        return mp4_path, True, title, duration

    if src and os.path.exists(src) and kind == "video":
        return src, True, title, duration

    if src and os.path.exists(src) and kind == "audio":
        return src, False, title, duration

    is_video = bool(item.get("is_video", False))
    info = await get_stream_info(src, video=is_video)
    stream_url = info.get("stream_url")
    if not stream_url:
        raise RuntimeError("Akış bağlantısı alınamadı (stream_url boş).")
    return stream_url, is_video, info.get("title") or title, int(info.get("duration") or duration)


async def start_or_enqueue(
    chat_id: int,
    items: list[dict],
    play_now_if_idle: bool = True,
) -> tuple[str, dict]:
    """
    Dönüş: ('playing' | 'queued', track_info)
    """
    if not items:
        raise ValueError("Oynatılacak veya listeye eklenecek öğe bulunamadı.")

    async with _get_lock(chat_id):
        if chat_id not in NOW_PLAYING and play_now_if_idle:
            first = items[0]
            rest = items[1:]

            playable, is_video, title, duration = await _resolve_playable_source(first)
            track = {
                **first,
                "title": title,
                "duration": duration,
                "playable": playable,
                "is_video": is_video,
            }
            NOW_PLAYING[chat_id] = track
            try:
                await call_py.play(chat_id, _build_stream(playable, is_video))
            except Exception:
                NOW_PLAYING.pop(chat_id, None)
                raise

            if rest:
                add_to_list(chat_id, rest, top=False)
            return "playing", track

        total = add_to_list(chat_id, items, top=False)
        preview = dict(items[0])
        preview["queue_position"] = total
        return "queued", preview


async def play_or_queue(
    chat_id: int,
    query: str,
    is_video: bool,
    requested_by: str,
) -> tuple[str, dict]:
    """
    Tek parça, liste veya bağlantı üzerinden yayın başlatır ya da listeye ekler.
    """
    mode = "video" if is_video else "audio"
    items = await resolve_input_items(query, mode=mode, requested_by=requested_by)
    return await start_or_enqueue(chat_id, items, play_now_if_idle=True)


async def skip_track(chat_id: int) -> dict | None:
    async with _get_lock(chat_id):
        nxt = pop_next(chat_id)
        if nxt:
            playable, is_video, title, duration = await _resolve_playable_source(nxt)
            track = {
                **nxt,
                "title": title,
                "duration": duration,
                "playable": playable,
                "is_video": is_video,
            }
            NOW_PLAYING[chat_id] = track
            try:
                await call_py.play(chat_id, _build_stream(playable, is_video))
            except Exception:
                NOW_PLAYING.pop(chat_id, None)
                raise
            return track

        NOW_PLAYING.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass
        return None


async def stop_stream(chat_id: int):
    async with _get_lock(chat_id):
        clear_list(chat_id)
        NOW_PLAYING.pop(chat_id, None)
        try:
            await call_py.leave_call(chat_id)
        except Exception:
            pass


async def pause_stream(chat_id: int):
    if hasattr(call_py, "pause"):
        await call_py.pause(chat_id)
    else:
        await call_py.pause_stream(chat_id)


async def resume_stream(chat_id: int):
    if hasattr(call_py, "resume"):
        await call_py.resume(chat_id)
    else:
        await call_py.resume_stream(chat_id)


@call_py.on_update(filters.stream_end())
async def _on_stream_end(_, update):
    chat_id = getattr(update, "chat_id", None)
    if chat_id is not None:
        await skip_track(chat_id)
