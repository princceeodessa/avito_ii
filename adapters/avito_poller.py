# adapters/avito_poller.py
import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from core.avito_api import AvitoAPI
from core.app_state import AppState

ALLOWED_TITLES_DEFAULT = [
    "Натяжные потолки. 2-й и 3-й потолок в подарок",
    "Натяжные потолки. Потолок в подарок",
]

HUMAN_TRIGGERS = [
    "оператор", "менеджер", "живой человек", "человек", "ассистент",
    "позови", "позовите", "соедини", "соедините",
    "не бот", "хочу человека", "переключи на человека",
]

# доп. защита: если вдруг title не пришёл, но текст вообще не про потолки — игнорируем
CEILING_KEYWORDS = [
    "потол", "натяж", "светиль", "люстр", "профил", "тенев", "карниз", "замер", "м2", "м²", "кв",
]


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _contains_any(text: str, needles: List[str]) -> bool:
    t = _norm(text)
    return any(_norm(n) in t for n in needles if n)


def _unread_count(chat: Dict[str, Any]) -> int:
    v = chat.get("unread_count") or chat.get("unreadCount") or chat.get("unread") or 0
    try:
        return int(v)
    except Exception:
        return 0


def _pick_chat_id(chat: Dict[str, Any]) -> Optional[str]:
    cid = chat.get("id") or chat.get("chat_id") or chat.get("chatId")
    return str(cid) if cid is not None else None


def _extract_title_url(chat_obj: Dict[str, Any]) -> Tuple[str, str, str]:
    """
    title, item_url, chat_url (если попадётся)
    """
    chat_url = str(chat_obj.get("url") or chat_obj.get("web_url") or chat_obj.get("webUrl") or "")
    ctx = chat_obj.get("context") or {}
    val = ctx.get("value") if isinstance(ctx.get("value"), dict) else {}
    title = str(val.get("title") or "")
    item_url = str(val.get("url") or "")
    return title, item_url, chat_url


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
    author = m.get("author_id") or m.get("authorId")
    try:
        if author is not None and int(author) == int(my_user_id):
            return False
    except Exception:
        pass
    # если нет author_id — считаем входящим (на практике хватает)
    return True

#
class _Debounce:
    """
    Склеиваем несколько быстрых сообщений клиента в одно (пользователи часто шлют 2-3 подряд).
    """
    def __init__(self, delay_sec: float = 1.2):
        self.delay_sec = delay_sec
        self.buf: Dict[str, List[Tuple[str, str]]] = {}
        self.tasks: Dict[str, asyncio.Task] = {}

    def push(self, chat_id: str, mid: str, text: str, cb):
        self.buf.setdefault(chat_id, []).append((mid, text))

        t = self.tasks.get(chat_id)
        if t and not t.done():
            t.cancel()

        async def _job():
            try:
                await asyncio.sleep(self.delay_sec)
            except asyncio.CancelledError:
                return
            items = self.buf.pop(chat_id, [])
            self.tasks.pop(chat_id, None)
            if not items:
                return
            merged = "\n".join(x[1] for x in items if x[1].strip())
            last_mid = items[-1][0]
            await cb(chat_id, merged, last_mid)

        self.tasks[chat_id] = asyncio.create_task(_job())


async def run_avito_poller(state: AppState):
    client_id = os.getenv("AVITO_CLIENT_ID", "").strip()
    client_secret = os.getenv("AVITO_CLIENT_SECRET", "").strip()
    token_path = os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json").strip()
    user_id = int(os.getenv("AVITO_USER_ID", "0") or "0")

    poll_interval = float(os.getenv("AVITO_POLL_INTERVAL", "5"))
    manual_hours = float(os.getenv("AVITO_MANUAL_HOURS", "6"))

    allowed_titles_raw = os.getenv("AVITO_ALLOWED_TITLES", "").strip()
    allowed_titles = [x.strip() for x in allowed_titles_raw.split("|") if x.strip()] if allowed_titles_raw else ALLOWED_TITLES_DEFAULT

    if not client_id or not client_secret or not user_id:
        raise RuntimeError("Нужно заполнить AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_USER_ID")

    api = AvitoAPI(client_id=client_id, client_secret=client_secret, user_id=user_id, token_path=token_path)
    debounce = _Debounce(delay_sec=1.2)

    async def token_refresher():
        # профилактически обновляем раз в ~23 часа
        while True:
            await asyncio.sleep(23 * 3600)
            try:
                await asyncio.to_thread(api.refresh_token)
            except Exception:
                pass

    asyncio.create_task(token_refresher())

    async def handle_merged(chat_id: str, merged_text: str, last_mid: str):
        k = f"avito:{chat_id}"
        mem: Dict[str, Any] = state.mem_store.load(k)

        # обновим last_mid как минимум (чтобы не зациклиться)
        mem["avito_last_mid"] = last_mid

        now = time.time()
        manual_until = float(mem.get("manual_until") or 0)

        # мета чата: title/url
        try:
            chat_full = await asyncio.to_thread(api.get_chat, chat_id)
        except Exception:
            state.mem_store.save(k, mem)
            return

        title, item_url, chat_url = _extract_title_url(chat_full)

        # 1) отвечаем только по нужным объявлениям
        if title and not any(title.strip() == t.strip() for t in allowed_titles):
            state.mem_store.save(k, mem)
            return

        # 2) доп. защита по тематике (если title пустой)
        if not title and not _contains_any(merged_text, CEILING_KEYWORDS):
            state.mem_store.save(k, mem)
            return

        # 3) ручной режим активен — не отвечаем, но можем пинговать TG (редко)
        if manual_until > now:
            last_ping = float(mem.get("manual_last_notify") or 0)
            if now - last_ping > 120:  # раз в 2 минуты
                mem["manual_last_notify"] = now
                link = chat_url or item_url or "https://www.avito.ru/profile/messenger"
                state.notify_now(
                    "🧑‍💼 (Manual mode) Новое сообщение в Авито\n"
                    f"Chat ID: {chat_id}\n"
                    f"Объявление: {title or '-'}\n"
                    f"Ссылка: {link}\n"
                    f"Текст:\n{merged_text}"
                )
            state.mem_store.save(k, mem)
            return

        # 4) клиент просит человека → включаем manual mode и уведомляем TG
        if _contains_any(merged_text, HUMAN_TRIGGERS):
            mem["manual_until"] = now + manual_hours * 3600
            mem["manual_started_at"] = now
            mem["manual_reason"] = "client_requested_human"
            state.mem_store.save(k, mem)

            link = chat_url or item_url or "https://www.avito.ru/profile/messenger"
            state.notify_now(
                "🆘 Клиент просит менеджера (Авито)\n"
                f"Chat ID: {chat_id}\n"
                f"Объявление: {title or '-'}\n"
                f"Ссылка: {link}\n"
                f"Сообщение:\n{merged_text}"
            )

            try:
                await asyncio.to_thread(api.send_text, chat_id, "Понял(а) ✅ Передал(а) менеджеру — он ответит вам в этом чате.")
            except Exception:
                pass
            return

        # 5) обычный режим: генерим ответ твоим AppState
        reply = await asyncio.to_thread(
            state.generate_reply,
            "avito",
            chat_id,
            merged_text,
            {"title": title, "item_url": item_url, "chat_url": chat_url},
        )

        if reply and reply.strip():
            try:
                await asyncio.to_thread(api.send_text, chat_id, reply)
            except Exception:
                pass

        # read (не критично)
        try:
            await asyncio.to_thread(api.mark_read, chat_id)
        except Exception:
            pass

        state.mem_store.save(k, mem)

    while True:
        try:
            # ensure токен каждый цикл (если скоро истекает — обновит)
            await asyncio.to_thread(api.ensure_token)

            chats = await asyncio.to_thread(api.list_chats, 100, 0)
            for ch in chats:
                if _unread_count(ch) <= 0:
                    continue
                cid = _pick_chat_id(ch)
                if not cid:
                    continue

                msgs = await asyncio.to_thread(api.list_messages, cid, 30, 0)
                if not msgs:
                    continue

                # берём только входящие, и только новые относительно last_mid
                k = f"avito:{cid}"
                mem = state.mem_store.load(k)
                last_mid = str(mem.get("avito_last_mid") or "")

                # в сообщениях порядок может быть разный — упорядочим "как пришло" (обычно уже ок)
                new_msgs: List[Tuple[str, str]] = []
                passed_last = (last_mid == "")

                for m in msgs:
                    mid = _msg_id(m)
                    if last_mid and mid and mid == last_mid:
                        passed_last = True
                        continue
                    if not passed_last:
                        continue
                    if not _is_incoming(m, user_id):
                        continue
                    txt = _msg_text(m)
                    if not txt:
                        continue
                    new_msgs.append((mid, txt))

                if not new_msgs:
                    continue

                # дебаунс (склеим 2-3 сообщения)
                for mid, txt in new_msgs:
                    debounce.push(cid, mid, txt, handle_merged)

        except Exception:
            # временные сбои не должны валить процесс
            pass

        await asyncio.sleep(float(poll_interval))
