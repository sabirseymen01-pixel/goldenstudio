import asyncio
from collections import deque
from pyrogram import Client
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from config import API_ID, API_HASH, SESSION_STRING
from media_engine import get_stream_info

assistant = Client(
    name="golden_assistant",
    api_id=API_ID,
    api_hash=API_HASH,
    session_string=SESSION_STRING,
)

call_py = PyTgCalls(assistant)

# chat_id -> deque([track_dict, ...])
QUEUES: dict[int, deque] = {}
NOW_PLAYING: dict[int, dict] = {}


def _build_stream(stream_url: str, is_video: bool) -> MediaStream:
    if is_video:
        return MediaStream(
            stream_url,
            audio_parameters=AudioQuality.HIGH,
            video_parameters=VideoQuality.HD_720p,
        )
    return MediaStream(
        stream_url,
        audio_parameters=AudioQuality.STUDIO,
        video_flags=MediaStream.Flags.IGNORE,
    )


async def play_or_queue(chat_id: int, query: str, is_video: bool, requested_by: str) -> tuple[str, dict]:
    """
    Dönüş: ('playing' | 'queued', track_info)
    """
    info = await get_stream_info(query, video=is_video)
    track = {
        "title": info["title"],
        "duration": info["duration"],
        "stream_url": info["stream_url"],
        "webpage_url": info["webpage_url"],
        "is_video": is_video,
        "requested_by": requested_by,
    }

    if chat_id not in QUEUES:
        QUEUES[chat_id] = deque()

    if chat_id in NOW_PLAYING:
        QUEUES[chat_id].append(track)
        return "queued", track

    NOW_PLAYING[chat_id] = track
    stream = _build_stream(track["stream_url"], is_video)
    await call_py.play(chat_id, stream)
    return "playing", track


async def skip_track(chat_id: int) -> dict | None:
    if chat_id in QUEUES and QUEUES[chat_id]:
        next_track = QUEUES[chat_id].popleft()
        NOW_PLAYING[chat_id] = next_track
        stream = _build_stream(next_track["stream_url"], next_track["is_video"])
        await call_py.play(chat_id, stream)
        return next_track

    NOW_PLAYING.pop(chat_id, None)
    await call_py.leave_call(chat_id)
    return None


async def stop_stream(chat_id: int):
    QUEUES.pop(chat_id, None)
    NOW_PLAYING.pop(chat_id, None)
    try:
        await call_py.leave_call(chat_id)
    except Exception:
        pass


@call_py.on_update(filters.stream_end())
async def _on_stream_end(_, update):
    chat_id = update.chat_id
    await skip_track(chat_id)
