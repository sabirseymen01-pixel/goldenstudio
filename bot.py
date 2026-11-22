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
from config import API_ID, API_HASH, BOT_TOKEN, PORT
from media_engine import download_media
from player import (
    assistant,
    call_py,
    play_or_queue,
    skip_track,
    stop_stream,
    NOW_PLAYING,
    QUEUES,
)

bot = Client(
    name="golden_studio_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# Kullanıcının butonla seçtiği aktif mod: user_id -> "audio" | "video" | "mp3" | "mp4"
USER_MODE: dict[int, str] = {}


def format_duration(seconds: int) -> str:
    if not seconds:
        return "Canlı / Bilinmiyor"
    mins, secs = divmod(int(seconds), 60)
    hours, mins = divmod(mins, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}" if hours else f"{mins:02d}:{secs:02d}"


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("🎵 Sesli Sohbette Çal", callback_data="mode_audio"),
                InlineKeyboardButton("🎬 Sesli Sohbette Video Aç", callback_data="mode_video"),
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
                InlineKeyboardButton("⏹️ Yayını Durdur", callback_data="ctrl_stop"),
                InlineKeyboardButton("🔙 Ana Stüdyo", callback_data="panel_home"),
            ],
        ]
    )


WELCOME_TEXT = (
    "✨ **GOLDEN STUDIO — VIP MEDYA & YAYIN MERKEZİ** ✨\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "Hoş geldiniz. **Golden Studio**, grubunuz ve özel sohbetleriniz için "
    "stüdyo kalitesinde ses, HD video yayını ve yüksek hızlı medya aktarımı sunar.\n\n"
    "🎛️ **Neler Yapabilirsiniz?**\n"
    "• **Sesli Sohbet Yayını:** Gruptaki sesli sohbette kesintisiz müzik veya video açar.\n"
    "• **MP3 & MP4 İndirme:** İstediğiniz şarkı veya klibi en yüksek kalitede doğrudan sohbete atar.\n\n"
    "👇 *Başlamak için aşağıdaki stüdyo butonlarından birini seçin veya doğrudan link/şarkı adı gönderin:*"
)


@bot.on_message(filters.command(["start", "studio"]))
async def cmd_start(_, message: Message):
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

    if data == "panel_help":
        help_text = (
            "📜 **GOLDEN STUDIO — KOMUT REHBERİ**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "🎵 `/play <şarkı veya link>` — Sesli sohbette müzik açar\n"
            "🎬 `/vplay <video veya link>` — Sesli sohbette video açar\n"
            "🎧 `/mp3 <şarkı veya link>` — 320kbps MP3 dosyası gönderir\n"
            "📹 `/mp4 <video veya link>` — 720p MP4 video gönderir\n"
            "⏸️ `/pause` • ▶️ `/resume` • ⏭️ `/skip` • ⏹️ `/stop`\n\n"
            "💡 *İpucu:* Stüdyo butonlarından bir mod seçtikten sonra sadece şarkı adını veya linkini yazmanız yeterlidir."
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
        q_len = len(QUEUES.get(chat_id, []))
        status_line = (
            f"🎶 **Şu An Yayında:** `{current['title']}`\n"
            f"📡 **Mod:** {'🎬 Video Yayını' if current['is_video'] else '🎵 Stüdyo Ses'}\n"
            f"📑 **Sıradaki Parça Sayısı:** `{q_len}`"
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
            "mp3": "🎧 **320kbps MP3 İndirme**",
            "mp4": "📹 **720p MP4 Video İndirme**",
        }
        await query.answer("Mod seçildi!", show_alert=False)
        await query.message.reply_text(
            f"✅ Aktif Mod: {labels[mode]}\n"
            f"✍️ Şimdi **şarkı adını** veya **YouTube/medya linkini** yazıp gönderin:"
        )
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
            await query.message.reply_text(f"⏭️ Sıradaki parçaya geçildi: **{nxt['title']}**")
        else:
            await query.message.reply_text("⏹️ Sırada başka parça kalmadı, yayın sonlandırıldı.")
        await query.answer()
    elif data == "ctrl_stop":
        await stop_stream(chat_id)
        await query.message.reply_text("⏹️ **Golden Studio** yayını durduruldu ve sıradaki liste temizlendi.")
        await query.answer()


async def _process_stream_request(message: Message, query_text: str, is_video: bool):
    if message.chat.type.value == "private":
        await message.reply_text(
            "⚠️ Sesli sohbet yayını yalnızca **gruplarda** başlatılabilir.\n"
            "Özel sohbette kullanmak için **🎧 MP3 İndir** veya **📹 MP4 İndir** butonlarını seçebilirsiniz."
        )
        return

    wait_msg = await message.reply_text("⏳ **Golden Studio** akışı hazırlıyor, lütfen bekleyin...")
    try:
        requester = message.from_user.first_name if message.from_user else "Üye"
        status, track = await play_or_queue(message.chat.id, query_text, is_video, requester)
        dur = format_duration(track["duration"])
        kind = "🎬 HD Video Yayını" if is_video else "🎵 Stüdyo Ses Yayını"

        if status == "playing":
            await wait_msg.edit_text(
                f"✨ **GOLDEN STUDIO — YAYINDA**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎶 **Parça:** [{track['title']}]({track['webpage_url']})\n"
                f"⏱️ **Süre:** `{dur}`\n"
                f"📡 **Format:** {kind}\n"
                f"👤 **İsteyen:** {requester}",
                reply_markup=player_control_keyboard(),
                disable_web_page_preview=True,
            )
        else:
            q_pos = len(QUEUES.get(message.chat.id, []))
            await wait_msg.edit_text(
                f"📑 **Sıraya Eklendi (#{q_pos})**\n"
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


# --- KOMUTLAR ---

@bot.on_message(filters.command(["play", "oynat"]))
async def cmd_play(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/play <şarkı adı veya link>`")
    await _process_stream_request(message, message.text.split(None, 1)[1], is_video=False)


@bot.on_message(filters.command(["vplay", "video"]))
async def cmd_vplay(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/vplay <video adı veya link>`")
    await _process_stream_request(message, message.text.split(None, 1)[1], is_video=True)


@bot.on_message(filters.command(["mp3"]))
async def cmd_mp3(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/mp3 <şarkı adı veya link>`")
    await _process_download_request(message, message.text.split(None, 1)[1], mode="mp3")


@bot.on_message(filters.command(["mp4"]))
async def cmd_mp4(_, message: Message):
    if len(message.command) < 2:
        return await message.reply_text("Kullanım: `/mp4 <video adı veya link>`")
    await _process_download_request(message, message.text.split(None, 1)[1], mode="mp4")


@bot.on_message(filters.command(["pause"]))
async def cmd_pause(_, message: Message):
    await call_py.pause_stream(message.chat.id)
    await message.reply_text("⏸️ Yayın duraklatıldı.")


@bot.on_message(filters.command(["resume"]))
async def cmd_resume(_, message: Message):
    await call_py.resume_stream(message.chat.id)
    await message.reply_text("▶️ Yayın devam ediyor.")


@bot.on_message(filters.command(["skip", "atla"]))
async def cmd_skip(_, message: Message):
    nxt = await skip_track(message.chat.id)
    if nxt:
        await message.reply_text(f"⏭️ Sıradaki parçaya geçildi: **{nxt['title']}**")
    else:
        await message.reply_text("⏹️ Sıra bitti, yayın kapatıldı.")


@bot.on_message(filters.command(["stop", "dur"]))
async def cmd_stop(_, message: Message):
    await stop_stream(message.chat.id)
    await message.reply_text("⏹️ **Golden Studio** yayını durduruldu.")


# --- BUTONLA MOD SEÇİLDİKTEN SONRA GELEN MESAJLARI YAKALAMA ---

@bot.on_message(filters.text & ~filters.command(["start", "studio", "play", "oynat", "vplay", "video", "mp3", "mp4", "pause", "resume", "skip", "atla", "stop", "dur"]))
async def handle_mode_input(_, message: Message):
    if not message.from_user:
        return
    user_id = message.from_user.id
    text = message.text.strip()

    # Kullanıcı butona bastıysa seçtiği modda çalıştır
    if user_id in USER_MODE:
        mode = USER_MODE.pop(user_id)
        if mode == "audio":
            await _process_stream_request(message, text, is_video=False)
        elif mode == "video":
            await _process_stream_request(message, text, is_video=True)
        elif mode in ("mp3", "mp4"):
            await _process_download_request(message, text, mode=mode)
        return

    # Özel sohbette doğrudan link atılırsa otomatik MP3/MP4 seçeneği sun
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


# --- RENDER HEALTHCHECK WEB SUNUCUSU ---

async def start_web_server():
    app = web.Application()
    app.router.add_get("/", lambda _: web.Response(text="Golden Studio VIP Bot Aktif"))
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()


async def main():
    await start_web_server()
    await bot.start()
    await assistant.start()
    await call_py.start()
    print("Golden Studio basariyla baslatildi!")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
