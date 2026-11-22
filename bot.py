import asyncio
import os
from aiohttp import web
from pyrogram import Client, filters
from pyrogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from config import API_ID, API_HASH, BOT_TOKEN, PORT, DOWNLOAD_DIR
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

bot = Client(
    name="golden_studio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
    in_memory=True,
)

USER_MODE: dict[int, str] = {}

ALL_COMMANDS = [
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
]


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
        "📋 **GOLDEN STUDIO — ÜSTTEN AŞAĞI LİSTE**",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if current:
        ikon_now = "🖼️" if current.get("kind") == "image" else ("🎬" if current.get("is_video") else "🎵")
        lines.append(f"▶️ **Şu An Yayında:** {ikon_now} `{current['title']}`")
    if not lst:
        lines.append("Sırada bekleyen başka öğe yok.")
    else:
        for idx, item in enumerate(lst, 1):
            ikon = "🖼️" if item.get("kind") == "image" else ("🎬" if item.get("is_video") else "🎵")
            lines.append(f"**{idx}.** {ikon} `{item['title']}`")
    lines.append("\n💡 *Sıra değiştirmek için:* `/sirala <eski_no> <yeni_no>` *(Örn: `/sirala 3 1`)*")
    return "\n".join(lines)


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎵 Sesli Sohbette Çal", callback_data="mode_audio"),
                InlineKeyboardButton("🎬 Sesli Sohbette Video Aç", callback_data="mode_video"),
            ],
            [
                InlineKeyboardButton("🖼️ Resim Sunumu", callback_data="mode_image"),
                InlineKeyboardButton("📋 Liste & Sırala", callback_data="panel_list"),
            ],
            [
                InlineKeyboardButton("🎧 MP3 İndir (320kbps)", callback_data="mode_mp3"),
                InlineKeyboardButton("📹 MP4 İndir (720p)", callback_data="mode_mp4"),
            ],
            [
                InlineKeyboardButton("🎛️ Canlı Kontrol Paneli", callback_data="panel_controls"),
                InlineKeyboardButton("📜 Komut Rehberi", callback_data="panel_help"),
            ],
        ]
    )


def player_control_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("⏸️ Duraklat", callback_data="ctrl_pause"),
                InlineKeyboardButton("▶️ Devam Et", callback_data="ctrl_resume"),
                InlineKeyboardButton("⏭️ Sıradaki", callback_data="ctrl_skip"),
            ],
            [
                InlineKeyboardButton("📋 Liste", callback_data="panel_list"),
                InlineKeyboardButton("⏹️ Durdur", callback_data="ctrl_stop"),
                InlineKeyboardButton("🔙 Stüdyo", callback_data="panel_home"),
            ],
        ]
    )


WELCOME_TEXT = (
    "✨ **GOLDEN STUDIO — VIP MEDYA & YAYIN MERKEZİ** ✨\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Hoş geldiniz. **Golden Studio**, grubunuz ve özel sohbetleriniz için "
    "stüdyo kalitesinde ses, HD video/görsel sunum yayını, liste yönetimi ve yüksek hızlı medya aktarımı sunar.\n\n"
    "🎛️ **Neler Yapabilirsiniz?**\n"
    "• **Sesli Sohbet Yayını:** Şarkı, video, YouTube linki/listesi veya sohbete atılan medya dosyalarını açar.\n"
    "• **Resim Sunumu:** Gönderilen fotoğrafları sesli sohbette sunum videosuna çevirir.\n"
    "• **Üstten Aşağı Liste:** `/liste` ve `/sirala` ile sırayı istediğiniz gibi düzenleyin.\n\n"
    "👇 *Başlamak için aşağıdaki stüdyo butonlarından birini seçin veya doğrudan link/şarkı/dosya gönderin:*"
)


@bot.on_message(filters.all, group=-1)
async def _debug_log(_, message: Message):
    print(f"[MESAJ GELDI] Chat: {message.chat.id} | Metin: {message.text}", flush=True)


@bot.on_message(filters.command(["start", "studio", "baslat"]))
async def cmd_start(_, message: Message):
    print(f"[BASLAT TETIKLENDI] User: {message.from_user.id if message.from_user else 'Yok'} | Chat: {message.chat.id}", flush=True)
    await message.reply_text(
        WELCOME_TEXT,
        reply_markup=main_menu_keyboard(),
        disable_web_page_preview=True,
    )


@bot.on_callback_query()
async def handle_callbacks(_, query: CallbackQuery):
    data = query.data
    user_id = query.from_user.id
    chat_id = query.message.chat.id

    if data == "panel_home":
        await query.message.edit_text(WELCOME_TEXT, reply_markup=main_menu_keyboard())
        return

    if data == "panel_list":
        await query.message.edit_text(
            _build_list_text(chat_id),
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("⏭️ Sıradaki", callback_data="ctrl_skip"),
                        InlineKeyboardButton("🔙 Stüdyo", callback_data="panel_home"),
                    ]
                ]
            ),
        )
        return

    if data == "panel_help":
        help_text = (
            "📜 **GOLDEN STUDIO — KOMUT REHBERİ**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎵 `/oynat` veya `/play <şarkı/link/liste>` — Sesli sohbette çalar veya listeye ekler\n"
            "🎬 `/video` veya `/vplay <video/link>` — Sesli sohbette video açar\n"
            "🖼️ `/sunum` — Yanıtlanan fotoğrafı görsel sunum olarak açar\n"
            "📋 `/liste` — Üstten aşağı listeyi gösterir\n"
            "🔢 `/sirala <eski_no> <yeni_no>` — Sırayı değiştirir (Örn: `/sirala 3 1`)\n"
            "🎧 `/indir_mp3` veya `/mp3 <şarkı/link>` — 320kbps MP3 gönderir\n"
            "📹 `/indir_mp4` veya `/mp4 <video/link>` — 720p MP4 gönderir\n"
            "⏸️ `/duraklat` • ▶️ `/devam` • ⏭️ `/atla` • ⏹️ `/dur`\n\n"
            "💡 *İpucu:* Sohbete doğrudan ses, video veya fotoğraf atarak da oynatabilir ya da listeye ekleyebilirsiniz."
        )
        await query.message.edit_text(
            help_text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔙 Ana Stüdyo", callback_data="panel_home")]]
            ),
        )
        return

    if data == "panel_controls":
        current = NOW_PLAYING.get(chat_id)
        q_len = len(LISTS.get(chat_id, []))
        status_line = (
            f"🎶 **Şu An Yayında:** `{current['title']}`\n"
            f"📡 **Mod:** {'🎬 Video / Sunum Yayını' if current.get('is_video') else '🎵 Stüdyo Ses'}\n"
            f"📑 **Sıradaki Öğe Sayısı:** `{q_len}`"
            if current
            else "🔇 *Şu anda aktif bir sesli sohbet yayını bulunmuyor.*"
        )
        await query.message.edit_text(
            f"🎛️ **GOLDEN STUDIO — CANLI KONTROL PANELİ**\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n{status_line}",
            reply_markup=player_control_keyboard(),
        )
        return

    if data.startswith("mode_"):
        mode = data.split("_", 1)[1]
        USER_MODE[user_id] = mode
        labels = {
            "audio": "🎵 **Sesli Sohbette Müzik Yayını**",
            "video": "🎬 **Sesli Sohbette Video Yayını**",
            "image": "🖼️ **Görsel Sunum Yayını**",
            "mp3": "🎧 **320kbps MP3 İndirme**",
            "mp4": "📹 **720p MP4 Video İndirme**",
        }
        hint = (
            "✍️ Şimdi bir **fotoğraf** gönderin:"
            if mode == "image"
            else "✍️ Şimdi **şarkı adını**, **YouTube bağlantısını/listesini** veya **medya dosyasını** gönderin:"
        )
        await query.answer("Mod seçildi!", show_alert=False)
        await query.message.reply_text(f"✅ Aktif Mod: {labels[mode]}\n{hint}")
        return

    if data == "ctrl_pause":
        try:
            await call_py.pause_stream(chat_id)
            await query.answer("⏸️ Yayın duraklatıldı.")
        except Exception as e:
            await query.answer(f"Hata: {e}", show_alert=True)
    elif data == "ctrl_resume":
        try:
            await call_py.resume_stream(chat_id)
            await query.answer("▶️ Yayın devam ediyor.")
        except Exception as e:
            await query.answer(f"Hata: {e}", show_alert=True)
    elif data == "ctrl_skip":
        nxt = await skip_track(chat_id)
        if nxt:
            await query.message.reply_text(f"⏭️ Sıradaki öğeye geçildi: **{nxt['title']}**")
        else:
            await query.message.reply_text("⏹️ Sırada başka öğe kalmadı, yayın sonlandırıldı.")
        await query.answer()
    elif data == "ctrl_stop":
        await stop_stream(chat_id)
        await query.message.reply_text("⏹️ **Golden Studio** yayını durduruldu ve liste temizlendi.")
        await query.answer()


async def _process_stream_request(message: Message, query_text: str, is_video: bool):
    if message.chat.type.value == "private":
        await message.reply_text(
            "⚠️ Sesli sohbet yayını yalnızca **gruplarda** başlatılabilir.\n"
            "Özel sohbette kullanmak için **🎧 MP3 İndir** veya **📹 MP4 İndir** butonlarını seçebilirsiniz."
        )
        return

    wait_msg = await message.reply_text("⏳ **Golden Studio** akışı ve listeyi hazırlıyor, lütfen bekleyin...")
    try:
        requester = message.from_user.first_name if message.from_user else "Üye"
        status, track = await play_or_queue(message.chat.id, query_text, is_video, requester)
        dur = format_duration(track.get("duration", 0))
        kind = "🎬 HD Video Yayını" if is_video else "🎵 Stüdyo Ses Yayını"

        if status == "playing":
            url = track.get("webpage_url") or query_text
            title_md = f"[{track['title']}]({url})" if url.startswith(("http://", "https://")) else f"`{track['title']}`"
            await wait_msg.edit_text(
                f"✨ **GOLDEN STUDIO — YAYINDA**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎶 **Parça:** {title_md}\n"
                f"⏱️ **Süre:** `{dur}`\n"
                f"📡 **Format:** {kind}\n"
                f"👤 **İsteyen:** {requester}",
                reply_markup=player_control_keyboard(),
                disable_web_page_preview=True,
            )
        else:
            q_pos = track.get("queue_position") or len(LISTS.get(message.chat.id, []))
            await wait_msg.edit_text(
                f"📑 **Listeye Eklendi (#{q_pos})**\n"
                f"🎶 **Parça:** `{track['title']}` (`{dur}`)\n"
                f"📡 **Format:** {kind}",
                reply_markup=player_control_keyboard(),
            )
    except Exception as e:
        await wait_msg.edit_text(f"❌ Yayın başlatılamadı:\n`{e}`")


async def _process_download_request(message: Message, query_text: str, mode: str):
    wait_msg = await message.reply_text(
        f"⏳ **Golden Studio** `{mode.upper()}` formatında hazırlıyor..."
    )
    file_path = None
    try:
        result = await download_media(query_text, mode=mode)
        file_path = result["file_path"]
        await wait_msg.edit_text("📤 Dosya sohbete yükleniyor...")

        if mode == "mp3":
            await message.reply_audio(
                audio=file_path,
                title=result["title"],
                performer=result["performer"],
                duration=result["duration"],
                caption=f"🎧 **{result['title']}**\n✨ *Golden Studio VIP Audio*",
            )
        else:
            await message.reply_video(
                video=file_path,
                duration=result["duration"],
                supports_streaming=True,
                caption=f"🎬 **{result['title']}**\n✨ *Golden Studio VIP Video*",
            )
        await wait_msg.delete()
    except Exception as e:
        await wait_msg.edit_text(f"❌ İndirme sırasında hata oluştu:\n`{e}`")
    finally:
        if file_path and os.path.exists(file_path):
            try:
                os.remove(file_path)
            except Exception:
                pass


async def _handle_media_message(message: Message):
    if message.chat.type.value == "private":
        await message.reply_text("ℹ️ Medya dosyalarını sesli sohbette açmak veya listeye eklemek için grupta gönderin.")
        return

    requester = message.from_user.first_name if message.from_user else "Üye"
    chat_id = message.chat.id
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    if message.photo:
        local_path = await message.download(file_name=os.path.join(DOWNLOAD_DIR, ""))
        items = await resolve_input_items(local_path, mode="image", requested_by=requester)
        status, track = await start_or_enqueue(chat_id, items)
        if status == "playing":
            await message.reply_text(
                f"🖼️ **Görsel Sunum Yayında:** `{track['title']}`",
                reply_markup=player_control_keyboard(),
            )
        else:
            await message.reply_text(
                f"📑 **Görsel Sunum Listeye Eklendi (#{track.get('queue_position', len(LISTS.get(chat_id, [])))}):** `{track['title']}`",
                reply_markup=player_control_keyboard(),
            )
        return

    if message.video or message.audio or message.voice or message.document:
        media_obj = message.video or message.audio or message.voice or message.document
        mime = getattr(media_obj, "mime_type", "") or ""
        is_vid = bool(message.video) or mime.startswith("video/")
        is_aud = bool(message.audio or message.voice) or mime.startswith("audio/")

        if not (is_vid or is_aud):
            return

        local_path = await message.download(file_name=os.path.join(DOWNLOAD_DIR, ""))
        items = await resolve_input_items(local_path, mode="video" if is_vid else "audio", requested_by=requester)
        status, track = await start_or_enqueue(chat_id, items)
        ikon = "🎬" if is_vid else "🎵"
        if status == "playing":
            await message.reply_text(
                f"✨ {ikon} **Dosya Yayında:** `{track['title']}`",
                reply_markup=player_control_keyboard(),
            )
        else:
            await message.reply_text(
                f"📑 {ikon} **Dosya Listeye Eklendi (#{track.get('queue_position', len(LISTS.get(chat_id, [])))}):** `{track['title']}`",
                reply_markup=player_control_keyboard(),
            )


@bot.on_message(filters.command(["play", "oynat"]))
async def cmd_play(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/oynat <şarkı adı, YouTube linki veya oynatma listesi>`")
    await _process_stream_request(message, message.text.split(None, 1)[1], is_video=False)


@bot.on_message(filters.command(["vplay", "video"]))
async def cmd_vplay(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/video <video adı veya YouTube linki>`")
    await _process_stream_request(message, message.text.split(None, 1)[1], is_video=True)


@bot.on_message(filters.command(["sunum"]))
async def cmd_sunum(_, message: Message):
    target = message.reply_to_message if message.reply_to_message and message.reply_to_message.photo else message
    if not target.photo:
        return await message.reply_text("Bir fotoğrafı yanıtlayıp `/sunum` yazın veya doğrudan fotoğraf gönderin.")
    await _handle_media_message(target)


@bot.on_message(filters.command(["liste"]))
async def cmd_liste(_, message: Message):
    await message.reply_text(
        _build_list_text(message.chat.id),
        reply_markup=player_control_keyboard(),
    )


@bot.on_message(filters.command(["sirala"]))
async def cmd_sirala(_, message: Message):
    if len(message.command) < 3 or not message.command[1].isdigit() or not message.command[2].isdigit():
        return await message.reply_text("Kullanım: `/sirala <eski_sıra> <yeni_sıra>` *(Örn: `/sirala 3 1`)*")
    ok, info_msg = reorder_list(message.chat.id, int(message.command[1]), int(message.command[2]))
    await message.reply_text(info_msg if ok else f"⚠️ {info_msg}")


@bot.on_message(filters.command(["mp3", "indir_mp3"]))
async def cmd_mp3(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/indir_mp3 <şarkı adı veya link>`")
    await _process_download_request(message, message.text.split(None, 1)[1], mode="mp3")


@bot.on_message(filters.command(["mp4", "indir_mp4"]))
async def cmd_mp4(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/indir_mp4 <video adı veya link>`")
    await _process_download_request(message, message.text.split(None, 1)[1], mode="mp4")


@bot.on_message(filters.command(["pause", "duraklat"]))
async def cmd_pause(_, message: Message):
    await call_py.pause_stream(message.chat.id)
    await message.reply_text("⏸️ Yayın duraklatıldı.")


@bot.on_message(filters.command(["resume", "devam"]))
async def cmd_resume(_, message: Message):
    await call_py.resume_stream(message.chat.id)
    await message.reply_text("▶️ Yayın devam ediyor.")


@bot.on_message(filters.command(["skip", "atla"]))
async def cmd_skip(_, message: Message):
    nxt = await skip_track(message.chat.id)
    if nxt:
        await message.reply_text(f"⏭️ Sıradaki öğeye geçildi: **{nxt['title']}**")
    else:
        await message.reply_text("⏹️ Sıra bitti, yayın kapatıldı.")


@bot.on_message(filters.command(["stop", "dur"]))
async def cmd_stop(_, message: Message):
    await stop_stream(message.chat.id)
    await message.reply_text("⏹️ **Golden Studio** yayını durduruldu ve liste temizlendi.")


@bot.on_message(filters.photo | filters.video | filters.audio | filters.voice)
async def on_uploaded_media(_, message: Message):
    await _handle_media_message(message)


@bot.on_message(filters.text & ~filters.command(ALL_COMMANDS))
async def handle_mode_input(_, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    text = message.text.strip()

    if user_id in USER_MODE:
        mode = USER_MODE.pop(user_id)
        if mode == "audio":
            await _process_stream_request(message, text, is_video=False)
        elif mode == "video":
            await _process_stream_request(message, text, is_video=True)
        elif mode == "image":
            await message.reply_text("🖼️ Görsel sunum için lütfen bir fotoğraf gönderin veya fotoğrafa yanıt verip `/sunum` yazın.")
        elif mode in ("mp3", "mp4"):
            await _process_download_request(message, text, mode=mode)
        return

    if message.chat.type.value == "private" and text.startswith(("http://", "https://")):
        USER_MODE[user_id] = "mp3"
        await message.reply_text(
            "🔗 Link algılandı! Nasıl indirmek istersiniz?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("🎧 MP3 Olarak Gönder", callback_data="mode_mp3"),
                        InlineKeyboardButton("📹 MP4 Olarak Gönder", callback_data="mode_mp4"),
                    ]
                ]
            ),
        )


async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Golden Studio VIP Bot Aktif"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    await start_web_server()
    await bot.start()
    me = await bot.get_me()
    print(f"AKTIF BOT: @{me.username} (ID: {me.id})", flush=True)
    await assistant.start()
    await call_py.start()
    print("Golden Studio basariyla baslatildi!", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
