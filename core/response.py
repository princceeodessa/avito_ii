# core/response.py
import os
from typing import Dict, List, Optional, Tuple, Union

import requests
from requests import exceptions as req_exc


TimeoutT = Union[int, float, Tuple[float, float]]  # (connect, read)


class LLMTimeoutError(TimeoutError):
    """Raised when LLM (Ollama) didn't respond within timeout."""


class OllamaClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "qwen2.5:3b",
        timeout: int = 60,
        request_timeout: Optional[int] = None,  # backward compat
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model

        if request_timeout is not None:
            timeout = request_timeout

        # Defaults tuned for "chat not waiting forever"
        default_ct = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "5"))
        default_rt = float(os.getenv("OLLAMA_READ_TIMEOUT", "60"))

        # If user provided both explicitly, use them; otherwise use defaults above
        ct = os.getenv("OLLAMA_CONNECT_TIMEOUT")
        rt = os.getenv("OLLAMA_READ_TIMEOUT")
        if ct and rt:
            try:
                self.timeout: TimeoutT = (float(ct), float(rt))
            except Exception:
                self.timeout = (default_ct, default_rt)
        else:
            # even if legacy `timeout` passed, keep it as READ timeout cap
            self.timeout = (default_ct, float(timeout))

        # retries=0 by default to avoid doubling wait time
        try:
            self.retries = int(os.getenv("OLLAMA_RETRIES", "0") or "0")
        except Exception:
            self.retries = 0

    def chat(self, messages: List[Dict[str, str]]) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {"model": self.model, "messages": messages, "stream": False}

        last_err: Optional[Exception] = None
        for _attempt in range(self.retries + 1):
            try:
                r = requests.post(url, json=payload, timeout=self.timeout)
                r.raise_for_status()
                data = r.json()
                return data["message"]["content"]
            except (req_exc.ReadTimeout, req_exc.ConnectTimeout) as e:
                last_err = e
                continue
            except req_exc.RequestException as e:
                # network/http errors: don't loop forever either
                last_err = e
                break

        # make timeout explicit so higher-level code can respond nicely
        if isinstance(last_err, (req_exc.ReadTimeout, req_exc.ConnectTimeout)):
            raise LLMTimeoutError(str(last_err))

        if last_err:
            raise last_err
        raise RuntimeError("Ollama request failed")