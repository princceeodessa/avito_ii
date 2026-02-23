# adapters/vk.py
import asyncio
import os
import socket
import time
from collections import defaultdict
from typing import Any, Dict, List

import aiohttp

from core.app_state import AppState

VK_API_VERSION = os.getenv("VK_API_VERSION", "5.131")


def parse_int_env(name: str, default: int = 0) -> int:
    """
    Поддерживает .env строки вида:
    VK_TEST_USER_ID=12345 # comment
    """
    raw = (os.getenv(name, "") or "").strip()
    if not raw:
        return default
    for sep in ("#", ";"):
        if sep in raw:
            raw = raw.split(sep, 1)[0].strip()
    return int(raw) if raw else default


class VKAPIError(RuntimeError):
    pass


async def vk_call(session: aiohttp.ClientSession, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"https://api.vk.com/method/{method}"
    async with session.post(url, data=params) as r:
        data = await r.json(content_type=None)

    if "error" in data:
        err = data["error"]
        raise VKAPIError(f"VK error {err.get('error_code')}: {err.get('error_msg')}")

    return data.get("response", {})


async def vk_send_message(session: aiohttp.ClientSession, token: str, peer_id: int, text: str) -> None:
    params = {
        "access_token": token,
        "v": VK_API_VERSION,
        "peer_id": peer_id,
        "random_id": int(time.time() * 1000),
        "message": text,
    }
    await vk_call(session, "messages.send", params)


async def vk_get_longpoll(session: aiohttp.ClientSession, token: str, group_id: int) -> Dict[str, Any]:
    resp = await vk_call(session, "groups.getById", {
        "access_token": token,
        "v": VK_API_VERSION,
        "group_id": group_id,
    })
    print("[vk] groups.getById OK:", resp)
    params = {"access_token": token, "v": VK_API_VERSION, "group_id": group_id}
    return await vk_call(session, "groups.getLongPollServer", params)


class DebouncedReply:
    """
    Как в Telegram: склеиваем несколько сообщений пользователя и отвечаем одним сообщением.
    """

    def __init__(self, state: AppState, delay: float = 1.2):
        self.state = state
        self.delay = float(delay)
        self._buffers: Dict[int, List[str]] = defaultdict(list)
        self._tasks: Dict[int, asyncio.Task] = {}

    async def push(
        self,
        user_id: int,
        peer_id: int,
        text: str,
        meta: Dict[str, Any],
        session: aiohttp.ClientSession,
        token: str,
    ) -> None:
        self._buffers[user_id].append(text.strip())

        t = self._tasks.get(user_id)
        if t and not t.done():
            t.cancel()

        self._tasks[user_id] = asyncio.create_task(self._flush(user_id, peer_id, meta, session, token))

    async def _flush(
        self,
        user_id: int,
        peer_id: int,
        meta: Dict[str, Any],
        session: aiohttp.ClientSession,
        token: str,
    ) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return

        parts = self._buffers.pop(user_id, [])
        if not parts:
            return

        user_text = "\n".join(parts).strip()

        # generate_reply может блокировать (LLM/requests) — уводим в thread
        reply = await asyncio.to_thread(
            self.state.generate_reply,
            "vk",
            str(user_id),
            user_text,
            meta,
        )

        if reply:
            reply = reply.replace("__PROMO_IMAGE__\n", "")
            await vk_send_message(session, token, peer_id, reply)


async def run_vk(state: AppState) -> None:

    token = os.getenv("VK_GROUP_TOKEN", "").strip()
    group_id = parse_int_env("VK_GROUP_ID", 0)
    test_user_id = parse_int_env("VK_TEST_USER_ID", 0)

    poll_wait = parse_int_env("VK_POLL_WAIT", 25)
    debounce_delay = float((os.getenv("VK_DEBOUNCE_DELAY", "1.2") or "1.2").strip())
    debug = os.getenv("VK_DEBUG", "0") == "1"

    if not token or not group_id or not test_user_id:
        raise RuntimeError("Нужно задать VK_GROUP_TOKEN, VK_GROUP_ID, VK_TEST_USER_ID")

    # ✅ Forced IPv4 (полезно на Windows)
    connector = aiohttp.TCPConnector(family=socket.AF_INET, ssl=True)

    # ✅ Таймауты
    timeout = aiohttp.ClientTimeout(total=60, connect=30, sock_connect=30, sock_read=60)

    debouncer = DebouncedReply(state=state, delay=debounce_delay)

    # анти-дубли апдейтов по conversation_message_id
    seen_conv_ids: Dict[int, int] = {}  # from_id -> last_conversation_message_id

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Получаем longpoll server/key/ts с ретраями
        while True:
            try:
                lp = await vk_get_longpoll(session, token, group_id)
                server = lp["server"]
                key = lp["key"]
                ts = lp["ts"]
                if debug:
                    print("[vk] longpoll server acquired")
                break
            except Exception as e:
                print(f"[vk] cannot get longpoll server: {e}")
                await asyncio.sleep(3)

        while True:
            try:
                params = {"act": "a_check", "key": key, "ts": ts, "wait": poll_wait}
                async with session.get(server, params=params) as r:
                    data = await r.json(content_type=None)

                if "failed" in data:
                    failed = int(data.get("failed") or 0)
                    if debug:
                        print(f"[vk] longpoll failed={failed}, refreshing server")
                    lp = await vk_get_longpoll(session, token, group_id)
                    server = lp["server"]
                    key = lp["key"]
                    ts = lp["ts"]
                    continue

                ts = data.get("ts", ts)
                updates = data.get("updates", [])

                for upd in updates:
                    if upd.get("type") != "message_new":
                        continue

                    msg = upd.get("object", {}).get("message", {}) or {}
                    text = (msg.get("text") or "").strip()
                    if not text:
                        continue

                    from_id = int(msg.get("from_id") or 0)
                    peer_id = int(msg.get("peer_id") or 0)

                    # тестовый режим: отвечаем только одному юзеру
                    if from_id != test_user_id:
                        if debug:
                            print(f"[vk] ignored user {from_id} (test only {test_user_id})")
                        continue

                    conv_id = int(msg.get("conversation_message_id") or 0)
                    if conv_id and seen_conv_ids.get(from_id) == conv_id:
                        # один и тот же апдейт пришёл повторно
                        continue
                    if conv_id:
                        seen_conv_ids[from_id] = conv_id

                    meta = {
                        "vk_from_id": from_id,
                        "peer_id": peer_id,
                    }

                    # команды
                    if text == "/reset":
                        state.reset_all("vk", str(from_id))
                        await vk_send_message(session, token, peer_id, "Ок, историю сбросил. Напишите новый запрос 🙂")
                        continue

                    if text == "/start":
                        await vk_send_message(
                            session,
                            token,
                            peer_id,
                            "Здравствуйте! Напишите город и примерную площадь (м²) — сориентирую по стоимости 😊",
                        )
                        continue

                    await debouncer.push(from_id, peer_id, text, meta, session, token)

            except asyncio.TimeoutError:
                continue
            except Exception as e:
                print(f"[vk] ERROR: {e}")
                # пробуем обновить server/key/ts
                try:
                    lp = await vk_get_longpoll(session, token, group_id)
                    server = lp["server"]
                    key = lp["key"]
                    ts = lp["ts"]
                except Exception:
                    pass
                await asyncio.sleep(2)