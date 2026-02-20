# scripts/avito_hard_test.py
import os
import time
import json
from urllib.parse import quote
from dotenv import load_dotenv

from core.avito_api import AvitoAPI


def must_env(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"Нет переменной окружения {name}. Добавь её в .env или в ENV системы.")
    return v


def short_body(text: str, n: int = 900) -> str:
    t = (text or "").strip()
    if len(t) > n:
        return t[:n] + " ...<cut>"
    return t


def pretty_json(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2)[:1200] + (" ...<cut>" if len(json.dumps(data, ensure_ascii=False)) > 1200 else "")
    except Exception:
        return str(data)[:1200]


def do_get(api: AvitoAPI, path: str, params=None):
    r = api._http.get(path, headers=api._auth_headers(), params=params)
    ct = r.headers.get("content-type", "")
    rid = r.headers.get("x-request-id", "")
    print(f"[GET] {path} params={params} -> {r.status_code} ct={ct} x-request-id={rid}")
    # печатаем тело аккуратно
    if "application/json" in ct:
        try:
            j = r.json()
            # покажем ключи/структуру
            if isinstance(j, dict):
                print("     json keys:", list(j.keys())[:30])
            elif isinstance(j, list):
                print("     json list len:", len(j))
            print("     json preview:\n", pretty_json(j))
        except Exception:
            print("     json parse error, body:\n", short_body(r.text))
    else:
        print("     body:\n", short_body(r.text))
    return r


def do_post(api: AvitoAPI, path: str, payload: dict):
    r = api._http.post(path, headers=api._auth_headers(), json=payload)
    ct = r.headers.get("content-type", "")
    rid = r.headers.get("x-request-id", "")
    print(f"[POST] {path} payload={list(payload.keys())} -> {r.status_code} ct={ct} x-request-id={rid}")
    if "application/json" in ct:
        try:
            print("      json:\n", pretty_json(r.json()))
        except Exception:
            print("      body:\n", short_body(r.text))
    else:
        print("      body:\n", short_body(r.text))
    return r


def main():
    load_dotenv()

    api = AvitoAPI(
        client_id=must_env("AVITO_CLIENT_ID"),
        client_secret=must_env("AVITO_CLIENT_SECRET"),
        user_id=int(must_env("AVITO_USER_ID")),
        token_path=os.getenv("AVITO_TOKEN_PATH", "data/avito_tokens.json"),
    )

    chat_id = (os.getenv("AVITO_HARD_CHAT_ID", "") or os.getenv("AVITO_TEST_CHAT_ID", "")).strip()
    if not chat_id:
        raise RuntimeError("Задай AVITO_HARD_CHAT_ID (или AVITO_TEST_CHAT_ID) в .env")

    # два варианта: как есть и url-encoded (на случай спецсимволов типа ~)
    chat_ids = [chat_id]
    enc = quote(chat_id, safe="")
    if enc != chat_id:
        chat_ids.append(enc)

    print("=== HARD TEST START ===")
    print("account(user_id):", api.user_id)
    print("chat_id:", chat_id)
    if len(chat_ids) > 1:
        print("chat_id encoded:", enc)

    try:
        api.ensure_token()
        print("[TOKEN] OK")

        # 1) найдём чат в списке и покажем его структуру (важно понять, какой там id)
        chats = api.list_chats(limit=100, offset=0, unread_only=None)
        print(f"[CHATS] got={len(chats)} (showing match by id/chat_id)")
        found = None
        for c in chats:
            cid = c.get("id") or c.get("chat_id") or c.get("chatId")
            if cid == chat_id:
                found = c
                break
        if found:
            print("[CHAT OBJ] keys:", list(found.keys())[:40])
            print("[CHAT OBJ] preview:\n", pretty_json(found))
        else:
            print("[CHAT OBJ] not found in first 100 chats (maybe offset needed)")

        # 2) ПРОБА: все варианты messages endpoint
        #    (с params и без params) + v1/v2 + со слэшем и без
        for cid in chat_ids:
            print("\n=== PROBE messages endpoints for chat_id =", cid, "===")
            candidates = [
                f"/messenger/v2/accounts/{api.user_id}/chats/{cid}/messages/",
                f"/messenger/v2/accounts/{api.user_id}/chats/{cid}/messages",
                f"/messenger/v1/accounts/{api.user_id}/chats/{cid}/messages/",
                f"/messenger/v1/accounts/{api.user_id}/chats/{cid}/messages",
            ]
            for p in candidates:
                do_get(api, p, params={"limit": 30, "offset": 0})
                do_get(api, p, params=None)

        # 3) ПРОБА: отправка сообщения (чтобы понять, есть ли write-доступ вообще)
        stamp = int(time.time())
        test_text = f"HARDTEST ✅ {stamp}"
        print("\n=== PROBE send message ===")
        send_path_v1 = f"/messenger/v1/accounts/{api.user_id}/chats/{chat_id}/messages"
        send_variants = [
            ("type+message", {"type": "text", "message": {"text": test_text}}),
            ("type+content", {"type": "text", "content": {"text": test_text}}),
            ("plain", {"text": test_text}),
        ]
        for name, payload in send_variants:
            print(f"\n--- send variant: {name} ---")
            do_post(api, send_path_v1, payload)

        # 4) и ещё раз messages после отправки (вдруг до этого чат был пустой/закрыт)
        print("\n=== RE-PROBE messages after send ===")
        candidates = [
            f"/messenger/v2/accounts/{api.user_id}/chats/{chat_id}/messages/",
            f"/messenger/v2/accounts/{api.user_id}/chats/{chat_id}/messages",
            f"/messenger/v1/accounts/{api.user_id}/chats/{chat_id}/messages/",
            f"/messenger/v1/accounts/{api.user_id}/chats/{chat_id}/messages",
        ]
        for p in candidates:
            do_get(api, p, params={"limit": 50, "offset": 0})

        print("\n=== HARD TEST END ===")

    finally:
        api.close()


if __name__ == "__main__":
    main()