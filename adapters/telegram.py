# adapters/telegram.py
import asyncio
import os
from collections import defaultdict
from typing import Dict, List

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.types import FSInputFile, Message

from core.app_state import AppState


class DebouncedReply:
    def __init__(self, bot: Bot, state: AppState, delay: float = 1.2, platform: str = "tg"):
        self.bot = bot
        self.state = state
        self.delay = float(delay)
        self.platform = platform
        self._buffers: Dict[int, List[str]] = defaultdict(list)
        self._tasks: Dict[int, asyncio.Task] = {}

    async def push(self, message: Message) -> None:
        if not message.text or not message.from_user:
            return

        uid = message.from_user.id
        self._buffers[uid].append(message.text.strip())

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

        try:
            reply = await asyncio.to_thread(
                self.state.generate_reply,
                platform=self.platform,
                user_id=str(uid),
                user_text=user_text,
                meta=meta,
            )
        except Exception as e:
            # чтобы TG не молчал при падении LLM
            await self.bot.send_message(
                chat_id=message.chat.id,
                text="Сервис ответа сейчас занят 😕 Сообщение получил. Если ответа не будет — напишите «+».",
            )
            print(f"[tg] generate_reply error: {e}")
            return

        if not reply:
            return

        # промо-маркер
        if reply.startswith("__PROMO_IMAGE__\n"):
            text = reply.split("\n", 1)[1].strip()
            promo_path = os.getenv("TG_PROMO_IMAGE_PATH", "data/promo_tg.jpg")
            # совместимость: если по умолчанию jpg отсутствует, пробуем png (и наоборот)
            if not os.path.exists(promo_path):
                alt = promo_path
                if alt.lower().endswith(".jpg"):
                    alt = alt[:-4] + ".png"
                elif alt.lower().endswith(".png"):
                    alt = alt[:-4] + ".jpg"
                if os.path.exists(alt):
                    promo_path = alt
            if os.path.exists(promo_path):
                await self.bot.send_photo(chat_id=message.chat.id, photo=FSInputFile(promo_path), caption=text)
            else:
                await self.bot.send_message(chat_id=message.chat.id, text=text)
            return

        await self.bot.send_message(chat_id=message.chat.id, text=reply)


async def run_telegram(
    state: AppState,
    bot_token: str,
    callcenter_chat_id: str = "",
    debounce_delay: float = 1.2,
) -> None:
    bot_token = (bot_token or "").strip()
    if not bot_token or ":" not in bot_token:
        raise RuntimeError("Telegram bot token is empty/invalid. Set TELEGRAM_TOKEN (or BOT_TOKEN).")

    bot = Bot(token=bot_token)
    dp = Dispatcher()
    router = Router()

    callcenter_chat_id = (callcenter_chat_id or "").strip()
    callcenter_chat_id_int = None
    if callcenter_chat_id:
        try:
            callcenter_chat_id_int = int(callcenter_chat_id)
        except Exception:
            print(f"[tg] CALLCENTER_CHAT_ID is not an int: {callcenter_chat_id!r}")

    async def notify_coro(text: str) -> None:
        if not callcenter_chat_id_int:
            return
        try:
            await bot.send_message(chat_id=callcenter_chat_id_int, text=text)
        except Exception as e:
            # важно: не молчим, иначе кажется что "заявки не прилетают"
            print(f"[tg] notify_coro error: {e}")
            return

    state.set_notifier(asyncio.get_running_loop(), notify_coro)
    debouncer = DebouncedReply(bot=bot, state=state, delay=debounce_delay, platform="tg")

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
        await message.answer("Ок, историю и данные сбросил. Напишите новый запрос 🙂")

    @router.message(F.text)
    async def on_text(message: Message):
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return
        await debouncer.push(message)

    dp.include_router(router)
    await dp.start_polling(bot)