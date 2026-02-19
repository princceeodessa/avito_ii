# core/avito_api.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx


class AvitoAPIError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None, details: str = "", request_id: str = ""):
        super().__init__(message)
        self.status_code = status_code
        self.details = details
        self.request_id = request_id


@dataclass
class AvitoToken:
    access_token: str
    token_type: str = "Bearer"
    expires_at: float = 0.0  # unix ts

    def is_valid(self, skew: int = 30) -> bool:
        return bool(self.access_token) and (time.time() + skew) < float(self.expires_at or 0.0)


class AvitoAPI:
    """
    Client-credentials + Messenger API helper.

    ВАЖНО:
    - polling работает без https
    - webhook требует публичный https (мы сейчас не используем)
    """

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        user_id: int,
        token_path: str = "data/avito_tokens.json",
        base_url: str = "https://api.avito.ru",
        timeout: float = 30.0,
    ) -> None:
        self.client_id = client_id.strip()
        self.client_secret = client_secret.strip()
        self.user_id = int(user_id)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.token_path = Path(token_path)
        self.token_path.parent.mkdir(parents=True, exist_ok=True)

        self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout)
        self._token: Optional[AvitoToken] = self._load_token()

    # -------- token --------

    def _load_token(self) -> Optional[AvitoToken]:
        if not self.token_path.exists():
            return None
        try:
            d = json.loads(self.token_path.read_text(encoding="utf-8"))
            if not isinstance(d, dict) or not d.get("access_token"):
                return None
            return AvitoToken(
                access_token=str(d.get("access_token", "")),
                token_type=str(d.get("token_type", "Bearer")),
                expires_at=float(d.get("expires_at", 0.0)),
            )
        except Exception:
            return None

    def _save_token(self, t: AvitoToken) -> None:
        self._token = t
        self.token_path.write_text(
            json.dumps(
                {"access_token": t.access_token, "token_type": t.token_type, "expires_at": t.expires_at},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def refresh_token(self) -> AvitoToken:
        r = self._http.post(
            "/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            data={
                "grant_type": "client_credentials",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            },
        )
        if r.status_code >= 400:
            raise AvitoAPIError(
                f"Token error: {r.status_code}",
                status_code=r.status_code,
                details=r.text[:2000],
                request_id=r.headers.get("x-request-id", ""),
            )
        j = r.json()
        expires_in = int(j.get("expires_in", 86400))
        t = AvitoToken(
            access_token=j["access_token"],
            token_type=j.get("token_type", "Bearer"),
            expires_at=time.time() + expires_in,
        )
        self._save_token(t)
        return t

    def ensure_token(self, refresh_if_less_than_sec: int = 3600) -> None:
        if self._token and self._token.is_valid(skew=30):
            if (self._token.expires_at - time.time()) > refresh_if_less_than_sec:
                return
        self.refresh_token()

    def _auth_headers(self) -> Dict[str, str]:
        self.ensure_token()
        assert self._token
        return {
            "Authorization": f"{self._token.token_type} {self._token.access_token}",
            "Accept": "application/json",
        }

    # -------- request helpers --------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        allow_statuses: Tuple[int, ...] = (),
    ) -> Tuple[int, Any, httpx.Response]:
        r = self._http.request(method, path, headers=self._auth_headers(), params=params, json=json_body)

        # если токен протух — обновим и повторим 1 раз
        if r.status_code in (401, 403):
            self._token = None
            self.ensure_token()
            r = self._http.request(method, path, headers=self._auth_headers(), params=params, json=json_body)

        if allow_statuses and r.status_code in allow_statuses:
            try:
                return r.status_code, r.json(), r
            except Exception:
                return r.status_code, r.text, r

        if r.status_code >= 400:
            raise AvitoAPIError(
                f"{method} {path} -> {r.status_code}",
                status_code=r.status_code,
                details=r.text[:2000],
                request_id=r.headers.get("x-request-id", ""),
            )

        try:
            return r.status_code, r.json(), r
        except Exception:
            return r.status_code, r.text, r

    def _pick_list(self, payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, list):
            return [x for x in payload if isinstance(x, dict)]
        if isinstance(payload, dict):
            for k in ("chats", "items", "data", "result", "messages"):
                v = payload.get(k)
                if isinstance(v, list):
                    return [x for x in v if isinstance(x, dict)]
        return []

    # -------- messenger --------

    def get_chat(self, chat_id: str) -> Dict[str, Any]:
        # чаще всего работает v1
        _, data, _ = self._request_json("GET", f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}")
        if isinstance(data, dict):
            return data
        return {}

    def list_chats(self, limit: int = 100, offset: int = 0) -> List[Dict[str, Any]]:
        """
        У Авито бывает:
        - v2 /chats без limit/offset (иначе 400)
        - v1 /chats с limit/offset
        Поэтому пробуем аккуратно.
        """
        # 1) v2 без params
        for p in (f"/messenger/v2/accounts/{self.user_id}/chats", f"/messenger/v2/accounts/{self.user_id}/chats/"):
            code, data, _ = self._request_json("GET", p, allow_statuses=(400, 404, 403))
            if code < 400:
                return self._pick_list(data)

        # 2) v1 с пагинацией
        for p in (f"/messenger/v1/accounts/{self.user_id}/chats", f"/messenger/v1/accounts/{self.user_id}/chats/"):
            code, data, _ = self._request_json(
                "GET",
                p,
                params={"limit": int(limit), "offset": int(offset)},
                allow_statuses=(400, 404, 403),
            )
            if code < 400:
                return self._pick_list(data)

        # 3) v2 с params (на всякий)
        for p in (f"/messenger/v2/accounts/{self.user_id}/chats", f"/messenger/v2/accounts/{self.user_id}/chats/"):
            code, data, _ = self._request_json(
                "GET",
                p,
                params={"limit": int(limit), "offset": int(offset)},
                allow_statuses=(400, 404, 403),
            )
            if code < 400:
                return self._pick_list(data)

        return []

    def list_messages(self, chat_id: str, limit: int = 30, offset: int = 0) -> List[Dict[str, Any]]:
        # v2 обычно стабильнее для сообщений
        paths = [
            f"/messenger/v2/accounts/{self.user_id}/chats/{chat_id}/messages/",
            f"/messenger/v2/accounts/{self.user_id}/chats/{chat_id}/messages",
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages/",
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages",
        ]
        for p in paths:
            code, data, _ = self._request_json(
                "GET",
                p,
                params={"limit": int(limit), "offset": int(offset)},
                allow_statuses=(400, 404, 403),
            )
            if code < 400:
                return self._pick_list(data)
        return []

    def mark_read(self, chat_id: str) -> None:
        for p in (
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/read",
            f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/read/",
        ):
            code, _, _ = self._request_json("POST", p, allow_statuses=(400, 404, 403))
            if code < 400:
                return

    def send_text(self, chat_id: str, text: str) -> None:
        path = f"/messenger/v1/accounts/{self.user_id}/chats/{chat_id}/messages"
        variants = [
            {"type": "text", "message": {"text": text}},
            {"type": "text", "content": {"text": text}},
            {"text": text},
        ]
        last: Optional[Tuple[int, str]] = None
        for payload in variants:
            r = self._http.post(path, headers=self._auth_headers(), json=payload)
            if r.status_code < 400:
                return
            last = (r.status_code, r.text)

        code, body = last or (400, "unknown")
        raise AvitoAPIError(f"send_text failed: {code}", status_code=code, details=body[:2000])

    def close(self) -> None:
        try:
            self._http.close()
        except Exception:
            pass
