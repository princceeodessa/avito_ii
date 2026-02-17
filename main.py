import asyncio
import os
from dotenv import load_dotenv

from core.app_state import AppState
from adapters.telegram import run_telegram


async def main():
    load_dotenv()

    bot_token = os.getenv("BOT_TOKEN")
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is not set in .env")

    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ollama_timeout = int(os.getenv("OLLAMA_TIMEOUT", "240"))
    callcenter_chat_id = os.getenv("CALLCENTER_CHAT_ID", "")

    state = AppState(model=model, ollama_timeout=ollama_timeout)

    # сейчас только TG
    await run_telegram(state, bot_token=bot_token, callcenter_chat_id=callcenter_chat_id)


if __name__ == "__main__":
    asyncio.run(main())
