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
)

call_py = PyTgCalls(assistant)


def _build_stream(source_url: str, is_video: bool) -> MediaStream:
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
    src = item["source"]

    if kind == "image":
        mp4_path = await prepare_image_presentation(src)
        return mp4_path, True, item["title"], int(item.get("duration") or 0)

    if os.path.exists(src) and kind == "video":
        return src, True, item["title"], int(item.get("duration") or 0)

    if os.path.exists(src) and kind == "audio":
        return src, False, item["title"], int(item.get("duration") or 0)

    is_video = bool(item.get("is_video", False))
    info = await get_stream_info(src, video=is_video)
    return info["stream_url"], is_video, info["title"], int(info.get("duration") or 0)


async def start_or_enqueue(
    chat_id: int,
    items: list[dict],
    play_now_if_idle: bool = True,
) -> tuple[str, dict]:
    """
    Dönüş: ('playing' | 'queued', track_info)
    """
    if chat_id not in NOW_PLAYING and play_now_if_idle:
        first = items[0]
        rest = items[1:]
        if rest:
            add_to_list(chat_id, rest, top=False)

        playable, is_video, title, duration = await _resolve_playable_source(first)
        track = {
            **first,
            "title": title,
            "duration": duration,
            "playable": playable,
            "is_video": is_video,
        }
        NOW_PLAYING[chat_id] = track
        await call_py.play(chat_id, _build_stream(playable, is_video))
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
    Mevcut çağrılarla uyumluluk için tek parça/liste/link üzerinden oynatır veya sıraya alır.
    """
    mode = "video" if is_video else "audio"
    items = await resolve_input_items(query, mode=mode, requested_by=requested_by)
    return await start_or_enqueue(chat_id, items, play_now_if_idle=True)


async def skip_track(chat_id: int) -> dict | None:
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
        await call_py.play(chat_id, _build_stream(playable, is_video))
        return track

    NOW_PLAYING.pop(chat_id, None)
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass
    return None


async def stop_stream(chat_id: int):
    clear_list(chat_id)
    NOW_PLAYING.pop(chat_id, None)
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass


@call_py.on_update(filters.stream_end())
async def _on_stream_end(_, update):
    chat_id = update.chat_id
    await skip_track(chat_id)
