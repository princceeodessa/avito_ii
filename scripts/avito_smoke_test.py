# scripts/avito_smoke_test.py
import os
from dotenv import load_dotenv

from core.avito_api import AvitoAPI, AvitoAPIError


def main():
    load_dotenv()
    api = AvitoAPI(
        client_id=os.getenv("AVITO_CLIENT_ID", "").strip(),
        client_secret=os.getenv("AVITO_CLIENT_SECRET", "").strip(),
        token_path=os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json").strip(),
    )

    try:
        tok = api.ensure_token()
        print("TOKEN OK. expires_at=", tok.expires_at)

        info = api.get_self()
        print("SELF:", info)

        account_id = api.resolve_account_id(os.getenv("AVITO_USER_ID", "").strip())
        print("account_id =", account_id)

        if account_id:
            chats = api.get_chats_any(account_id)
            print("chats payload type:", type(chats).__name__)
            # не печатаем всё, только “верх”
            if isinstance(chats, dict):
                print("keys:", list(chats.keys())[:20])
            elif isinstance(chats, list):
                print("list_len:", len(chats))

        print("SMOKE TEST DONE OK")

    except AvitoAPIError as e:
        rid = f" (x-request-id={e.request_id})" if getattr(e, "request_id", "") else ""
        print(f"ERROR: {e}{rid}")
        if getattr(e, "details", ""):
            print("DETAILS:", e.details)
        raise
    finally:
        api.close()


if __name__ == "__main__":
    main()
