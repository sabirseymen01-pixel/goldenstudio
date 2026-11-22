import asyncio
import inspect
import os
import time
from typing import Any
import aiohttp
from aiohttp import web
from config import BOT_TOKEN, PORT, DOWNLOAD_DIR
from media_engine import prepare_slideshow_presentation
from player import (
    init_player,
    play_or_queue,
    start_or_enqueue,
    skip_track,
    stop_stream,
    pause_stream,
    resume_stream,
    NOW_PLAYING,
)
from playlist_manager import (
    LISTS,
    resolve_input_items,
    reorder_list,
    get_list,
)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
USER_MODE: dict[int, str] = {}
SLIDE_COLLECTORS: dict[tuple[int, int], list[str]] = {}

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}

# Tekrar / spam korumasi
_ACTION_COOLDOWN_SEC = 2.0
_LAST_ACTION_TS: dict[tuple[int, int | str, str], float] = {}
_ACTIVE_ACTION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _is_duplicate_action(chat_id: int, actor_id: int | str | None, scope: str, cooldown: float = _ACTION_COOLDOWN_SEC) -> bool:
    key = (chat_id, actor_id if actor_id is not None else "anon", scope)
    now = time.monotonic()
    prev = _LAST_ACTION_TS.get(key, 0.0)
    if (now - prev) < cooldown:
        return True
    _LAST_ACTION_TS[key] = now
    return False


def _get_scope_lock(chat_id: int, scope: str) -> asyncio.Lock:
    key = (chat_id, scope)
    lock = _ACTIVE_ACTION_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _ACTIVE_ACTION_LOCKS[key] = lock
    return lock


def _detect_media_kind(msg: dict[str, Any]) -> str | None:
    if msg.get("photo"):
        return "photo"
    media_obj = msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document")
    if not media_obj:
        return None
    mime = (media_obj.get("mime_type") or "").lower()
    file_name = (media_obj.get("file_name") or "").lower()
    ext = os.path.splitext(file_name)[1]
    if bool(msg.get("video")) or mime.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if bool(msg.get("audio") or msg.get("voice")) or mime.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
    return None


ALL_COMMANDS = {
    "start", "studio", "baslat",
    "play", "oynat",
    "vplay", "video",
    "sunum", "slayt",
    "liste", "sirala",
    "pause", "duraklat",
    "resume", "devam",
    "skip", "atla",
    "stop", "dur",
}

WELCOME_TEXT = (
    "✨ <b>GOLDEN STUDIO — VIP MEDYA & YAYIN MERKEZİ</b> ✨\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Hoş geldiniz. <b>Golden Studio</b>, grubunuz için doğrudan gönderilen veya yanıtlanan "
    "ses/video dosyaları, tek görsel <code>/sunum</code> ve çoklu görsel <code>/slayt</code> "
    "yayınlarıyla sesli sohbette çalışır.\n\n"
    "🎛️ <b>Neler Yapabilirsiniz?</b>\n"
    "• <b>Doğrudan veya Yanıtla Ses/Video:</b> MP3/MP4 gönderin veya mesaja yanıt verip <code>/oynat</code> / <code>/video</code> yazın.\n"
    "• <b>Resim Sunumu & Slayt:</b> Görsele yanıt verip <code>/sunum</code> veya <code>/slayt</code> kullanın.\n"
    "• <b>Üstten Aşağı Liste:</b> <code>/liste</code> ve <code>/sirala</code> ile sırayı düzenleyin.\n\n"
    "👇 <i>Başlamak için aşağıdaki butonlardan birini seçin veya doğrudan dosya/fotoğraf gönderin:</i>"
)


def main_menu_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🎵 Ses / Dosya Yayını", "callback_data": "mode_audio"},
                {"text": "🎬 Video Yayını", "callback_data": "mode_video"},
            ],
            [
                {"text": "🖼️ Resim Sunumu", "callback_data": "mode_image"},
                {"text": "🎞️ Slayt Başlat", "callback_data": "mode_slide"},
            ],
            [
                {"text": "📋 Liste & Sırala", "callback_data": "panel_list"},
                {"text": "🎛️ Canlı Kontrol Paneli", "callback_data": "panel_controls"},
            ],
            [
                {"text": "📜 Komut Rehberi", "callback_data": "panel_help"},
            ],
        ]
    }


def player_control_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "⏸️ Duraklat", "callback_data": "ctrl_pause"},
                {"text": "▶️ Devam Et", "callback_data": "ctrl_resume"},
                {"text": "⏭️ Sıradaki", "callback_data": "ctrl_skip"},
            ],
            [
                {"text": "📋 Liste", "callback_data": "panel_list"},
                {"text": "⏹️ Durdur", "callback_data": "ctrl_stop"},
                {"text": "🔙 Stüdyo", "callback_data": "panel_home"},
            ],
        ]
    }


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Sunum / Akış"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


def _kind_icon(item: dict[str, Any]) -> str:
    kind = item.get("kind")
    if kind == "slideshow":
        return "🎞️"
    if kind == "image":
        return "🖼️"
    if item.get("is_video") or kind == "video":
        return "🎬"
    return "🎵"


def _build_list_text(chat_id: int) -> str:
    lst = get_list(chat_id)
    current = NOW_PLAYING.get(chat_id)
    lines = [
        "📋 <b>GOLDEN STUDIO — ÜSTTEN AŞAĞI LİSTE</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if current:
        lines.append(f"▶️ <b>Şu An Yayında:</b> {_kind_icon(current)} <code>{current['title']}</code>")
    if not lst:
        lines.append("Sırada bekleyen başka öğe yok.")
    else:
        for idx, item in enumerate(lst, 1):
            lines.append(f"<b>{idx}.</b> {_kind_icon(item)} <code>{item['title']}</code>")
    lines.append("\n💡 <i>Sıra değiştirmek için:</i> <code>/sirala &lt;eski_no&gt; &lt;yeni_no&gt;</code> <i>(Örn:</i> <code>/sirala 3 1</code><i>)</i>")
    return "\n".join(lines)


async def tg_call(http: Any, method: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    url = f"{API_BASE}/{method}"
    async with http.post(url, json=payload or {}, timeout=35) as resp:
        data = await resp.json()
        if not data.get("ok"):
            print(f"[TG API HATA] {method}: {data}", flush=True)
        return data


async def send_msg(
    http: Any,
    chat_id: int | str,
    text: str,
    reply_markup: dict[str, Any] | None = None,
    reply_to_message_id: int | None = None,
    disable_preview: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": disable_preview},
    }
    if reply_markup:
        body["reply_markup"] = reply_markup
    if reply_to_message_id:
        body["reply_to_message_id"] = reply_to_message_id
    return await tg_call(http, "sendMessage", body)


async def edit_msg(
    http: Any,
    chat_id: int | str,
    message_id: int,
    text: str,
    reply_markup: dict[str, Any] | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "HTML",
        "link_preview_options": {"is_disabled": True},
    }
    if reply_markup:
        body["reply_markup"] = reply_markup
    return await tg_call(http, "editMessageText", body)


async def answer_cb(http: Any, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
    await tg_call(
        http,
        "answerCallbackQuery",
        {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        },
    )


async def maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


async def _process_stream_request(http: Any, msg: dict[str, Any], query_text: str, is_video: bool) -> None:
    chat = msg["chat"]
    chat_id = chat["id"]
    msg_id = msg["message_id"]
    user_id = (msg.get("from") or {}).get("id")
    if chat.get("type") == "private":
        await send_msg(
            http,
            chat_id,
            "⚠️ Sesli sohbet yayını yalnızca <b>gruplarda</b> başlatılabilir.\n"
            "Lütfen grupta doğrudan ses/video dosyası gönderin veya <code>/sunum</code> ile görsel paylaşın.",
            reply_to_message_id=msg_id,
        )
        return

    if query_text.startswith(("http://", "https://")):
        await send_msg(
            http,
            chat_id,
            "ℹ️ Bağlantı üzerinden indirme/akış kapatıldı. Lütfen doğrudan <b>MP3/MP4 dosyası</b> gönderin veya <b>fotoğraf</b> paylaşın.",
            reply_to_message_id=msg_id,
        )
        return

    if _is_duplicate_action(chat_id, user_id, f"stream:{query_text.lower()}", cooldown=3.0):
        return

    scope_lock = _get_scope_lock(chat_id, "stream")
    if scope_lock.locked():
        await send_msg(http, chat_id, "⏳ Mevcut yayın isteği işleniyor, lütfen kısa bir süre bekleyin...", reply_to_message_id=msg_id)
        return

    async with scope_lock:
        requester = (msg.get("from") or {}).get("first_name") or "Üye"
        try:
            status, track = await maybe_await(play_or_queue(chat_id, query_text, is_video, requester))
            dur = format_duration(track.get("duration", 0))
            kind = "🎬 HD Video Yayını" if is_video else "🎵 Stüdyo Ses Yayını"
            if status == "playing":
                text = (
                    f"✨ <b>GOLDEN STUDIO — YAYINDA</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎶 <b>Parça:</b> <code>{track['title']}</code>\n"
                    f"⏱️ <b>Süre:</b> <code>{dur}</code>\n"
                    f"📡 <b>Format:</b> {kind}\n"
                    f"👤 <b>İsteyen:</b> {requester}"
                )
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(chat_id, []))
                text = (
                    f"📑 <b>Listeye Eklendi (#{q_pos})</b>\n"
                    f"🎶 <b>Parça:</b> <code>{track['title']}</code> (<code>{dur}</code>)\n"
                    f"📡 <b>Format:</b> {kind}"
                )
            await send_msg(http, chat_id, text, reply_markup=player_control_markup(), reply_to_message_id=msg_id)
        except Exception as e:
            print(f"[YAYIN HATA] {e}", flush=True)
            await send_msg(http, chat_id, f"❌ Yayın başlatılamadı:\n<code>{e}</code>", reply_to_message_id=msg_id)


async def _download_tg_file_to_disk(http: Any, file_id: str, suffix: str = "") -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    info = await tg_call(http, "getFile", {"file_id": file_id})
    file_path_tg = (info.get("result") or {}).get("file_path")
    if not file_path_tg:
        raise RuntimeError(f"Dosya yolu alınamadı: {info}")
    ext = os.path.splitext(file_path_tg)[1] or suffix
    local_path = os.path.join(DOWNLOAD_DIR, f"tg_{file_id[-12:]}{ext}")
    dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path_tg}"
    async with http.get(dl_url, timeout=120) as resp:
        resp.raise_for_status()
        with open(local_path, "wb") as out:
            out.write(await resp.read())
    return local_path


async def _finalize_slideshow(http: Any, chat_id: int, actor_id: int, requester: str, reply_to_id: int | None = None) -> None:
    key = (chat_id, actor_id)
    photos = SLIDE_COLLECTORS.pop(key, [])
    if not photos:
        await send_msg(http, chat_id, "⚠️ Slayt için toplanmış fotoğraf bulunamadı.", reply_to_message_id=reply_to_id)
        return

    wait_res = await send_msg(
        http,
        chat_id,
        f"🎞️ <b>{len(photos)} görselden slayt videosu hazırlanıyor...</b>",
        reply_to_message_id=reply_to_id,
    )
    wait_id = (wait_res.get("result") or {}).get("message_id")
    try:
        mp4_path = await prepare_slideshow_presentation(photos, seconds_per_slide=6)
        item = {
            "kind": "video",
            "source": mp4_path,
            "title": f"Slayt Sunumu ({len(photos)} Görsel)",
            "duration": len(photos[:15]) * 6,
            "is_video": True,
            "requested_by": requester,
        }
        status, track = await maybe_await(start_or_enqueue(chat_id, [item]))
        msg_txt = (
            f"🎞️ <b>Slayt Yayında:</b> <code>{track['title']}</code>"
            if status == "playing"
            else f"📑 <b>Slayt Listeye Eklendi:</b> <code>{track['title']}</code>"
        )
        if wait_id:
            await edit_msg(http, chat_id, wait_id, msg_txt, reply_markup=player_control_markup())
        else:
            await send_msg(http, chat_id, msg_txt, reply_markup=player_control_markup())
    except Exception as e:
        print(f"[SLAYT HATA] {e}", flush=True)
        err_txt = f"❌ Slayt oluşturulamadı:\n<code>{e}</code>"
        if wait_id:
            await edit_msg(http, chat_id, wait_id, err_txt)
        else:
            await send_msg(http, chat_id, err_txt)


async def _handle_media_message(
    http: Any,
    msg: dict[str, Any],
    force_presentation: bool = False,
    expected_kind: str | None = None,
    requester_override: str | None = None,
    reply_to_id: int | None = None,
) -> None:
    chat = msg["chat"]
    chat_id = chat["id"]
    target_reply_id = reply_to_id if reply_to_id is not None else msg["message_id"]
    if chat.get("type") == "private":
        await send_msg(
            http,
            chat_id,
            "ℹ️ Medya dosyalarını sesli sohbette açmak veya listeye eklemek için grupta gönderin.",
            reply_to_message_id=target_reply_id,
        )
        return

    user = msg.get("from") or {}
    user_id = user.get("id")
    requester = requester_override or user.get("first_name") or "Üye"

    try:
        if msg.get("photo"):
            best_photo = msg["photo"][-1]
            local_path = await _download_tg_file_to_disk(http, best_photo["file_id"], ".jpg")

            if user_id and (chat_id, user_id) in SLIDE_COLLECTORS and not force_presentation:
                bucket = SLIDE_COLLECTORS[(chat_id, user_id)]
                bucket.append(local_path)
                await send_msg(
                    http,
                    chat_id,
                    f"🖼️ Slayta eklendi (<b>{len(bucket)}</b> görsel). Bitirmek için <code>/slayt bitir</code> yazın.",
                    reply_to_message_id=target_reply_id,
                )
                return

            items = await maybe_await(resolve_input_items(local_path, mode="image", requested_by=requester))
            status, track = await maybe_await(start_or_enqueue(chat_id, items))
            if status == "playing":
                await send_msg(http, chat_id, f"🖼️ <b>Görsel Sunum Yayında:</b> <code>{track['title']}</code>", reply_markup=player_control_markup(), reply_to_message_id=target_reply_id)
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(chat_id, []))
                await send_msg(
                    http,
                    chat_id,
                    f"📑 <b>Görsel Sunum Listeye Eklendi (#{q_pos}):</b> <code>{track['title']}</code>",
                    reply_markup=player_control_markup(),
                    reply_to_message_id=target_reply_id,
                )
            return

        media_obj = msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document")
        if media_obj:
            kind = _detect_media_kind(msg)
            if kind not in ("video", "audio"):
                return
            if expected_kind and kind != expected_kind:
                await send_msg(
                    http,
                    chat_id,
                    f"⚠️ Yanıtlanan dosya <b>{kind}</b> türünde; bu komut <b>{expected_kind}</b> bekliyor.",
                    reply_to_message_id=target_reply_id,
                )
                return

            is_vid = kind == "video"
            file_name = (media_obj.get("file_name") or "").lower()
            ext = os.path.splitext(file_name)[1]
            default_suffix = ext if ext in (AUDIO_EXTS | VIDEO_EXTS) else (".mp4" if is_vid else ".mp3")
            local_path = await _download_tg_file_to_disk(http, media_obj["file_id"], default_suffix)
            items = await maybe_await(
                resolve_input_items(local_path, mode="video" if is_vid else "audio", requested_by=requester)
            )
            status, track = await maybe_await(start_or_enqueue(chat_id, items))
            ikon = "🎬" if is_vid else "🎵"
            if status == "playing":
                await send_msg(http, chat_id, f"✨ {ikon} <b>Dosya Yayında:</b> <code>{track['title']}</code>", reply_markup=player_control_markup(), reply_to_message_id=target_reply_id)
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(chat_id, []))
                await send_msg(
                    http,
                    chat_id,
                    f"📑 {ikon} <b>Dosya Listeye Eklendi (#{q_pos}):</b> <code>{track['title']}</code>",
                    reply_markup=player_control_markup(),
                    reply_to_message_id=target_reply_id,
                )
    except Exception as e:
        print(f"[MEDYA HATA] {e}", flush=True)
        await send_msg(http, chat_id, f"❌ Medya yayına alınamadı:\n<code>{e}</code>", reply_to_message_id=target_reply_id)


async def handle_callback_query(http: Any, cq: dict[str, Any]) -> None:
    cq_id = cq["id"]
    data = cq.get("data") or ""
    user_id = (cq.get("from") or {}).get("id")
    msg = cq.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    print(f"[CALLBACK] User: {user_id} | Chat: {chat_id} | Data: {data}", flush=True)

    if not chat_id or not msg_id:
        await answer_cb(http, cq_id)
        return

    if _is_duplicate_action(chat_id, user_id, f"cb:{data}", cooldown=1.5):
        await answer_cb(http, cq_id, "⏳ Lütfen biraz bekleyin...")
        return

    if data == "panel_home":
        await edit_msg(http, chat_id, msg_id, WELCOME_TEXT, reply_markup=main_menu_markup())
        await answer_cb(http, cq_id)
        return

    if data == "panel_list":
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_list_text(chat_id),
            reply_markup={
                "inline_keyboard": [
                    [
                        {"text": "⏭️ Sıradaki", "callback_data": "ctrl_skip"},
                        {"text": "🔙 Stüdyo", "callback_data": "panel_home"},
                    ]
                ]
            },
        )
        await answer_cb(http, cq_id)
        return

    if data == "panel_help":
        help_text = (
            "📜 <b>GOLDEN STUDIO — KOMUT REHBERİ</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎵 <b>Doğrudan veya Yanıtla Ses/Video:</b> MP3/MP4 gönderin veya yanıtlayıp <code>/oynat</code> / <code>/video</code> yazın\n"
            "🖼️ <code>/sunum</code> — Fotoğrafa yanıt verip veya doğrudan gönderip 30 sn sunum açar\n"
            "🎞️ <code>/slayt</code> — Fotoğrafa yanıt vererek veya sırayla gönderip <code>/slayt bitir</code> ile başlatır\n"
            "📋 <code>/liste</code> — Üstten aşağı listeyi gösterir\n"
            "🔢 <code>/sirala &lt;eski_no&gt; &lt;yeni_no&gt;</code> — Sırayı değiştirir (Örn: <code>/sirala 3 1</code>)\n"
            "⏸️ <code>/duraklat</code> • ▶️ <code>/devam</code> • ⏭️ <code>/atla</code> • ⏹️ <code>/dur</code>"
        )
        await edit_msg(
            http,
            chat_id,
            msg_id,
            help_text,
            reply_markup={"inline_keyboard": [[{"text": "🔙 Ana Stüdyo", "callback_data": "panel_home"}]]},
        )
        await answer_cb(http, cq_id)
        return

    if data == "panel_controls":
        current = NOW_PLAYING.get(chat_id)
        q_len = len(LISTS.get(chat_id, []))
        status_line = (
            f"🎶 <b>Şu An Yayında:</b> <code>{current['title']}</code>\n"
            f"📡 <b>Mod:</b> {'🎬 Video / Sunum Yayını' if current.get('is_video') else '🎵 Stüdyo Ses'}\n"
            f"📑 <b>Sıradaki Öğe Sayısı:</b> <code>{q_len}</code>"
            if current
            else "🔇 <i>Şu anda aktif bir sesli sohbet yayını bulunmuyor.</i>"
        )
        await edit_msg(
            http,
            chat_id,
            msg_id,
            f"🎛️ <b>GOLDEN STUDIO — CANLI KONTROL PANELİ</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{status_line}",
            reply_markup=player_control_markup(),
        )
        await answer_cb(http, cq_id)
        return

    if data.startswith("mode_"):
        mode = data.split("_", 1)[1]
        if user_id and mode == "slide":
            SLIDE_COLLECTORS[(chat_id, user_id)] = []
            await answer_cb(http, cq_id, "Slayt modu açıldı!")
            await send_msg(http, chat_id, "🎞️ Slayt modu başladı. Fotoğrafları gönderin, bitince <code>/slayt bitir</code> yazın.")
            return
        if user_id:
            USER_MODE[user_id] = mode
        labels = {
            "audio": "🎵 <b>Ses / Dosya Yayını</b>",
            "video": "🎬 <b>Video Yayını</b>",
            "image": "🖼️ <b>Görsel Sunum Yayını</b>",
        }
        hint = (
            "✍️ Şimdi bir <b>fotoğraf</b> gönderin:"
            if mode == "image"
            else "✍️ Şimdi doğrudan <b>ses/video dosyasını</b> veya kayıtlı parça adını gönderin:"
        )
        await answer_cb(http, cq_id, "Mod seçildi!", show_alert=False)
        await send_msg(http, chat_id, f"✅ Aktif Mod: {labels.get(mode, mode)}\n{hint}")
        return

    if data == "ctrl_pause":
        try:
            await maybe_await(pause_stream(chat_id))
            await answer_cb(http, cq_id, "⏸️ Yayın duraklatıldı.")
        except Exception as e:
            await answer_cb(http, cq_id, f"Hata: {e}", show_alert=True)
    elif data == "ctrl_resume":
        try:
            await maybe_await(resume_stream(chat_id))
            await answer_cb(http, cq_id, "▶️ Yayın devam ediyor.")
        except Exception as e:
            await answer_cb(http, cq_id, f"Hata: {e}", show_alert=True)
    elif data == "ctrl_skip":
        nxt = await maybe_await(skip_track(chat_id))
        if nxt:
            await send_msg(http, chat_id, f"⏭️ Sıradaki öğeye geçildi: <b>{nxt['title']}</b>")
        else:
            await send_msg(http, chat_id, "⏹️ Sırada başka öğe kalmadı, yayın sonlandırıldı.")
        await answer_cb(http, cq_id)
    elif data == "ctrl_stop":
        await maybe_await(stop_stream(chat_id))
        await send_msg(http, chat_id, "⏹️ <b>Golden Studio</b> yayını durduruldu ve liste temizlendi.")
        await answer_cb(http, cq_id)


async def handle_message(http: Any, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    msg_id = msg.get("message_id")
    if not chat_id:
        return

    user = msg.get("from") or {}
    user_id = user.get("id")
    requester = user.get("first_name") or "Üye"
    reply_msg = msg.get("reply_to_message") or {}
    text_raw = msg.get("text") or msg.get("caption") or ""
    text = text_raw.strip()
    print(f"[MESAJ GELDI] Chat: {chat_id} | User: {user_id} | Metin: {text}", flush=True)

    if text.startswith("/"):
        first_token = text.split(None, 1)[0][1:].split("@")[0].lower()
        arg_str = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

        if first_token in ("start", "studio", "baslat"):
            if _is_duplicate_action(chat_id, user_id, "cmd:start", cooldown=2.0):
                return
            await send_msg(http, chat_id, WELCOME_TEXT, reply_markup=main_menu_markup(), reply_to_message_id=msg_id)
            return

        if first_token in ("play", "oynat"):
            reply_kind = _detect_media_kind(reply_msg) if reply_msg else None
            if reply_kind in ("audio", "video"):
                await _handle_media_message(
                    http,
                    reply_msg,
                    requester_override=requester,
                    reply_to_id=msg_id,
                )
                return
            if not arg_str:
                await send_msg(http, chat_id, "Doğrudan MP3/MP4 gönderin, bir ses/video mesajına yanıt verip <code>/oynat</code> yazın veya <code>/oynat &lt;kayıtlı_parça&gt;</code> kullanın.", reply_to_message_id=msg_id)
                return
            await _process_stream_request(http, msg, arg_str, is_video=False)
            return

        if first_token in ("vplay", "video"):
            reply_kind = _detect_media_kind(reply_msg) if reply_msg else None
            if reply_kind == "video":
                await _handle_media_message(
                    http,
                    reply_msg,
                    expected_kind="video",
                    requester_override=requester,
                    reply_to_id=msg_id,
                )
                return
            if not arg_str:
                await send_msg(http, chat_id, "Doğrudan MP4 gönderin, bir video mesajına yanıt verip <code>/video</code> yazın veya <code>/video &lt;kayıtlı_video&gt;</code> kullanın.", reply_to_message_id=msg_id)
                return
            await _process_stream_request(http, msg, arg_str, is_video=True)
            return

        if first_token == "sunum":
            target = reply_msg if reply_msg.get("photo") else msg
            if not target.get("photo"):
                await send_msg(http, chat_id, "Bir fotoğrafı yanıtlayıp <code>/sunum</code> yazın veya doğrudan fotoğraf gönderin.", reply_to_message_id=msg_id)
                return
            await _handle_media_message(
                http,
                target,
                force_presentation=True,
                requester_override=requester,
                reply_to_id=msg_id,
            )
            return

        if first_token == "slayt":
            if not user_id:
                return
            sub = arg_str.lower()
            if sub in ("bitir", "tamam", "baslat", "oynat"):
                await _finalize_slideshow(http, chat_id, user_id, requester, reply_to_id=msg_id)
                return
            if sub in ("iptal", "temizle"):
                SLIDE_COLLECTORS.pop((chat_id, user_id), None)
                await send_msg(http, chat_id, "🗑️ Slayt toplama listesi iptal edildi.", reply_to_message_id=msg_id)
                return
            SLIDE_COLLECTORS[(chat_id, user_id)] = []
            if reply_msg.get("photo"):
                try:
                    best_photo = reply_msg["photo"][-1]
                    local_path = await _download_tg_file_to_disk(http, best_photo["file_id"], ".jpg")
                    SLIDE_COLLECTORS[(chat_id, user_id)].append(local_path)
                    await send_msg(
                        http,
                        chat_id,
                        "🎞️ Slayt modu açıldı ve yanıtlanan fotoğraf (<b>1</b>. görsel) eklendi. Diğer fotoğrafları gönderin, bitince <code>/slayt bitir</code> yazın.",
                        reply_to_message_id=msg_id,
                    )
                    return
                except Exception as e:
                    print(f"[SLAYT REPLY HATA] {e}", flush=True)
            await send_msg(
                http,
                chat_id,
                "🎞️ Slayt modu açıldı. Görselleri sırayla gönderin, hazır olduğunda <code>/slayt bitir</code> yazın.",
                reply_to_message_id=msg_id,
            )
            return

        if first_token == "liste":
            if _is_duplicate_action(chat_id, user_id, "cmd:liste", cooldown=2.0):
                return
            await send_msg(http, chat_id, _build_list_text(chat_id), reply_markup=player_control_markup(), reply_to_message_id=msg_id)
            return

        if first_token == "sirala":
            parts = arg_str.split()
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                await send_msg(http, chat_id, "Kullanım: <code>/sirala &lt;eski_sıra&gt; &lt;yeni_sıra&gt;</code> <i>(Örn:</i> <code>/sirala 3 1</code><i>)</i>", reply_to_message_id=msg_id)
                return
            ok, info_msg = reorder_list(chat_id, int(parts[0]), int(parts[1]))
            await send_msg(http, chat_id, info_msg if ok else f"⚠️ {info_msg}", reply_to_message_id=msg_id)
            return

        if first_token in ("pause", "duraklat"):
            if _is_duplicate_action(chat_id, user_id, "cmd:pause", cooldown=2.0):
                return
            try:
                await maybe_await(pause_stream(chat_id))
                await send_msg(http, chat_id, "⏸️ Yayın duraklatıldı.", reply_to_message_id=msg_id)
            except Exception as e:
                await send_msg(http, chat_id, f"⚠️ Duraklatılamadı:\n<code>{e}</code>", reply_to_message_id=msg_id)
            return

        if first_token in ("resume", "devam"):
            if _is_duplicate_action(chat_id, user_id, "cmd:resume", cooldown=2.0):
                return
            try:
                await maybe_await(resume_stream(chat_id))
                await send_msg(http, chat_id, "▶️ Yayın devam ediyor.", reply_to_message_id=msg_id)
            except Exception as e:
                await send_msg(http, chat_id, f"⚠️ Devam ettirilemedi:\n<code>{e}</code>", reply_to_message_id=msg_id)
            return

        if first_token in ("skip", "atla"):
            if _is_duplicate_action(chat_id, user_id, "cmd:skip", cooldown=2.5):
                return
            nxt = await maybe_await(skip_track(chat_id))
            if nxt:
                await send_msg(http, chat_id, f"⏭️ Sıradaki öğeye geçildi: <b>{nxt['title']}</b>", reply_to_message_id=msg_id)
            else:
                await send_msg(http, chat_id, "⏹️ Sıra bitti, yayın kapatıldı.", reply_to_message_id=msg_id)
            return

        if first_token in ("stop", "dur"):
            if _is_duplicate_action(chat_id, user_id, "cmd:stop", cooldown=2.5):
                return
            await maybe_await(stop_stream(chat_id))
            await send_msg(http, chat_id, "⏹️ <b>Golden Studio</b> yayını durduruldu ve liste temizlendi.", reply_to_message_id=msg_id)
            return

    if msg.get("photo") or msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document"):
        await _handle_media_message(http, msg)
        return

    if text and user_id and user_id in USER_MODE:
        mode = USER_MODE.pop(user_id)
        if mode == "audio":
            await _process_stream_request(http, msg, text, is_video=False)
        elif mode == "video":
            await _process_stream_request(http, msg, text, is_video=True)
        elif mode == "image":
            await send_msg(http, chat_id, "🖼️ Görsel sunum için lütfen bir fotoğraf gönderin veya fotoğrafa yanıt verip <code>/sunum</code> yazın.", reply_to_message_id=msg_id)


async def polling_loop(http: Any) -> None:
    await tg_call(http, "deleteWebhook", {"drop_pending_updates": False})
    me_res = await tg_call(http, "getMe")
    me = me_res.get("result") or {}
    print(f"AKTIF BOT (BOT API): @{me.get('username')} (ID: {me.get('id')})", flush=True)

    offset: int | None = None
    conflict_streak = 0
    while True:
        try:
            params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            url = f"{API_BASE}/getUpdates"
            async with http.get(url, params=params, timeout=35) as resp:
                data = await resp.json()
            if not data.get("ok"):
                err_code = data.get("error_code")
                desc = str(data.get("description") or "")
                print(f"[GETUPDATES HATA] {data}", flush=True)
                if err_code == 409 or "conflict" in desc.lower():
                    conflict_streak += 1
                    wait_sec = min(45.0, 6.0 * (2 ** min(conflict_streak - 1, 3)))
                    await asyncio.sleep(wait_sec)
                else:
                    conflict_streak = 0
                    await asyncio.sleep(3)
                continue

            conflict_streak = 0
            for upd in data.get("result", []):
                upd_id = upd.get("update_id")
                if upd_id is not None:
                    offset = upd_id + 1
                if "message" in upd:
                    asyncio.create_task(handle_message(http, upd["message"]))
                elif "callback_query" in upd:
                    asyncio.create_task(handle_callback_query(http, upd["callback_query"]))
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"[POLLING HATA] {e}", flush=True)
            await asyncio.sleep(4)


async def start_web_server() -> None:
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Golden Studio VIP Bot Aktif"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main() -> None:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    await start_web_server()

    try:
        await init_player()
        print("Asistan ve PyTgCalls baslatildi!", flush=True)
    except Exception as e:
        print(f"[ASISTAN UYARISI] {e}", flush=True)

    print("Golden Studio basariyla baslatildi!", flush=True)

    async with aiohttp.ClientSession() as http:
        await polling_loop(http)


if __name__ == "__main__":
    asyncio.run(main())
