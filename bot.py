import asyncio
import html
import inspect
import os
import re
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
    get_chat_volume,
    set_chat_volume,
    NOW_PLAYING,
)
import player as _player_mod
from playlist_manager import (
    LISTS,
    resolve_input_items,
    reorder_list,
    move_item_step,
    remove_from_list,
    get_list,
    save_track_to_catalog,
    list_saved_tracks,
    register_linked_group,
    unregister_linked_group,
    sync_bot_active_groups,
    get_linked_groups,
    set_user_target_chat,
    get_user_target_chat,
    extract_media_title,
    clean_track_title,
)

API_BASE = f"https://api.telegram.org/bot{BOT_TOKEN}"
USER_MODE: dict[int, str] = {}
SLIDE_COLLECTORS: dict[tuple[int, int], list[str]] = {}
COMBO_SESSIONS: dict[tuple[int, int], dict[str, Any]] = {}
SELECTED_LIST_IDX: dict[int, int] = {}
BOT_ID: int | None = None

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".opus", ".wav", ".flac"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

_ACTION_COOLDOWN_SEC = 1.5
_LAST_ACTION_TS: dict[tuple[int, int | str, str], float] = {}
_ACTIVE_ACTION_LOCKS: dict[tuple[int, str], asyncio.Lock] = {}


def _is_duplicate_action(
    chat_id: int,
    actor_id: int | str | None,
    scope: str,
    cooldown: float = _ACTION_COOLDOWN_SEC,
) -> bool:
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
    if mime.startswith("image/") or ext in IMAGE_EXTS:
        return "photo"
    if bool(msg.get("video")) or mime.startswith("video/") or ext in VIDEO_EXTS:
        return "video"
    if bool(msg.get("audio") or msg.get("voice")) or mime.startswith("audio/") or ext in AUDIO_EXTS:
        return "audio"
    return None


async def _verify_bot_in_chat(http: Any, chat_id: int) -> bool:
    if BOT_ID is None:
        return True
    res = await tg_call(http, "getChatMember", {"chat_id": chat_id, "user_id": BOT_ID})
    if not res.get("ok"):
        return False
    status = ((res.get("result") or {}).get("status") or "").lower()
    return status in ("member", "administrator", "creator")


async def _refresh_bot_groups(http: Any) -> None:
    existing = get_linked_groups()
    valid_groups: list[dict[str, Any]] = []
    for g in existing:
        cid = int(g["chat_id"])
        if await _verify_bot_in_chat(http, cid):
            valid_groups.append(g)
    sync_bot_active_groups(valid_groups)


async def _track_bot_group_from_msg(http: Any, msg: dict[str, Any]) -> None:
    chat = msg.get("chat") or {}
    cid = chat.get("id")
    ctype = (chat.get("type") or "").lower()
    if cid and ctype in ("group", "supergroup", "channel"):
        if await _verify_bot_in_chat(http, int(cid)):
            title = chat.get("title") or chat.get("username") or f"Grup {cid}"
            register_linked_group(int(cid), title, ctype)


async def handle_my_chat_member(http: Any, mcm: dict[str, Any]) -> None:
    chat = mcm.get("chat") or {}
    cid = chat.get("id")
    ctype = (chat.get("type") or "").lower()
    if not cid or ctype not in ("group", "supergroup", "channel"):
        return

    new_member = mcm.get("new_chat_member") or {}
    member_user = new_member.get("user") or {}
    if BOT_ID is not None and member_user.get("id") != BOT_ID:
        return

    new_status = (new_member.get("status") or "").lower()
    title = chat.get("title") or chat.get("username") or f"Grup {cid}"
    if new_status in ("member", "administrator", "creator"):
        register_linked_group(int(cid), title, ctype)
        print(f"[BOT GRUBA EKLENDI] {title} ({cid}) | Durum: {new_status}", flush=True)
    elif new_status in ("left", "kicked", "banned"):
        unregister_linked_group(int(cid))
        print(f"[BOT GRUPTAN AYRILDI] {title} ({cid}) | Durum: {new_status}", flush=True)


def _get_group_name(chat_id: int) -> str:
    for g in get_linked_groups():
        if int(g.get("chat_id", 0)) == int(chat_id):
            return str(g.get("title") or chat_id)
    return str(chat_id)


WELCOME_TEXT = (
    "✨ <b>GOLDEN STUDIO — VIP MEDYA & YAYIN MERKEZI</b> ✨\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Botun eklendigi gruplardan birini <b>Bagli Gruplar</b> veya <code>/hedefler</code> ile secin; "
    "ardindan dogrudan dosya gonderin veya mesaja yanit vererek yayin baslatin.\n\n"
    "🎛️ <b>Hizli Kullanim:</b>\n"
    "• <b>Hedef Grup Sec:</b> <code>/hedefler</code> -> botun ekli oldugu grup ismine tiklayin.\n"
    "• <b>Dosya / Yanitla:</b> MP3/MP4/fotograf gonderin veya yanitlayip <code>/oynat</code>, <code>/video</code>, <code>/sunum</code>, <code>/slayt</code>, <code>/kombin</code>, <code>/kaydet</code> yazin.\n"
    "• <b>Ses Seviyesi:</b> <code>/ses 0-200</code> veya kontrol butonlari.\n"
    "• <b>Kolay Liste:</b> <code>/liste</code> altindaki butonlarla sirayi degistirin veya <code>/sirala 3 1</code> yazin.\n\n"
    "👇 <i>Asagidan bir secenek belirleyin:</i>"
)


def main_menu_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🎯 Bagli Gruplar", "callback_data": "panel_targets"},
                {"text": "📋 Liste & Duzenle", "callback_data": "panel_list"},
            ],
            [
                {"text": "🎵 Ses Yayini", "callback_data": "mode_audio"},
                {"text": "🎬 Video Yayini", "callback_data": "mode_video"},
            ],
            [
                {"text": "🖼️ Resim Sunumu", "callback_data": "mode_image"},
                {"text": "🎞️ Slayt Baslat", "callback_data": "mode_slide"},
            ],
            [
                {"text": "🖼️🎵 Görsel + Şarkı", "callback_data": "mode_combo"},
                {"text": "💾 Kayitli Sarkilar", "callback_data": "panel_catalog"},
            ],
            [
                {"text": "🎛️ Canli Kontrol", "callback_data": "panel_controls"},
            ],
        ]
    }


def player_control_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🔉 -20%", "callback_data": "vol_down"},
                {"text": "🔊 +20%", "callback_data": "vol_up"},
                {"text": "🔇 Sessiz", "callback_data": "vol_mute"},
            ],
            [
                {"text": "⏸️ Duraklat", "callback_data": "ctrl_pause"},
                {"text": "▶️ Devam Et", "callback_data": "ctrl_resume"},
                {"text": "⏭️ Siradaki", "callback_data": "ctrl_skip"},
            ],
            [
                {"text": "💾 Kaydet", "callback_data": "ctrl_save_current"},
                {"text": "📋 Liste", "callback_data": "panel_list"},
                {"text": "⏹️ Durdur", "callback_data": "ctrl_stop"},
            ],
        ]
    }


def list_editor_markup(chat_id: int, user_id: int | None) -> dict[str, Any]:
    lst = get_list(chat_id)
    sel = SELECTED_LIST_IDX.get(user_id or 0, 1)
    if lst:
        sel = max(1, min(len(lst), sel))
        if user_id:
            SELECTED_LIST_IDX[user_id] = sel

    rows: list[list[dict[str, str]]] = []
    if lst:
        num_row: list[dict[str, str]] = []
        for idx in range(1, min(len(lst), 6) + 1):
            mark = f"[{idx}]" if idx == sel else f"{idx}"
            num_row.append({"text": mark, "callback_data": f"lst_sel_{idx}"})
        rows.append(num_row)

        rows.append(
            [
                {"text": "⬆️ Yukari", "callback_data": "lst_up"},
                {"text": "⬇️ Asagi", "callback_data": "lst_down"},
                {"text": "🔝 En Uste", "callback_data": "lst_top"},
                {"text": "🗑️ Sil", "callback_data": "lst_del"},
            ]
        )

    rows.append(
        [
            {"text": "⏭️ Siradaki", "callback_data": "ctrl_skip"},
            {"text": "🎯 Hedef Grup", "callback_data": "panel_targets"},
            {"text": "🔙 Studyo", "callback_data": "panel_home"},
        ]
    )
    return {"inline_keyboard": rows}


def target_groups_markup(user_id: int | None, active_target: int) -> dict[str, Any]:
    groups = get_linked_groups()
    rows: list[list[dict[str, str]]] = []
    for g in groups[:12]:
        gid = int(g["chat_id"])
        title = str(g.get("title") or gid)[:32]
        prefix = "✅ " if gid == active_target else "👥 "
        rows.append([{"text": f"{prefix}{title}", "callback_data": f"tgt_set_{gid}"}])
    rows.append(
        [
            {"text": "🔄 Gruplari Yenile", "callback_data": "tgt_refresh"},
            {"text": "📋 Liste", "callback_data": "panel_list"},
            {"text": "🔙 Studyo", "callback_data": "panel_home"},
        ]
    )
    return {"inline_keyboard": rows}


def _build_targets_panel_text(active_target: int) -> str:
    groups = get_linked_groups()
    is_selected = active_target in {int(g["chat_id"]) for g in groups}
    active_name = _get_group_name(active_target) if is_selected else "Secilmedi"
    lines = [
        "🎯 <b>BOTUN EKLİ OLDUĞU GRUPLAR</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ <b>Secili Grup:</b> <b>{active_name}</b>" + (f" <code>({active_target})</code>" if is_selected else ""),
        "",
    ]
    if not groups:
        lines.append(
            "Botun uye oldugu kayitli grup yok. Botu bir gruba ekleyip o gruptan bir mesaj atin "
            "veya <b>Gruplari Yenile</b> butonuna basin."
        )
    else:
        lines.append("Asagidaki gruplardan birine tiklayarak hedefi secin:")
        for idx, g in enumerate(groups[:12], 1):
            gid = int(g["chat_id"])
            gname = str(g.get("title") or gid)
            mark = " ✅" if gid == active_target else ""
            lines.append(f"<b>{idx}.</b> {gname}{mark}")
    return "\n".join(lines)


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Akis"
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


def _build_list_text(chat_id: int, user_id: int | None = None) -> str:
    lst = get_list(chat_id)
    current = NOW_PLAYING.get(chat_id)
    sel = SELECTED_LIST_IDX.get(user_id or 0, 1)
    gname = _get_group_name(chat_id)
    lines = [
        f"📋 <b>LISTE — {gname}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if current:
        lines.append(f"▶️ <b>Yayinda:</b> {_kind_icon(current)} <code>{current['title']}</code>")
    if not lst:
        lines.append("Sirada bekleyen baska oge yok.")
    else:
        for idx, item in enumerate(lst, 1):
            pointer = "👉 " if idx == sel else ""
            lines.append(f"{pointer}<b>{idx}.</b> {_kind_icon(item)} <code>{item['title']}</code>")
    lines.append("\n💡 <i>Butonla secip tasiyabilir veya</i> <code>/sirala 3 1</code> <i>yazabilirsiniz.</i>")
    return "\n".join(lines)


def _strip_html_tags(raw: str) -> str:
    cleaned = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(cleaned)


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
    res = await tg_call(http, "sendMessage", body)
    desc = str((res or {}).get("description") or "").lower()
    if not (res or {}).get("ok") and "can't parse entities" in desc:
        plain_body = dict(body)
        plain_body.pop("parse_mode", None)
        plain_body["text"] = _strip_html_tags(text)
        res = await tg_call(http, "sendMessage", plain_body)
    return res


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
    url = f"{API_BASE}/editMessageText"
    async with http.post(url, json=body, timeout=35) as resp:
        data = await resp.json()
    desc = str((data or {}).get("description") or "").lower()
    if not (data or {}).get("ok"):
        if "message is not modified" in desc:
            return {"ok": True, "result": True}
        if "can't parse entities" in desc:
            plain_body = dict(body)
            plain_body.pop("parse_mode", None)
            plain_body["text"] = _strip_html_tags(text)
            async with http.post(url, json=plain_body, timeout=35) as resp2:
                return await resp2.json()
        print(f"[TG API HATA] editMessageText: {data}", flush=True)
    return data


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


async def _download_tg_file_to_disk(
    http: Any,
    file_id: str,
    suffix: str = "",
    msg: dict[str, Any] | None = None,
) -> str:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    info = await tg_call(http, "getFile", {"file_id": file_id})
    file_path_tg = (info.get("result") or {}).get("file_path")

    if file_path_tg:
        ext = os.path.splitext(file_path_tg)[1] or suffix
        local_path = os.path.join(DOWNLOAD_DIR, f"tg_{file_id[-12:]}{ext}")
        dl_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path_tg}"
        async with http.get(dl_url, timeout=300) as resp:
            resp.raise_for_status()
            with open(local_path, "wb") as out:
                out.write(await resp.read())
        return local_path

    err_desc = str((info or {}).get("description") or "")
    client = getattr(_player_mod, "assistant", None)
    if "file is too big" in err_desc.lower():
        if client is None:
            raise RuntimeError("20 MB ustu dosya icin asistan (Pyrogram) aktif degil.")
        if not msg:
            raise RuntimeError("20 MB ustu dosya icin mesaj bilgisi eksik.")

        chat = msg.get("chat") or {}
        chat_id = chat.get("id")
        chat_type = str(chat.get("type") or "").lower()
        message_id = msg.get("message_id")
        if chat_type == "private":
            raise RuntimeError(
                "20 MB ustu dosyalar bota ozel mesajdan indirilemez. "
                "Dosyayi hedef gruba gonderip o mesaja yanit vererek /video veya /oynat yazin."
            )
        if not chat_id or not message_id:
            raise RuntimeError("20 MB ustu dosya icin chat_id veya message_id bulunamadi.")

        cid = int(chat_id)
        try:
            await client.resolve_peer(cid)
        except Exception:
            pass

        target_file = os.path.join(DOWNLOAD_DIR, f"tg_{file_id[-12:]}{suffix or '.mp4'}")
        last_pct = {"val": -1}

        async def _on_progress(current: int, total: int) -> None:
            if total > 0:
                pct = int((current * 100) / total)
                if pct >= last_pct["val"] + 25 or pct == 100:
                    last_pct["val"] = pct
                    print(f"[ASISTAN INDIRME %] {pct}% ({current}/{total})", flush=True)

        print(f"[ASISTAN INDIRME BASLADI] Chat: {cid} | Msg: {message_id}", flush=True)
        try:
            tg_msg = await client.get_messages(cid, int(message_id))
            if not tg_msg or getattr(tg_msg, "empty", False):
                raise RuntimeError("Asistan hedef mesaji bulamadi veya bos dondu.")
            downloaded = await asyncio.wait_for(
                client.download_media(tg_msg, file_name=target_file, progress=_on_progress),
                timeout=240,
            )
        except Exception as py_err:
            raise RuntimeError(f"Asistan indirme hatasi: {py_err}") from py_err

        if downloaded and os.path.exists(str(downloaded)):
            print(f"[ASISTAN INDIRME TAMAM] {downloaded}", flush=True)
            return str(downloaded)
        raise RuntimeError("Asistan dosyayi diske kaydedemedi.")

    raise RuntimeError(f"Dosya yolu alinamadi: {info}")


async def _merge_photo_and_audio_to_mp4(photo_path: str, audio_path: str, out_path: str) -> None:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-loop",
        "1",
        "-i",
        photo_path,
        "-i",
        audio_path,
        "-vf",
        "scale='min(1280,iw)':-2,format=yuv420p",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "27",
        "-r",
        "15",
        "-c:a",
        "aac",
        "-b:a",
        "128k",
        "-shortest",
        "-movflags",
        "+faststart",
        out_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0 or not os.path.exists(out_path):
        err_tail = (stderr_bytes or b"").decode("utf-8", errors="replace").strip().splitlines()
        short_err = err_tail[-1] if err_tail else "Bilinmeyen ffmpeg hatasi"
        print(f"[FFMPEG STDERR] {' | '.join(err_tail[-4:])}", flush=True)
        raise RuntimeError(f"Gorsel ve ses birlestirilemedi: {short_err}")


async def _extract_photo_from_msg(http: Any, msg: dict[str, Any]) -> str | None:
    if msg.get("photo"):
        return await _download_tg_file_to_disk(http, msg["photo"][-1]["file_id"], ".jpg", msg=msg)
    if _detect_media_kind(msg) == "photo" and msg.get("document"):
        doc = msg["document"]
        fname = (doc.get("file_name") or "").lower()
        ext = os.path.splitext(fname)[1] if os.path.splitext(fname)[1] in IMAGE_EXTS else ".jpg"
        return await _download_tg_file_to_disk(http, doc["file_id"], ext, msg=msg)
    return None


async def _process_stream_request(http: Any, msg: dict[str, Any], query_text: str, is_video: bool) -> None:
    chat = msg["chat"]
    reply_chat_id = chat["id"]
    msg_id = msg["message_id"]
    user_id = (msg.get("from") or {}).get("id")
    target_chat_id = reply_chat_id if chat.get("type") != "private" else get_user_target_chat(user_id, reply_chat_id)

    if query_text.startswith(("http://", "https://")):
        await send_msg(
            http,
            reply_chat_id,
            "ℹ️ Harici baglanti indirme kapatildi. Lutfen dogrudan <b>MP3/MP4 dosyasi</b> gonderin veya kayitli parca adi yazin.",
            reply_to_message_id=msg_id,
        )
        return

    if chat.get("type") == "private" and target_chat_id == reply_chat_id:
        await send_msg(
            http,
            reply_chat_id,
            "⚠️ Once <code>/hedefler</code> ile botun ekli oldugu bir grup secin veya grupta dogrudan dosya/komut gonderin.",
            reply_to_message_id=msg_id,
        )
        return

    if _is_duplicate_action(target_chat_id, user_id, f"stream:{query_text.lower()}", cooldown=2.5):
        return

    scope_lock = _get_scope_lock(target_chat_id, "stream")
    if scope_lock.locked():
        await send_msg(http, reply_chat_id, "⏳ Yayin istegi isleniyor, lutfen bekleyin...", reply_to_message_id=msg_id)
        return

    async with scope_lock:
        requester = (msg.get("from") or {}).get("first_name") or "Uye"
        try:
            status, track = await maybe_await(play_or_queue(target_chat_id, query_text, is_video, requester))
            dur = format_duration(track.get("duration", 0))
            kind = "🎬 Video" if is_video else "🎵 Ses"
            vol = get_chat_volume(target_chat_id)
            gname = _get_group_name(target_chat_id)
            hedef_not = f"\n📡 <b>Hedef Grup:</b> <b>{gname}</b>" if target_chat_id != reply_chat_id else ""
            if status == "playing":
                text = (
                    f"✨ <b>GOLDEN STUDIO — YAYINDA</b>\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🎶 <b>Parca:</b> <code>{track['title']}</code>\n"
                    f"⏱️ <b>Sure:</b> <code>{dur}</code> • {kind} • 🔊 <code>%{vol}</code>\n"
                    f"👤 <b>Isteyen:</b> {requester}{hedef_not}"
                )
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(target_chat_id, []))
                text = (
                    f"📑 <b>Listeye Eklendi (#{q_pos})</b>\n"
                    f"🎶 <b>Parca:</b> <code>{track['title']}</code> (<code>{dur}</code>) • {kind}{hedef_not}"
                )
            await send_msg(http, reply_chat_id, text, reply_markup=player_control_markup(), reply_to_message_id=msg_id)
        except Exception as e:
            print(f"[YAYIN HATA] {e}", flush=True)
            await send_msg(http, reply_chat_id, f"❌ Yayin baslatilamadi:\n<code>{e}</code>", reply_to_message_id=msg_id)


async def _finalize_slideshow(
    http: Any,
    reply_chat_id: int,
    target_chat_id: int,
    actor_id: int,
    requester: str,
    reply_to_id: int | None = None,
) -> None:
    key = (reply_chat_id, actor_id)
    photos = SLIDE_COLLECTORS.pop(key, [])
    if not photos:
        await send_msg(http, reply_chat_id, "⚠️ Slayt icin toplanmis fotograf bulunamadi.", reply_to_message_id=reply_to_id)
        return

    wait_res = await send_msg(
        http,
        reply_chat_id,
        f"🎞️ <b>{len(photos)} gorselden slayt hazirlaniyor...</b>",
        reply_to_message_id=reply_to_id,
    )
    wait_id = (wait_res.get("result") or {}).get("message_id")
    try:
        mp4_path = await prepare_slideshow_presentation(photos, seconds_per_slide=6)
        item = {
            "kind": "video",
            "source": mp4_path,
            "title": f"Slayt Sunumu ({len(photos)} Gorsel)",
            "duration": 86400,
            "is_video": True,
            "requested_by": requester,
        }
        status, track = await maybe_await(start_or_enqueue(target_chat_id, [item]))
        gname = _get_group_name(target_chat_id)
        msg_txt = (
            f"🎞️ <b>Slayt Yayinda ({gname}):</b> <code>{track['title']}</code>"
            if status == "playing"
            else f"📑 <b>Slayt Listeye Eklendi ({gname}):</b> <code>{track['title']}</code>"
        )
        if wait_id:
            await edit_msg(http, reply_chat_id, wait_id, msg_txt, reply_markup=player_control_markup())
        else:
            await send_msg(http, reply_chat_id, msg_txt, reply_markup=player_control_markup())
    except Exception as e:
        print(f"[SLAYT HATA] {e}", flush=True)
        err_txt = f"❌ Slayt olusturulamadi:\n<code>{e}</code>"
        if wait_id:
            await edit_msg(http, reply_chat_id, wait_id, err_txt)
        else:
            await send_msg(http, reply_chat_id, err_txt)


async def _handle_media_message(
    http: Any,
    msg: dict[str, Any],
    force_presentation: bool = False,
    expected_kind: str | None = None,
    requester_override: str | None = None,
    reply_to_id: int | None = None,
    collector_chat_id: int | None = None,
    custom_title: str | None = None,
) -> None:
    chat = msg["chat"]
    reply_chat_id = chat["id"]
    target_reply_id = reply_to_id if reply_to_id is not None else msg["message_id"]
    user = msg.get("from") or {}
    user_id = user.get("id")
    requester = requester_override or user.get("first_name") or "Uye"

    target_chat_id = reply_chat_id if chat.get("type") != "private" else get_user_target_chat(user_id, reply_chat_id)
    if chat.get("type") == "private" and target_chat_id == reply_chat_id:
        await send_msg(
            http,
            reply_chat_id,
            "ℹ️ Medyayi bir grubun sesli sohbetine aktarmak icin once <code>/hedefler</code> ile grup secin.",
            reply_to_message_id=target_reply_id,
        )
        return

    gname = _get_group_name(target_chat_id)
    hedef_ek = f" (<b>{gname}</b>)" if target_chat_id != reply_chat_id else ""

    try:
        if msg.get("photo") or _detect_media_kind(msg) == "photo":
            local_path = await _extract_photo_from_msg(http, msg)
            if not local_path:
                return
            c_chat = collector_chat_id or reply_chat_id

            if user_id and (c_chat, user_id) in SLIDE_COLLECTORS and not force_presentation:
                bucket = SLIDE_COLLECTORS[(c_chat, user_id)]
                bucket.append(local_path)
                await send_msg(
                    http,
                    reply_chat_id,
                    f"🖼️ Slayta eklendi (<b>{len(bucket)}</b> gorsel). Bitirmek icin <code>/slayt bitir</code> yazin.",
                    reply_to_message_id=target_reply_id,
                )
                return

            detected_title = clean_track_title(
                custom_title or extract_media_title(msg, local_path=local_path),
                fallback="Gorsel Sunum",
            )
            items = await maybe_await(
                resolve_input_items(local_path, mode="image", requested_by=requester, custom_title=detected_title)
            )
            for _it in items:
                _it["duration"] = 86400
            status, track = await maybe_await(start_or_enqueue(target_chat_id, items))
            if status == "playing":
                await send_msg(
                    http,
                    reply_chat_id,
                    f"🖼️ <b>Gorsel Sunum Yayinda{hedef_ek}:</b> <code>{track['title']}</code>",
                    reply_markup=player_control_markup(),
                    reply_to_message_id=target_reply_id,
                )
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(target_chat_id, []))
                await send_msg(
                    http,
                    reply_chat_id,
                    f"📑 <b>Gorsel Sunum Listeye Eklendi (#{q_pos}){hedef_ek}:</b> <code>{track['title']}</code>",
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
                    reply_chat_id,
                    f"⚠️ Yanitlanan dosya <b>{kind}</b> turunde; bu komut <b>{expected_kind}</b> bekliyor.",
                    reply_to_message_id=target_reply_id,
                )
                return

            is_vid = kind == "video"
            file_name = (media_obj.get("file_name") or "").lower()
            ext = os.path.splitext(file_name)[1]
            default_suffix = ext if ext in (AUDIO_EXTS | VIDEO_EXTS) else (".mp4" if is_vid else ".mp3")
            local_path = await _download_tg_file_to_disk(http, media_obj["file_id"], default_suffix, msg=msg)
            detected_title = extract_media_title(msg, media_obj=media_obj, local_path=local_path)
            final_title = clean_track_title(custom_title or detected_title, fallback="Parca")

            items = await maybe_await(
                resolve_input_items(
                    local_path,
                    mode="video" if is_vid else "audio",
                    requested_by=requester,
                    custom_title=final_title,
                )
            )
            status, track = await maybe_await(start_or_enqueue(target_chat_id, items))
            ikon = "🎬" if is_vid else "🎵"
            if status == "playing":
                await send_msg(
                    http,
                    reply_chat_id,
                    f"✨ {ikon} <b>Dosya Yayinda{hedef_ek}:</b> <code>{track['title']}</code>",
                    reply_markup=player_control_markup(),
                    reply_to_message_id=target_reply_id,
                )
            else:
                q_pos = track.get("queue_position") or len(LISTS.get(target_chat_id, []))
                await send_msg(
                    http,
                    reply_chat_id,
                    f"📑 {ikon} <b>Dosya Listeye Eklendi (#{q_pos}){hedef_ek}:</b> <code>{track['title']}</code>",
                    reply_markup=player_control_markup(),
                    reply_to_message_id=target_reply_id,
                )
    except Exception as e:
        print(f"[MEDYA HATA] {e}", flush=True)
        await send_msg(
            http,
            reply_chat_id,
            f"❌ Medya yayina alinamadi:\n<code>{e}</code>",
            reply_to_message_id=target_reply_id,
        )


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

    if _is_duplicate_action(chat_id, user_id, f"cb:{data}", cooldown=1.0):
        await answer_cb(http, cq_id)
        return

    active_chat = get_user_target_chat(user_id, chat_id)

    if data == "panel_home":
        await edit_msg(http, chat_id, msg_id, WELCOME_TEXT, reply_markup=main_menu_markup())
        await answer_cb(http, cq_id)
        return

    if data == "tgt_refresh":
        await _refresh_bot_groups(http)
        active_chat = get_user_target_chat(user_id, chat_id)
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_targets_panel_text(active_chat),
            reply_markup=target_groups_markup(user_id, active_chat),
        )
        await answer_cb(http, cq_id, "Botun ekli oldugu gruplar guncellendi!")
        return

    if data == "vol_down":
        new_vol = await set_chat_volume(active_chat, get_chat_volume(active_chat) - 20)
        await answer_cb(http, cq_id, f"🔉 Ses: %{new_vol}")
        return

    if data == "vol_up":
        new_vol = await set_chat_volume(active_chat, get_chat_volume(active_chat) + 20)
        await answer_cb(http, cq_id, f"🔊 Ses: %{new_vol}")
        return

    if data == "vol_mute":
        cur_vol = get_chat_volume(active_chat)
        new_vol = await set_chat_volume(active_chat, 100 if cur_vol == 0 else 0)
        await answer_cb(http, cq_id, f"🔇 Ses: %{new_vol}")
        return

    if data == "panel_targets":
        await _refresh_bot_groups(http)
        active_chat = get_user_target_chat(user_id, chat_id)
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_targets_panel_text(active_chat),
            reply_markup=target_groups_markup(user_id, active_chat),
        )
        await answer_cb(http, cq_id)
        return

    if data.startswith("tgt_set_"):
        try:
            tgt_id = int(data.split("_", 2)[2])
        except ValueError:
            await answer_cb(http, cq_id, "Gecersiz grup ID", show_alert=True)
            return
        if not await _verify_bot_in_chat(http, tgt_id):
            unregister_linked_group(tgt_id)
            await edit_msg(
                http,
                chat_id,
                msg_id,
                _build_targets_panel_text(active_chat),
                reply_markup=target_groups_markup(user_id, active_chat),
            )
            await answer_cb(http, cq_id, "Bot bu grupta aktif degil, listeden cikarildi.", show_alert=True)
            return
        if user_id:
            set_user_target_chat(user_id, tgt_id)
        gname = _get_group_name(tgt_id)
        await edit_msg(
            http,
            chat_id,
            msg_id,
            f"✅ Secili grup: <b>{gname}</b> <code>({tgt_id})</code>\nSimdi dosyanizi gonderebilir veya komut kullanabilirsiniz.",
            reply_markup=target_groups_markup(user_id, tgt_id),
        )
        await answer_cb(http, cq_id, f"Secildi: {gname}")
        return

    if data == "panel_catalog":
        tracks = list_saved_tracks(12)
        if not tracks:
            txt = "💾 <b>KAYITLI SARKILAR</b>\n━━━━━━━━━━━━━━━━━━━━━━\nHenuz katalogda kayitli parca yok."
        else:
            lines = ["💾 <b>KAYITLI SARKILAR (MONGODB / KATALOG)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
            for i, t in enumerate(tracks, 1):
                lines.append(f"<b>{i}.</b> <code>{t['title']}</code>")
            lines.append("\n💡 <i>Calmak icin:</i> <code>/oynat &lt;parca_adi&gt;</code>")
            txt = "\n".join(lines)
        await edit_msg(
            http,
            chat_id,
            msg_id,
            txt,
            reply_markup={"inline_keyboard": [[{"text": "🔙 Studyo", "callback_data": "panel_home"}]]},
        )
        await answer_cb(http, cq_id)
        return

    if data == "panel_list":
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_list_text(active_chat, user_id),
            reply_markup=list_editor_markup(active_chat, user_id),
        )
        await answer_cb(http, cq_id)
        return

    if data.startswith("lst_sel_"):
        idx = int(data.split("_", 2)[2])
        if user_id:
            SELECTED_LIST_IDX[user_id] = idx
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_list_text(active_chat, user_id),
            reply_markup=list_editor_markup(active_chat, user_id),
        )
        await answer_cb(http, cq_id, f"#{idx} secildi")
        return

    if data in ("lst_up", "lst_down", "lst_top", "lst_del"):
        sel = SELECTED_LIST_IDX.get(user_id or 0, 1)
        if data == "lst_up":
            ok, info = move_item_step(active_chat, sel, -1)
            if ok and user_id:
                SELECTED_LIST_IDX[user_id] = max(1, sel - 1)
        elif data == "lst_down":
            ok, info = move_item_step(active_chat, sel, 1)
            if ok and user_id:
                SELECTED_LIST_IDX[user_id] = sel + 1
        elif data == "lst_top":
            ok, info = reorder_list(active_chat, sel, 1)
            if ok and user_id:
                SELECTED_LIST_IDX[user_id] = 1
        else:
            ok, info = remove_from_list(active_chat, sel)
        await edit_msg(
            http,
            chat_id,
            msg_id,
            _build_list_text(active_chat, user_id),
            reply_markup=list_editor_markup(active_chat, user_id),
        )
        await answer_cb(http, cq_id, info[:180])
        return

    if data == "panel_controls":
        current = NOW_PLAYING.get(active_chat)
        q_len = len(LISTS.get(active_chat, []))
        vol = get_chat_volume(active_chat)
        gname = _get_group_name(active_chat)
        status_line = (
            f"🎶 <b>Yayinda:</b> <code>{current['title']}</code>\n"
            f"🔊 <b>Ses Seviyesi:</b> <code>%{vol}</code>\n"
            f"📡 <b>Hedef:</b> <b>{gname}</b>\n"
            f"📑 <b>Siradaki Oge:</b> <code>{q_len}</code>"
            if current
            else f"🔇 <i>Aktif yayin yok.</i>\n🔊 <b>Ses Seviyesi:</b> <code>%{vol}</code>\n📡 <b>Hedef:</b> <b>{gname}</b>"
        )
        await edit_msg(
            http,
            chat_id,
            msg_id,
            f"🎛️ <b>GOLDEN STUDIO — CANLI KONTROL</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{status_line}",
            reply_markup=player_control_markup(),
        )
        await answer_cb(http, cq_id)
        return

    if data == "ctrl_save_current":
        cur = NOW_PLAYING.get(active_chat)
        if not cur:
            await answer_cb(http, cq_id, "Yayinda parca yok.", show_alert=True)
            return
        rec = save_track_to_catalog(cur)
        await answer_cb(http, cq_id, f"Kaydedildi: {rec['title']}")
        return

    if data.startswith("mode_"):
        mode = data.split("_", 1)[1]
        if user_id and mode == "slide":
            SLIDE_COLLECTORS[(chat_id, user_id)] = []
            await answer_cb(http, cq_id, "Slayt modu acildi!")
            await send_msg(http, chat_id, "🎞️ Slayt modu basladi. Fotograflari gonderin, bitince <code>/slayt bitir</code> yazin.")
            return
        if user_id and mode == "combo":
            COMBO_SESSIONS[(chat_id, user_id)] = {
                "step": "waiting_photo",
                "target_chat_id": active_chat,
                "photo_path": None,
            }
            await answer_cb(http, cq_id, "1/2 Gorsel bekleniyor!")
            await send_msg(
                http,
                chat_id,
                "🖼️🎵 <b>1/2 Adim:</b> Grup sohbetinden veya buradan once <b>resim/fotograf</b> gonderin (iptal: <code>/kombin iptal</code>).",
            )
            return
        if user_id:
            USER_MODE[user_id] = mode
        await answer_cb(http, cq_id, "Mod secildi!")
        await send_msg(http, chat_id, f"✅ Mod secildi: <code>{mode}</code>. Dosya/fotograf gonderin veya kayitli adi yazin.")
        return

    if data == "ctrl_pause":
        try:
            await maybe_await(pause_stream(active_chat))
            await answer_cb(http, cq_id, "⏸️ Duraklatildi.")
        except Exception as e:
            await answer_cb(http, cq_id, f"Hata: {e}", show_alert=True)
    elif data == "ctrl_resume":
        try:
            await maybe_await(resume_stream(active_chat))
            await answer_cb(http, cq_id, "▶️ Devam ediyor.")
        except Exception as e:
            await answer_cb(http, cq_id, f"Hata: {e}", show_alert=True)
    elif data == "ctrl_skip":
        nxt = await maybe_await(skip_track(active_chat))
        if nxt:
            await send_msg(http, chat_id, f"⏭️ Siradaki: <b>{nxt['title']}</b>")
        else:
            await send_msg(http, chat_id, "⏹️ Sira bitti, yayin kapatildi.")
        await answer_cb(http, cq_id)
    elif data == "ctrl_stop":
        await maybe_await(stop_stream(active_chat))
        await send_msg(http, chat_id, "⏹️ Yayin durduruldu ve liste temizlendi.")
        await answer_cb(http, cq_id)


async def handle_message(http: Any, msg: dict[str, Any]) -> None:
    await _track_bot_group_from_msg(http, msg)
    chat = msg.get("chat") or {}
    chat_id = chat.get("id")
    msg_id = msg.get("message_id")
    if not chat_id:
        return

    user = msg.get("from") or {}
    user_id = user.get("id")
    requester = user.get("first_name") or "Uye"
    reply_msg = msg.get("reply_to_message") or {}
    text_raw = msg.get("text") or msg.get("caption") or ""
    text = text_raw.strip()
    print(f"[MESAJ GELDI] Chat: {chat_id} | User: {user_id} | Metin: {text}", flush=True)

    target_chat_id = chat_id if chat.get("type") != "private" else get_user_target_chat(user_id, chat_id)

    if text.startswith("/"):
        first_token = text.split(None, 1)[0][1:].split("@")[0].lower()
        arg_str = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

        if first_token in ("ses", "volume", "vol"):
            if not arg_str or not arg_str.isdigit():
                cur_v = get_chat_volume(target_chat_id)
                await send_msg(
                    http,
                    chat_id,
                    f"🔊 Mevcut ses seviyesi: <code>%{cur_v}</code>\nAyarlamak icin: <code>/ses 0-200</code> (Ornek: <code>/ses 80</code>)",
                    reply_to_message_id=msg_id,
                )
                return
            new_v = await set_chat_volume(target_chat_id, int(arg_str))
            await send_msg(http, chat_id, f"🔊 Ses seviyesi <code>%{new_v}</code> olarak ayarlandi.", reply_to_message_id=msg_id)
            return

        if first_token in ("start", "studio", "baslat", "basla"):
            await send_msg(http, chat_id, WELCOME_TEXT, reply_markup=main_menu_markup(), reply_to_message_id=msg_id)
            return

        if first_token in ("hedefler", "gruplar", "aktar"):
            await _refresh_bot_groups(http)
            target_chat_id = chat_id if chat.get("type") != "private" else get_user_target_chat(user_id, chat_id)
            await send_msg(
                http,
                chat_id,
                _build_targets_panel_text(target_chat_id),
                reply_markup=target_groups_markup(user_id, target_chat_id),
                reply_to_message_id=msg_id,
            )
            return

        if first_token in ("kombin", "birlesik"):
            if not user_id:
                return
            if arg_str.lower() in ("iptal", "temizle"):
                COMBO_SESSIONS.pop((chat_id, user_id), None)
                await send_msg(http, chat_id, "🗑️ Gorsel+Sarki adimi iptal edildi.", reply_to_message_id=msg_id)
                return
            COMBO_SESSIONS[(chat_id, user_id)] = {
                "step": "waiting_photo",
                "target_chat_id": target_chat_id,
                "photo_path": None,
            }
            await send_msg(
                http,
                chat_id,
                "🖼️🎵 <b>1/2 Adim:</b> Grup sohbetinden veya buradan once <b>resim/fotograf</b> gonderin (iptal: <code>/kombin iptal</code>).",
                reply_to_message_id=msg_id,
            )
            return

        if first_token == "kaydet":
            src_msg = reply_msg if _detect_media_kind(reply_msg) in ("audio", "video") else msg
            media_obj = src_msg.get("audio") or src_msg.get("voice") or src_msg.get("video") or src_msg.get("document")
            if not media_obj:
                await send_msg(
                    http,
                    chat_id,
                    "Kaydetmek icin bir ses/video mesajina yanit verip <code>/kaydet &lt;ad&gt;</code> yazin.",
                    reply_to_message_id=msg_id,
                )
                return
            kind = _detect_media_kind(src_msg) or "audio"
            ext = ".mp4" if kind == "video" else ".mp3"
            wait_res = await send_msg(
                http,
                chat_id,
                "⏳ <b>Dosya indiriliyor ve yayina hazirlaniyor...</b>",
                reply_to_message_id=msg_id,
            )
            wait_id = (wait_res.get("result") or {}).get("message_id")
            try:
                local_path = await _download_tg_file_to_disk(http, media_obj["file_id"], ext, msg=src_msg)
                if kind == "video":
                    opt_path = os.path.join(DOWNLOAD_DIR, f"opt_{media_obj['file_id'][-12:]}.mp4")
                    proc = await asyncio.create_subprocess_exec(
                        "ffmpeg",
                        "-y",
                        "-i",
                        local_path,
                        "-vf",
                        "scale='min(1280,iw)':-2",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "ultrafast",
                        "-crf",
                        "26",
                        "-r",
                        "24",
                        "-g",
                        "48",
                        "-c:a",
                        "aac",
                        "-b:a",
                        "128k",
                        "-movflags",
                        "+faststart",
                        opt_path,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    await proc.communicate()
                    if proc.returncode == 0 and os.path.exists(opt_path):
                        try:
                            os.remove(local_path)
                        except OSError:
                            pass
                        local_path = opt_path

                auto_title = extract_media_title(src_msg, media_obj=media_obj, local_path=local_path)
                title = clean_track_title(arg_str or auto_title, fallback="Kayitli Parca")
                rec = save_track_to_catalog(
                    {
                        "kind": kind,
                        "source": local_path,
                        "title": title,
                        "is_video": kind == "video",
                        "requested_by": requester,
                    },
                    custom_title=title,
                )
                done_txt = (
                    f"💾 Kataloga kaydedildi: <code>{rec['title']}</code>\n"
                    f"Listeye almak icin: <code>/video {rec['title']}</code> veya <code>/oynat {rec['title']}</code>"
                )
                if wait_id:
                    await edit_msg(http, chat_id, wait_id, done_txt)
                else:
                    await send_msg(http, chat_id, done_txt, reply_to_message_id=msg_id)
            except Exception as e:
                err_txt = f"❌ Kaydetme hatasi:\n<code>{e}</code>"
                if wait_id:
                    await edit_msg(http, chat_id, wait_id, err_txt)
                else:
                    await send_msg(http, chat_id, err_txt, reply_to_message_id=msg_id)
            return

        if first_token in ("play", "oynat"):
            reply_kind = _detect_media_kind(reply_msg) if reply_msg else None
            if reply_kind in ("audio", "video"):
                await _handle_media_message(
                    http,
                    reply_msg,
                    requester_override=requester,
                    reply_to_id=msg_id,
                    custom_title=arg_str or None,
                )
                return
            if not arg_str:
                await send_msg(
                    http,
                    chat_id,
                    "Ses/video gonderin, yanitlayip <code>/oynat</code> yazin veya <code>/oynat &lt;kayitli_ad&gt;</code> kullanin.",
                    reply_to_message_id=msg_id,
                )
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
                    custom_title=arg_str or None,
                )
                return
            if not arg_str:
                await send_msg(
                    http,
                    chat_id,
                    "Video gonderin, yanitlayip <code>/video</code> yazin veya <code>/video &lt;kayitli_ad&gt;</code> kullanin.",
                    reply_to_message_id=msg_id,
                )
                return
            await _process_stream_request(http, msg, arg_str, is_video=True)
            return

        if first_token == "sunum":
            target = reply_msg if reply_msg.get("photo") else msg
            if not target.get("photo"):
                await send_msg(
                    http,
                    chat_id,
                    "Fotografa yanit verip <code>/sunum</code> veya <code>/sunum &lt;kayitli_sarki_adi&gt;</code> yazin.",
                    reply_to_message_id=msg_id,
                )
                return

            if arg_str:
                matched_audio = None
                needle = arg_str.lower()
                for tr in list_saved_tracks(100):
                    t_title = str(tr.get("title") or "").lower()
                    t_src = str(tr.get("source") or "")
                    if needle in t_title and t_src and os.path.exists(t_src):
                        matched_audio = tr
                        break

                if not matched_audio:
                    await send_msg(
                        http,
                        chat_id,
                        f"⚠️ Katalogda <code>{arg_str}</code> adinda kayitli sarki bulunamadi. Once <code>/kaydet</code> ile kaydedin veya sadece <code>/sunum</code> yazin.",
                        reply_to_message_id=msg_id,
                    )
                    return

                wait_res = await send_msg(
                    http,
                    chat_id,
                    f"🖼️🎵 <b>Gorsel ve <code>{matched_audio['title']}</code> birlestiriliyor...</b>",
                    reply_to_message_id=msg_id,
                )
                wait_id = (wait_res.get("result") or {}).get("message_id")
                try:
                    img_path = await _download_tg_file_to_disk(http, target["photo"][-1]["file_id"], ".jpg", msg=target)
                    combo_path = os.path.join(DOWNLOAD_DIR, f"combo_{target['photo'][-1]['file_id'][-10:]}.mp4")
                    await _merge_photo_and_audio_to_mp4(img_path, matched_audio["source"], combo_path)

                    item = {
                        "kind": "video",
                        "source": combo_path,
                        "title": f"{matched_audio['title']} (Gorselli)",
                        "duration": 86400,
                        "is_video": True,
                        "requested_by": requester,
                    }
                    status, track = await maybe_await(start_or_enqueue(target_chat_id, [item]))
                    gname = _get_group_name(target_chat_id)
                    done_txt = (
                        f"🖼️🎵 <b>Gorsel + Sarki Yayinda ({gname}):</b> <code>{track['title']}</code>"
                        if status == "playing"
                        else f"📑 <b>Gorsel + Sarki Listeye Eklendi ({gname}):</b> <code>{track['title']}</code>"
                    )
                    if wait_id:
                        await edit_msg(http, chat_id, wait_id, done_txt, reply_markup=player_control_markup())
                    else:
                        await send_msg(http, chat_id, done_txt, reply_markup=player_control_markup(), reply_to_message_id=msg_id)
                except Exception as e:
                    print(f"[SUNUM+SES HATA] {e}", flush=True)
                    err_txt = f"❌ Birlesik yayin olusturulamadi:\n<code>{e}</code>"
                    if wait_id:
                        await edit_msg(http, chat_id, wait_id, err_txt)
                    else:
                        await send_msg(http, chat_id, err_txt, reply_to_message_id=msg_id)
                return

            await _handle_media_message(
                http,
                target,
                force_presentation=True,
                requester_override=requester,
                reply_to_id=msg_id,
                custom_title=arg_str or None,
            )
            return

        if first_token == "slayt":
            if not user_id:
                return
            sub = arg_str.lower()
            if sub in ("bitir", "tamam", "baslat", "oynat"):
                await _finalize_slideshow(http, chat_id, target_chat_id, user_id, requester, reply_to_id=msg_id)
                return
            if sub in ("iptal", "temizle"):
                SLIDE_COLLECTORS.pop((chat_id, user_id), None)
                await send_msg(http, chat_id, "🗑️ Slayt listesi temizlendi.", reply_to_message_id=msg_id)
                return
            SLIDE_COLLECTORS[(chat_id, user_id)] = []
            if reply_msg.get("photo"):
                try:
                    local_path = await _download_tg_file_to_disk(http, reply_msg["photo"][-1]["file_id"], ".jpg", msg=reply_msg)
                    SLIDE_COLLECTORS[(chat_id, user_id)].append(local_path)
                    await send_msg(
                        http,
                        chat_id,
                        "🎞️ Slayt basladi, yanitlanan fotograf (<b>1</b>.) eklendi. Diger fotograflari atip <code>/slayt bitir</code> yazin.",
                        reply_to_message_id=msg_id,
                    )
                    return
                except Exception as e:
                    print(f"[SLAYT REPLY HATA] {e}", flush=True)
            await send_msg(
                http,
                chat_id,
                "🎞️ Slayt modu acildi. Fotograflari gonderin, bitince <code>/slayt bitir</code> yazin.",
                reply_to_message_id=msg_id,
            )
            return

        if first_token == "liste":
            await send_msg(
                http,
                chat_id,
                _build_list_text(target_chat_id, user_id),
                reply_markup=list_editor_markup(target_chat_id, user_id),
                reply_to_message_id=msg_id,
            )
            return

        if first_token == "sirala":
            parts = arg_str.split()
            if len(parts) < 2 or not parts[0].isdigit() or not parts[1].isdigit():
                await send_msg(
                    http,
                    chat_id,
                    "Kullanim: <code>/sirala &lt;eski&gt; &lt;yeni&gt;</code> (Orn: <code>/sirala 3 1</code>)",
                    reply_to_message_id=msg_id,
                )
                return
            ok, info_msg = reorder_list(target_chat_id, int(parts[0]), int(parts[1]))
            await send_msg(http, chat_id, info_msg if ok else f"⚠️ {info_msg}", reply_to_message_id=msg_id)
            return

        if first_token in ("pause", "duraklat"):
            try:
                await maybe_await(pause_stream(target_chat_id))
                await send_msg(http, chat_id, "⏸️ Yayin duraklatildi.", reply_to_message_id=msg_id)
            except Exception as e:
                await send_msg(http, chat_id, f"⚠️ Hata:\n<code>{e}</code>", reply_to_message_id=msg_id)
            return

        if first_token in ("resume", "devam"):
            try:
                await maybe_await(resume_stream(target_chat_id))
                await send_msg(http, chat_id, "▶️ Yayin devam ediyor.", reply_to_message_id=msg_id)
            except Exception as e:
                await send_msg(http, chat_id, f"⚠️ Hata:\n<code>{e}</code>", reply_to_message_id=msg_id)
            return

        if first_token in ("skip", "atla"):
            nxt = await maybe_await(skip_track(target_chat_id))
            await send_msg(
                http,
                chat_id,
                f"⏭️ Siradaki: <b>{nxt['title']}</b>" if nxt else "⏹️ Sira bitti.",
                reply_to_message_id=msg_id,
            )
            return

        if first_token in ("stop", "dur"):
            await maybe_await(stop_stream(target_chat_id))
            await send_msg(http, chat_id, "⏹️ Yayin durduruldu ve liste temizlendi.", reply_to_message_id=msg_id)
            return

    combo = COMBO_SESSIONS.get((chat_id, user_id)) if user_id else None
    if combo and not text.startswith("/"):
        c_target = int(combo.get("target_chat_id") or target_chat_id)
        if combo.get("step") == "waiting_photo":
            p_path = await _extract_photo_from_msg(http, msg)
            if p_path:
                combo["photo_path"] = p_path
                combo["step"] = "waiting_audio"
                await send_msg(
                    http,
                    chat_id,
                    "✅ Gorsel alindi.\n🎵 <b>2/2 Adim:</b> Grup sohbetinden veya buradan <b>sarki dosyasi</b> gonderin ya da <b>kayitli sarki adi</b> yazin.",
                    reply_to_message_id=msg_id,
                )
            else:
                await send_msg(
                    http,
                    chat_id,
                    "⚠️ Lutfen 1. adim icin once bir <b>resim/fotograf</b> gonderin (iptal: <code>/kombin iptal</code>).",
                    reply_to_message_id=msg_id,
                )
            return

        if combo.get("step") == "waiting_audio":
            photo_path = combo.get("photo_path")
            audio_path: str | None = None
            audio_title: str = "Birlesik Parca"

            media_obj = msg.get("audio") or msg.get("voice") or msg.get("document")
            if media_obj and _detect_media_kind(msg) == "audio":
                file_name = (media_obj.get("file_name") or "").lower()
                ext = os.path.splitext(file_name)[1] or ".mp3"
                audio_path = await _download_tg_file_to_disk(http, media_obj["file_id"], ext, msg=msg)
                audio_title = clean_track_title(extract_media_title(msg, media_obj=media_obj, local_path=audio_path), fallback="Ses")
            elif text:
                needle = text.lower()
                for tr in list_saved_tracks(100):
                    t_title = str(tr.get("title") or "").lower()
                    t_src = str(tr.get("source") or "")
                    if needle in t_title and t_src and os.path.exists(t_src):
                        audio_path = t_src
                        audio_title = str(tr.get("title") or text)
                        break
                if not audio_path:
                    await send_msg(
                        http,
                        chat_id,
                        f"⚠️ Katalogda <code>{text}</code> bulunamadi. Ses dosyasi gonderin veya kayitli sarki adi yazin.",
                        reply_to_message_id=msg_id,
                    )
                    return
            else:
                await send_msg(
                    http,
                    chat_id,
                    "⚠️ Lutfen 2. adim icin <b>sarki dosyasi</b> gonderin veya <b>kayitli sarki adi</b> yazin.",
                    reply_to_message_id=msg_id,
                )
                return

            COMBO_SESSIONS.pop((chat_id, user_id), None)
            wait_res = await send_msg(
                http,
                chat_id,
                f"⏳ <b>Gorsel ve <code>{audio_title}</code> birlestiriliyor...</b>",
                reply_to_message_id=msg_id,
            )
            wait_id = (wait_res.get("result") or {}).get("message_id")
            try:
                combo_path = os.path.join(DOWNLOAD_DIR, f"combo_{msg_id}_{user_id}.mp4")
                await _merge_photo_and_audio_to_mp4(photo_path, audio_path, combo_path)

                item = {
                    "kind": "video",
                    "source": combo_path,
                    "title": f"{audio_title} (Gorselli)",
                    "duration": 86400,
                    "is_video": True,
                    "requested_by": requester,
                }
                status, track = await maybe_await(start_or_enqueue(c_target, [item]))
                gname = _get_group_name(c_target)
                done_txt = (
                    f"🖼️🎵 <b>Gorsel + Sarki Yayinda ({gname}):</b> <code>{track['title']}</code>"
                    if status == "playing"
                    else f"📑 <b>Gorsel + Sarki Listeye Eklendi ({gname}):</b> <code>{track['title']}</code>"
                )
                if wait_id:
                    await edit_msg(http, chat_id, wait_id, done_txt, reply_markup=player_control_markup())
                else:
                    await send_msg(http, chat_id, done_txt, reply_markup=player_control_markup(), reply_to_message_id=msg_id)
            except Exception as e:
                print(f"[KOMBIN HATA] {e}", flush=True)
                err_txt = f"❌ Birlesik yayin olusturulamadi:\n<code>{e}</code>"
                if wait_id:
                    await edit_msg(http, chat_id, wait_id, err_txt)
                else:
                    await send_msg(http, chat_id, err_txt, reply_to_message_id=msg_id)
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
            await send_msg(
                http,
                chat_id,
                "🖼️ Fotografa yanit verip <code>/sunum</code> yazin veya dogrudan fotograf gonderin.",
                reply_to_message_id=msg_id,
            )


async def polling_loop(http: Any) -> None:
    global BOT_ID
    await tg_call(http, "deleteWebhook", {"drop_pending_updates": False})
    me_res = await tg_call(http, "getMe")
    me = me_res.get("result") or {}
    if me.get("id"):
        BOT_ID = int(me["id"])
    print(f"AKTIF BOT (BOT API): @{me.get('username')} (ID: {me.get('id')})", flush=True)
    await _refresh_bot_groups(http)

    offset: int | None = None
    conflict_streak = 0
    while True:
        try:
            params: dict[str, Any] = {
                "timeout": 25,
                "allowed_updates": ["message", "callback_query", "my_chat_member"],
            }
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
                elif "my_chat_member" in upd:
                    asyncio.create_task(handle_my_chat_member(http, upd["my_chat_member"]))
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
