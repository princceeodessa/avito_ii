# core/response.py
import os
from typing import Dict, List, Optional, Tuple, Union

import requests
from requests import exceptions as req_exc


TimeoutT = Union[int, float, Tuple[float, float]]  # (connect, read)


class OllamaClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "qwen2.5:3b",
        timeout: int = 120,
        request_timeout: Optional[int] = None,  # совместимость со старым именем, если было
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model

        # если кто-то передал request_timeout — используем его
        if request_timeout is not None:
            timeout = request_timeout

        # Можно задать раздельные таймауты через env:
        # OLLAMA_CONNECT_TIMEOUT=5
        # OLLAMA_READ_TIMEOUT=600
        ct = os.getenv("OLLAMA_CONNECT_TIMEOUT")
        rt = os.getenv("OLLAMA_READ_TIMEOUT")
        if ct and rt:
            try:
                self.timeout: TimeoutT = (float(ct), float(rt))
            except Exception:
                self.timeout = int(timeout)
        else:
            self.timeout = int(timeout)

        # очень аккуратный ретрай на read-timeout (часто модель отвечает, но чуть дольше)
        try:
            self.retries = int(os.getenv("OLLAMA_RETRIES", "1") or "1")
        except Exception:
            self.retries = 1

    def chat(self, messages: List[Dict[str, str]]) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {"model": self.model, "messages": messages, "stream": False}

        last_err: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                return data["message"]["content"]
            except (req_exc.ReadTimeout, req_exc.ConnectTimeout) as e:
                last_err = e
                continue

        # если дошли сюда — все попытки исчерпаны
        if last_err:
            raise last_err
        raise RuntimeError("Ollama request failed")
