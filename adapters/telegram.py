# adapters/telegram.py
import asyncio
from collections import defaultdict
from typing import Dict, List, Optional, Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from core.app_state import AppState


class DebouncedReply:
    """
    Склеивает сообщения от одного пользователя, пришедшие подряд за короткое время,
    и отвечает одним сообщением.
    """
    def __init__(self, bot: Bot, state: AppState, delay: float = 5, platform: str = "tg"):
        self.bot = bot
        self.state = state
        self.delay = delay
        self.platform = platform

        self._buffers: Dict[int, List[str]] = defaultdict(list)
        self._tasks: Dict[int, asyncio.Task] = {}

    #
    async def push(self, message: Message) -> None:
        if not message.text or not message.from_user:
            return

        uid = message.from_user.id
        self._buffers[uid].append(message.text.strip())

        # если юзер докинул сообщение — отменяем предыдущую отправку и ждём заново
        t = self._tasks.get(uid)
        if t and not t.done():
            t.cancel()

        self._tasks[uid] = asyncio.create_task(self._flush(uid, message))

    async def _flush(self, uid: int, message: Message) -> None:
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            return

        parts = self._buffers.pop(uid, [])
        if not parts:
            return

        user_text = "\n".join(parts).strip()

        meta = {
            "username": (message.from_user.username or ""),
            "name": (message.from_user.full_name or ""),
        }

        reply = self.state.generate_reply(
            platform=self.platform,
            user_id=str(uid),
            user_text=user_text,
            meta=meta
        )

        if reply:
            await self.bot.send_message(chat_id=message.chat.id, text=reply)


async def run_telegram(
    state: AppState,
    bot_token: str,
    callcenter_chat_id: str = "",
    debounce_delay: float = 1.2,
) -> None:
    bot = Bot(token=bot_token)
    dp = Dispatcher()
    router = Router()

    # --- callcenter notifier ---
    callcenter_chat_id = (callcenter_chat_id or "").strip()

    async def notify_coro(text: str) -> None:
        if not callcenter_chat_id:
            return
        try:
            await bot.send_message(chat_id=int(callcenter_chat_id), text=text)
        except Exception:
            # чтобы бот не падал из-за проблем с отправкой в колл-центр
            return

    state.set_notifier(asyncio.get_running_loop(), notify_coro)

    debouncer = DebouncedReply(bot=bot, state=state, delay=debounce_delay, platform="tg")

    # --- commands (без дебаунса) ---

    @router.message(Command("start"))
    async def cmd_start(message: Message):
        await message.answer(
            "Здравствуйте! Я менеджер по натяжным потолкам 😊\n"
            "Напишите, пожалуйста, город и примерную площадь (м²).\n"
            "Замер бесплатный — мастер приезжает с каталогами и образцами.\n"
            "/reset — сбросить диалог."
        )

    @router.message(Command("reset"))
    async def cmd_reset(message: Message):
        if not message.from_user:
            return
        state.reset_all(platform="tg", user_id=str(message.from_user.id))
        await message.answer("Ок, историю и данные сбросил. Напишите новый запрос.")

    # --- обычные сообщения (с дебаунсом) ---

    @router.message(F.text)
    async def on_text(message: Message):
        text = (message.text or "").strip()
        if not text:
            return

        # любые команды пусть обрабатываются только Command-хендлерами
        if text.startswith("/"):
            return

        await debouncer.push(message)

    dp.include_router(router)
    await dp.start_polling(bot)
