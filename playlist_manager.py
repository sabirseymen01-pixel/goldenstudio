import os

# chat_id -> list[dict] (üstten aşağı sıralı liste: index 0 en üst/sıradaki)
LISTS: dict[int, list[dict]] = {}
NOW_PLAYING: dict[int, dict] = {}


def _display_title(source: str) -> str:
    cleaned = source.strip()
    if os.path.sep in cleaned or "/" in cleaned or "\\" in cleaned:
        base = os.path.basename(cleaned)
        name, _ = os.path.splitext(base)
        return name or base or "Medya Dosyası"
    return cleaned or "Medya Dosyası"


async def resolve_input_items(query: str, mode: str, requested_by: str) -> list[dict]:
    """
    mode: 'audio' | 'video' | 'image'
    Doğrudan medya dosyası veya görsel sunum öğesi üretir.
    """
    source = query.strip()
    title = _display_title(source)

    if mode == "image":
        return [
            {
                "kind": "image",
                "title": f"Görsel Sunum ({requested_by})",
                "source": source,
                "is_video": True,
                "duration": 30,
                "requested_by": requested_by,
            }
        ]

    return [
        {
            "kind": "video" if mode == "video" else "audio",
            "title": title,
            "source": source,
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
    return True, f"#{from_pos} sırasındaki <b>{item['title']}</b> -> #{to_pos} sırasına taşındı."


def pop_next(chat_id: int) -> dict | None:
    lst = get_list(chat_id)
    if not lst:
        return None
    return lst.pop(0)


def clear_list(chat_id: int):
    LISTS[chat_id] = []
