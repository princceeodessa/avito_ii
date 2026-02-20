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

    # core/pricing.py

    def calculate(self, city: str, area_m2: Optional[float], extras: List[str]) -> Estimate:
        if not area_m2:
            return Estimate(None, None, details="Площадь не указана")

        cities = self.rules.get("cities", {})
        city_obj = cities.get(city) or cities.get("default") or {}

        base_price = float(city_obj.get("base_price_per_sqm", 0))
        if base_price <= 0:
            base_price = 600.0  # fallback

        base_total = base_price * area_m2

        # ✅ 1) допы из города (как в твоём pricing_rules.json)
        city_extras = city_obj.get("extras", {}) or {}

        # ✅ 2) допы из верхнего уровня (как в текущем PricingEngine) — на будущее
        global_extras = self.rules.get("extras", {}) or {}

        extras_total = 0.0
        breakdown = [f"База: {base_price:.0f} ₽/м² × {area_m2:g} м²"]

        for e in extras:
            rule = None
            if e in city_extras:
                rule = city_extras[e]
            elif e in global_extras:
                rule = global_extras[e]

            if rule is None:
                continue

            # формат 1: число = фикс сумма
            if isinstance(rule, (int, float)):
                extras_total += float(rule)
                breakdown.append(f"{e}: {float(rule):.0f} ₽ фикс.")
                continue

            # формат 2: dict = {type,value}
            if isinstance(rule, dict):
                rtype = rule.get("type")
                val = float(rule.get("value", 0))
                if rtype == "per_sqm":
                    add = val * area_m2
                    extras_total += add
                    breakdown.append(f"{e}: {val:.0f} ₽/м² × {area_m2:g} м²")
                elif rtype == "fixed":
                    extras_total += val
                    breakdown.append(f"{e}: {val:.0f} ₽ фикс.")
                continue

        total = base_total + extras_total
        min_price = int(total * 0.85)
        max_price = None
        details = " | ".join(breakdown)

        return Estimate(min_price, max_price, details=details)
