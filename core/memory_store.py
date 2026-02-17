import json
import os
from typing import Any, Dict

class FileKVStore:
    def __init__(self, dir_path: str = "data/memory"):
        self.dir_path = dir_path
        os.makedirs(self.dir_path, exist_ok=True)

    def _path(self, key: str) -> str:
        safe = "".join(ch for ch in key if ch.isalnum() or ch in ("_", "-", ":"))
        return os.path.join(self.dir_path, f"{safe}.json")

    def load(self, key: str) -> Dict[str, Any]:
        p = self._path(key)
        if not os.path.exists(p):
            return {}
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def save(self, key: str, data: Dict[str, Any]) -> None:
        p = self._path(key)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def reset(self, key: str) -> None:
        self.save(key, {})
