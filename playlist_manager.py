import os
import re
from typing import Any

try:
    from pymongo import MongoClient
except Exception:
    MongoClient = None  # type: ignore

from config import DOWNLOAD_DIR

LISTS: dict[int, list[dict[str, Any]]] = {}
NOW_PLAYING: dict[int, dict[str, Any]] = {}
SAVED_TRACKS: dict[str, dict[str, Any]] = {}
LINKED_GROUPS: dict[int, dict[str, Any]] = {}
USER_TARGETS: dict[int, int] = {}

_MONGO_URI = (
    os.getenv("MONGODB_URI")
    or os.getenv("MONGO_URI")
    or os.getenv("DATABASE_URL")
    or ""
).strip()
_MONGO_DB_NAME = (os.getenv("MONGODB_DB") or "golden_studio").strip()

_db = None
_lists_col = None
_tracks_col = None
_groups_col = None
_targets_col = None


def _init_mongo() -> None:
    global _db, _lists_col, _tracks_col, _groups_col, _targets_col
    if _db is not None or not _MONGO_URI or MongoClient is None:
        return
    try:
        client = MongoClient(_MONGO_URI, serverSelectionTimeoutMS=4000)
        client.admin.command("ping")
        _db = client[_MONGO_DB_NAME]
        _lists_col = _db["lists"]
        _tracks_col = _db["saved_tracks"]
        _groups_col = _db["linked_groups"]
        _targets_col = _db["user_targets"]
    except Exception as e:
        print(f"[MONGO UYARI] Baglanti kurulamadi, bellek ici mod aktif: {e}", flush=True)
        _db = None


_init_mongo()


def clean_track_title(raw_title: str | None, fallback: str = "Bilinmeyen Parca") -> str:
    text = (raw_title or "").strip()
    if not text or re.fullmatch(r"tg_[a-zA-Z0-9_-]{6,}", text):
        return fallback
    text = re.sub(r"\.[a-z0-9]{2,5}$", "", text, flags=re.IGNORECASE)
    text = text.replace("_", " ").replace("-", " ")
    text = re.sub(r"\[[^\]]*\]|\([^\)]*\)", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" ._-")
    return text or fallback


def extract_media_title(msg: dict[str, Any], media_obj: dict[str, Any] | None = None, local_path: str = "") -> str:
    obj = media_obj or msg.get("audio") or msg.get("video") or msg.get("voice") or msg.get("document") or {}
    performer = (obj.get("performer") or "").strip()
    title_field = (obj.get("title") or "").strip()
    file_name = (obj.get("file_name") or "").strip()
    caption = (msg.get("caption") or "").strip()

    if performer and title_field:
        return clean_track_title(f"{performer} - {title_field}", fallback="Kayitli Parca")
    if title_field:
        return clean_track_title(title_field, fallback="Kayitli Parca")
    if file_name:
        return clean_track_title(file_name, fallback="Kayitli Parca")
    if caption and not caption.startswith("/"):
        return clean_track_title(caption, fallback="Kayitli Parca")
    if local_path:
        base = os.path.splitext(os.path.basename(local_path))[0]
        if not re.fullmatch(r"tg_[a-zA-Z0-9_-]{6,}", base):
            return clean_track_title(base, fallback="Kayitli Parca")
    if msg.get("photo"):
        return clean_track_title(caption, fallback="Gorsel Sunum")
    if msg.get("voice"):
        return "Ses Kaydi"
    return "Medya Parcasi"


def _normalize_key(title: str) -> str:
    cleaned = clean_track_title(title, fallback="").lower()
    return re.sub(r"\s+", " ", cleaned).strip()


def _sync_list_to_mongo(chat_id: int) -> None:
    if _lists_col is None:
        return
    try:
        _lists_col.update_one(
            {"_id": str(chat_id)},
            {"$set": {"chat_id": int(chat_id), "items": LISTS.get(chat_id, [])}},
            upsert=True,
        )
    except Exception as e:
        print(f"[MONGO LISTE HATA] {e}", flush=True)


def get_list(chat_id: int) -> list[dict[str, Any]]:
    if chat_id not in LISTS and _lists_col is not None:
        try:
            doc = _lists_col.find_one({"_id": str(chat_id)})
            if doc and isinstance(doc.get("items"), list):
                LISTS[chat_id] = doc["items"]
        except Exception as e:
            print(f"[MONGO OKUMA HATA] {e}", flush=True)
    return LISTS.setdefault(chat_id, [])


def add_to_list(chat_id: int, items: list[dict[str, Any]], top: bool = False) -> int:
    lst = get_list(chat_id)
    normalized: list[dict[str, Any]] = []
    for item in items:
        copied = dict(item)
        copied["title"] = clean_track_title(copied.get("title"), fallback="Parca")
        normalized.append(copied)
    if top:
        LISTS[chat_id] = normalized + lst
    else:
        lst.extend(normalized)
    _sync_list_to_mongo(chat_id)
    return len(LISTS[chat_id])


def pop_next(chat_id: int) -> dict[str, Any] | None:
    lst = get_list(chat_id)
    if not lst:
        return None
    item = lst.pop(0)
    _sync_list_to_mongo(chat_id)
    return item


def clear_list(chat_id: int) -> None:
    LISTS[chat_id] = []
    _sync_list_to_mongo(chat_id)


def reorder_list(chat_id: int, old_pos: int, new_pos: int) -> tuple[bool, str]:
    lst = get_list(chat_id)
    n = len(lst)
    if old_pos < 1 or old_pos > n or new_pos < 1 or new_pos > n:
        return False, f"Gecerli sira araligi 1 ile {n} arasindadir."
    if old_pos == new_pos:
        return True, f"<b>{lst[old_pos - 1]['title']}</b> zaten {new_pos}. sirada."
    item = lst.pop(old_pos - 1)
    lst.insert(new_pos - 1, item)
    _sync_list_to_mongo(chat_id)
    return True, f"<b>{item['title']}</b> ogesi {old_pos}. siradan {new_pos}. siraya tasindi."


def move_item_step(chat_id: int, index_1based: int, delta: int) -> tuple[bool, str]:
    lst = get_list(chat_id)
    n = len(lst)
    if index_1based < 1 or index_1based > n:
        return False, "Gecersiz sira numarasi."
    target = max(1, min(n, index_1based + delta))
    return reorder_list(chat_id, index_1based, target)


def remove_from_list(chat_id: int, index_1based: int) -> tuple[bool, str]:
    lst = get_list(chat_id)
    if index_1based < 1 or index_1based > len(lst):
        return False, "Gecersiz sira numarasi."
    removed = lst.pop(index_1based - 1)
    _sync_list_to_mongo(chat_id)
    return True, f"<b>{removed['title']}</b> listeden cikarildi."


def save_track_to_catalog(item: dict[str, Any], custom_title: str | None = None) -> dict[str, Any]:
    title = clean_track_title(custom_title or item.get("title"), fallback="Kayitli Parca")
    key = _normalize_key(title) or f"track_{abs(hash(title))}"
    record = {
        "key": key,
        "title": title,
        "kind": item.get("kind", "audio"),
        "source": item.get("source", ""),
        "duration": int(item.get("duration") or 0),
        "is_video": bool(item.get("is_video", False)),
        "requested_by": item.get("requested_by") or "Katalog",
    }
    SAVED_TRACKS[key] = record
    if _tracks_col is not None:
        try:
            _tracks_col.update_one({"_id": key}, {"$set": record}, upsert=True)
        except Exception as e:
            print(f"[MONGO KAYIT HATA] {e}", flush=True)
    return record


def find_saved_track(query: str) -> dict[str, Any] | None:
    q_norm = _normalize_key(query)
    if not q_norm:
        return None
    if q_norm in SAVED_TRACKS:
        rec = SAVED_TRACKS[q_norm]
        if rec.get("source") and os.path.exists(rec["source"]):
            return dict(rec)

    if _tracks_col is not None:
        try:
            doc = _tracks_col.find_one({"_id": q_norm})
            if not doc:
                doc = _tracks_col.find_one({"title": {"$regex": re.escape(query.strip()), "$options": "i"}})
            if doc:
                rec = {k: v for k, v in doc.items() if k != "_id"}
                SAVED_TRACKS[rec.get("key") or q_norm] = rec
                if rec.get("source") and os.path.exists(rec["source"]):
                    return dict(rec)
        except Exception as e:
            print(f"[MONGO ARAMA HATA] {e}", flush=True)

    for rec in SAVED_TRACKS.values():
        t_norm = _normalize_key(rec.get("title", ""))
        if q_norm in t_norm or t_norm in q_norm:
            if rec.get("source") and os.path.exists(rec["source"]):
                return dict(rec)
    return None


def list_saved_tracks(limit: int = 15) -> list[dict[str, Any]]:
    results: dict[str, dict[str, Any]] = dict(SAVED_TRACKS)
    if _tracks_col is not None:
        try:
            for doc in _tracks_col.find().limit(limit):
                rec = {k: v for k, v in doc.items() if k != "_id"}
                key = rec.get("key") or _normalize_key(rec.get("title", ""))
                if key:
                    results[key] = rec
        except Exception as e:
            print(f"[MONGO KATALOG HATA] {e}", flush=True)
    return list(results.values())[:limit]


def register_linked_group(chat_id: int, title: str, chat_type: str = "supergroup") -> None:
    if not chat_id:
        return
    clean_title = (title or f"Grup {chat_id}").strip()
    rec = {"chat_id": int(chat_id), "title": clean_title, "type": chat_type}
    LINKED_GROUPS[int(chat_id)] = rec
    if _groups_col is not None:
        try:
            _groups_col.update_one({"_id": str(chat_id)}, {"$set": rec}, upsert=True)
        except Exception as e:
            print(f"[MONGO GRUP HATA] {e}", flush=True)


def unregister_linked_group(chat_id: int) -> None:
    cid = int(chat_id)
    LINKED_GROUPS.pop(cid, None)
    for uid, tgt in list(USER_TARGETS.items()):
        if tgt == cid:
            USER_TARGETS.pop(uid, None)
    if _groups_col is not None:
        try:
            _groups_col.delete_one({"_id": str(cid)})
        except Exception as e:
            print(f"[MONGO GRUP SIL HATA] {e}", flush=True)


def sync_bot_active_groups(active_groups: list[dict[str, Any]]) -> None:
    """
    Yalnizca botun aktif olarak uyesi oldugu gruplari tutar; alakasiz kayitlari temizler.
    """
    fresh: dict[int, dict[str, Any]] = {}
    for g in active_groups:
        cid = g.get("chat_id")
        if cid is None:
            continue
        ic = int(cid)
        fresh[ic] = {
            "chat_id": ic,
            "title": (g.get("title") or f"Grup {ic}").strip(),
            "type": g.get("type") or "supergroup",
        }

    LINKED_GROUPS.clear()
    LINKED_GROUPS.update(fresh)

    if _groups_col is not None:
        try:
            _groups_col.delete_many({})
            for ic, rec in fresh.items():
                _groups_col.update_one({"_id": str(ic)}, {"$set": rec}, upsert=True)
        except Exception as e:
            print(f"[MONGO GRUP SENK HATA] {e}", flush=True)


def get_linked_groups() -> list[dict[str, Any]]:
    if not LINKED_GROUPS and _groups_col is not None:
        try:
            for doc in _groups_col.find():
                cid = doc.get("chat_id")
                if cid is not None:
                    LINKED_GROUPS[int(cid)] = {
                        "chat_id": int(cid),
                        "title": doc.get("title") or f"Grup {cid}",
                        "type": doc.get("type") or "supergroup",
                    }
        except Exception as e:
            print(f"[MONGO GRUPLAR HATA] {e}", flush=True)
    return list(LINKED_GROUPS.values())


def set_user_target_chat(user_id: int, target_chat_id: int) -> None:
    USER_TARGETS[int(user_id)] = int(target_chat_id)
    if _targets_col is not None:
        try:
            _targets_col.update_one(
                {"_id": str(user_id)},
                {"$set": {"user_id": int(user_id), "target_chat_id": int(target_chat_id)}},
                upsert=True,
            )
        except Exception as e:
            print(f"[MONGO HEDEF HATA] {e}", flush=True)


def get_user_target_chat(user_id: int | None, fallback_chat_id: int) -> int:
    if user_id is None:
        return fallback_chat_id
    if int(user_id) in USER_TARGETS:
        tgt = USER_TARGETS[int(user_id)]
        if tgt in LINKED_GROUPS:
            return tgt
    if _targets_col is not None:
        try:
            doc = _targets_col.find_one({"_id": str(user_id)})
            if doc and doc.get("target_chat_id") is not None:
                val = int(doc["target_chat_id"])
                if val in LINKED_GROUPS:
                    USER_TARGETS[int(user_id)] = val
                    return val
        except Exception as e:
            print(f"[MONGO HEDEF OKUMA HATA] {e}", flush=True)
    return fallback_chat_id


async def resolve_input_items(
    source_or_query: str,
    mode: str = "audio",
    requested_by: str = "Uye",
    custom_title: str | None = None,
) -> list[dict[str, Any]]:
    raw = (source_or_query or "").strip()
    if not raw:
        raise ValueError("Gecerli bir dosya veya kayitli parca adi girilmedi.")

    if os.path.exists(raw):
        base_name = clean_track_title(custom_title or os.path.basename(raw), fallback="Yerel Medya")
        is_vid = mode in ("video", "image", "slideshow")
        kind = mode if mode in ("video", "image", "slideshow") else "audio"
        item = {
            "kind": kind,
            "source": raw,
            "title": base_name,
            "duration": 30 if kind == "image" else 0,
            "is_video": is_vid,
            "requested_by": requested_by,
        }
        if kind == "audio":
            save_track_to_catalog(item)
        return [item]

    saved = find_saved_track(raw)
    if saved:
        return [
            {
                **saved,
                "title": clean_track_title(custom_title or saved.get("title"), fallback="Kayitli Parca"),
                "requested_by": requested_by,
            }
        ]

    if os.path.isdir(DOWNLOAD_DIR):
        q_low = _normalize_key(raw)
        for fname in os.listdir(DOWNLOAD_DIR):
            f_low = _normalize_key(fname)
            if q_low and q_low in f_low:
                full_path = os.path.join(DOWNLOAD_DIR, fname)
                if os.path.isfile(full_path):
                    is_vid = mode == "video" or fname.lower().endswith((".mp4", ".mkv", ".mov", ".webm"))
                    return [
                        {
                            "kind": "video" if is_vid else "audio",
                            "source": full_path,
                            "title": clean_track_title(custom_title or fname, fallback="Parca"),
                            "duration": 0,
                            "is_video": is_vid,
                            "requested_by": requested_by,
                        }
                    ]

    raise FileNotFoundError(
        f"'{raw}' icin yerel dosya veya MongoDB katalog kaydi bulunamadi. Lutfen dosya gonderin veya mesaja yanit verip /oynat yazin."
    )
