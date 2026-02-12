import json
import re
from typing import Dict, Optional


class PricingEngine:

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.rules = json.load(f)

    def extract_data(self, text: str) -> Dict:
        text = text.lower()

        area_match = re.search(r"(\d+)\s?(кв|м|квм|метр)", text)
        area = int(area_match.group(1)) if area_match else None

        extras = {
            "light": text.count("светильник"),
            "chandelier": text.count("люстр"),
            "pipe": text.count("труб"),
            "cornice": text.count("карниз"),
        }

        return {"area": area, "extras": extras}

    def calculate(self, city: str, area: int, extras: Dict) -> Optional[int]:
        if city not in self.rules:
            return None

        city_rules = self.rules[city]
        total = area * city_rules["price_per_m2"]

        total += extras["light"] * city_rules["light_price"]
        total += extras["chandelier"] * city_rules["chandelier_price"]
        total += extras["pipe"] * city_rules["pipe_price"]
        total += extras["cornice"] * city_rules["cornice_price"]

        return total
