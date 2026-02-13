import json
from pathlib import Path
from typing import Dict, Any, Optional

class PromotionManager:
    def __init__(self, promotions_file: str):
        self.path = Path(promotions_file)
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"default": ""}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def get_promo(self, city: str) -> str:
        # поддержка: {"default": "...", "Moscow":"..."} или {"cities":{...}}
        if "cities" in self.data:
            return self.data["cities"].get(city) or self.data["cities"].get("default") or ""
        return self.data.get(city) or self.data.get("default") or ""
