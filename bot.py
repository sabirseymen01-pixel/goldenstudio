import asyncio
import inspect
import os
from typing import Any
import aiohttp
from aiohttp import web
from config import BOT_TOKEN, PORT, DOWNLOAD_DIR
from media_engine import download_media
from player import (
    assistant,
    call_py,
    play_or_queue,
    start_or_enqueue,
    skip_track,
    stop_stream,
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

ALL_COMMANDS = {
    "start", "studio", "baslat",
    "play", "oynat",
    "vplay", "video",
    "sunum",
    "liste", "sirala",
    "mp3", "indir_mp3",
    "mp4", "indir_mp4",
    "pause", "duraklat",
    "resume", "devam",
    "skip", "atla",
    "stop", "dur",
}

WELCOME_TEXT = (
    "✨ <b>GOLDEN STUDIO — VIP MEDYA & YAYIN MERKEZİ</b> ✨\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Hoş geldiniz. <b>Golden Studio</b>, grubunuz ve özel sohbetleriniz için "
    "stüdyo kalitesinde ses, HD video/görsel sunum yayını, liste yönetimi ve yüksek hızlı medya aktarımı sunar.\n\n"
    "🎛️ <b>Neler Yapabilirsiniz?</b>\n"
    "• <b>Sesli Sohbet Yayını:</b> Şarkı, video, YouTube linki/listesi veya sohbete atılan medya dosyalarını açar.\n"
    "• <b>Resim Sunumu:</b> Gönderilen fotoğrafları sesli sohbette sunum videosuna çevirir.\n"
    "• <b>Üstten Aşağı Liste:</b> <code>/liste</code> ve <code>/sirala</code> ile sırayı istediğiniz gibi düzenleyin.\n\n"
    "👇 <i>Başlamak için aşağıdaki stüdyo butonlarından birini seçin veya doğrudan link/şarkı/dosya gönderin:</i>"
)


def main_menu_markup() -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "🎵 Sesli Sohbette Çal", "callback_data": "mode_audio"},
                {"text": "🎬 Sesli Sohbette Video Aç", "callback_data": "mode_video"},
            ],
            [
                {"text": "🖼️ Resim Sunumu", "callback_data": "mode_image"},
                {"text": "📋 Liste & Sırala", "callback_data": "panel_list"},
            ],
            [
                {"text": "🎧 MP3 İndir (320kbps)", "callback_data": "mode_mp3"},
                {"text": "📹 MP4 İndir (720p)", "callback_data": "mode_mp4"},
            ],
            [
                {"text": "🎛️ Canlı Kontrol Paneli", "callback_data": "panel_controls"},
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
        return "Canlı / Sunum"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


def _build_list_text(chat_id: int) -> str:
    lst = get_list(chat_id)
    current = NOW_PLAYING.get(chat_id)
    lines = [
        "📋 <b>GOLDEN STUDIO — ÜSTTEN AŞAĞI LİSTE</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if current:
        ikon_now = "🖼️" if current.get("kind") == "image" else ("🎬" if current.get("is_video") else "🎵")
        lines.append(f"▶️ <b>Şu An Yayında:</b> {ikon_now} <code>{current['title']}</code>")
    if not lst:
        lines.append("Sırada bekleyen başka öğe yok.")
    else:
        for idx, item in enumerate(lst, 1):
            ikon = "🖼️" if item.get("kind") == "image" else ("🎬" if item.get("is_video") else "🎵")
            lines.append(f"<b>{idx}.</b> {ikon} <code>{item['title']}</code>")
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


async def delete_msg(http: Any, chat_id: int | str, message_id: int) -> None:
    await tg_call(http, "deleteMessage", {"chat_id": chat_id, "message_id": message_id})


async def maybe_await(val: Any) -> Any:
    if inspect.isawaitable(val):
        return await val
    return val


async def _process_stream_request(http: Any, msg: dict[str, Any], query_text: str, is_video: bool) -> None:
    chat = msg["chat"]
    chat_id = chat["id"]
    msg_id = msg["message_id"]
    if chat.get("type") == "private":
        await send_msg(
            http,
            chat_id,
            "⚠️ Sesli sohbet yayını yalnızca <b>gruplarda</b> başlatılabilir.\n"
            "Özel sohbette kullanmak için <b>🎧 MP3 İndir</b> veya <b>📹 MP4 İndir</b> butonlarını seçebilirsiniz.",
            reply_to_message_id=msg_id,
        )
        return

    wait_res = await send_msg(
        http,
        chat_id,
        "⏳ <b>Golden Studio</b> akışı ve listeyi hazırlıyor, lütfen bekleyin...",
        reply_to_message_id=msg_id,
    )
    wait_id = (wait_res.get("result") or {}).get("message_id")
    requester = (msg.get("from") or {}).get("first_name") or "Üye"

    try:
        status, track = await maybe_await(play_or_queue(chat_id, query_text, is_video, requester))
        dur = format_duration(track.get("duration", 0))
        kind = "🎬 HD Video Yayını" if is_video else "🎵 Stüdyo Ses Yayını"

        if status == "playing":
            url = track.get("webpage_url") or query_text
            title_html = (
                f'<a href="{url}">{track["title"]}</a>'
                if isinstance(url, str) and url.startswith(("http://", "https://"))
                else f"<code>{track['title']}</code>"
            )
            text = (
                f"✨ <b>GOLDEN STUDIO — YAYINDA</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎶 <b>Parça:</b> {title_html}\n"
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

        if wait_id:
            await edit_msg(http, chat_id, wait_id, text, reply_markup=player_control_markup())
        else:
            await send_msg(http, chat_id, text, reply_markup=player_control_markup())
    except Exception as e:
        err_text = f"❌ Yayın başlatılamadı:\n<code>{e}</code>"
        if wait_id:
            await edit_msg(http, chat_id, wait_id, err_text)
        else:
            await send_msg(http, chat_id, err_text)


async def _process_download_request(http: Any, msg: dict[str, Any], query_text: str, mode: str) -> None:
    chat_id = msg["chat"]["id"]
    msg_id = msg["message_id"]
    wait_res = await send_msg(
        http,
        chat_id,
        f"⏳ <b>Golden Studio</b> <code>{mode.upper()}</code> formatında hazırlıyor...",
        reply_to_message_id=msg_id,
    )
    wait_id = (wait_res.get("result") or {}).get("message_id")
    file_path = None

    try:
        result = await maybe_await(download_media(query_text, mode=mode))
        file_path = result["file_path"]
        if wait_id:
            await edit_msg(http, chat_id, wait_id, "📤 Dosya sohbete yükleniyor...")

        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("reply_to_message_id", str(msg_id))
        form.add_field("parse_mode", "HTML")

        if mode == "mp3":
            form.add_field("title", str(result.get("title") or "Golden Studio"))
            form.add_field("performer", str(result.get("performer") or "Golden Studio"))
            form.add_field("duration", str(int(result.get("duration") or 0)))
            form.add_field("caption", f"🎧 <b>{result['title']}</b>\n✨ <i>Golden Studio VIP Audio</i>")
            with open(file_path, "rb") as f:
                form.add_field("audio", f, filename=os.path.basename(file_path))
                async with http.post(f"{API_BASE}/sendAudio", data=form, timeout=180) as r:
                    data = await r.json()
                    if not data.get("ok"):
                        raise RuntimeError(str(data))
        else:
            form.add_field("duration", str(int(result.get("duration") or 0)))
            form.add_field("supports_streaming", "true")
            form.add_field("caption", f"🎬 <b>{result['title']}</b>\n✨ <i>Golden Studio VIP Video</i>")
            with open(file_path, "rb") as f:
                form.add_field("video", f, filename=os.path.basename(file_path))
                async with http.post(f"{API_BASE}/sendVideo", data=form, timeout=180) as r:
                    data = await r.json()
                    if not data.get("ok"):
                        raise RuntimeError(str(data))

        if wait_id:
            await delete_msg(http, chat_id, wait_id)
    except Exception as e:
        err_text = f"❌ İndirme sırasında hata oluştu:\n<code>{e}</code>"
        if wait_id:
            await edit_msg(http, chat_id, wait_id, err_text)
        else:
            await send_msg(http, chat_id, err_text)
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


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


async def _handle_media_message(http: Any, msg: dict[str, Any]) -> None:
    chat = msg["chat"]
    chat_id = chat["id"]
    msg_id = msg["message_id"]
    if chat.get("type") == "private":
        await send_msg(
            http,
            chat_id,
            "ℹ️ Medya dosyalarını sesli sohbette açmak veya listeye eklemek için grupta gönderin.",
            reply_to_message_id=msg_id,
        )
        return

    requester = (msg.get("from") or {}).get("first_name") or "Üye"

    if msg.get("photo"):
        best_photo = msg["photo"][-1]
        local_path = await _download_tg_file_to_disk(http, best_photo["file_id"], ".jpg")
        items = await maybe_await(resolve_input_items(local_path, mode="image", requested_by=requester))
        status, track = await maybe_await(start_or_enqueue(chat_id, items))
        if status == "playing":
            await send_msg(http, chat_id, f"🖼️ <b>Görsel Sunum Yayında:</b> <code>{track['title']}</code>", reply_markup=player_control_markup())
        else:
            q_pos = track.get("queue_position") or len(LISTS.get(chat_id, []))
            await send_msg(
                http,
                chat_id,
                f"📑 <b>Görsel Sunum Listeye Eklendi (#{q_pos}):</b> <code>{track['title']}</code>",
                reply_markup=player_control_markup(),
            )
        return

    media_obj = msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document")
    if media_obj:
        mime = media_obj.get("mime_type") or ""
        is_vid = bool(msg.get("video")) or mime.startswith("video/")
        is_aud = bool(msg.get("audio") or msg.get("voice")) or mime.startswith("audio/")
        if not (is_vid or is_aud):
            return
        local_path = await _download_tg_file_to_disk(http, media_obj["file_id"], ".mp4" if is_vid else ".mp3")
        items = await maybe_await(
            resolve_input_items(local_path, mode="video" if is_vid else "audio", requested_by=requester)
        )
        status, track = await maybe_await(start_or_enqueue(chat_id, items))
        ikon = "🎬" if is_vid else "🎵"
        if status == "playing":
            await send_msg(http, chat_id, f"✨ {ikon} <b>Dosya Yayında:</b> <code>{track['title']}</code>", reply_markup=player_control_markup())
        else:
            q_pos = track.get("queue_position") or len(LISTS.get(chat_id, []))
            await send_msg(
                http,
                chat_id,
                f"📑 {ikon} <b>Dosya Listeye Eklendi (#{q_pos}):</b> <code>{track['title']}</code>",
                reply_markup=player_control_markup(),
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
            "🎵 <code>/oynat</code> veya <code>/play &lt;şarkı/link/liste&gt;</code> — Sesli sohbette çalar veya listeye ekler\n"
            "🎬 <code>/video</code> veya <code>/vplay &lt;video/link&gt;</code> — Sesli sohbette video açar\n"
            "🖼️ <code>/sunum</code> — Yanıtlanan fotoğrafı görsel sunum olarak açar\n"
            "📋 <code>/liste</code> — Üstten aşağı listeyi gösterir\n"
            "🔢 <code>/sirala &lt;eski_no&gt; &lt;yeni_no&gt;</code> — Sırayı değiştirir (Örn: <code>/sirala 3 1</code>)\n"
            "🎧 <code>/indir_mp3</code> veya <code>/mp3 &lt;şarkı/link&gt;</code> — 320kbps MP3 gönderir\n"
            "📹 <code>/indir_mp4</code> veya <code>/mp4 &lt;video/link&gt;</code> — 720p MP4 gönderir\n"
            "⏸️ <code>/duraklat</code> • ▶️ <code>/devam</code> • ⏭️ <code>/atla</code> • ⏹️ <code>/dur</code>\n\n"
            "💡 <i>İpucu:</i> Sohbete doğrudan ses, video veya fotoğraf atarak da oynatabilir ya da listeye ekleyebilirsiniz."
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
        if user_id:
            USER_MODE[user_id] = mode
        labels = {
            "audio": "🎵 <b>Sesli Sohbette Müzik Yayını</b>",
            "video": "🎬 <b>Sesli Sohbette Video Yayını</b>",
            "image": "🖼️ <b>Görsel Sunum Yayını</b>",
            "mp3": "🎧 <b>320kbps MP3 İndirme</b>",
            "mp4": "📹 <b>720p MP4 Video İndirme</b>",
        }
        hint = (
            "✍️ Şimdi bir <b>fotoğraf</b> gönderin:"
            if mode == "image"
            else "✍️ Şimdi <b>şarkı adını</b>, <b>YouTube bağlantısını/listesini</b> veya <b>medya dosyasını</b> gönderin:"
        )
        await answer_cb(http, cq_id, "Mod seçildi!", show_alert=False)
        await send_msg(http, chat_id, f"✅ Aktif Mod: {labels.get(mode, mode)}\n{hint}")
        return

    if data == "ctrl_pause":
        try:
            await maybe_await(call_py.pause_stream(chat_id))
            await answer_cb(http, cq_id, "⏸️ Yayın duraklatıldı.")
        except Exception as e:
            await answer_cb(http, cq_id, f"Hata: {e}", show_alert=True)
    elif data == "ctrl_resume":
        try:
            await maybe_await(call_py.resume_stream(chat_id))
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
    text_raw = msg.get("text") or msg.get("caption") or ""
    text = text_raw.strip()
    print(f"[MESAJ GELDI] Chat: {chat_id} | User: {user_id} | Metin: {text}", flush=True)

    if text.startswith("/"):
        first_token = text.split(None, 1)[0][1:].split("@")[0].lower()
        arg_str = text.split(None, 1)[1].strip() if len(text.split(None, 1)) > 1 else ""

        if first_token in ("start", "studio", "baslat"):
            print(f"[BASLAT TETIKLENDI] User: {user_id} | Chat: {chat_id}", flush=True)
            await send_msg(http, chat_id, WELCOME_TEXT, reply_markup=main_menu_markup(), reply_to_message_id=msg_id)
            return

        if first_token in ("play", "oynat"):
            if not arg_str:
                await send_msg(http, chat_id, "Kullanım: <code>/oynat &lt;şarkı adı, YouTube linki veya oynatma listesi&gt;</code>", reply_to_message_id=msg_id)
                return
            await _process_stream_request(http, msg, arg_str, is_video=False)
            return

        if first_token in ("vplay", "video"):
            if not arg_str:
                await send_msg(http, chat_id, "Kullanım: <code>/video &lt;video adı veya YouTube linki&gt;</code>", reply_to_message_id=msg_id)
                return
            await _process_stream_request(http, msg, arg_str, is_video=True)
            return

        if first_token == "sunum":
            target = msg.get("reply_to_message") if (msg.get("reply_to_message") or {}).get("photo") else msg
            if not target.get("photo"):
                await send_msg(http, chat_id, "Bir fotoğrafı yanıtlayıp <code>/sunum</code> yazın veya doğrudan fotoğraf gönderin.", reply_to_message_id=msg_id)
                return
            await _handle_media_message(http, target)
            return

        if first_token == "liste":
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

        if first_token in ("mp3", "indir_mp3"):
            if not arg_str:
                await send_msg(http, chat_id, "Kullanım: <code>/indir_mp3 &lt;şarkı adı veya link&gt;</code>", reply_to_message_id=msg_id)
                return
            await _process_download_request(http, msg, arg_str, mode="mp3")
            return

        if first_token in ("mp4", "indir_mp4"):
            if not arg_str:
                await send_msg(http, chat_id, "Kullanım: <code>/indir_mp4 &lt;video adı veya link&gt;</code>", reply_to_message_id=msg_id)
                return
            await _process_download_request(http, msg, arg_str, mode="mp4")
            return

        if first_token in ("pause", "duraklat"):
            await maybe_await(call_py.pause_stream(chat_id))
            await send_msg(http, chat_id, "⏸️ Yayın duraklatıldı.", reply_to_message_id=msg_id)
            return

        if first_token in ("resume", "devam"):
            await maybe_await(call_py.resume_stream(chat_id))
            await send_msg(http, chat_id, "▶️ Yayın devam ediyor.", reply_to_message_id=msg_id)
            return

        if first_token in ("skip", "atla"):
            nxt = await maybe_await(skip_track(chat_id))
            if nxt:
                await send_msg(http, chat_id, f"⏭️ Sıradaki öğeye geçildi: <b>{nxt['title']}</b>", reply_to_message_id=msg_id)
            else:
                await send_msg(http, chat_id, "⏹️ Sıra bitti, yayın kapatıldı.", reply_to_message_id=msg_id)
            return

        if first_token in ("stop", "dur"):
            await maybe_await(stop_stream(chat_id))
            await send_msg(http, chat_id, "⏹️ <b>Golden Studio</b> yayını durduruldu ve liste temizlendi.", reply_to_message_id=msg_id)
            return

    if msg.get("photo") or msg.get("video") or msg.get("audio") or msg.get("voice") or msg.get("document"):
        await _handle_media_message(http, msg)
        return

    if text and user_id:
        if user_id in USER_MODE:
            mode = USER_MODE.pop(user_id)
            if mode == "audio":
                await _process_stream_request(http, msg, text, is_video=False)
            elif mode == "video":
                await _process_stream_request(http, msg, text, is_video=True)
            elif mode == "image":
                await send_msg(http, chat_id, "🖼️ Görsel sunum için lütfen bir fotoğraf gönderin veya fotoğrafa yanıt verip <code>/sunum</code> yazın.", reply_to_message_id=msg_id)
            elif mode in ("mp3", "mp4"):
                await _process_download_request(http, msg, text, mode=mode)
            return

        if chat.get("type") == "private" and text.startswith(("http://", "https://")):
            USER_MODE[user_id] = "mp3"
            await send_msg(
                http,
                chat_id,
                "🔗 Link algılandı! Nasıl indirmek istersiniz?",
                reply_markup={
                    "inline_keyboard": [
                        [
                            {"text": "🎧 MP3 Olarak Gönder", "callback_data": "mode_mp3"},
                            {"text": "📹 MP4 Olarak Gönder", "callback_data": "mode_mp4"},
                        ]
                    ]
                },
                reply_to_message_id=msg_id,
            )


async def polling_loop(http: Any) -> None:
    await tg_call(http, "deleteWebhook", {"drop_pending_updates": False})
    me_res = await tg_call(http, "getMe")
    me = me_res.get("result") or {}
    print(f"AKTIF BOT (BOT API): @{me.get('username')} (ID: {me.get('id')})", flush=True)

    offset: int | None = None
    while True:
        try:
            params: dict[str, Any] = {"timeout": 25, "allowed_updates": ["message", "callback_query"]}
            if offset is not None:
                params["offset"] = offset
            url = f"{API_BASE}/getUpdates"
            async with http.get(url, params=params, timeout=35) as resp:
                data = await resp.json()
            if not data.get("ok"):
                print(f"[GETUPDATES HATA] {data}", flush=True)
                await asyncio.sleep(3)
                continue

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
            await asyncio.sleep(3)


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
        await maybe_await(assistant.start())
        await maybe_await(call_py.start())
        print("Asistan ve PyTgCalls baslatildi!", flush=True)
    except Exception as e:
        print(f"[ASISTAN UYARISI] {e}", flush=True)

    print("Golden Studio basariyla baslatildi!", flush=True)

    async with aiohttp.ClientSession() as http:
        await polling_loop(http)


if __name__ == "__main__":
    asyncio.run(main())
