import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional


@dataclass
class Estimate:
    min_price: Optional[int]
    max_price: Optional[int]
    currency: str = "RUB"
    details: str = ""
    base_total: Optional[float] = None
    min_floor_applied: bool = False


class PricingEngine:
    def __init__(self, pricing_file: str):
        self.path = Path(pricing_file)
        self.rules: Dict[str, Any] = self._load()

        # Минимальная "от"-стоимость для потолков (по требованиям бизнеса)
        # Можно переопределить в .env: CEILING_MIN_ESTIMATE=8000
        try:
            self.ceiling_min_estimate = int(os.getenv("CEILING_MIN_ESTIMATE", "8000") or "8000")
        except Exception:
            self.ceiling_min_estimate = 8000

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"pricing file not found: {self.path}")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def calculate(self, city: str, area_m2: Optional[float], extras: List[str]) -> Estimate:
        """Примерный расчёт.

        ВАЖНО: по требованиям бизнеса стоимость доп.работ НЕ добавляем в примерный расчёт.
        Допы (люстры/светильники/карниз/углы/профиль и т.д.) уточняются на замере.
        Поэтому ориентир строим только от площади.

        extras параметр оставлен для совместимости (и для вывода в details в будущем), но НЕ влияет на цену.
        """
        if not area_m2:
            return Estimate(None, None, details="Площадь не указана")

        cities = self.rules.get("cities", {})
        city_obj = cities.get(city) or cities.get("default") or {}

        base_price = float(city_obj.get("base_price_per_sqm", 0))
        if base_price <= 0:
            base_price = 600.0  # fallback

        base_total = base_price * float(area_m2)

        raw_min = int(base_total * 0.85)
        min_floor = int(self.ceiling_min_estimate or 8000)
        min_floor_applied = raw_min < min_floor
        min_price = max(raw_min, min_floor)

        details = f"База: {base_price:.0f} ₽/м² × {float(area_m2):g} м² (допы уточняются на замере)"

        return Estimate(
            min_price=min_price,
            max_price=None,
            details=details,
            base_total=base_total,
            min_floor_applied=min_floor_applied,
        )
