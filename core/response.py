"""core/response.py

Centralized Ollama client wrapper.

Goals:
- Keep latency reasonable (avoid 4–6 minute waits).
- Expose explicit timeout errors so upper layers can reply nicely (fallback)
  instead of showing a scary "service busy" message.

Env knobs:
- OLLAMA_URL
- OLLAMA_MODEL
- OLLAMA_CONNECT_TIMEOUT (seconds)
- OLLAMA_READ_TIMEOUT (seconds)
- OLLAMA_RETRIES (default 0)

Legacy compatibility:
- `timeout` and `request_timeout` are treated as READ timeout caps.
"""

import os
from typing import Dict, List, Optional, Tuple, Union

import requests
from requests import exceptions as req_exc


TimeoutT = Union[int, float, Tuple[float, float]]  # (connect, read)


class LLMTimeoutError(TimeoutError):
    """Raised when the LLM didn't respond within timeout."""


class OllamaClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "qwen2.5:3b",
        timeout: int = 60,
        request_timeout: Optional[int] = None,
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model

        # Backward compat: request_timeout overrides timeout
        if request_timeout is not None:
            timeout = request_timeout

        # Defaults tuned for chat UX.
        default_ct = float(os.getenv("OLLAMA_CONNECT_TIMEOUT", "5"))
        default_rt = float(os.getenv("OLLAMA_READ_TIMEOUT", "60"))

        ct = os.getenv("OLLAMA_CONNECT_TIMEOUT")
        rt = os.getenv("OLLAMA_READ_TIMEOUT")
        if ct and rt:
            try:
                self.timeout: TimeoutT = (float(ct), float(rt))
            except Exception:
                self.timeout = (default_ct, default_rt)
        else:
            # Legacy: treat `timeout` as a READ timeout cap
            self.timeout = (default_ct, float(timeout))

        # retries=0 by default to avoid doubling waiting time
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
                # Other HTTP/network errors: no endless retry loop
                last_err = e
                break

        if isinstance(last_err, (req_exc.ReadTimeout, req_exc.ConnectTimeout)):
            raise LLMTimeoutError(str(last_err))
        if last_err:
            raise last_err
        raise RuntimeError("Ollama request failed")
