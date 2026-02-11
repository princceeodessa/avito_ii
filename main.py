import threading
from urllib.parse import urlencode

from config import CLIENT_ID, AUTH_URL, REDIRECT_URI, SCOPES
from tests.auth_server import run_server, wait_for_code
from tests.auth import AvitoAuth


def main():
    auth = AvitoAuth()

    if auth.access_token is None:
        print("🌐 Запуск авторизации через браузер")

        threading.Thread(target=run_server, daemon=True).start()

        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "scope": SCOPES,
            "redirect_uri": REDIRECT_URI
        }

        url = f"{AUTH_URL}?{urlencode(params)}"

        print("\nОткрой эту ссылку в браузере вручную:\n")
        print(url)
        print("\nЖду авторизацию...\n")

        code = wait_for_code()
        auth.exchange_code(code, REDIRECT_URI)

    print("✅ Авторизация завершена")


if __name__ == "__main__":
    main()
