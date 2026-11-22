import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int = 0) -> int:
    val = os.getenv(name, "").strip()
    return int(val) if val else default


API_ID: int = _int_env("API_ID")
API_HASH: str = os.getenv("API_HASH", "").strip()
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "").strip()
SESSION_STRING: str = os.getenv("SESSION_STRING", "").strip()

# Render port ayarı (UptimeRobot / Healthcheck için)
PORT: int = _int_env("PORT", 8080)

# İsteğe bağlı: Admin kullanıcı ID'leri (virgülle ayrılmış)
_admins = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS: list[int] = [int(x.strip()) for x in _admins.split(",") if x.strip().isdigit()]

DOWNLOAD_DIR: str = os.getenv("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
