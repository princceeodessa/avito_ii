import re
from dataclasses import dataclass
from typing import List, Optional

@dataclass
class ExtractedInfo:
    area_m2: Optional[float]
    extras: List[str]

# core/extractor.py

EXTRA_ALIASES = {
    "светильник": ["светильник", "светильники", "точечн", "споты", "ламп", "свет"],
    "люстра": ["люстра", "люстры"],
    "труба": ["труба", "трубы"],
    "карниз": ["карниз", "карнизы"],
    "парящий профиль": ["парящ", "парящий"],
    "скрытый карниз": ["скрыт", "скрытый карниз"],
    "ниша": ["ниша"],
    "двухуровневый": ["двухуров", "2 уров", "два уровня"],
    "фотопечать": ["фотопеч", "печать"],
}


def extract_area_m2(text: str) -> Optional[float]:
    t = text.lower().replace(",", ".")
    # "20 м2", "20м2", "20 кв м", "20 квадратов"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:м2|м\^2|кв\.?\s*м|квадрат)", t)
    if m:
        try:
            val = float(m.group(1))
            if 3 <= val <= 300:  # разумный фильтр
                return val
        except:
            return None
    return None

def extract_extras(text: str) -> List[str]:
    t = text.lower()
    extras = []
    for extra_name, keys in EXTRA_ALIASES.items():
        for k in keys:
            if k in t:
                extras.append(extra_name)
                break
    # уникализируем сохранив порядок
    seen = set()
    out = []
    for e in extras:
        if e not in seen:
            seen.add(e)
            out.append(e)
    return out

def extract_info(text: str) -> ExtractedInfo:
    return ExtractedInfo(
        area_m2=extract_area_m2(text),
        extras=extract_extras(text),
    )
