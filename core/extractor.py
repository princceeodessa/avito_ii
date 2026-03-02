import re
from dataclasses import dataclass
from typing import Dict, List, Optional

@dataclass
class ExtractedInfo:
    area_m2: Optional[float]
    extras: List[str]
    extras_counts: Dict[str, int]

# core/extractor.py

EXTRA_ALIASES = {
    # Важно: НЕ используем слишком общий триггер "свет" (ловит "подсветка" и даёт ложные допы).
    "светильник": ["светильник", "светильники", "точечн", "споты", "gx53", "mr16", "трек"],
    "люстра": ["люстра", "люстры"],
    "труба": ["труба", "трубы"],
    "карниз": ["карниз", "карнизы"],
    "парящий профиль": ["парящ", "парящий"],
    "скрытый карниз": ["скрыт", "скрытый карниз"],
    "ниша": ["ниша"],
    "двухуровневый": ["двухуров", "2 уров", "два уровня"],
    "фотопечать": ["фотопеч", "печать"],
    # отдельным тегом: подсветка (сама по себе не равна "светильники")
    "подсветка": ["подсвет"],
}


_NUM_WORDS = {
    "ноль": 0,
    "один": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
}


def extract_extras_counts(text: str) -> Dict[str, int]:
    """Пробует извлечь количества по ключевым допам.

    Возвращает counts по тем позициям, где количество указано явно:
    - 3 люстры / одна люстра
    - 7 светильников
    - карниз 3 м / 3 метра карниза

    Примечание: углы/профиль мы храним отдельно (в app_state), т.к. в pricing_rules их может не быть.
    """
    t = (text or "").lower().replace("ё", "е")
    counts: Dict[str, int] = {}

    def _add(name: str, n: int) -> None:
        if n <= 0:
            return
        counts[name] = int(counts.get(name, 0) + int(n))

    # 1) digits: "3 люстры", "7 светильников"
    for n, _ in re.findall(r"\b(\d{1,3})\s*(люстр\w*)\b", t):
        _add("люстра", int(n))

    for n, _ in re.findall(r"\b(\d{1,3})\s*(светильник\w*|точк\w*\s*светильник\w*|спот\w*)\b", t):
        _add("светильник", int(n))

    # 2) word-numbers: "одна люстра", "две люстры"
    words_re = "|".join(map(re.escape, _NUM_WORDS.keys()))
    for w, _ in re.findall(rf"\b({words_re})\s*(люстр\w*)\b", t):
        _add("люстра", _NUM_WORDS.get(w, 0))

    # 3) карниз: если указана длина в метрах — считаем как количество метров (округляем вверх)
    m = re.search(r"карниз[^\n\r]{0,60}?(\d{1,3}(?:[\.,]\d+)?)\s*(?:м\b|метр\w*)", t)
    if m:
        try:
            val = float(m.group(1).replace(",", "."))
            n = int(val) if abs(val - int(val)) < 1e-9 else int(val) + 1
            _add("карниз", n)
        except Exception:
            pass
    else:
        # если карниз упомянут, но длины нет — считаем 1
        if "карниз" in t:
            _add("карниз", 1)

    return counts


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
        extras_counts=extract_extras_counts(text),
    )
