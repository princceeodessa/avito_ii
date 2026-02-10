import requests
from config import CHATS_URL
from auth import AvitoAuth


class AvitoChats:
    def __init__(self, auth: AvitoAuth):
        self.auth = auth

    def get_all_chats(self, limit=50, cursor=None):
        token = self.auth.get_access_token()

        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json"
        }

        params = {
            "limit": limit
        }

        if cursor:
            params["cursor"] = cursor

        response = requests.get(
            CHATS_URL,
            headers=headers,
            params=params
        )

        if response.status_code != 200:
            print("❌ Ошибка Avito API:")
            print(response.status_code)
            print(response.text)

        response.raise_for_status()
        return response.json()
