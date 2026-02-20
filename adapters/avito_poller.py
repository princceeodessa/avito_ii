# adapters/avito_poller.py
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from core.avito_api import AvitoAPI
from core.app_state import AppState


ALLOWED_TITLES_DEFAULT = [
    "Натяжные потолки. 2-й и 3-й потолок в подарок",
    "Натяжные потолки. Потолок в подарок",
]

HUMAN_TRIGGERS = [
    "оператор", "менеджер", "живой человек", "человек", "ассистент",
    "позови", "позовите", "соедини", "соедините", "не бот",
    "хочу человека", "переключи на человека",
]

CEILING_KEYWORDS = [
    "потол", "натяж", "светиль", "люстр", "профил", "тенев",
    "карниз", "замер", "м2", "м²", "кв",
]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _contains_any(text: str, needles: List[str]) -> bool:
    t = _norm(text)
    return any(_norm(n) in t for n in needles if n)


def _pick_chat_id(chat: Dict[str, Any]) -> Optional[str]:
    cid = chat.get("id") or chat.get("chat_id") or chat.get("chatId")
    return str(cid) if cid is not None else None


def _extract_meta(chat_obj: Dict[str, Any]) -> Dict[str, str]:
    chat_url = str(chat_obj.get("url") or chat_obj.get("web_url") or chat_obj.get("webUrl") or "")
    ctx = chat_obj.get("context") or {}
    val = ctx.get("value") if isinstance(ctx.get("value"), dict) else {}

    title = str(val.get("title") or "")
    item_url = str(val.get("url") or "")
    price_string = str(val.get("price_string") or val.get("priceString") or "")

    loc = val.get("location") if isinstance(val.get("location"), dict) else {}
    location_title = str((loc or {}).get("title") or "")

    return {
        "title": title,
        "item_url": item_url,
        "chat_url": chat_url,
        "city": location_title,
        "price_string": price_string,
    }


def _get_last_message(chat_obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    lm = chat_obj.get("last_message") or chat_obj.get("lastMessage") or chat_obj.get("last_message_info")
    return lm if isinstance(lm, dict) else None


def _msg_id(m: Dict[str, Any]) -> str:
    mid = m.get("id") or m.get("message_id") or m.get("messageId")
    return str(mid) if mid is not None else ""


def _msg_text(m: Dict[str, Any]) -> str:
    content = m.get("content") or {}
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"].strip()

    msg = m.get("message") or {}
    if isinstance(msg, dict) and isinstance(msg.get("text"), str):
        return msg["text"].strip()

    if isinstance(m.get("text"), str):
        return m["text"].strip()

    return ""


def _is_incoming(m: Dict[str, Any], my_user_id: int) -> bool:
    direction = (m.get("direction") or "").lower().strip()
    if direction in ("in", "incoming"):
        return True
    if direction in ("out", "outgoing"):
        return False

    author = m.get("author_id") or m.get("authorId")
    try:
        if author is not None and int(author) == int(my_user_id):
            return False
    except Exception:
        pass
    return True


async def run_avito_poller(state: AppState) -> None:
    client_id = os.getenv("AVITO_CLIENT_ID", "").strip()
    client_secret = os.getenv("AVITO_CLIENT_SECRET", "").strip()
    token_path = os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json").strip()
    user_id = int(os.getenv("AVITO_USER_ID", "0") or "0")

    poll_interval = float(os.getenv("AVITO_POLL_INTERVAL", "3"))
    manual_hours = float(os.getenv("AVITO_MANUAL_HOURS", "6"))

    debug = os.getenv("AVITO_DEBUG", "0") == "1"
    trace_chat_id = os.getenv("AVITO_TRACE_CHAT_ID", "").strip()

    allowed_titles_raw = os.getenv("AVITO_ALLOWED_TITLES", "").strip()
    allowed_titles = [x.strip() for x in allowed_titles_raw.split("|") if x.strip()] if allowed_titles_raw else ALLOWED_TITLES_DEFAULT

    if not client_id or not client_secret or not user_id:
        raise RuntimeError("Нужно заполнить AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_USER_ID")

    api = AvitoAPI(
        client_id=client_id,
        client_secret=client_secret,
        user_id=user_id,
        token_path=token_path,
    )

    async def token_refresher():
        while True:
            await asyncio.sleep(23 * 3600)
            try:
                await asyncio.to_thread(api.refresh_token)
            except Exception:
                pass

    asyncio.create_task(token_refresher())

    print("[avito_poller] mode: LAST_MESSAGE (GET messages is not available: 405/404)")

    while True:
        try:
            await asyncio.to_thread(api.ensure_token)
            chats = await asyncio.to_thread(api.list_chats, 100, 0)

            if debug:
                print(f"[avito_poller] tick: chats={len(chats)}")

            for ch in chats:
                chat_id = _pick_chat_id(ch)
                if not chat_id:
                    continue
                if trace_chat_id and chat_id != trace_chat_id:
                    continue

                last = _get_last_message(ch)
                if not last:
                    continue

                mid = _msg_id(last)
                text = _msg_text(last)
                incoming = _is_incoming(last, user_id)

                if not text or not mid or not incoming:
                    continue

                k = f"avito:{chat_id}"

                # ✅ ВАЖНО: читаем mem только чтобы проверить дубль
                mem_before: Dict[str, Any] = state.mem_store.load(k)
                if str(mem_before.get("avito_last_in_mid") or "") == mid:
                    continue

                meta = _extract_meta(ch)
                title = meta.get("title", "")

                if debug:
                    print(f"[TRACE] chat={chat_id} title='{title}' last_id={mid} in={incoming} text={text!r}")

                # фильтр по объявлениям
                if title and allowed_titles and not any(title.strip() == t.strip() for t in allowed_titles):
                    mem_before["avito_last_in_mid"] = mid
                    state.mem_store.save(k, mem_before)
                    if debug:
                        print("[TRACE] skip: title not allowed")
                    continue

                # защита по тематике (если title пустой)
                if not title and not _contains_any(text, CEILING_KEYWORDS):
                    mem_before["avito_last_in_mid"] = mid
                    state.mem_store.save(k, mem_before)
                    if debug:
                        print("[TRACE] skip: not ceiling topic")
                    continue

                now = time.time()
                manual_until = float(mem_before.get("manual_until") or 0)

                if manual_until > now:
                    mem_before["avito_last_in_mid"] = mid
                    state.mem_store.save(k, mem_before)
                    continue

                if _contains_any(text, HUMAN_TRIGGERS):
                    mem_before["manual_until"] = now + manual_hours * 3600
                    mem_before["manual_started_at"] = now
                    mem_before["manual_reason"] = "client_requested_human"
                    mem_before["avito_last_in_mid"] = mid
                    state.mem_store.save(k, mem_before)

                    link = meta.get("chat_url") or meta.get("item_url") or "https://www.avito.ru/profile/messenger"
                    state.notify_now(
                        "🆘 Клиент просит менеджера (Авито)\n"
                        f"Chat ID: {chat_id}\n"
                        f"Объявление: {title or '-'}\n"
                        f"Ссылка: {link}\n"
                        f"Сообщение:\n{text}"
                    )
                    try:
                        await asyncio.to_thread(api.send_text, chat_id, "Понял(а) ✅ Передал(а) менеджеру — он ответит вам в этом чате.")
                    except Exception:
                        pass
                    continue

                # ✅ Генерация ответа (AppState сам сохраняет память/диалог)
                reply = await asyncio.to_thread(
                    state.generate_reply,
                    "avito",
                    chat_id,
                    text,
                    meta,
                )

                if reply and reply.strip():
                    try:
                        await asyncio.to_thread(api.send_text, chat_id, reply)
                    except Exception as e:
                        print(f"[avito_poller] send error: {e}")

                try:
                    await asyncio.to_thread(api.mark_read, chat_id)
                except Exception:
                    pass

                # ✅ КЛЮЧЕВО: НЕ сохраняем старый mem_before, а подгружаем актуальный mem после generate_reply
                mem_after: Dict[str, Any] = state.mem_store.load(k)
                mem_after["avito_last_in_mid"] = mid
                state.mem_store.save(k, mem_after)

        except Exception as e:
            print(f"[avito_poller] LOOP ERROR: {e}")

        await asyncio.sleep(float(poll_interval))