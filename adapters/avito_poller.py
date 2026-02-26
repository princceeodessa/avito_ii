# adapters/avito_poller.py
import asyncio
import os
import time
from typing import Any, Dict, List, Optional

from core.avito_api import AvitoAPI
from core.app_state import AppState


HUMAN_TRIGGERS = [
    "оператор", "менеджер", "живой человек", "человек", "ассистент",
    "позови", "позовите", "соедини", "соедините", "не бот",
    "хочу человека", "переключи на человека",
]
# Важно: НЕ используем слабые триггеры ("кв", "м²", "замер") для определения темы.
# Они встречаются в куче нецелевых чатов ("квартира", "кв. м" и т.п.) и запускают спам.
STRONG_SERVICE_KEYWORDS = [
    # потолки
    "потол", "натяж", "светиль", "люстр", "профил", "тенев", "карниз",
    # шумоизоляция
    "шумо", "звуко", "изоляц", "акуст", "войлок", "мембран",
]


def _parse_csv_env(name: str) -> List[str]:
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return []
    parts = [p.strip() for p in raw.split(",")]
    return [p for p in parts if p]


def _title_allowed(title: str, allowed: List[str]) -> bool:
    if not allowed:
        return True
    t = _norm(title)
    return any(_norm(a) in t for a in allowed)


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

    # ✅ Опционально: allowlist по заголовкам объявлений.
    # Пример в .env:
    # AVITO_ALLOWED_TITLES=Натяжные потолки в Ижевске,Шумоизоляция и звукоизоляция под ключ
    allowed_titles = _parse_csv_env("AVITO_ALLOWED_TITLES")

    # ⚠️ Если у тебя в .env было AVITO_TRACE_CHAT_ID — оно режет всё до одного чата.
    # Оставляем как "отладочный" флаг, но если хочешь отвечать всем — просто не задавай его.
    trace_chat_id = os.getenv("AVITO_TRACE_CHAT_ID", "").strip()

    if not client_id or not client_secret or not user_id:
        raise RuntimeError("Нужно заполнить AVITO_CLIENT_ID, AVITO_CLIENT_SECRET, AVITO_USER_ID")

    api = AvitoAPI(
        client_id=client_id,
        client_secret=client_secret,
        user_id=user_id,
        token_path=token_path,
    )

    # ✅ На старте НЕ отвечаем на старые сообщения.
    # Идея: делаем "снимок" последних входящих сообщений по всем чатам и сохраняем их как уже обработанные.
    # Тогда бот будет отвечать только на новые сообщения, которые появятся ПОСЛЕ запуска.
    ignore_backlog_on_start = os.getenv("AVITO_IGNORE_BACKLOG_ON_START", "1") == "1"

    async def token_refresher():
        while True:
            await asyncio.sleep(23 * 3600)
            try:
                await asyncio.to_thread(api.refresh_token)
            except Exception:
                pass

    asyncio.create_task(token_refresher())

    print("[avito_poller] mode: LAST_MESSAGE (GET messages is not available: 405/404)")

    if ignore_backlog_on_start:
        try:
            await asyncio.to_thread(api.ensure_token)
            boot_cnt = 0
            limit = 100
            offset = 0
            # ⚠️ Важно: на аккаунте может быть >100 чатов.
            # Пролистываем все страницы, иначе бот «догонит» историю и будет спамить.
            while True:
                chats0 = await asyncio.to_thread(api.list_chats, limit, offset)
                if not chats0:
                    break
                for ch0 in chats0:
                    chat_id0 = _pick_chat_id(ch0)
                    if not chat_id0:
                        continue
                    last0 = _get_last_message(ch0)
                    if not last0:
                        continue
                    mid0 = _msg_id(last0)
                    txt0 = _msg_text(last0)
                    incoming0 = _is_incoming(last0, user_id)
                    if not mid0 or not txt0 or not incoming0:
                        continue

                    k0 = f"avito:{chat_id0}"
                    mem0: Dict[str, Any] = state.mem_store.load(k0)
                    mem0["avito_last_in_mid"] = mid0
                    state.mem_store.save(k0, mem0)
                    boot_cnt += 1

                if len(chats0) < limit:
                    break
                offset += limit

            print(f"[avito_poller] bootstrap: ignored backlog for {boot_cnt} chats")
        except Exception as e:
            print(f"[avito_poller] bootstrap error: {e}")

    while True:
        try:
            await asyncio.to_thread(api.ensure_token)
            # Листаем все страницы, иначе новые сообщения в «дальних» чатах не будут обрабатываться.
            limit = 100
            offset = 0
            total = 0
            while True:
                chats = await asyncio.to_thread(api.list_chats, limit, offset)
                if not chats:
                    break
                total += len(chats)

                if debug and offset == 0:
                    print(f"[avito_poller] tick: first_page_chats={len(chats)}")

                for ch in chats:
                    chat_id = _pick_chat_id(ch)
                    if not chat_id:
                        continue

                    # ✅ Отладка (по умолчанию пусто). Если заполнено — режет до одного чата.
                    if trace_chat_id and chat_id != trace_chat_id:
                        continue

                    last = _get_last_message(ch)
                    if not last:
                        continue

                    mid = _msg_id(last)
                    text = _msg_text(last)
                    incoming = _is_incoming(last, user_id)

                    # отвечаем ТОЛЬКО на новые входящие от клиента
                    if not text or not mid or not incoming:
                        continue

                    k = f"avito:{chat_id}"
                    mem_before: Dict[str, Any] = state.mem_store.load(k)

                    # ✅ анти-дубль: уже обработали этот incoming
                    if str(mem_before.get("avito_last_in_mid") or "") == mid:
                        continue

                    meta = _extract_meta(ch)
                    title = meta.get("title", "")

                    # ✅ Allowlist по объявлениям (если задано)
                    if title and not _title_allowed(title, allowed_titles):
                        mem_before["avito_last_in_mid"] = mid
                        state.mem_store.save(k, mem_before)
                        if debug:
                            print("[TRACE] skip: title not allowed")
                        continue

                    if debug:
                        print(f"[TRACE] chat={chat_id} title='{title}' last_id={mid} in={incoming} text={text!r}")

                    # ✅ Тема: только по «сильным» ключам
                    if not (_contains_any(text, STRONG_SERVICE_KEYWORDS) or _contains_any(title, STRONG_SERVICE_KEYWORDS)):
                        mem_before["avito_last_in_mid"] = mid
                        state.mem_store.save(k, mem_before)
                        if debug:
                            print("[TRACE] skip: not our service topic")
                        continue

                    now = time.time()
                    manual_until = float(mem_before.get("manual_until") or 0)
                    if manual_until > now:
                        mem_before["avito_last_in_mid"] = mid
                        state.mem_store.save(k, mem_before)
                        continue

                    # 🆘 запрос менеджера
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
                            await asyncio.to_thread(api.send_text, chat_id, "Поняла ✅ Передала менеджеру — он ответит вам в чате.")
                        except Exception:
                            pass
                        continue

                    # ✅ Генерация ответа
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

                    # ✅ Запоминаем последний входящий mid
                    mem_after: Dict[str, Any] = state.mem_store.load(k)
                    mem_after["avito_last_in_mid"] = mid
                    state.mem_store.save(k, mem_after)

                # pagination
                if len(chats) < limit:
                    break
                offset += limit

            if debug:
                print(f"[avito_poller] tick done: chats_scanned={total}")

        except Exception as e:
            print(f"[avito_poller] LOOP ERROR: {e}")

        await asyncio.sleep(float(poll_interval))