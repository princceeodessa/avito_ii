# scripts/avito_smoke_test.py
import os
from dotenv import load_dotenv

from core.avito_api import AvitoAPI, AvitoAPIError


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Нет переменной окружения {name}. Добавь её в .env или в ENV системы.")
    return v


def main():
    # подхватит .env из текущей папки проекта (и из рядом стоящих)
    load_dotenv()

    api = AvitoAPI(
        client_id=must_env("AVITO_CLIENT_ID"),
        client_secret=must_env("AVITO_CLIENT_SECRET"),
        user_id=int(must_env("AVITO_USER_ID")),
        token_path=os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json"),
    )

    try:
        api.ensure_token()
        print("OK: token loaded/refreshed")

        chats = api.list_chats(limit=20, offset=0, unread_only=True)
        print(f"unread chats: {len(chats)}")

        for c in chats[:10]:
            cid = c.get("id") or c.get("chat_id") or c.get("chatId")
            title = ""
            ctx = c.get("context") or {}
            val = ctx.get("value") if isinstance(ctx.get("value"), dict) else {}
            if isinstance(val, dict):
                title = str(val.get("title") or "")
            print(" chat:", cid, "| title:", title)

        test_chat = os.getenv("AVITO_TEST_CHAT_ID", "").strip()
        if test_chat:
            msgs = api.list_messages(test_chat, limit=20, offset=0)
            print(f"messages in {test_chat}: {len(msgs)}")

            if os.getenv("AVITO_SEND_TEST", "0") == "1":
                api.send_text(test_chat, "Тест: бот видит чат и умеет отправлять ✅")
                api.mark_read(test_chat)
                print("OK: test message sent & chat marked read")

    except AvitoAPIError as e:
        print("AVITO API ERROR:", str(e))
        print(" status:", getattr(e, "status_code", None))
        print(" details:", (getattr(e, "details", "") or "")[:800])
        print(" request_id:", getattr(e, "request_id", ""))
        raise
    finally:
        api.close()


if __name__ == "__main__":
    main()# scripts/avito_smoke_test.py
import os
from dotenv import load_dotenv

from core.avito_api import AvitoAPI, AvitoAPIError


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Нет переменной окружения {name}. Добавь её в .env или в ENV системы.")
    return v


def main():
    # подхватит .env из текущей папки проекта (и из рядом стоящих)
    load_dotenv()

    api = AvitoAPI(
        client_id=must_env("AVITO_CLIENT_ID"),
        client_secret=must_env("AVITO_CLIENT_SECRET"),
        user_id=int(must_env("AVITO_USER_ID")),
        token_path=os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json"),
    )

    try:
        api.ensure_token()
        print("OK: token loaded/refreshed")

        chats = api.list_chats(limit=20, offset=0, unread_only=True)
        print(f"unread chats: {len(chats)}")

        for c in chats[:10]:
            cid = c.get("id") or c.get("chat_id") or c.get("chatId")
            title = ""
            ctx = c.get("context") or {}
            val = ctx.get("value") if isinstance(ctx.get("value"), dict) else {}
            if isinstance(val, dict):
                title = str(val.get("title") or "")
            print(" chat:", cid, "| title:", title)

        test_chat = os.getenv("AVITO_TEST_CHAT_ID", "").strip()
        if test_chat:
            msgs = api.list_messages(test_chat, limit=20, offset=0)
            print(f"messages in {test_chat}: {len(msgs)}")

            if os.getenv("AVITO_SEND_TEST", "0") == "1":
                api.send_text(test_chat, "Тест: бот видит чат и умеет отправлять ✅")
                api.mark_read(test_chat)
                print("OK: test message sent & chat marked read")

    except AvitoAPIError as e:
        print("AVITO API ERROR:", str(e))
        print(" status:", getattr(e, "status_code", None))
        print(" details:", (getattr(e, "details", "") or "")[:800])
        print(" request_id:", getattr(e, "request_id", ""))
        raise
    finally:
        api.close()


if __name__ == "__main__":
    main()