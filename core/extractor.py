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
    """Пробует извлечь площадь из текста.

    Поддерживает варианты:
    - 20 м2 / 20м2 / 20 м^2 / 20 м²
    - 20 кв м / 20 кв.м / 20кв.м / 20 кв
    - 20 квадратов / 20 квадратных метров
    - диапазоны: 20-25 кв (берём верхнюю границу, чтобы не занижать ориентир)
    """
    t = (text or "").lower().replace(",", ".")

    unit = r"(?:м2|м\^2|м²|кв\.?\s*м|кв\.?\s*метр(?:а|ов)?|квадратн\w*\s*метр(?:а|ов)?|квадрат(?:а|ов)?|кв\.?\b|квм\b)"

    # 1) диапазон 20-25 кв.м
    m = re.search(rf"(\d{{1,3}}(?:\.\d+)?)\s*[\-–—]\s*(\d{{1,3}}(?:\.\d+)?)\s*{unit}", t)
    if m:
        try:
            a = float(m.group(1))
            b = float(m.group(2))
            val = max(a, b)
            if 3 <= val <= 300:
                return val
        except Exception:
            return None

    # 2) одиночное значение 20кв / 20 кв.м / 20 м²
    m = re.search(rf"(\d{{1,3}}(?:\.\d+)?)\s*{unit}", t)
    if m:
        try:
            val = float(m.group(1))
            if 3 <= val <= 300:  # разумный фильтр
                return val
        except Exception:
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
