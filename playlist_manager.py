import asyncio
import re
from collections import deque
import yt_dlp

# chat_id -> list[dict] (üstten aşağı sıralı liste: index 0 en üst/sıradaki)
LISTS: dict[int, list[dict]] = {}
NOW_PLAYING: dict[int, dict] = {}


def _is_youtube_url(text: str) -> bool:
    return bool(
        re.match(
            r"^https?://(www\.)?(youtube\.com|youtu\.be|m\.youtube\.com)/",
            text.strip(),
            re.IGNORECASE,
        )
    )


async def resolve_input_items(query: str, mode: str, requested_by: str) -> list[dict]:
    """
    mode: 'audio' | 'video' | 'image'
    Tek şarkı/link veya YouTube playlist linki için öğe listesi döndürür.
    """
    query = query.strip()

    if mode == "image":
        return [
            {
                "kind": "image",
                "title": f"Görsel Sunum ({requested_by})",
                "source": query,  # yerel dosya yolu
                "is_video": True,
                "duration": 0,
                "requested_by": requested_by,
            }
        ]

    if _is_youtube_url(query) and "list=" in query:
        loop = asyncio.get_running_loop()
        ydl_opts = {
            "extract_flat": True,
            "quiet": True,
            "no_warnings": True,
        }

        def _extract_list():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(query, download=False)
                entries = info.get("entries") or []
                items = []
                for e in entries[:30]:
                    url = e.get("url") or e.get("webpage_url")
                    if not url:
                        vid = e.get("id")
                        if vid:
                            url = f"https://www.youtube.com/watch?v={vid}"
                    if url:
                        items.append(
                            {
                                "kind": "video" if mode == "video" else "audio",
                                "title": e.get("title", "YouTube Öğesi"),
                                "source": url,
                                "is_video": mode == "video",
                                "duration": int(e.get("duration") or 0),
                                "requested_by": requested_by,
                            }
                        )
                return items

        found = await loop.run_in_executor(None, _extract_list)
        if found:
            return found

    return [
        {
            "kind": "video" if mode == "video" else "audio",
            "title": query,
            "source": query,
            "is_video": mode == "video",
            "duration": 0,
            "requested_by": requested_by,
        }
    ]


def get_list(chat_id: int) -> list[dict]:
    return LISTS.setdefault(chat_id, [])


def add_to_list(chat_id: int, items: list[dict], top: bool = False) -> int:
    lst = get_list(chat_id)
    if top:
        for item in reversed(items):
            lst.insert(0, item)
    else:
        lst.extend(items)
    return len(lst)


def reorder_list(chat_id: int, from_pos: int, to_pos: int) -> tuple[bool, str]:
    """
    1-tabanlı (üstten aşağı) sıralama: from_pos sırasındaki öğeyi to_pos sırasına taşır.
    """
    lst = get_list(chat_id)
    n = len(lst)
    if n == 0:
        return False, "Liste şu an boş."
    if not (1 <= from_pos <= n) or not (1 <= to_pos <= n):
        return False, f"Geçerli sıra numarası 1 ile {n} arasında olmalıdır."
    if from_pos == to_pos:
        return True, "Sıralama aynı kaldı."

    item = lst.pop(from_pos - 1)
    lst.insert(to_pos - 1, item)
    return True, f"`#{from_pos}` sırasındaki **{item['title']}** -> `#{to_pos}` sırasına taşındı."


def pop_next(chat_id: int) -> dict | None:
    lst = get_list(chat_id)
    if not lst:
        return None
    return lst.pop(0)


def clear_list(chat_id: int):
    LISTS[chat_id] = []
