# core/avito_api.py
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional

import httpx

AVITO_BASE_URL = "https://api.avito.ru"


@dataclass
class AvitoToken:
    access_token: str
    token_type: str = "Bearer"
    expires_at: float = 0.0  # unix ts
    refresh_token: Optional[str] = None  # будет только для authorization_code

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AvitoToken":
        return cls(
            access_token=str(d.get("access_token", "")),
            token_type=str(d.get("token_type", "Bearer")),
            expires_at=float(d.get("expires_at", 0.0)),
            refresh_token=d.get("refresh_token"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "expires_at": self.expires_at,
            "refresh_token": self.refresh_token,
        }

    def valid(self, skew: int = 60) -> bool:
        return bool(self.access_token) and (time.time() + skew) < self.expires_at


class AvitoAPI:
    """
    Поддерживает:
    - client_credentials (персональная авторизация)  ✅ для твоего кейса
    - authorization_code + refresh_token (если когда-нибудь понадобится)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        user_id: int,
        auth_flow: str = "client_credentials",
        redirect_uri: Optional[str] = None,
        token_path: str = "data/avito_tokens.json",
        timeout: int = 30,
    ) -> None:
        self.client_id = client_id
        self.client_secret = client_secret
        self.user_id = int(user_id)

        self.auth_flow = (auth_flow or "client_credentials").strip()
        self.redirect_uri = redirect_uri
        self.token_path = token_path

        self._http = httpx.Client(base_url=AVITO_BASE_URL, timeout=timeout)
        self._token: Optional[AvitoToken] = None

        os.makedirs(os.path.dirname(self.token_path) or ".", exist_ok=True)

    # ---------- token cache ----------

    def _load_token(self) -> Optional[AvitoToken]:
        if self._token:
            return self._token
        if not os.path.exists(self.token_path):
            return None
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                self._token = AvitoToken.from_dict(json.load(f))
            return self._token
        except Exception:
            return None

    def _save_token(self, t: AvitoToken) -> None:
        self._token = t
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(t.to_dict(), f, ensure_ascii=False, indent=2)

    # ---------- OAuth flows ----------

    def _token_client_credentials(self) -> AvitoToken:
        r = self._http.post(
            "/token/",
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        j = r.json()
        expires_in = int(j.get("expires_in", 86400))
        t = AvitoToken(
            access_token=j["access_token"],
            token_type=j.get("token_type", "Bearer"),
            expires_at=time.time() + expires_in,
        )
        self._save_token(t)
        return t

    def _token_refresh(self, refresh_token: str) -> AvitoToken:
        r = self._http.post(
            "/token/",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        r.raise_for_status()
        j = r.json()
        expires_in = int(j.get("expires_in", 86400))
        t = AvitoToken(
            access_token=j["access_token"],
            token_type=j.get("token_type", "Bearer"),
            refresh_token=j.get("refresh_token", refresh_token),
            expires_at=time.time() + expires_in,
        )
        self._save_token(t)
        return t

    def get_access_token(self) -> str:
        """
        Для твоего кейса: client_credentials.
        """
        t = self._load_token()
        if t and t.valid():
            return t.access_token

        if self.auth_flow == "client_credentials":
            return self._token_client_credentials().access_token

        # если вдруг кто-то включит authorization_code, то нужно иметь refresh_token в файле
        if t and t.refresh_token:
            return self._token_refresh(t.refresh_token).access_token

        raise RuntimeError("No valid token. For authorization_code you must first obtain refresh_token.")

    def _auth_headers(self) -> Dict[str, str]:
        return {"Authorization": f"Bearer {self.get_access_token()}"}

    # ---------- Messenger API ----------

    def get_chats(self, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        r = self._http.get(
            f"/messenger/v1/accounts/{self.user_id}/chats",
            params={"limit": limit, "offset": offset},
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        return r.json()

    def get_messages(self, chat_id: str, limit: int = 100, offset: int = 0) -> Dict[str, Any]:
        r = self._http.get(
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages/",
            params={"limit": limit, "offset": offset},
            headers=self._auth_headers(),
        )
        r.raise_for_status()
        return r.json()

    def send_text(self, chat_id: str, text: str) -> Dict[str, Any]:
        r = self._http.post(
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages",
            json={"type": "text", "message": {"text": text}},
            headers={**self._auth_headers(), "Content-Type": "application/json"},
        )
        r.raise_for_status()
        return r.json()
