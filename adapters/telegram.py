# adapters/telegram.py
import asyncio
import os
import ssl
import smtplib
from email.message import EmailMessage

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message


async def make_email_sender():
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)
    smtp_to = os.getenv("SMTP_TO", "")

    async def send_email(subject: str, body: str, file_path: str) -> bool:
        if not (smtp_host and smtp_user and smtp_pass and smtp_to and file_path):
            return False

        def _send_blocking() -> bool:
            msg = EmailMessage()
            msg["Subject"] = subject
            msg["From"] = smtp_from
            msg["To"] = smtp_to
            msg.set_content(body)

            with open(file_path, "rb") as f:
                data = f.read()
            filename = os.path.basename(file_path)
            msg.add_attachment(data, maintype="text", subtype="plain", filename=filename)

            context = ssl.create_default_context()
            with smtplib.SMTP(smtp_host, smtp_port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.login(smtp_user, smtp_pass)
                server.send_message(msg)
            return True

        try:
            ok = await asyncio.to_thread(_send_blocking)
            if ok:
                try:
                    os.remove(file_path)  # ✅ удаляем файл после успешной отправки
                except Exception:
                    pass
            return ok
        except Exception:
            # ❗ если отправка не удалась — файл НЕ удаляем
            return False

    return send_email


async def run_telegram(state, bot_token: str, callcenter_chat_id: str = ""):
    bot = Bot(token=bot_token)
    dp = Dispatcher()

    # подключаем email sender (если SMTP_* в .env заполнены)
    email_sender = await make_email_sender()
    state.set_email_sender(asyncio.get_running_loop(), email_sender)

    # подключаем мгновенную отправку в телеграм колл-центра (если chat_id задан)
    async def _send_to_callcenter(text: str):
        if not callcenter_chat_id:
            return
        await bot.send_message(callcenter_chat_id, text)

    state.set_notifier(asyncio.get_running_loop(), _send_to_callcenter)

    @dp.message(Command("start"))
    async def start(m: Message):
        await m.answer(
            "Привет! Я менеджер по натяжным потолкам 🙂\n"
            "Напиши сообщение.\n"
            "/reset — сбросить диалог."
        )

    @dp.message(Command("reset"))
    async def reset(m: Message):
        uid = str(m.from_user.id)
        state.reset_all("tg", uid)
        await m.answer("Ок, историю и данные сбросил. Напиши новый запрос.")

    @dp.message(F.text)
    async def on_text(m: Message):
        uid = str(m.from_user.id)
        text = (m.text or "").strip()
        if not text:
            return

        meta = {
            "username": m.from_user.username or "",
            "name": " ".join(x for x in [m.from_user.first_name, m.from_user.last_name] if x) or "",
        }

        # чтобы не блокировать aiogram loop
        answer = await asyncio.to_thread(state.generate_reply, "tg", uid, text, meta)

        if len(answer) > 3900:
            answer = answer[:3900] + "\n\n(сообщение обрезано)"
        await m.answer(answer)

    await dp.start_polling(bot)
