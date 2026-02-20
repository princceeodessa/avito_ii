# core/response.py
import os
from typing import Dict, List, Optional

import requests


class OllamaClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        model: str = "llama3",
        timeout: int = 120,
        request_timeout: Optional[int] = None,  # совместимость со старым именем, если было
    ):
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://localhost:11434")).rstrip("/")
        self.model = model

        # если кто-то передал request_timeout — используем его
        if request_timeout is not None:
            timeout = request_timeout
        self.timeout = int(timeout)

    def chat(self, messages: List[Dict[str, str]]) -> str:
        url = f"{self.base_url}/api/chat"
        payload = {"model": self.model, "messages": messages, "stream": False}
        r = requests.post(url, json=payload, timeout=self.timeout)
        r.raise_for_status()
        data = r.json()
        return data["message"]["content"]