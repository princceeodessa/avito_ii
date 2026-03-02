# core/app_state.py
import asyncio
import datetime
import os
import re
import time
from pathlib import Path
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.extractor import extract_info
from core.history import ChatHistory
from core.intent import IntentDetector
from core.lead_store import LeadStoreTxt, LeadStoreJsonl
from core.memory_store import FileKVStore
from core.fewshot import FewShotManager
from core.pricing import PricingEngine
from core.promotions import PromotionManager
from core.response import OllamaClient, LLMTimeoutError

DEFAULT_SYSTEM_PROMPT = """Ты — Ульяна, менеджер по натяжным потолкам.
Общайся по-русски.

ЖЁСТКИЕ ПРАВИЛА:
- НЕ придумывай имена клиентов и не обращайся по имени, если клиент сам не представился.
- НЕ придумывай телефоны/контакты компании и НЕ пиши "позвоните по номеру".
- НЕ говори "мы ждём вас", "приходите". Только: "мастер приедет", "диспетчер подтвердит".
- НЕ говори "я приеду/я проведу замер". Ты оформляешь заявку.

Правила:
1) НЕ называй точную итоговую цену. Только ориентир: ‘от N ₽’ (без ‘до’).
2) Замер ВСЕГДА бесплатный. Замерщик приезжает с каталогами и примерами работ.
3) Для расчёта нужны город + площадь. Телефон для расчёта НЕ обязателен.
4) Коротко и вежливо: 3–7 предложений.
4.1) Пиши по-дружески и чуть теплее: 1–3 уместных смайла (😊✅✨) в сообщении — можно.
4.2) Если диалог подходит к завершению (клиент благодарит/прощается) — заверши логично: «Если будут вопросы — пишите 😊».
5) Если есть акция — можно упомянуть в первом ответе.
6) Для замера собери: город, адрес, дату, время, телефон.
7) Не здоровайся повторно, если диалог уже начался.

ВАЖНО:
- НЕ упоминай доплаты за выезд/расстояние/"за город" и не говори, что цена увеличится из-за расстояния.
- Если клиент спросил, выезжаем ли за пределы города: сначала уточни город/населённый пункт. Доп.стоимость за выезд НЕ озвучивай.
  Скажи, что замер бесплатный, а логистику/возможность выезда по точному адресу уточнит диспетчер.

ОТДЕЛЬНЫЙ ТОВАР/УСЛУГА (не называй это "доп.услугой"):
- «Шумоизоляция и звукоизоляция под ключ» — ориентир от 3375 ₽/м².
  Материал: акустический войлок 10 мм + тяжёлая вязкоэластичная мембрана 3 мм.
  Собственная звукоизоляция материала ~40 дБ, вместе с конструкцией натяжного потолка — до ~60 дБ.
  Толщина материала 13 мм, укладывается в стандартные 5–6 см опуска потолка, высоту почти не съедает.
"""


# ------------------- supported cities (как в твоём ТГ-поведении) -------------------
CITIES_IZH = [
    "Ижевск", "Воткинск", "Агрыз", "Завьялово", "Каменное", "Ува", "Глазов", "Сарапул",
    "Октябрьский", "Якшур", "Хохряки", "Локшудья", "Селычка", "Якшур-Бодья", "Постол",
    "Лудорвай", "Пирогово", "Вараксино", "Юськи", "Малая Пурга", "Ильинское", "Бабино",
    "Бураново", "Нечкино", "Новая Казмаска", "Шаркан", "Подшивалово", "Совхозный",
    "Большая Венья", "Старые Кены", "Старый Чультем", "Сизево", "Пычанки", "Чультем",
    "Мартьяново", "Первомайский", "Семеново", "Италмас", "Старое Михайловское",
    "Русский Вожой", "Ягул", "Солнечный", "Медведево", "Орловское", "Новые Ярушки",
    "Домоседово", "Починок",
]

CITIES_EKB = [
    "Екатеринбург", "Верхняя Пышма", "Шайдурово", "Горный щит", "Березовский",
    "Прохладный", "Логиново", "Хризолитовый",
]
SUPPORTED_CITIES = sorted(set(CITIES_IZH + CITIES_EKB), key=len, reverse=True)


# ------------------- city normalization/fuzzy -------------------
_CASE_ENDINGS = (
    "ыми", "ими", "ого", "ему", "ому", "ами", "ями", "ях", "ах", "ью", "ией",
    "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ым", "им", "ом", "ем", "ых", "их",
    "а", "я", "у", "ю", "е", "и", "о"
)

def _compress_repeats(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)

def _stem_ru_word(w: str) -> str:
    w = (w or "").lower().replace("ё", "е").replace("—", "-").replace("–", "-")
    w = re.sub(r"[^a-zа-я\-]+", "", w, flags=re.IGNORECASE)
    w = _compress_repeats(w)
    for suf in _CASE_ENDINGS:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w

def _norm_phrase(phrase: str) -> str:
    phrase = (phrase or "").replace("ё", "е").replace("—", "-").replace("–", "-")
    phrase = re.sub(r"\s+", " ", phrase).strip().replace("-", " ")
    words = [w for w in phrase.split() if w]
    words = [_stem_ru_word(w) for w in words]
    words = [w for w in words if w]
    return " ".join(words).strip()

NORM_CITIES: List[Tuple[str, str]] = [(c, _norm_phrase(c)) for c in SUPPORTED_CITIES]

# Short aliases users often type.
# Important: we normalize latin look-alikes too (EKB, etc.).
CITY_ALIASES: Dict[str, str] = {
    # Екатеринбург
    "екб": "Екатеринбург",
    "екат": "Екатеринбург",
    "ека": "Екатеринбург",
    "ekb": "Екатеринбург",
    # Ижевск
    "иж": "Ижевск",
    "izh": "Ижевск",
}

_LATIN_LOOKALIKES = str.maketrans({
    # common latin → cyrillic look-alikes
    "e": "е",
    "k": "к",
    "b": "в",
    "a": "а",
    "o": "о",
    "p": "р",
    "c": "с",
    "x": "х",
    "m": "м",
    "t": "т",
    "y": "у",
    "h": "н",
})


def _norm_city_token(s: str) -> str:
    s = (s or "").strip().lower().replace("ё", "е")
    s = s.translate(_LATIN_LOOKALIKES)
    s = re.sub(r"[^a-zа-я\-]+", "", s, flags=re.IGNORECASE)
    return s

def extract_city(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None

    # Alias hit (including inside phrases: "я из ижа", "в екб")
    if CITY_ALIASES:
        nt = _norm_city_token(t)
        if nt in CITY_ALIASES:
            return CITY_ALIASES[nt]
        for tok in re.findall(r"[A-Za-zА-Яа-яЁё\-]{2,}", t):
            nt = _norm_city_token(tok)
            if nt in CITY_ALIASES:
                return CITY_ALIASES[nt]

    # Token-level stem match ("из ижевска" -> token "ижевска" -> stem "ижевск")
    try:
        for tok in re.findall(r"[A-Za-zА-Яа-яЁё\-]{3,}", t):
            st = _stem_ru_word(tok)
            if not st:
                continue
            for city, ncity in NORM_CITIES:
                if st == ncity:
                    return city
    except Exception:
        pass

    # быстрый exact (с учётом регистра)
    for city in SUPPORTED_CITIES:
        if re.search(rf"\b{re.escape(city)}\b", t, flags=re.IGNORECASE):
            return city

    # fuzzy
    norm_text = _norm_phrase(t)
    if not norm_text:
        return None

    best_city = None
    best_score = 0.0
    for city, ncity in NORM_CITIES:
        if not ncity:
            continue
        score = SequenceMatcher(None, norm_text, ncity).ratio()
        # небольшой бонус, если city-строка как подстрока
        if ncity and ncity in norm_text:
            score += 0.08
        if score > best_score:
            best_score = score
            best_city = city

    if best_city and best_score >= 0.86:
        return best_city
    return None

# если человек явно написал "город Москва", а Москва не поддерживается —
# мы НЕ должны переспрашивать город, а должны сразу сказать "не работаем"
CITY_CANDIDATE_RE = re.compile(r"\b(?:город|г\.)\s*([A-Za-zА-Яа-яЁё\-\s]{3,40})", re.IGNORECASE)

def extract_city_candidate(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None

    # Нормализуем для проверок
    low = t.lower().replace("ё", "е").strip()
    low = re.sub(r"[^\w\s\-]+", "", low, flags=re.IGNORECASE)
    low = re.sub(r"\s+", " ", low).strip()

    # 1) Отсекаем приветствия/мусор (включая частые опечатки)
    if re.fullmatch(
        r"(привет|приветствую|здравствуйте|здравствуй|здраствуйте|здравсвуйте|здрастуйте|здрасьте|"
        r"добрый день|добрый вечер|доброе утро|спасибо|ок|окей|ага)",
        low,
    ):
        return None

    # если одно слово и явно похоже на приветствие (даже с опечаткой)
    if re.fullmatch(r"[a-zа-я\-]{3,30}", low) and re.match(r"^(здрав|здра|прив|доб|спас)", low):
        return None

    # 2) Явная форма: "город X" / "г. X"
    m = CITY_CANDIDATE_RE.search(t)
    if m:
        cand = m.group(1).strip(" ,.!?:;()[]{}\"'").strip()
        cand = re.sub(r"\s+", " ", cand)
        if 2 <= len(cand) <= 40:
            return cand

    # 3) Если сообщение — просто одно слово (часто так пишут город)
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]{3,30}", t):
        return t

    return None

# --- anti-duplicate helpers ---
QUESTION_MATERIALS_RE = re.compile(
    r"\b(материал(ы|ов)?|полотно|работа\s+под\s+ключ|под\s+ключ|с\s+работой|монтаж|установка|включает)\b",
    re.IGNORECASE,
)

THANKS_CHEAP_RE = re.compile(r"\b(спасибо|понял(а)?|ок)\b", re.IGNORECASE)

GOODBYE_RE = re.compile(r"\b(пока|до\s+свидания|всего\s+доброго|хорошего\s+дня|до\s+связи)\b", re.IGNORECASE)

def detect_materials_question(text: str) -> bool:
    return bool(QUESTION_MATERIALS_RE.search(text or ""))

def _estimate_signature(mem: Dict[str, Any], min_price: Optional[int]) -> str:
    city = str(mem.get("city") or "")
    area = str(mem.get("area_m2") or "")
    extras = ",".join(mem.get("extras") or []) if isinstance(mem.get("extras"), list) else str(mem.get("extras") or "")
    mp = str(min_price or "")
    return f"{city}|{area}|{extras}|{mp}"

def _is_duplicate_estimate(mem: Dict[str, Any], sig: str, ttl_sec: int = 900) -> bool:
    prev = mem.get("_last_estimate_sig")
    prev_ts = float(mem.get("_last_estimate_ts") or 0)
    if prev and prev == sig and (time.time() - prev_ts) < ttl_sec:
        return True
    return False

def _remember_estimate(mem: Dict[str, Any], sig: str) -> None:
    mem["_last_estimate_sig"] = sig
    mem["_last_estimate_ts"] = time.time()

def build_materials_vs_turnkey(first: bool) -> str:
    return (
        f"{t_hello(first)}Это ориентир *под ключ* ✅\n"
        "Обычно включает: полотно, профиль/крепёж и монтаж.\n"
        "Не входит (если нужно): сложные ниши/карнизы, доп.работы по электрике, большое количество светильников.\n"
        "Если скажете, сколько точек света и есть ли карниз/трубы — уточню ориентир."
    )
# ------------------- discounts/intent helpers -------------------
DISCOUNT_RE = re.compile(r"\b(скидк\w*|акци\w*|подар\w*|промокод\w*|купон\w*|бонус\w*)\b", re.IGNORECASE)
def detect_discount_mention(text: str) -> bool:
    return bool(DISCOUNT_RE.search(text or ""))

MEASURE_DECLINE_RE = re.compile(r"\b(без\s+замера|не\s+нужен\s+замер|не\s+надо\s+замер|не\s+выезжайте|не\s+приезжайте)\b", re.IGNORECASE)
CALC_ONLY_RE = re.compile(r"\b(просто\s+стоимость|только\s+стоимость|только\s+цен[ау]|просто\s+цен[ау])\b", re.IGNORECASE)
def detect_measurement_decline(text: str) -> bool:
    return bool(MEASURE_DECLINE_RE.search(text or ""))
def detect_calc_only(text: str) -> bool:
    return bool(CALC_ONLY_RE.search(text or ""))


# запрос живого человека / оператора
HANDOFF_RE = re.compile(
    r"(позов(и|ите)|соедини(те)?|нужен\s+оператор|оператор|менеджер|жив(ой|ого)\s+человек|человека\s+позови|позови\s+человека|позови\s+менеджера|позови\s+оператора|позови\s+ассистента|передай\s+человеку)",
    re.IGNORECASE,
)

def detect_handoff_request(text: str) -> bool:
    if not text:
        return False
    t = text.strip()
    # если человек пишет "не хочу замер, позови ассистента" — это точно handoff
    return bool(HANDOFF_RE.search(t))


AFFIRM_RE = re.compile(r"\b(да|ок|хорошо|давайте|согласен|согласна|подтверждаю|записывайте)\b", re.IGNORECASE)
NEG_RE = re.compile(r"\b(нет|не надо|не нужно|отмена|передумал|передумала)\b", re.IGNORECASE)
def detect_affirm(text: str) -> bool:
    low = (text or "").lower()
    return bool(AFFIRM_RE.search(low)) and not bool(re.search(r"\bне\b", low))
def detect_neg(text: str) -> bool:
    return bool(NEG_RE.search(text or ""))

def detect_price_question(text: str) -> bool:
    low = (text or "").lower()
    triggers = [
        "сколько стоит", "стоимость", "цена", "по чем", "почем",
        "просчитать", "рассчитать", "посчитать", "посчитайте",
        "примерно", "ориентир", "сколько выйдет", "предварительно",
    ]
    return any(t in low for t in triggers)

MEASURE_BOOK_RE = re.compile(r"\b(запиш|замер|приех|выех|когда\s+можете|когда\s+приедете)\b", re.IGNORECASE)
def detect_measurement_booking_intent(text: str) -> bool:
    if detect_measurement_decline(text):
        return False
    return bool(MEASURE_BOOK_RE.search(text or ""))

MEASURE_INFO_RE = re.compile(r"\b(как\s+проходит\s+замер|про\s+замер|бесплатн\w*\s+замер|сколько\s+стоит\s+замер)\b", re.IGNORECASE)
def detect_measurement_info_question(text: str) -> bool:
    if detect_measurement_decline(text):
        return False
    return bool(MEASURE_INFO_RE.search(text or ""))


# ------------------- out-of-city / logistics questions -------------------
OUT_OF_CITY_RE = re.compile(
    r"\b(выезж\w*|выезд\w*|приезж\w*|приед\w*|работа\w*|монтир\w*|устанавлив\w*)\b.*\b(за\s*город|за\s*предел\w*\s+город\w*|в\s+область|по\s+области|в\s+пригород|в\s+район|в\s+деревн\w+|в\s+пос[её]л\w+|в\s+другой\s+город)\b",
    re.IGNORECASE,
)

def detect_out_of_city_question(text: str) -> bool:
    return bool(OUT_OF_CITY_RE.search(text or ""))


# ------------------- soundproofing / noise insulation -------------------
SOUNDPROOF_RE = re.compile(r"\b(шумоизоляц\w*|звукоизоляц\w*|акустич\w*|войлок|мембран\w*)\b", re.IGNORECASE)

def detect_soundproofing_question(text: str) -> bool:
    return bool(SOUNDPROOF_RE.search(text or ""))

PHONE_RE = re.compile(r"(?:(?:\+7|8)\s*[\(\- ]?\d{3}[\)\- ]?\s*\d{3}[\- ]?\d{2}[\- ]?\d{2})")
PHONE_ANY_RE = re.compile(r"(?:\+7|8)\s*[\(\- ]?\d{3}[\)\- ]?\s*\d{3}[\- ]?\d{2}[\- ]?\d{2}")
PHONE_REFUSAL_RE = re.compile(r"\b(не\s+дам\s+телефон|без\s+телефон|телефон\s+не\s+дам|не\s+оставлю\s+телефон)\b", re.IGNORECASE)
def detect_phone_refusal(text: str) -> bool:
    return bool(PHONE_REFUSAL_RE.search(text or ""))

def extract_phone(text: str) -> Optional[str]:
    m = PHONE_RE.search(text or "")
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(0))
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    if len(digits) == 11 and digits.startswith("7"):
        return "+" + digits
    return None


# ------------------- date/time/address -------------------
TIME_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3])[:\.][0-5]\d\b")
TIME_PLAIN_H_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*(?:ч|час)\s*$", re.IGNORECASE)
DATE_NUM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
DATE_WORD_RE = re.compile(r"\b(\d{1,2})\s*(января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря)\b", re.IGNORECASE)

ADDRESS_HINT_RE = re.compile(r"\b(ул\.|улица|проспект|пр-т|дом|д\.|кв\.|квартира|корпус|строение)\b", re.IGNORECASE)

AREA_HINT_RE = re.compile(r"\b(м2|м²|м\^2|кв\.?\s*м|кв\.?\b|квм\b|квадрат)\b", re.IGNORECASE)

def extract_visit_time(text: str) -> Optional[str]:
    low = (text or "").lower()
    m = TIME_HHMM_RE.search(text or "")
    if m:
        return m.group(0).replace(".", ":")
    m = TIME_PLAIN_H_RE.match((text or "").strip())
    if m:
        hh = int(m.group(1))
        if hh <= 7 and ("утра" not in low) and ("ноч" not in low):
            hh += 12
        return f"{hh:02d}:00"
    if "обед" in low:
        return "обед"
    if "утром" in low:
        return "утром"
    if "днем" in low or "днём" in low:
        return "днем"
    if "вечером" in low:
        return "вечером"
    return None

def extract_visit_date(text: str) -> Optional[str]:
    low = (text or "").lower()
    if "сегодня" in low:
        return "сегодня"
    if "завтра" in low:
        return "завтра"
    m = DATE_NUM_RE.search(text or "")
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        if yy:
            return f"{dd}.{mm}.{yy}"
        return f"{dd}.{mm}"
    m = DATE_WORD_RE.search(low)
    if m:
        return f"{m.group(1)} {m.group(2)}"
    return None

def resolve_relative_date(vdate: str) -> str:
    if not vdate:
        return vdate
    today = datetime.date.today()
    if vdate == "сегодня":
        return today.strftime("%d.%m.%Y")
    if vdate == "завтра":
        return (today + datetime.timedelta(days=1)).strftime("%d.%m.%Y")
    return vdate

def extract_address(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None
    low = t.lower()
    # если это похоже на дату/время/площадь — не адрес
    if TIME_HHMM_RE.search(t) or DATE_NUM_RE.search(t) or DATE_WORD_RE.search(low):
        return None
    if AREA_HINT_RE.search(t):
        return None
    # «после 2», «до обеда», «после обеда» и т.п. — это про время, не про адрес
    if re.search(r"\b(после|до)\b", low) and re.search(r"\d", low):
        return None
    if re.search(r"\b(обед|утром|вечером|днем|дн[её]м)\b", low):
        return None
    # если есть подсказки адреса — берём
    if ADDRESS_HINT_RE.search(t) and re.search(r"\d", t):
        return t
    # или если просто "ворошилова 4" (но не "после 2")
    if re.search(r"[А-Яа-яЁё]{3,}", t) and re.search(r"\d", t) and len(t) <= 80:
        return t
    return None


# ------------------- sanitizer (чтобы Avito выглядел прилично) -------------------
GREET_RE = re.compile(r"^\s*(здравствуйте|добрый день|добрый вечер|доброе утро|привет|приветствую)[\s!\.,:;-]*", re.IGNORECASE)

GREET_REQUEST_RE = re.compile(r"\b(приветств|поприветств|приветствие)\b", re.IGNORECASE)

def detect_greeting(text: str) -> bool:
    return bool(GREET_RE.match(text or ""))

def detect_greeting_request(text: str) -> bool:
    return bool(GREET_REQUEST_RE.search(text or ""))

BAD_WAIT_RE = re.compile(r"(?i)\b(жд[её]м\s+вас|приходите|ожидаем\s+вас)\b")
BAD_I_RE = re.compile(r"(?i)\bя\s+(приеду|выех\w*|проведу\s+замер|замерю)\b")
BAD_CALL_RE = re.compile(r"(?i)\b(позвоню|позвоним|созвон|позвоните|звоните|наберите)\b[^\n]*")

def sanitize_answer(answer: str, allow_greet: bool, allow_phone_echo: bool = False) -> str:
    if not answer:
        return answer
    s = answer.strip()
    if not allow_greet:
        s = GREET_RE.sub("", s, count=1).strip()
    s = BAD_WAIT_RE.sub("мастер приедет", s)
    s = BAD_I_RE.sub("мастер приедет", s)
    s = BAD_CALL_RE.sub("", s).strip()
    if not allow_phone_echo:
        s = PHONE_ANY_RE.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


# ------------------- text builders (паттерны как в ТГ) -------------------
PROMO_DISCOUNTS_TEXT = (
    "На каждый второй потолок (меньший по площади) полотно идет в подарок 😊\n"
    "Если кто-то из ваших близких участник СВО или работник оборонного предприятия — 3-е полотно тоже в подарок.\n"
    "Скидка на освещение до 50%.\n"
)

def t_hello(first: bool) -> str:
    return "Здравствуйте 😊 " if first else ""

def build_welcome(first: bool) -> str:
    return (
        f"{t_hello(first)}Будем рады помочь 😊\n"
        "Подскажите, пожалуйста, ваш город и примерную площадь (м²).\n"
        "Замер бесплатный — мастер приедет с каталогами и образцами."
    )

def build_need_city(first: bool) -> str:
    return f"{t_hello(first)}Подскажите, пожалуйста, в каком вы городе? 🙂"


def build_out_of_city_need_city(first: bool) -> str:
    return (
        f"{t_hello(first)}Подскажите, пожалуйста, в каком городе вы находитесь и куда нужен выезд (район/посёлок)?\n"
        "Замер бесплатный ✅ Возможность выезда по точному адресу уточнит диспетчер."
    )


def build_out_of_city_answer(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}В {city} и ближайшие районы выезжаем ✅\n"
        "Замер бесплатный. Напишите, пожалуйста, куда именно нужен выезд (район/посёлок) — передам диспетчеру для уточнения логистики."
    )


SOUNDPROOF_PRICE_PER_SQM = 3375

SOUNDPROOF_INFO_TEXT = (
    "Материал: многослойный — акустический войлок 10 мм + тяжёлая вязкоэластичная мембрана 3 мм. "
    "Собственная звукоизоляция материала около 40 дБ, вместе с конструкцией натяжного потолка — до ~60 дБ. "
    "Толщина 13 мм — укладывается в стандартные 5–6 см опуска, высоту почти не съедает."
)


def build_soundproofing_need_city(first: bool) -> str:
    return f"{t_hello(first)}Подскажите, пожалуйста, в каком вы городе? 🙂" \
           " (для расчёта шумо/звукоизоляции)"


def build_soundproofing_need_area(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}{city} — поняла.\n"
        "Подскажите, пожалуйста, примерную площадь потолка (м²), которую нужно шумоизолировать." 
        " Можно примерно."
    )


def build_soundproofing_estimate(first: bool, city: str, area_m2: float) -> str:
    min_price = int(round(area_m2 * SOUNDPROOF_PRICE_PER_SQM))
    min_price_str = f"{min_price:,}".replace(",", " ")
    return (
        f"{t_hello(first)}Ориентир по «шумоизоляции и звукоизоляции под ключ»: от {min_price_str} ₽ ✅\n"
        f"(от {SOUNDPROOF_PRICE_PER_SQM} ₽/м², {city}, {area_m2:g} м²)\n"
        f"{SOUNDPROOF_INFO_TEXT}\n"
        "Если хотите — запишу на бесплатный замер: мастер приедет и подберёт решение под ваш объект."
    )

def build_soundproofing_info(first: bool, city: Optional[str]) -> str:
    city_line = "" if not city else f"В {city} можем сделать. "
    return (
        f"{t_hello(first)}Да, делаем «шумоизоляцию и звукоизоляцию под ключ» ✅\n"
        f"{city_line}Ориентир от {SOUNDPROOF_PRICE_PER_SQM} ₽/м². {SOUNDPROOF_INFO_TEXT}\n"
        "Если подскажете город и примерную площадь (м²) — сориентирую по сумме и запишу на бесплатный замер."
    )

def build_city_not_supported(first: bool, city_candidate: str) -> str:
    return (
        f"{t_hello(first)}Поняла вас. Пока, к сожалению, не работаем в городе «{city_candidate}».\n"
        "Сейчас выезжаем по Ижевску и Екатеринбургу (и ближайшим районам).\n"
        "Если объект в этих городах — напишите город и площадь (м²), сориентирую по стоимости."
    )

def build_need_area(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}{city} — поняла.\n"
        "Чтобы назвать ориентир по стоимости, подскажите площадь (м²). Можно примерно 🙂"
    )

def build_discounts_message(first: bool, city: Optional[str]) -> str:
    city_line = f"В {city} работаем.\n" if city else ""
    return (
        f"{t_hello(first)}{city_line}"
        "У нас сейчас есть такие акции:\n\n"
        f"{PROMO_DISCOUNTS_TEXT}\n"
        "Если хотите — напишите город и площадь (м²), сориентирую по стоимости.\n"
        "Замер бесплатный — мастер приедет с каталогами и образцами."
    )

def build_estimate(min_price: int, city: str, area_m2: float, ask_measure: bool) -> str:
    tail = (
        "Если захотите уточнить точнее — замер бесплатный: мастер приедет с каталогами и образцами."
        + ("\nЗаписать вас на замер?" if ask_measure else "")
    )
    return (
        f"Ориентир за потолок (по площади): от {min_price} ₽ ✅\n"
        f"({city}, {area_m2:g} м²)\n"
        "Важно: доп.работы (люстры/светильники/карнизы/ниши/углы/профиль и т.д.) в ориентир НЕ включены — точная стоимость уточняется на замере.\n"
        "Минимальная стоимость заказа — от 8 000 ₽.\n"
        f"{tail}"
    )

def build_measure_info(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}В {city} выезжаем.\n"
        "Замер бесплатный ✅ Мастер приедет с каталогами и образцами.\n"
        "Если хотите — запишу на удобные дату и время."
    )

def build_measure_intro(first: bool) -> str:
    return (
        f"{t_hello(first)}Отлично, оформим бесплатный замер ✅\n"
        "Мастер приедет с каталогами и образцами. Уточню один момент:"
    )

def build_lead_confirmation(mem: Dict[str, Any]) -> str:
    vdate = resolve_relative_date(mem.get("visit_date") or "")
    vtime = mem.get("visit_time") or "-"
    return (
        "Спасибо! Заявка на бесплатный замер принята ✅\n\n"
        f"Город: {mem.get('city')}\n"
        f"Адрес: {mem.get('address')}\n"
        f"Телефон: {mem.get('phone')}\n"
        f"Дата и время: {vdate} в {vtime}\n\n"
        "Менеджер свяжется с вами и подтвердит детали. Если нужно поменять время — просто напишите 😊"
    )

def _looks_like_polite_close(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    # если в сообщении есть вопрос/цифры/ключевые слова по цене — это не закрытие
    if "?" in t:
        return False
    if re.search(r"\d", t):
        return False
    low = t.lower()
    if detect_price_question(low) or detect_measurement_booking_intent(low) or detect_discount_mention(low):
        return False
    return bool(THANKS_CHEAP_RE.search(low) or GOODBYE_RE.search(low))


def build_closing(mem: Dict[str, Any], first: bool) -> str:
    # мягкое завершение без давления
    if mem.get("lead_created"):
        return f"{t_hello(first)}Пожалуйста 😊 Хорошего дня! Если появятся вопросы — пишите."

    if mem.get("measure_offer_pending") and not mem.get("agreed_measurement"):
        return (
            f"{t_hello(first)}Пожалуйста 😊\n"
            "Если захотите — запишу на бесплатный замер. Достаточно адреса и удобной даты/времени ✅\n"
            "Если будут вопросы — пишите 😊"
        )

    if mem.get("city") and mem.get("area_m2"):
        return (
            f"{t_hello(first)}Пожалуйста 😊\n"
            "Если решите уточнить точнее — замер бесплатный, мастер приедет с каталогами и образцами ✅\n"
            "Если будут вопросы — пишите 😊"
        )

    return (
        f"{t_hello(first)}Пожалуйста 😊\n"
        "Если подскажете город и примерную площадь (м²) — сориентирую по стоимости ✅\n"
        "Если будут вопросы — пишите 😊"
    )



# ------------------- AppState -------------------
EmailSender = Callable[[str, str, str], Awaitable[bool]]


class AppState:
    """
    Единое ядро: память + история + лиды + LLM.
    Адаптеры (tg/avito/...) просто вызывают generate_reply().
    """

    def __init__(self, model: str, ollama_timeout: int = 60):
        # ВАЖНО: у systemd/cron рабочая директория часто не равна папке проекта.
        # Поэтому все относительные пути приводим к абсолютным относительно корня репозитория.
        self.base_dir = Path(__file__).resolve().parents[1]

        def _abs(p: str) -> str:
            p = (p or "").strip()
            if not p:
                return str(self.base_dir)
            pp = Path(p)
            return str(pp) if pp.is_absolute() else str(self.base_dir / pp)

        self.ollama_timeout = int(ollama_timeout)
        self.ollama = OllamaClient(model=model, timeout=self.ollama_timeout)

        # Позволяем управлять поведением через .env (в проекте уже есть SYSTEM_PROMPT/MAX_HISTORY).
        self.system_prompt = (os.getenv("SYSTEM_PROMPT") or DEFAULT_SYSTEM_PROMPT).strip()
        try:
            self.max_history = int(os.getenv("MAX_HISTORY", "20") or "20")
        except Exception:
            self.max_history = 20

        self.pricing = PricingEngine(_abs(os.getenv("PRICING_FILE", "data/pricing_rules.json")))
        self.promos = PromotionManager(_abs(os.getenv("PROMOTIONS_FILE", "data/promotions.json")))
        self.intents = IntentDetector()
        self.dialog_log_dir = _abs(os.getenv("DIALOG_LOG_DIR", "data/dialog_logs"))
        os.makedirs(self.dialog_log_dir, exist_ok=True)
        self.mem_store = FileKVStore(dir_path=_abs(os.getenv("MEMORY_DIR", "data/memory")))
        self.leads = LeadStoreTxt(
            path=_abs(os.getenv("LEADS_PATH", "data/leads.txt")),
            leads_dir=_abs(os.getenv("LEADS_DIR", "data/leads")),
        )

        # События по лидам (обновления, переносы времени, уточнения) — append-only.
        self.lead_events = LeadStoreJsonl(_abs(os.getenv("LEADS_EVENTS_PATH", "data/leads_events.jsonl")))

        # Few-shot (быстрый способ улучшить стиль/воронку без обучения модели)
        self.fewshot = FewShotManager(_abs(os.getenv("FEWSHOT_PATH", "data/fewshot/ulyana_fewshot.json")))
        try:
            self.fewshot_k = int(os.getenv("FEWSHOT_K", "4") or "4")
        except Exception:
            self.fewshot_k = 4

        self.histories: Dict[str, ChatHistory] = {}

        self._loop = None
        self._notify_coro = None

        self._email_loop = None
        self._email_sender: Optional[EmailSender] = None

    # ---------- notifier (callcenter TG) ----------
    def set_notifier(self, loop, notify_coro_func):
        self._loop = loop
        self._notify_coro = notify_coro_func

    def notify_now(self, text: str) -> None:
        if not self._loop or not self._notify_coro:
            return

        def _schedule():
            asyncio.create_task(self._notify_coro(text))

        self._loop.call_soon_threadsafe(_schedule)

    # ---------- support / handoff on failures ----------
    def create_support_request(
        self,
        platform: str,
        user_id: str,
        meta: Optional[Dict[str, Any]],
        mem: Optional[Dict[str, Any]],
        user_text: str,
        error: str = "",
        reason: str = "exception",
    ) -> None:
        """Creates a support lead and notifies callcenter.

        Used when the bot can't reply (timeouts/exceptions) so we don't lose the client.
        """
        meta = meta or {}
        mem = mem or {}

        # anti-spam: at most one support ping per chat in 3 minutes
        try:
            last_ts = float(mem.get("_support_last_ts") or 0)
            if time.time() - last_ts < 180:
                return
            mem["_support_last_ts"] = time.time()
            self.mem_store.save(self._key(platform, user_id), mem)
        except Exception:
            pass

        lead_key = f"{platform}:{user_id}:{mem.get('service') or 'unknown'}"

        # collect last turns
        dialog = mem.get("_dialog") if isinstance(mem.get("_dialog"), list) else []
        tail = []
        for it in dialog[-6:]:
            if isinstance(it, dict) and isinstance(it.get("text"), str) and isinstance(it.get("role"), str):
                role = "Клиент" if it["role"] == "user" else "Менеджер"
                tail.append(f"{role}: {it['text']}")

        lead = {
            "ts": int(time.time()),
            "platform": platform,
            "user_id": user_id,
            "username": meta.get("username") or "",
            "name": meta.get("name") or "",
            "lead_kind": "support_request",
            "reason": reason,
            "error": (error or "")[:500],
            "service": mem.get("service"),
            "city": mem.get("city"),
            "area_m2": mem.get("area_m2"),
            "extras": mem.get("extras"),
            "address": mem.get("address"),
            "visit_date": mem.get("visit_date"),
            "visit_time": mem.get("visit_time"),
            "phone": mem.get("phone"),
            "text": user_text,
            "lead_key": lead_key,
            "dialog_tail": tail,
        }

        try:
            self.leads.append(lead)
        except Exception:
            pass

        try:
            self.lead_events.append(
                {
                    "ts": int(time.time()),
                    "event": "support_request",
                    "lead_key": lead_key,
                    "platform": platform,
                    "user_id": user_id,
                    "reason": reason,
                    "text": user_text,
                }
            )
        except Exception:
            pass

        # notify callcenter
        try:
            uname = f"@{meta.get('username')}" if meta.get("username") else "-"
            link = meta.get("chat_url") or meta.get("item_url") or meta.get("link") or "-"
            self.notify_now(
                "🆘 Нужна помощь менеджера\n"
                f"Платформа: {platform}\n"
                f"ID: {user_id}\n"
                f"Username: {uname}\n"
                f"Имя: {meta.get('name') or '-'}\n"
                f"Товар: {mem.get('service') or '-'}\n"
                f"Город: {mem.get('city') or '-'}\n"
                f"Площадь: {mem.get('area_m2') or '-'}\n"
                f"Телефон: {mem.get('phone') or '-'}\n"
                f"Ссылка: {link}\n"
                f"Текст: {user_text}\n"
                + ("\n".join(tail) if tail else "")
            )
        except Exception:
            pass

    # ---------- email sender ----------
    def set_email_sender(self, loop, email_sender: EmailSender) -> None:
        self._email_loop = loop
        self._email_sender = email_sender

    def send_email_now(self, subject: str, body: str, file_path: str) -> None:
        if not self._email_loop or not self._email_sender:
            return
        if not file_path:
            return

        def _schedule():
            asyncio.create_task(self._email_sender(subject, body, file_path))

        self._email_loop.call_soon_threadsafe(_schedule)

    # ---------- keys/history ----------
    def _key(self, platform: str, user_id: str) -> str:
        return f"{platform}:{user_id}"

    def _load_history_from_mem(self, k: str, history: ChatHistory, mem: Dict[str, Any]) -> None:
        """
        Avito не даёт /messages, поэтому переписку копим сами в mem["_dialog"].
        При рестарте поднимаем историю оттуда.
        """
        dialog = mem.get("_dialog")
        if not isinstance(dialog, list):
            return
        for it in dialog[-14:]:
            if not isinstance(it, dict):
                continue
            role = it.get("role")
            text = it.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if role == "user":
                history.add_user(text.strip())
            elif role == "assistant":
                history.add_assistant(text.strip())

    def get_history(self, platform: str, user_id: str, mem: Dict[str, Any]) -> ChatHistory:
        k = self._key(platform, user_id)
        if k not in self.histories:
            h = ChatHistory(self.system_prompt, max_messages=self.max_history)
            self._load_history_from_mem(k, h, mem)
            self.histories[k] = h
        return self.histories[k]

    def _push_dialog(self, mem: Dict[str, Any], role: str, text: str, max_items: int = 30) -> None:
        dialog = mem.get("_dialog")
        if not isinstance(dialog, list):
            dialog = []
        dialog.append({"ts": int(time.time()), "role": role, "text": text})
        mem["_dialog"] = dialog[-max_items:]

    def reset_all(self, platform: str, user_id: str) -> None:
        k = self._key(platform, user_id)
        self.histories[k] = ChatHistory(self.system_prompt, max_messages=self.max_history)
        self.mem_store.reset(k)

    def _append_dialog_log(self, platform: str, user_id: str, role: str, text: str) -> None:
        try:
            safe_user = "".join(ch for ch in str(user_id) if ch.isalnum() or ch in ("-", "_"))[:80]
            path = os.path.join(self.dialog_log_dir, f"{platform}_{safe_user}.txt")
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            line = f"[{ts}] {role}: {text.strip()}\n"
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
        except Exception:
            pass
    # ---------- lead helpers ----------
    def _ask_next_measure_field(self, mem: Dict[str, Any], first: bool) -> str:
        """
        Спрашиваем по ОДНОМУ полю.
        Порядок: город -> адрес -> дата -> время -> телефон
        """
        if not mem.get("city"):
            mem["asked_city"] = True
            return build_need_city(first)

        intro = build_measure_intro(first) if not mem.get("measure_intro_sent") else "Спасибо! Уточню ещё один момент:"
        mem["measure_intro_sent"] = True

        if not mem.get("address"):
            mem["asked_address"] = True
            return f"{intro}\nНапишите, пожалуйста, адрес (улица, дом, квартира/офис)."

        if not mem.get("visit_date"):
            mem["asked_date"] = True
            return f"{intro}\nНа какую дату удобно? (например: 19.02 или 19 февраля)"

        vt = mem.get("visit_time")
        if not vt or vt in ("обед", "утром", "днем", "вечером"):
            mem["asked_time"] = True
            return f"{intro}\nКакое точное время удобно? (например: 13:00)"
        if not mem.get("phone") and not mem.get("no_phone"):
            mem["asked_phone"] = True
            return f"{intro}\nИ номер телефона для подтверждения заявки (можно 8XXXXXXXXXX)."

        # если клиент явно не хочет оставлять телефон — не давим, оформим заявку по чату
        if not mem.get("phone") and mem.get("no_phone"):
            return ""

        return ""

    def _maybe_create_measure_lead_if_ready(
        self,
        platform: str,
        user_id: str,
        mem: Dict[str, Any],
        meta: Dict[str, Any],
        first: bool,
    ) -> Optional[str]:
        if not mem.get("agreed_measurement"):
            return None
        if mem.get("lead_created"):
            return None

        msg = self._ask_next_measure_field(mem, first=first)
        if msg:
            return msg

        lead = {
            "ts": int(time.time()),
            "platform": platform,
            "user_id": user_id,
            "username": meta.get("username", ""),
            "name": meta.get("name", ""),
            "lead_kind": "measure",
            "service": mem.get("service") or ("soundproof" if mem.get("soundproof_pending") else "ceiling"),
            "city": mem.get("city"),
            "area_m2": mem.get("area_m2"),
            "extras": mem.get("extras"),
            "address": mem.get("address"),
            "visit_date": resolve_relative_date(mem.get("visit_date") or ""),
            "visit_time": mem.get("visit_time"),
            "phone": mem.get("phone"),
            "meta": meta,
        }

        lead_file_path = self.leads.append(lead)
        mem["lead_created"] = True
        # снапшот для последующих обновлений (перенос времени, уточнение адреса и т.д.)
        mem["lead_key"] = f"{platform}:{user_id}:measure"
        mem["lead_snapshot"] = {
            "city": lead.get("city"),
            "area_m2": lead.get("area_m2"),
            "extras": lead.get("extras"),
            "address": lead.get("address"),
            "visit_date": lead.get("visit_date"),
            "visit_time": lead.get("visit_time"),
            "phone": lead.get("phone"),
        }
        try:
            self.lead_events.append(
                {
                    "ts": int(time.time()),
                    "event": "measure_create",
                    "lead_key": mem.get("lead_key"),
                    "platform": platform,
                    "user_id": user_id,
                    "snapshot": mem.get("lead_snapshot"),
                }
            )
        except Exception:
            pass

        uname = f"@{lead['username']}" if lead.get("username") else "-"
        lead_text = (
            "📩 Новая заявка на бесплатный замер\n"
            f"Платформа: {lead['platform']}\n"
            f"User ID: {lead['user_id']}\n"
            f"Username: {uname}\n"
            f"Имя: {lead.get('name') or '-'}\n"
            f"Город: {lead.get('city') or '-'}\n"
            f"Адрес: {lead.get('address') or '-'}\n"
            f"Дата: {lead.get('visit_date') or '-'}\n"
            f"Время: {lead.get('visit_time') or '-'}\n"
            f"Телефон: {lead.get('phone') or '-'}\n"
            f"Площадь: {lead.get('area_m2') or '-'}\n"
            f"Допы: {lead.get('extras') or '-'}"
        )
        self.notify_now(lead_text)

        if lead_file_path:
            subject = f"Заявка на замер: {lead.get('city')} / {lead.get('visit_date')} {lead.get('visit_time')}"
            body = lead_text + "\n\nФайл заявки во вложении."
            self.send_email_now(subject, body, lead_file_path)

        return build_lead_confirmation(mem)

    def _maybe_create_warm_lead(self, platform: str, user_id: str, mem: Dict[str, Any], meta: Dict[str, Any], user_text: str) -> None:
        """
        Тёплый лид = есть город+площадь и явный интерес (цена/замер/акция/шумо).
        Нужен, чтобы лид не терялся, даже если клиент не дошёл до оформления замера.
        """
        try:
            if mem.get("lead_created"):
                return
            if not (mem.get("city") and mem.get("area_m2")):
                return

            # не чаще 1 раза в 12 часов
            last_ts = float(mem.get("warm_lead_ts") or 0)
            if last_ts and (time.time() - last_ts) < 43200:
                return

            low = (user_text or "").lower()
            intent = (
                detect_price_question(low)
                or detect_measurement_booking_intent(low)
                or detect_measurement_info_question(low)
                or detect_discount_mention(low)
                or detect_soundproofing_question(low)
            )
            if not intent:
                return

            service = mem.get("service") or ("soundproof" if mem.get("soundproof_pending") else "ceiling")
            lead = {
                "ts": int(time.time()),
                "platform": platform,
                "user_id": user_id,
                "username": meta.get("username", ""),
                "name": meta.get("name", ""),
                "lead_kind": "warm",
                "service": service,
                "city": mem.get("city"),
                "area_m2": mem.get("area_m2"),
                "extras": mem.get("extras"),
                "address": mem.get("address"),
                "visit_date": resolve_relative_date(mem.get("visit_date") or ""),
                "visit_time": mem.get("visit_time"),
                "phone": mem.get("phone"),
                "meta": meta,
            }
            self.leads.append(lead)
            mem["warm_lead_ts"] = time.time()

            # короткое уведомление в коллцентр (не как полноценная заявка)
            if platform == "avito":
                link = meta.get("chat_url") or meta.get("item_url") or "https://www.avito.ru/profile/messenger"
                self.notify_now(
                    "🟡 Тёплый лид (есть город и площадь)\n"
                    f"Платформа: {platform}\n"
                    f"Chat ID: {user_id}\n"
                    f"Товар: {service}\n"
                    f"Город: {mem.get('city') or '-'}\n"
                    f"Площадь: {mem.get('area_m2') or '-'}\n"
                    f"Ссылка: {link}"
                )
        except Exception:
            return

    def _maybe_update_measure_lead(self, platform: str, user_id: str, mem: Dict[str, Any], meta: Dict[str, Any]) -> None:
        """If a measure lead was already created, send an update when key fields changed.

        This fixes the "lead doesn't update after reschedule" issue.
        """
        if not mem.get("lead_created"):
            return

        prev = mem.get("lead_snapshot")
        cur = {
            "city": mem.get("city"),
            "area_m2": mem.get("area_m2"),
            "extras": mem.get("extras"),
            "address": mem.get("address"),
            "visit_date": resolve_relative_date(mem.get("visit_date") or ""),
            "visit_time": mem.get("visit_time"),
            "phone": mem.get("phone") if not mem.get("no_phone") else None,
        }

        # first time snapshot might be missing (compat with old mem)
        if not isinstance(prev, dict):
            mem["lead_snapshot"] = cur
            mem["lead_key"] = mem.get("lead_key") or f"{platform}:{user_id}:measure"
            return

        changed = {k: (prev.get(k), cur.get(k)) for k in cur.keys() if prev.get(k) != cur.get(k)}
        if not changed:
            return

        # anti-spam
        last_ts = float(mem.get("lead_update_ts") or 0)
        if (time.time() - last_ts) < 15:
            mem["lead_snapshot"] = cur
            return

        mem["lead_update_ts"] = time.time()
        mem["lead_snapshot"] = cur

        def _fmt(v):
            return "-" if v in (None, "", [], {}) else v

        uname = f"@{meta.get('username')}" if meta.get("username") else "-"
        lines = []
        ru = {
            "city": "Город",
            "area_m2": "Площадь",
            "extras": "Допы",
            "address": "Адрес",
            "visit_date": "Дата",
            "visit_time": "Время",
            "phone": "Телефон",
        }
        for k, (a, b) in changed.items():
            lines.append(f"{ru.get(k,k)}: {_fmt(a)} → {_fmt(b)}")

        self.notify_now(
            "🛠 Обновление заявки на замер\n"
            f"Платформа: {platform}\n"
            f"User ID: {user_id}\n"
            f"Username: {uname}\n"
            + "\n".join(lines)
        )

        try:
            self.lead_events.append(
                {
                    "ts": int(time.time()),
                    "event": "measure_update",
                    "lead_key": mem.get("lead_key") or f"{platform}:{user_id}:measure",
                    "platform": platform,
                    "user_id": user_id,
                    "changed": changed,
                }
            )
        except Exception:
            pass


    # ---------- public API ----------
    def generate_reply(
        self,
        platform: str,
        user_id: str,
        user_text: str,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        meta = meta or {}
        k = self._key(platform, user_id)

        mem: Dict[str, Any] = self.mem_store.load(k)
        first = not bool(mem.get("_started"))

        history = self.get_history(platform, user_id, mem)

        user_text = (user_text or "").strip()
        if not user_text:
            return ""

        
        # --- запрос живого человека / оператора ---
        if detect_handoff_request(user_text):
            # сбрасываем любые "анкеты замера", чтобы не продолжать спрашивать время/адрес
            mem["measure_offer_pending"] = False
            mem["agreed_measurement"] = False
            mem["handoff_requested"] = True

            # событие для трекинга
            try:
                lead_key = mem.get("lead_key") or f"{platform}:{user_id}"
                mem["lead_key"] = lead_key
                self.lead_events.append(
                    {
                        "ts": int(time.time()),
                        "event": "handoff_request",
                        "lead_key": lead_key,
                        "platform": platform,
                        "user_id": user_id,
                        "username": meta.get("username"),
                        "name": meta.get("name"),
                        "text": user_text,
                        "service": mem.get("service"),
                        "city": mem.get("city"),
                        "area_m2": mem.get("area_m2"),
                    }
                )
            except Exception:
                pass

            def _platform_label(p: str) -> str:
                p = (p or "").lower()
                return {
                    "tg": "TG",
                    "telegram": "TG",
                    "avito": "Авито",
                    "vk": "VK",
                }.get(p, p or "-")
            # уведомление в менеджерский чат
            try:
                uname = f"@{meta.get('username')}" if meta.get("username") else "-"
                header = f"🆘 Просьба подключить менеджера ({_platform_label(platform)})"
                link = meta.get("link") or ("https://www.avito.ru/profile/messenger" if platform == "avito" else "-")
                self.notify_now(
                    f"{header}\n"
                    f"ID: {user_id}\n"
                    f"Username: {uname}\n"
                    f"Имя: {meta.get('name') or '-'}\n"
                    f"Товар: {mem.get('service') or '-'}\n"
                    f"Город: {mem.get('city') or '-'}\n"
                    f"Площадь: {mem.get('area_m2') or '-'}\n"
                    f"Телефон: {mem.get('phone') or '-'}\n"
                    f"Ссылка: {link}\n"
                    f"Текст: {user_text}"
                )
            except Exception:
                pass

            self.mem_store.save(k, mem)
            return (
                "Поняла вас 😊 Подключу менеджера.\n"
                "Пока он подключается, напишите, пожалуйста, одним сообщением: город и что нужно (потолок/шумоизоляция/расчёт).\n"
                "Если не хотите оставлять телефон — можно просто ваш @ник, чтобы с вами связались."
            )

        wants_greet = detect_greeting(user_text) or detect_greeting_request(user_text)
        greet = first or wants_greet

        # пишем входящее в историю/память ВСЕГДА (важно для Avito)
        self._push_dialog(mem, "user", user_text)
        history.add_user(user_text)

        # greeting-only: отвечаем приветствием ТОЛЬКО если это реально короткое приветствие.
        # Иначе (как в кейсе: "Добрый день! Нужно потолок... 11 квадратов...") мы обязаны распарсить сообщение.
        is_pure_greeting = (
            bool(wants_greet)
            and len((user_text or "").strip().split()) <= 3
            and not re.search(r"\d", user_text or "")
            and not re.search(r"\b(потолк|натяж|шумо|звуко|изоляц|цена|стоим|сколько)\b", user_text or "", re.IGNORECASE)
        )

        if is_pure_greeting and not mem.get("city") and not mem.get("area_m2"):
            ans = sanitize_answer(build_welcome(first=True), allow_greet=True)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans


        # ---- определяем направление по заголовку объявления (Авито) ----
        # ВАЖНО: «Шумоизоляция и звукоизоляция под ключ» — это отдельный товар.
        meta_title = str(meta.get("title") or "")
        if platform == "avito":
            if detect_soundproofing_question(meta_title):
                mem["service"] = "soundproof"
            elif not mem.get("service"):
                mem["service"] = "ceiling"

        # ---- извлечение площади/допов ----
        extracted = extract_info(user_text)

        # площадь
        if getattr(extracted, "area_m2", None):
            mem["area_m2"] = extracted.area_m2

        # допы: сохраняем и накапливаем (часто клиент пишет допы в 2-3 сообщения)
        prev_extras = mem.get("extras") if isinstance(mem.get("extras"), list) else []
        prev_counts = mem.get("extras_counts") if isinstance(mem.get("extras_counts"), dict) else {}

        # количества по допам (если клиент указал явно — обновляем)
        if getattr(extracted, "extras_counts", None):
            try:
                new_counts = dict(prev_counts)
                for kx, vx in (extracted.extras_counts or {}).items():
                    # явное количество из последнего сообщения приоритетнее
                    new_counts[str(kx)] = int(vx)
                mem["extras_counts"] = new_counts
            except Exception:
                pass

        # признаки допов (без количеств) — мерджим, не затираем
        if getattr(extracted, "extras", None):
            try:
                merged = list(prev_extras)
                for e in (extracted.extras or []):
                    if e not in merged:
                        merged.append(e)
                mem["extras"] = merged
            except Exception:
                pass

        # углы/профиль — полезно для контекста (даже если в pricing_rules пока не учтены)
        try:
            low = (user_text or "").lower().replace("ё", "е")
            m_ang = re.search(r"\b(\d{1,2})\s*(угл\w*)\b", low)
            if m_ang:
                mem["angles"] = int(m_ang.group(1))
        except Exception:
            pass

        try:
            low = (user_text or "").lower().replace("ё", "е")
            if re.search(r"\bне\s+парящ", low):
                mem["profile"] = "standard"
            elif re.search(r"\bпарящ", low):
                mem["profile"] = "floating"
        except Exception:
            pass

        def _extras_for_pricing() -> List[str]:
            out: List[str] = []
            counts = mem.get("extras_counts") if isinstance(mem.get("extras_counts"), dict) else {}
            # counts first (so they can multiply)
            for name, cnt in (counts or {}).items():
                try:
                    n = int(cnt)
                except Exception:
                    continue
                if n <= 0:
                    continue
                # защита от огромных чисел
                if n > 60:
                    n = 60
                out.extend([str(name)] * n)

            extras_list = mem.get("extras") if isinstance(mem.get("extras"), list) else []
            for e in extras_list:
                if isinstance(counts, dict) and e in counts:
                    continue
                out.append(e)
            return out

        # эвристика площади: ловим число даже без "кв.м"
        # ВАЖНО: не подменяем "3 потолка/3 комнаты" на "3 м²" — это ломает диалог.
        cleaned = PHONE_ANY_RE.sub(" ", user_text)
        nums = [int(n) for n in re.findall(r"\b(\d{1,3})\b", cleaned)]
        nums = [n for n in nums if 1 <= n <= 300]
        has_area_hint = bool(AREA_HINT_RE.search(cleaned) or re.search(r"\bплощад", cleaned, re.IGNORECASE))
        looks_like_rooms = bool(re.search(r"\b(потолк|комнат|уровн|помещен)\b", cleaned, re.IGNORECASE))

        if nums and has_area_hint:
            mem["area_m2"] = float(max(nums))
        elif nums and (mem.get("asked_area") or mem.get("asked_area_soundproof")) and not looks_like_rooms:
            # пользователь отвечает просто числом на вопрос про площадь
            mem["area_m2"] = float(max(nums))

        if platform == "avito":
            if mem.get("city") and mem.get("area_m2") and not detect_price_question(user_text):
                # чтобы не повторять один и тот же расчет бесконечно:
                marker = f"{mem.get('city')}|{mem.get('area_m2')}"
                if mem.get("last_auto_estimate") != marker:
                    mem["last_auto_estimate"] = marker
                    # принудительно считаем как price question
                    user_text = user_text + " (рассчитай стоимость)"
        # ---- city handling ----
        supported_city = extract_city(user_text)
        if supported_city:
            mem["city"] = supported_city
            mem.pop("unsupported_city_candidate", None)
        else:
            cand = extract_city_candidate(user_text)
            if cand:
                # если явно сказал город, но мы его не поддерживаем — отвечаем сразу
                mem["unsupported_city_candidate"] = cand

        # ---- phone/address/date/time ----
        if detect_phone_refusal(user_text):
            mem["no_phone"] = True
        ph = extract_phone(user_text)
        if ph:
            mem["phone"] = ph
            mem.pop("no_phone", None)

        addr = extract_address(user_text)
        if addr:
            mem["address"] = addr

        vdate = extract_visit_date(user_text)
        if vdate:
            mem["visit_date"] = vdate

        vt = extract_visit_time(user_text)
        if vt:
            mem["visit_time"] = vt

        hot_fields = 0
        hot_fields += 1 if mem.get("address") else 0
        hot_fields += 1 if mem.get("visit_date") else 0
        hot_fields += 1 if mem.get("visit_time") else 0
        hot_fields += 1 if mem.get("phone") else 0

        hot_intent = detect_measurement_booking_intent(user_text) or detect_affirm(user_text)
        hot_discount = detect_discount_mention(user_text) and bool(mem.get("area_m2") and mem.get("city"))

        def _platform_label(p: str) -> str:
            p = (p or "").lower()
            return {
                "avito": "Авито",
                "tg": "TG",
                "telegram": "TG",
                "vk": "VK",
                "whatsapp": "WA",
            }.get(p, p or "-")

        def _lead_key() -> str:
            # service важен: у одного user_id могут быть разные товары/воронки
            return f"{platform}:{user_id}:{mem.get('service') or 'unknown'}"

        def _snapshot() -> Dict[str, Any]:
            return {
                "city": mem.get("city"),
                "area_m2": mem.get("area_m2"),
                "address": mem.get("address"),
                "visit_date": mem.get("visit_date"),
                "visit_time": mem.get("visit_time"),
                "phone": mem.get("phone"),
                "extras": mem.get("extras"),
                "service": mem.get("service"),
                "username": meta.get("username"),
                "name": meta.get("name"),
            }

        # 1) Первое уведомление "горячий" — platform-aware.
        if (hot_intent or hot_fields >= 2 or hot_discount) and not mem.get("hot_notified"):
            mem["hot_notified"] = True
            mem["hot_lead_key"] = _lead_key()
            mem["hot_snapshot"] = _snapshot()

            link = meta.get("chat_url") or meta.get("item_url")
            if platform != "avito":
                link = link or "-"
            else:
                link = link or "https://www.avito.ru/profile/messenger"

            uname = f"@{meta.get('username')}" if meta.get("username") else "-"
            header = f"🔥 Горячий интерес ({_platform_label(platform)})"
            self.notify_now(
                f"{header}\n"
                f"ID: {user_id}\n"
                f"Username: {uname}\n"
                f"Имя: {meta.get('name') or '-'}\n"
                f"Товар: {mem.get('service') or '-'}\n"
                f"Город: {mem.get('city') or '-'}\n"
                f"Площадь: {mem.get('area_m2') or '-'}\n"
                f"Адрес: {mem.get('address') or '-'}\n"
                f"Дата: {mem.get('visit_date') or '-'}\n"
                f"Время: {mem.get('visit_time') or '-'}\n"
                f"Телефон: {mem.get('phone') or '-'}\n"
                f"Ссылка: {link}\n"
                f"Текст: {user_text}"
            )

            # событие
            try:
                self.lead_events.append(
                    {
                        "ts": int(time.time()),
                        "event": "hot_create",
                        "lead_key": mem.get("hot_lead_key"),
                        "platform": platform,
                        "user_id": user_id,
                        "snapshot": mem.get("hot_snapshot"),
                    }
                )
            except Exception:
                pass

        # 2) Обновление: если "горячий" уже отправляли, но данные изменились — шлём дифф.
        if mem.get("hot_notified"):
            prev = mem.get("hot_snapshot") or {}
            cur = _snapshot()
            changed = {k: (prev.get(k), cur.get(k)) for k in cur.keys() if prev.get(k) != cur.get(k)}
            # анти-спам: не чаще 1 раза в 20 секунд
            last_u = float(mem.get("hot_update_ts") or 0)
            if changed and (time.time() - last_u) > 20:
                mem["hot_update_ts"] = time.time()
                mem["hot_snapshot"] = cur

                def _fmt(v):
                    return "-" if v in (None, "", [], {}) else v

                lines = []
                for k2, (a, b) in changed.items():
                    if k2 in ("username", "name"):
                        continue
                    lines.append(f"{k2}: {_fmt(a)} → {_fmt(b)}")

                if lines:
                    header = f"📝 Обновление лида ({_platform_label(platform)})"
                    self.notify_now(
                        f"{header}\n"
                        f"ID: {user_id}\n"
                        f"LeadKey: {mem.get('hot_lead_key') or _lead_key()}\n"
                        + "\n".join(lines)
                    )
                    try:
                        self.lead_events.append(
                            {
                                "ts": int(time.time()),
                                "event": "hot_update",
                                "lead_key": mem.get("hot_lead_key") or _lead_key(),
                                "platform": platform,
                                "user_id": user_id,
                                "changed": changed,
                            }
                        )
                    except Exception:
                        pass
        # создаём тёплый лид, чтобы не терять обращения
        self._maybe_create_warm_lead(platform, user_id, mem, meta, user_text)

        # если заявка уже была создана — отправляем обновление при изменениях (перенос времени/адреса и т.д.)
        self._maybe_update_measure_lead(platform, user_id, mem, meta)

        # ---- unsupported city short-circuit ----
        if mem.get("unsupported_city_candidate") and not mem.get("city"):
            ans = build_city_not_supported(greet, str(mem["unsupported_city_candidate"]))
            ans = sanitize_answer(ans, allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ---- вопросы про выезд за пределы города (сначала уточняем город, доп.стоимость не озвучиваем) ----
        if detect_out_of_city_question(user_text):
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = build_out_of_city_need_city(greet)
            else:
                ans = build_out_of_city_answer(greet, str(mem.get("city")))

            ans = sanitize_answer(ans, allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ---- потолки: если уже есть город+площадь, не уводим в "анкету" — даём ориентир сразу ----
        if (mem.get("service") or "ceiling") != "soundproof":
            if mem.get("city") and mem.get("area_m2") and not mem.get("price_given"):
                # если клиент прямо просит записать/"давайте" — пусть уходит в замер-ветку ниже
                if not (detect_measurement_booking_intent(user_text) or detect_affirm(user_text)):
                    est = self.pricing.calculate(city=str(mem.get("city")), area_m2=float(mem.get("area_m2")), extras=_extras_for_pricing())
                    if getattr(est, "min_price", None) is not None:
                        mem["price_given"] = True
                        ans = (
                            f"Ориентир за потолок (по площади): от {int(est.min_price)} ₽ ✅\n"
                            f"({mem.get('city')}, {int(float(mem.get('area_m2')))} м²)\n"
                            "Важно: доп.работы (люстры/светильники/карнизы/ниши/углы/профиль и т.д.) в ориентир НЕ включены — точная стоимость уточняется на замере.\n"
                            "Минимальная стоимость заказа — от 8 000 ₽.\n"
                            "Хотите просто ориентир (без замера) или записать на бесплатный замер?"
                        )
                        ans = sanitize_answer(ans, allow_greet=greet)
                        history.add_assistant(ans)
                        self._push_dialog(mem, "assistant", ans)
                        mem["_started"] = True
                        self.mem_store.save(k, mem)
                        return ans

        # ---- отдельный товар: шумо/звукоизоляция под ключ ----
        if mem.get("service") == "soundproof":
            # если клиент пишет про выезд — логистика уже отработана выше
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = build_soundproofing_need_city(greet)
            else:
                # если спрашивает цену или просто прислал площадь — даём ориентир
                if not mem.get("area_m2") and (detect_price_question(user_text) or mem.get("calc_only")):
                    mem["asked_area"] = True
                    ans = build_soundproofing_need_area(greet, str(mem.get("city")))
                elif mem.get("area_m2") and (detect_price_question(user_text) or detect_soundproofing_question(user_text) or mem.get("calc_only")):
                    ans = build_soundproofing_estimate(greet, str(mem.get("city")), float(mem.get("area_m2")))
                else:
                    ans = build_soundproofing_info(greet, mem.get("city"))

            ans = sanitize_answer(ans, allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # Если объявление про потолки, но клиент уточняет про шумоизоляцию — кратко ответим (без перевода в другой товар).
        if detect_soundproofing_question(user_text):
            mem["soundproof_pending"] = True
            mem["soundproof_pending_ts"] = time.time()
            if not mem.get("city"):
                mem["asked_city"] = True
            else:
                mem["asked_area_soundproof"] = True
            ans = build_soundproofing_info(greet, mem.get("city"))

            ans = sanitize_answer(ans, allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # follow-up: если недавно обсуждали шумоизоляцию и клиент прислал город/площадь
        if mem.get("soundproof_pending"):
            ts = float(mem.get("soundproof_pending_ts") or 0)
            if ts and (time.time() - ts) > 1800:
                mem.pop("soundproof_pending", None)
                mem.pop("soundproof_pending_ts", None)
                mem.pop("asked_area_soundproof", None)
            else:
                # если клиент явно вернулся к потолкам — снимаем ожидание
                if re.search(r"\b(потолк|натяж)\w*\b", user_text, re.IGNORECASE) and not detect_soundproofing_question(user_text):
                    mem.pop("soundproof_pending", None)
                    mem.pop("soundproof_pending_ts", None)
                    mem.pop("asked_area_soundproof", None)
                else:
                    if not mem.get("city"):
                        mem["asked_city"] = True
                        ans = build_soundproofing_need_city(greet)
                        ans = sanitize_answer(ans, allow_greet=greet)
                        history.add_assistant(ans)
                        self._push_dialog(mem, "assistant", ans)
                        mem["_started"] = True
                        self.mem_store.save(k, mem)
                        return ans

                    if not mem.get("area_m2"):
                        mem["asked_area_soundproof"] = True
                        ans = build_soundproofing_need_area(greet, str(mem.get("city")))
                        ans = sanitize_answer(ans, allow_greet=greet)
                        history.add_assistant(ans)
                        self._push_dialog(mem, "assistant", ans)
                        mem["_started"] = True
                        self.mem_store.save(k, mem)
                        return ans

                    # есть город и площадь — считаем
                    ans = build_soundproofing_estimate(greet, str(mem.get("city")), float(mem.get("area_m2")))
                    # оставляем контекст шумоизоляции активным, чтобы можно было уточнить площадь следующим сообщением
                    mem["soundproof_pending"] = True
                    mem["soundproof_pending_ts"] = time.time()
                    mem["asked_area_soundproof"] = True
                    ans = sanitize_answer(ans, allow_greet=greet)
                    history.add_assistant(ans)
                    self._push_dialog(mem, "assistant", ans)
                    mem["_started"] = True
                    self.mem_store.save(k, mem)
                    return ans

        # ---- скидки ----
        if detect_discount_mention(user_text):
            mem["measure_offer_pending"] = True
            msg = build_discounts_message(greet, mem.get("city"))
            msg = sanitize_answer(msg, allow_greet=greet)

            history.add_assistant(msg)
            self._push_dialog(mem, "assistant", msg)

            mem["_started"] = True
            self.mem_store.save(k, mem)

            # для TG можем вернуть маркер под картинку
            if platform == "tg":
                return "__PROMO_IMAGE__\n" + msg
            return msg

        # ---- намерения ----
        # если клиент уточняет допы (люстры/карниз/углы/профиль и т.п.),
        # обычно он ждёт пересчёт — даже если не написал слово "цена".
        low_now = (user_text or "").lower().replace("ё", "е")
        spec_update = bool(
            (getattr(extracted, "extras_counts", None) and extracted.extras_counts)
            or (getattr(extracted, "extras", None) and extracted.extras)
            or re.search(r"\bугл\w*\b", low_now)
            or re.search(r"\bпарящ\w*\b", low_now)
            or re.search(r"\bплинтус\w*\b", low_now)
            or re.search(r"\bподсвет\w*\b", low_now)
        )

        price_q = detect_price_question(user_text) or bool(mem.get("calc_only")) or (spec_update and bool(mem.get("city") and mem.get("area_m2")))
        book_measure = detect_measurement_booking_intent(user_text)
        info_measure = detect_measurement_info_question(user_text)

        # отказ от замера / только расчёт
        if detect_measurement_decline(user_text) or detect_calc_only(user_text):
            mem["calc_only"] = True
            mem.pop("agreed_measurement", None)

        # если ранее предложили замер и клиент прислал "да/дата/время/адрес"
        if mem.get("measure_offer_pending") and not mem.get("agreed_measurement"):
            if detect_affirm(user_text) or book_measure or addr or vdate or vt:
                mem["agreed_measurement"] = True
                mem.pop("measure_offer_pending", None)
                mem.pop("calc_only", None)

        # явное желание записаться на замер
        if book_measure and not mem.get("calc_only"):
            mem["agreed_measurement"] = True

        # авто-согласие, если клиент сам присылает поля заявки (кроме режима "только расчёт")
        details_count = sum(
            [
                1 if mem.get("address") else 0,
                1 if mem.get("visit_date") else 0,
                1 if mem.get("visit_time") else 0,
                1 if mem.get("phone") else 0,
            ]
        )
        if details_count >= 2 and not mem.get("calc_only"):
            mem["agreed_measurement"] = True

        # ------------------- 1) расчёт -------------------
        if price_q:
            # 1) нет города / площади — просим недостающее и выходим (важно: не падать!)
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = sanitize_answer(build_need_city(greet), allow_greet=greet)

                history.add_assistant(ans)
                self._push_dialog(mem, "assistant", ans)
                mem["_started"] = True
                self.mem_store.save(k, mem)
                return ans

            if not mem.get("area_m2"):
                mem["asked_area"] = True
                ans = sanitize_answer(build_need_area(greet, mem["city"]), allow_greet=greet)

                history.add_assistant(ans)
                self._push_dialog(mem, "assistant", ans)
                mem["_started"] = True
                self.mem_store.save(k, mem)
                return ans

            # 2) считаем ориентир
            estimate = self.pricing.calculate(city=mem.get("city"), area_m2=mem.get("area_m2"), extras=_extras_for_pricing())
            if getattr(estimate, "min_price", None) is None:
                mem["asked_area"] = True
                ans = sanitize_answer(build_need_area(greet, mem["city"]), allow_greet=greet)

                history.add_assistant(ans)
                self._push_dialog(mem, "assistant", ans)
                mem["_started"] = True
                self.mem_store.save(k, mem)
                return ans

            minp = int(estimate.min_price)
            sig = _estimate_signature(mem, minp)

            # 3) если уже недавно давали такой же прайс — не повторяем прайс, а ведём диалог дальше
            if _is_duplicate_estimate(mem, sig):
                # Но если клиент только что уточнил допы — лучше вежливо подтвердить,
                # даже если сумма не изменилась.
                if spec_update:
                    prefix_lines = []
                    counts = mem.get("extras_counts") if isinstance(mem.get("extras_counts"), dict) else {}
                    if counts.get("люстра"):
                        prefix_lines.append(f"Люстры: {int(counts['люстра'])} шт.")
                    if counts.get("светильник"):
                        prefix_lines.append(f"Светильники: {int(counts['светильник'])} шт.")
                    if counts.get("карниз"):
                        prefix_lines.append(f"Карниз: ~{int(counts['карниз'])} м")
                    if mem.get("angles"):
                        prefix_lines.append(f"Углы: {mem.get('angles')}")
                    if mem.get("profile") == "floating":
                        prefix_lines.append("Профиль: парящий")
                    elif mem.get("profile") == "standard":
                        prefix_lines.append("Профиль: обычный")

                    ask_measure = not bool(mem.get("calc_only"))
                    base_msg = build_estimate(minp, city=str(mem["city"]), area_m2=float(mem["area_m2"]), ask_measure=ask_measure)
                    ans = "Поняла, учла ✅\n" + ("\n".join(prefix_lines) + "\n\n" if prefix_lines else "") + base_msg
                elif detect_materials_question(user_text):
                    ans = build_materials_vs_turnkey(greet)
                elif "дорог" in (user_text or "").lower():
                    ans = (
                        "Понимаю 😊\n"
                        "Можем сделать дешевле: матовый/сатин, простой профиль и без сложных ниш.\n"
                        "Хотите — подберу минимальный вариант под ваш бюджет. Сколько светильников планируете?"
                    )
                elif detect_measurement_booking_intent(user_text) and not mem.get("calc_only"):
                    mem["agreed_measurement"] = True
                    ans = build_measure_intro(first)
                else:
                    ans = (
                        f"{t_hello(first)}Поняла 😊\n"
                        "Если нужно — уточните допы (светильники/карниз/трубы), чтобы менеджеру было проще на замере.\n"
                        "Либо могу записать на бесплатный замер."
                    )
            else:
                _remember_estimate(mem, sig)
                # после расчёта — предлагаем замер, но если клиент явно "без замера" — не давим
                ask_measure = not bool(mem.get("calc_only"))
                mem["measure_offer_pending"] = True

                prefix_lines = []
                # красиво подхватываем уточнения клиента (чтобы не казалось, что "инфа потерялась")
                counts = mem.get("extras_counts") if isinstance(mem.get("extras_counts"), dict) else {}
                if counts.get("люстра"):
                    prefix_lines.append(f"Люстры: {int(counts['люстра'])} шт.")
                if counts.get("светильник"):
                    prefix_lines.append(f"Светильники: {int(counts['светильник'])} шт.")
                if counts.get("карниз"):
                    prefix_lines.append(f"Карниз: ~{int(counts['карниз'])} м")
                if mem.get("angles"):
                    prefix_lines.append(f"Углы: {mem.get('angles')}")
                if mem.get("profile") == "floating":
                    prefix_lines.append("Профиль: парящий")
                elif mem.get("profile") == "standard":
                    prefix_lines.append("Профиль: обычный")

                base_msg = build_estimate(minp, city=str(mem["city"]), area_m2=float(mem["area_m2"]), ask_measure=ask_measure)
                if spec_update and prefix_lines:
                    ans = "Поняла, учла допы ✅\n" + "\n".join(prefix_lines) + "\n\n" + base_msg
                else:
                    ans = base_msg

            ans = sanitize_answer(ans, allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans


        # ------------------- 2) инфо про замер -------------------
        if info_measure and not mem.get("agreed_measurement"):
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = sanitize_answer(build_need_city(greet), allow_greet=greet)
            else:
                mem["measure_offer_pending"] = True
                ans = sanitize_answer(build_measure_info(greet, mem["city"]), allow_greet=greet)

            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ------------------- 3) оформление лида на замер -------------------
        lead_flow = self._maybe_create_measure_lead_if_ready(platform, user_id, mem, meta, first=first)
        if lead_flow:
            lead_flow = sanitize_answer(lead_flow, allow_greet=greet, allow_phone_echo=True)
            history.add_assistant(lead_flow)
            self._push_dialog(mem, "assistant", lead_flow)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return lead_flow

        # ------------------- 3.5) вежливое завершение диалога -------------------
        if _looks_like_polite_close(user_text):
            ans = sanitize_answer(build_closing(mem, first=first), allow_greet=greet)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ------------------- 4) старт -------------------
        if first and not mem.get("city") and not price_q and not info_measure and not book_measure:
            ans = sanitize_answer(build_welcome(first=True), allow_greet=True)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ------------------- 5) fallback LLM (но с контекстом переписки) -------------------
        city = mem.get("city")
        promo = self.promos.get_promo(city) if city else ""

        estimate = None
        if city and mem.get("area_m2"):
            estimate = self.pricing.calculate(city=city, area_m2=mem.get("area_m2"), extras=_extras_for_pricing())

        context_parts = []
        if city:
            context_parts.append(f"Город клиента: {city}")
        if mem.get("area_m2"):
            context_parts.append(f"Площадь (из памяти): {mem['area_m2']} м²")
        if mem.get("extras"):
            context_parts.append(f"Допы (из памяти): {mem['extras']}")
        if estimate and getattr(estimate, "min_price", None) is not None:
            context_parts.append(f"Оценка: от {estimate.min_price} ₽ (ориентир, не точная цена)")
        if promo:
            context_parts.append(f"Акция: {promo}")

        # важное: добавим последние сообщения переписки (чтобы не переспрашивал)
        dialog = mem.get("_dialog") if isinstance(mem.get("_dialog"), list) else []
        last_turns = []
        for it in dialog[-10:]:
            if isinstance(it, dict) and isinstance(it.get("text"), str) and isinstance(it.get("role"), str):
                role = "Клиент" if it["role"] == "user" else "Менеджер"
                last_turns.append(f"{role}: {it['text']}")
        if last_turns:
            context_parts.append("Последние сообщения:\n" + "\n".join(last_turns))

        context_parts.append(f"Сообщение клиента: {user_text}")
        context = "\n".join(context_parts)

        msgs = history.to_ollama_messages()

        # system prompt уже есть (msgs[0]). Сначала добавим факты-контекст,
        # затем few-shot примеры, затем уже история переписки.
        insert_at = 1
        if context:
            msgs.insert(insert_at, {"role": "system", "content": context})
            insert_at += 1

        fewshot_msgs = self.fewshot.select(user_text=user_text, mem=mem, k=self.fewshot_k)
        if fewshot_msgs:
            msgs[insert_at:insert_at] = fewshot_msgs

        try:
            answer = self.ollama.chat(msgs)
        except LLMTimeoutError:
            # UX-friendly fallback on LLM timeout.
            # Важно: НЕ падать (никаких неопределённых переменных / несуществующих методов).
            service_now = mem.get("service") or ("soundproof" if mem.get("soundproof_pending") else "ceiling")

            if service_now == "soundproof":
                if mem.get("city") and mem.get("area_m2"):
                    answer = build_soundproofing_estimate(greet, str(mem.get("city")), float(mem.get("area_m2")))
                elif not mem.get("city"):
                    answer = build_soundproofing_need_city(greet)
                else:
                    answer = build_soundproofing_need_area(greet, str(mem.get("city")))
            else:
                if mem.get("city") and mem.get("area_m2"):
                    est2 = self.pricing.calculate(city=str(mem.get("city")), area_m2=float(mem.get("area_m2")), extras=_extras_for_pricing())
                    if getattr(est2, "min_price", None) is not None:
                        answer = (
                            f"Ориентир по стоимости: от {int(est2.min_price)} ₽ ✅\n"
                            f"({mem.get('city')}, {int(float(mem.get('area_m2')))} м²)\n"
                            "Если хотите — уточните допы (светильники/люстры/карниз), и я учту в заявке (точную стоимость уточним на замере)."
                        )
                    else:
                        answer = build_need_area(greet, str(mem.get("city")))
                elif not mem.get("city"):
                    answer = build_need_city(greet)
                else:
                    answer = build_need_area(greet, str(mem.get("city")))
        except Exception as e:
            answer = "Сервис ответа сейчас перегружен 😕 Сообщение получил. Если ответа не будет — напишите «+»."

        answer = sanitize_answer(answer, allow_greet=greet)
        history.add_assistant(answer)
        self._push_dialog(mem, "assistant", answer)

        mem["_started"] = True
        self.mem_store.save(k, mem)
        return answer