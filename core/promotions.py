# core/promotions.py
import json
from pathlib import Path
from typing import Any, Dict


class PromotionManager:
    def __init__(self, promotions_file: str):
        self.path = Path(promotions_file)
        self.data: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"default": ""}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def get_promo(self, city: str) -> str:
        """
        Поддержка 2-х форматов:

        1) Новый (как у тебя сейчас в data/promotions.json):
           {"active": true, "text": "...", "channels": {...}}

        2) Старый:
           {"default": "...", "cities": {"Москва":"...", "default":"..."}}
           или плоский {"Москва":"...", "default":"..."}
        """
        # новый формат
        if "text" in self.data:
            if self.data.get("active", True) is False:
                return ""
            return str(self.data.get("text") or "")

        # старый формат
        if "cities" in self.data and isinstance(self.data["cities"], dict):
            return self.data["cities"].get(city) or self.data["cities"].get("default") or ""

        return self.data.get(city) or self.data.get("default") or ""