import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, List, Optional

@dataclass
class Estimate:
    min_price: Optional[int]
    max_price: Optional[int]
    currency: str = "RUB"
    details: str = ""

class PricingEngine:
    def __init__(self, pricing_file: str):
        self.path = Path(pricing_file)
        self.rules: Dict[str, Any] = self._load()

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            raise FileNotFoundError(f"pricing file not found: {self.path}")
        return json.loads(self.path.read_text(encoding="utf-8"))

    def calculate(self, city: str, area_m2: Optional[float], extras: List[str]) -> Estimate:
        if not area_m2:
            return Estimate(None, None, details="Площадь не указана")

        cities = self.rules.get("cities", {})
        extras_rules = self.rules.get("extras", {})

        city_obj = cities.get(city) or cities.get("default") or {}
        base_price = float(city_obj.get("base_price_per_sqm", 0))

        if base_price <= 0:
            # fallback
            base_price = 600.0

        base_total = base_price * area_m2

        extras_total = 0.0
        breakdown = [f"База: {base_price:.0f} ₽/м² × {area_m2:g} м²"]

        for e in extras:
            rule = extras_rules.get(e)
            if not rule:
                continue

            rtype = rule.get("type")
            val = float(rule.get("value", 0))

            if rtype == "per_sqm":
                add = val * area_m2
                extras_total += add
                breakdown.append(f"{e}: {val:.0f} ₽/м² × {area_m2:g} м²")
            elif rtype == "fixed":
                extras_total += val
                breakdown.append(f"{e}: {val:.0f} ₽ фикс.")
            else:
                # неизвестный тип — игнорим
                continue

        total = base_total + extras_total

        # Диапазон, чтобы не давать “точную цену”
        min_price = int(total * 0.85)
        max_price = int(total * 1.15)

        details = " | ".join(breakdown)
        return Estimate(min_price, max_price, details=details)
