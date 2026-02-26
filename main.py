import os
import sys
import asyncio
from dotenv import load_dotenv

if sys.platform.startswith("win"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from core.app_state import AppState
from adapters.telegram import run_telegram
from adapters.avito_poller import run_avito_poller
from adapters.vk import run_vk


def log_task_result(t: asyncio.Task) -> None:
    try:
        exc = t.exception()
        if exc:
            print(f"[task error] {t.get_name()}: {exc}")
    except asyncio.CancelledError:
        pass


async def main():
    load_dotenv()

    model = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
    ollama_timeout = int(os.getenv("OLLAMA_TIMEOUT", "240") or "240")
    state = AppState(model=model, ollama_timeout=ollama_timeout)

    tasks = []
    enable_tg = (os.getenv("ENABLE_TG") or os.getenv("ENABLE_TELEGRAM") or "0").strip()
    if enable_tg == "1":
        tg_token = (os.getenv("TELEGRAM_TOKEN") or os.getenv("BOT_TOKEN") or "").strip()
        print("[main] ENABLE_TG=1, token_len=", len(tg_token))
        t = asyncio.create_task(
            run_telegram(
                state=state,
                bot_token=tg_token,
                callcenter_chat_id=os.getenv("CALLCENTER_CHAT_ID", ""),
                debounce_delay=float(os.getenv("TG_DEBOUNCE_DELAY", "5") or "5"),
            ),
            name="telegram",
        )
        t.add_done_callback(log_task_result)
        tasks.append(t)

    if os.getenv("ENABLE_AVITO", "0") == "0":
        t = asyncio.create_task(run_avito_poller(state), name="avito")
        t.add_done_callback(log_task_result)
        tasks.append(t)

    if os.getenv("ENABLE_VK", "0") == "0":
        t = asyncio.create_task(run_vk(state), name="vk")
        t.add_done_callback(log_task_result)
        tasks.append(t)

    if not tasks:
        raise RuntimeError("Ни один адаптер не включён. Поставь ENABLE_TG/ENABLE_AVITO/ENABLE_VK=1")

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for r in results:
        if isinstance(r, Exception):
            print("[main] task exception:", repr(r))


if __name__ == "__main__":
    asyncio.run(main())