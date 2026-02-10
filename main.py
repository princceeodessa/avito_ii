import threading
import webbrowser
from urllib.parse import urlencode

from config import CLIENT_ID, AUTH_URL, REDIRECT_URI, SCOPES
from auth_server import run_server, wait_for_code
from auth import AvitoAuth


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
        print("➡️ Открой в браузере:", url)
        webbrowser.open(url)

        code = wait_for_code()
        auth.exchange_code(code, REDIRECT_URI)

    print("✅ Авторизация завершена")


if __name__ == "__main__":
    main()
