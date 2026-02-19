# scripts/avito_dump_chats.py
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv

from core.avito_api import AvitoAPI, AvitoAPIError


def _extract_chats_list(payload):
    # разные форматы: {"chats":[...]} или {"result":[...]} или просто список
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        for key in ("chats", "result", "data", "items"):
            v = payload.get(key)
            if isinstance(v, list):
                return v
    return []


def main():
    load_dotenv()

    client_id = os.getenv("AVITO_CLIENT_ID", "").strip()
    client_secret = os.getenv("AVITO_CLIENT_SECRET", "").strip()
    token_path = os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json").strip()
    env_user_id = os.getenv("AVITO_USER_ID", "").strip()

    if not client_id or not client_secret:
        raise RuntimeError("AVITO_CLIENT_ID/AVITO_CLIENT_SECRET не заданы в .env")

    api = AvitoAPI(client_id=client_id, client_secret=client_secret, token_path=token_path)

    try:
        # 1) проверим токен/само-аккаунт
        account_id = api.resolve_account_id(fallback_env_user_id=env_user_id)
        if not account_id:
            raise RuntimeError(
                "Не смог определить account_id. "
                "Проверь AVITO_USER_ID или доступность /core/v1/accounts/self."
            )

        # 2) попробуем получить чаты (если v1 — дальше можно пагинировать)
        all_chats = []
        limit = 50
        offset = 0

        # сначала один запрос “как умеет” (v2 обычно отдаст сразу список)
        payload = api.get_chats_any(account_id, limit=limit, offset=offset)
        chunk = _extract_chats_list(payload)

        # если это v2 и пришел большой список — просто сохраняем
        # если это v1 и пришла страница — попробуем пагинацию (пока страницы не закончатся)
        if chunk:
            all_chats.extend(chunk)

        # эвристика: если пришло ровно limit — вероятно v1-страница, попробуем листать
        # если меньше — скорее всего всё уже получили
        while len(chunk) == limit:
            offset += limit
            payload = api.get_chats_any(account_id, limit=limit, offset=offset)
            chunk = _extract_chats_list(payload)
            if not chunk:
                break
            all_chats.extend(chunk)

        out_dir = Path("data")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"avito_chats_dump_{int(time.time())}.json"
        out_path.write_text(json.dumps(all_chats, ensure_ascii=False, indent=2), encoding="utf-8")

        print(f"OK. account_id={account_id}")
        print(f"Сохранено чатов: {len(all_chats)}")
        print(f"Файл: {out_path}")

        # покажем пару chat_id (если есть)
        sample_ids = []
        for c in all_chats[:5]:
            if isinstance(c, dict):
                for k in ("id", "chat_id"):
                    if k in c:
                        sample_ids.append(str(c[k]))
                        break
        if sample_ids:
            print("Примеры chat_id:", ", ".join(sample_ids))

    except AvitoAPIError as e:
        rid = f" (x-request-id={e.request_id})" if getattr(e, "request_id", "") else ""
        details = getattr(e, "details", "") or ""
        print(f"{e}{rid}")
        if details:
            print("DETAILS:", details)
        raise
    finally:
        api.close()


if __name__ == "__main__":
    main()
