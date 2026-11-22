import asyncio
import os
import random
from collections import defaultdict
from pyrogram import Client
from pytgcalls import PyTgCalls, filters
from pytgcalls.types import MediaStream, AudioQuality, VideoQuality
from config import API_ID, API_HASH, SESSION_STRING, DOWNLOAD_DIR
from media_engine import (
    prepare_image_presentation,
    prepare_slideshow_presentation,
)
from playlist_manager import (
    LISTS,
    NOW_PLAYING,
    add_to_list,
    pop_next,
    clear_list,
    resolve_input_items,
    save_track_to_catalog,
    clean_track_title,
)

assistant: Client | None = None
call_py: PyTgCalls | None = None
_CHAT_LOCKS: dict[int, asyncio.Lock] = {}
_LAST_ATTEMPT: dict[int, float] = defaultdict(float)

_USE_1080 = os.getenv("STREAM_HEIGHT", "720").strip() == "1080"
_VIDEO_QUALITY = (
    getattr(VideoQuality, "HD_1080p", getattr(VideoQuality, "FHD_1080p", VideoQuality.HD_720p))
    if _USE_1080
    else VideoQuality.HD_720p
)


def _get_lock(chat_id: int) -> asyncio.Lock:
    lock = _CHAT_LOCKS.get(chat_id)
    if lock is None:
        lock = asyncio.Lock()
        _CHAT_LOCKS[chat_id] = lock
    return lock


def _cleanup_track_files(track: dict | None) -> None:
    if not track:
        return
    for key in ("playable", "source"):
        path = track.get(key) or ""
        if not path or not os.path.isfile(path):
            continue
        norm = os.path.abspath(path)
        renders_dir = os.path.abspath(os.path.join(DOWNLOAD_DIR, "renders"))
        dl_dir = os.path.abspath(DOWNLOAD_DIR)
        if norm.startswith(renders_dir) or (norm.startswith(dl_dir) and os.path.basename(norm).startswith("tg_")):
            try:
                os.remove(norm)
            except OSError:
                pass


async def init_player() -> None:
    global assistant, call_py
    if assistant is None:
        assistant = Client(
            name="golden_assistant",
            api_id=API_ID,
            api_hash=API_HASH,
            session_string=SESSION_STRING,
            in_memory=True,
        )
    if call_py is None:
        call_py = PyTgCalls(assistant)

    await assistant.start()
    await call_py.start()

    @call_py.on_update(filters.stream_end())
    async def _on_stream_end(_, update):
        chat_id = getattr(update, "chat_id", None)
        if chat_id is not None:
            await skip_track(chat_id)


async def run_protected_call(
    chat_id: int,
    coro_factory,
    *,
    cooldown_sec: float = 3.0,
    base_backoff: float = 5.0,
    max_backoff: float = 60.0,
    max_retries: int = 3,
):
    loop = asyncio.get_running_loop()
    now = loop.time()
    last = _LAST_ATTEMPT.get(chat_id, 0.0)
    elapsed = now - last
    if elapsed < cooldown_sec:
        await asyncio.sleep(cooldown_sec - elapsed)

    attempt = 0
    while True:
        _LAST_ATTEMPT[chat_id] = loop.time()
        try:
            return await coro_factory()
        except Exception as exc:
            msg = str(exc).lower()
            is_conflict = "409" in msg or "conflict" in msg
            is_flood = "429" in msg or "flood" in msg or "socket.send" in msg

            if not (is_conflict or is_flood) or attempt >= max_retries:
                raise

            delay = min(max_backoff, base_backoff * (2 ** attempt)) + random.uniform(0.5, 2.0)
            if is_flood:
                delay = max(delay, 12.0)

            attempt += 1
            await asyncio.sleep(delay)


def _build_stream(source_url: str, is_video: bool) -> MediaStream:
    if not source_url:
        raise ValueError("Gecerli bir yayin kaynagi bulunamadi.")
    if is_video:
        return MediaStream(
            source_url,
            audio_parameters=AudioQuality.STUDIO,
            video_parameters=_VIDEO_QUALITY,
        )
    return MediaStream(
        source_url,
        audio_parameters=AudioQuality.STUDIO,
        video_flags=MediaStream.Flags.IGNORE,
    )


async def _resolve_playable_source(item: dict) -> tuple[str, bool, str, int]:
    kind = item.get("kind", "audio")
    src = item.get("source", "")
    title = clean_track_title(item.get("title"), fallback="Bilinmeyen Parca")
    duration = int(item.get("duration") or 0)

    if kind == "slideshow":
        sources = item.get("sources") or ([src] if src else [])
        existing = [p for p in sources if p and os.path.exists(p)]
        if not existing:
            raise FileNotFoundError("Slayt olusturmak icin gecerli gorsel dosyasi bulunamadi.")
        sec_per_slide = int(item.get("seconds_per_slide") or 6)
        mp4_path = await prepare_slideshow_presentation(existing, seconds_per_slide=sec_per_slide)
        est_duration = duration or (len(existing[:25]) * max(2, min(sec_per_slide, 30)))
        return mp4_path, True, title, est_duration

    if kind == "image":
        if not src or not os.path.exists(src):
            raise FileNotFoundError("Sunum icin gorsel dosyasi bulunamadi.")
        duration_sec = int(item.get("duration_sec") or 30)
        mp4_path = await prepare_image_presentation(src, duration_sec=duration_sec)
        return mp4_path, True, title, duration or duration_sec

    if src and os.path.exists(src) and kind == "video":
        return src, True, title, duration

    if src and os.path.exists(src):
        return src, False, title, duration

    raise FileNotFoundError("Yerel medya dosyasi bulunamadi.")


async def start_or_enqueue(
    chat_id: int,
    items: list[dict],
    play_now_if_idle: bool = True,
) -> tuple[str, dict]:
    if call_py is None:
        raise RuntimeError("PyTgCalls henuz baslatilmadi.")
    if not items:
        raise ValueError("Oynatilacak veya listeye eklenecek oge bulunamadi.")

    prepared_items: list[dict] = []
    for it in items:
        copied = dict(it)
        copied["title"] = clean_track_title(copied.get("title"), fallback="Parca")
        if copied.get("kind") == "audio" and copied.get("source"):
            save_track_to_catalog(copied)
        prepared_items.append(copied)

    async with _get_lock(chat_id):
        if chat_id not in NOW_PLAYING and play_now_if_idle:
            first = prepared_items[0]
            rest = prepared_items[1:]

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
                stream = _build_stream(playable, is_video)
                await run_protected_call(chat_id, lambda: call_py.play(chat_id, stream))
            except Exception:
                _cleanup_track_files(track)
                NOW_PLAYING.pop(chat_id, None)
                raise

            if rest:
                add_to_list(chat_id, rest, top=False)
            return "playing", track

        total = add_to_list(chat_id, prepared_items, top=False)
        preview = dict(prepared_items[0])
        preview["queue_position"] = total
        return "queued", preview


async def play_or_queue(
    chat_id: int,
    query: str,
    is_video: bool,
    requested_by: str,
    custom_title: str | None = None,
) -> tuple[str, dict]:
    mode = "video" if is_video else "audio"
    items = await resolve_input_items(query, mode=mode, requested_by=requested_by, custom_title=custom_title)
    return await start_or_enqueue(chat_id, items, play_now_if_idle=True)


async def skip_track(chat_id: int) -> dict | None:
    if call_py is None:
        return None
    async with _get_lock(chat_id):
        prev = NOW_PLAYING.pop(chat_id, None)
        _cleanup_track_files(prev)

        while True:
            nxt = pop_next(chat_id)
            if not nxt:
                break
            try:
                playable, is_video, title, duration = await _resolve_playable_source(nxt)
                track = {
                    **nxt,
                    "title": title,
                    "duration": duration,
                    "playable": playable,
                    "is_video": is_video,
                }
                NOW_PLAYING[chat_id] = track
                stream = _build_stream(playable, is_video)
                await run_protected_call(chat_id, lambda: call_py.play(chat_id, stream))
                return track
            except Exception:
                _cleanup_track_files(nxt)
                NOW_PLAYING.pop(chat_id, None)
                continue

        try:
            await run_protected_call(
                chat_id,
                lambda: call_py.leave_call(chat_id),
                cooldown_sec=2.0,
                max_retries=1,
            )
        except Exception:
            pass
        return None


async def stop_stream(chat_id: int):
    async with _get_lock(chat_id):
        prev = NOW_PLAYING.pop(chat_id, None)
        _cleanup_track_files(prev)
        for queued_item in LISTS.get(chat_id, []):
            _cleanup_track_files(queued_item)
        clear_list(chat_id)
        if call_py is not None:
            try:
                await run_protected_call(
                    chat_id,
                    lambda: call_py.leave_call(chat_id),
                    cooldown_sec=2.0,
                    max_retries=1,
                )
            except Exception:
                pass


async def pause_stream(chat_id: int):
    if call_py is None:
        raise RuntimeError("PyTgCalls aktif degil.")
    if hasattr(call_py, "pause"):
        await call_py.pause(chat_id)
    else:
        await call_py.pause_stream(chat_id)


async def resume_stream(chat_id: int):
    if call_py is None:
        raise RuntimeError("PyTgCalls aktif degil.")
    if hasattr(call_py, "resume"):
        await call_py.resume(chat_id)
    else:
        await call_py.resume_stream(chat_id)
