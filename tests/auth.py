import time
import json
import base64
import requests
from config import CLIENT_ID, CLIENT_SECRET, TOKEN_URL


class AvitoAuth:
    def __init__(self, storage_path="token_storage.json"):
        self.storage_path = storage_path
        self.access_token = None
        self.refresh_token = None
        self.expires_at = 0
        self._load_tokens()

    def _load_tokens(self):
        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)
                self.access_token = data["access_token"]
                self.refresh_token = data["refresh_token"]
                self.expires_at = data["expires_at"]
        except FileNotFoundError:
            pass

    def _save_tokens(self):
        with open(self.storage_path, "w") as f:
            json.dump({
                "access_token": self.access_token,
                "refresh_token": self.refresh_token,
                "expires_at": self.expires_at
            }, f, indent=2)

    def _auth_headers(self):
        basic = base64.b64encode(
            f"{CLIENT_ID}:{CLIENT_SECRET}".encode()
        ).decode()

        return {
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded"
        }

    def exchange_code(self, code, redirect_uri):
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri
        }

        r = requests.post(
            TOKEN_URL,
            headers=self._auth_headers(),
            data=data
        )

        if r.status_code != 200:
            print("❌ Ошибка получения токена:")
            print(r.status_code)
            print(r.text)

        r.raise_for_status()
        self._apply_token_response(r.json())

    def refresh(self):
        data = {
            "grant_type": "refresh_token",
            "refresh_token": self.refresh_token
        }

        r = requests.post(
            TOKEN_URL,
            headers=self._auth_headers(),
            data=data
        )

        r.raise_for_status()
        self._apply_token_response(r.json())

    def _apply_token_response(self, data):
        self.access_token = data["access_token"]
        self.refresh_token = data["refresh_token"]
        self.expires_at = time.time() + data["expires_in"]
        self._save_tokens()
        print("🔑 Токены успешно сохранены")

    def get_access_token(self):
        if time.time() >= self.expires_at - 60:
            self.refresh()
        return self.access_token
