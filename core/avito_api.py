# core/avito_api.py
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List, Union

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

    @property
    def is_valid(self) -> bool:
        # небольшой запас, чтобы не ловить истечение “впритык”
        return bool(self.access_token) and time.time() < (self.expires_at - 30)


class AvitoTokenStore:
    def __init__(self, path: str):
        self.path = Path(path)

    def load(self) -> Optional[AvitoToken]:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return AvitoToken(
                access_token=data.get("access_token", ""),
                token_type=data.get("token_type", "Bearer"),
                expires_at=float(data.get("expires_at", 0.0)),
            )
        except Exception:
            return None

    def save(self, token: AvitoToken) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "access_token": token.access_token,
                    "token_type": token.token_type,
                    "expires_at": token.expires_at,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


class AvitoAPI:
    """
    Минимальный клиент Авито:
    - Получение токена client_credentials
    - /core/v1/accounts/self (чтобы узнать правильный account_id)
    - Получение списка чатов (пробуем v2 и v1)
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        token_path: str = "data/avito_tokens.json",
        base_url: str = "https://api.avito.ru",
        timeout: float = 30.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

        self.tokens = AvitoTokenStore(token_path)
        self._token: Optional[AvitoToken] = self.tokens.load()

        self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout)

    # ---------- token ----------

    def ensure_token(self) -> AvitoToken:
        if self._token and self._token.is_valid:
            return self._token

        # получаем новый токен (client_credentials)
        r = self._client.post(
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
                f"Не удалось получить токен: {r.status_code}",
                status_code=r.status_code,
                details=r.text[:1500],
                request_id=r.headers.get("x-request-id", ""),
            )

        data = r.json()
        access_token = data.get("access_token", "")
        token_type = data.get("token_type", "Bearer")
        expires_in = int(data.get("expires_in", 86400))

        tok = AvitoToken(
            access_token=access_token,
            token_type=token_type,
            expires_at=time.time() + expires_in,
        )
        self._token = tok
        self.tokens.save(tok)
        return tok

    def _auth_headers(self) -> Dict[str, str]:
        tok = self.ensure_token()
        return {
            "Authorization": f"{tok.token_type} {tok.access_token}",
            "Accept": "application/json",
        }

    # ---------- low-level request ----------

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        allow_statuses: Tuple[int, ...] = (),
    ) -> Tuple[int, Dict[str, Any] | List[Any] | str, httpx.Response]:
        r = self._client.request(
            method,
            path,
            headers=self._auth_headers(),
            params=params,
            json=json_body,
        )

        # если токен внезапно протух — обновим и повторим 1 раз
        if r.status_code in (401, 403):
            self._token = None
            self.ensure_token()
            r = self._client.request(
                method,
                path,
                headers=self._auth_headers(),
                params=params,
                json=json_body,
            )

        if allow_statuses and r.status_code in allow_statuses:
            # вернем как есть (для fallback-логики)
            payload: Any
            try:
                payload = r.json()
            except Exception:
                payload = r.text
            return r.status_code, payload, r

        if r.status_code >= 400:
            raise AvitoAPIError(
                f"Ошибка Avito API: {method} {path} -> {r.status_code}",
                status_code=r.status_code,
                details=r.text[:2000],
                request_id=r.headers.get("x-request-id", ""),
            )

        try:
            payload = r.json()
        except Exception:
            payload = r.text
        return r.status_code, payload, r

    # ---------- account/self ----------

    def get_self(self) -> Optional[Dict[str, Any]]:
        """
        Пытаемся получить инфу о текущем аккаунте.
        Если эндпоинт недоступен/нет прав — вернем None.
        """
        for p in ("/core/v1/accounts/self", "/core/v1/accounts/self/"):
            try:
                code, data, _ = self._request_json("GET", p, allow_statuses=(404, 403))
                if code in (404, 403):
                    continue
                if isinstance(data, dict):
                    return data
            except AvitoAPIError:
                continue
        return None

    def resolve_account_id(self, fallback_env_user_id: Optional[str] = None) -> Optional[int]:
        info = self.get_self()
        if isinstance(info, dict):
            # разные варианты ключей
            for key in ("id", "account_id", "user_id"):
                if key in info and str(info[key]).isdigit():
                    return int(info[key])

        if fallback_env_user_id and str(fallback_env_user_id).isdigit():
            return int(fallback_env_user_id)

        return None

    # ---------- messenger/chats ----------

    def get_chats_any(self, account_id: int, *, limit: int = 50, offset: int = 0) -> Dict[str, Any] | List[Any]:
        """
        Пробуем:
        1) v2 без параметров (часто именно так работает)
        2) v2 с параметрами (если поддерживается)
        3) v1 с limit/offset
        """
        # v2 без params
        v2_paths = [
            f"/messenger/v2/accounts/{account_id}/chats",
            f"/messenger/v2/accounts/{account_id}/chats/",
        ]
        for p in v2_paths:
            code, data, _ = self._request_json("GET", p, allow_statuses=(400, 404, 403))
            if code < 400:
                return data

        # v2 с params (на случай если поддерживается)
        for p in v2_paths:
            code, data, _ = self._request_json(
                "GET", p, params={"limit": max(1, min(limit, 99)), "offset": max(0, offset)}, allow_statuses=(400, 404, 403)
            )
            if code < 400:
                return data

        # v1 с params
        v1_paths = [
            f"/messenger/v1/accounts/{account_id}/chats",
            f"/messenger/v1/accounts/{account_id}/chats/",
        ]
        for p in v1_paths:
            code, data, _ = self._request_json(
                "GET", p, params={"limit": max(1, min(limit, 99)), "offset": max(0, offset)}, allow_statuses=(400, 404, 403)
            )
            if code < 400:
                return data

        # если дошли сюда — значит все варианты дали 400/403/404
        raise AvitoAPIError(
            "Не удалось получить список чатов: все варианты эндпоинтов вернули 400/403/404. "
            "Проверь account_id и доступ к messenger scope в Авито.",
            status_code=400,
        )

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass
