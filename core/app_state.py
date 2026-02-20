# core/app_state.py
import asyncio
import datetime
import os
import re
import time
from difflib import SequenceMatcher
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from core.extractor import extract_info
from core.history import ChatHistory
from core.intent import IntentDetector
from core.lead_store import LeadStoreTxt
from core.memory_store import FileKVStore
from core.pricing import PricingEngine
from core.promotions import PromotionManager
from core.response import OllamaClient

SYSTEM_PROMPT = """Ты — менеджер по натяжным потолкам.
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
5) Если есть акция — можно упомянуть в первом ответе.
6) Для замера собери: город, адрес, дату, время, телефон.
7) Не здоровайся повторно, если диалог уже начался.
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

def extract_city(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None

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
    m = CITY_CANDIDATE_RE.search(t)
    if m:
        cand = m.group(1).strip(" ,.!?:;()[]{}\"'").strip()
        cand = re.sub(r"\s+", " ", cand)
        if 2 <= len(cand) <= 40:
            return cand
    # если сообщение — это просто одно слово (часто так пишут город)
    if re.fullmatch(r"[A-Za-zА-Яа-яЁё\-]{3,30}", t):
        return t
    return None


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

AREA_HINT_RE = re.compile(r"\b(м2|м²|кв\.?\s*м|квадрат)\b", re.IGNORECASE)

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
    # если есть подсказки адреса — берём
    if ADDRESS_HINT_RE.search(t) and re.search(r"\d", t):
        return t
    # или если просто "ворошилова 4"
    if re.search(r"[А-Яа-яЁё]", t) and re.search(r"\d", t) and len(t) <= 80:
        return t
    return None


# ------------------- sanitizer (чтобы Avito выглядел прилично) -------------------
GREET_RE = re.compile(r"^\s*(здравствуйте|добрый день|добрый вечер|доброе утро|привет|приветствую)[\s!\.,:;-]*", re.IGNORECASE)
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
    return "Здравствуйте! " if first else ""

def build_welcome(first: bool) -> str:
    return (
        f"{t_hello(first)}Будем рады помочь 😊\n"
        "Подскажите, пожалуйста, ваш город и примерную площадь (м²).\n"
        "Замер бесплатный — мастер приедет с каталогами и образцами."
    )

def build_need_city(first: bool) -> str:
    return f"{t_hello(first)}Подскажите, пожалуйста, в каком вы городе?"

def build_city_not_supported(first: bool, city_candidate: str) -> str:
    return (
        f"{t_hello(first)}Понял(а) вас. Пока, к сожалению, не работаем в городе «{city_candidate}».\n"
        "Сейчас выезжаем по Ижевску и Екатеринбургу (и ближайшим районам).\n"
        "Если объект в этих городах — напишите город и площадь (м²), сориентирую по стоимости."
    )

def build_need_area(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}{city} — понял(а).\n"
        "Чтобы назвать ориентир по стоимости, подскажите площадь (м²). Можно примерно."
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
        f"Ориентир по стоимости: от {min_price} ₽ ✅\n"
        f"({city}, {area_m2:g} м²)\n"
        "Точная цена зависит от углов, светильников и выбранного профиля/материала.\n"
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
        "Мастер/диспетчер подтвердит детали. Если нужно поменять время — просто напишите."
    )


# ------------------- AppState -------------------
EmailSender = Callable[[str, str, str], Awaitable[bool]]


class AppState:
    """
    Единое ядро: память + история + лиды + LLM.
    Адаптеры (tg/avito/...) просто вызывают generate_reply().
    """

    def __init__(self, model: str, ollama_timeout: int = 240):
        self.ollama_timeout = int(ollama_timeout)
        self.ollama = OllamaClient(model=model, timeout=self.ollama_timeout)

        self.pricing = PricingEngine("data/pricing_rules.json")
        self.promos = PromotionManager("data/promotions.json")
        self.intents = IntentDetector()
        self.dialog_log_dir = os.getenv("DIALOG_LOG_DIR", "data/dialog_logs")
        os.makedirs(self.dialog_log_dir, exist_ok=True)
        self.mem_store = FileKVStore(dir_path="data/memory")
        self.leads = LeadStoreTxt(path="data/leads.txt")

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
            h = ChatHistory(SYSTEM_PROMPT, max_messages=20)
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
        self.histories[k] = ChatHistory(SYSTEM_PROMPT, max_messages=20)
        self.mem_store.reset(k)

    def _append_dialog_log(self, platform: str, user_id: str, role: str, text: str) -> None:
        try:
            safe_user = "".join(ch for ch in str(user_id) if ch.isalnum() or ch in ("-", "_"))[:80]
            self._append_dialog_log(platform, user_id, role, text)
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

        if not mem.get("phone"):
            mem["asked_phone"] = True
            return f"{intro}\nИ номер телефона для подтверждения заявки (можно 8XXXXXXXXXX)."

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

        # пишем входящее в историю/память ВСЕГДА (важно для Avito)
        self._push_dialog(mem, "user", user_text)
        history.add_user(user_text)

        # ---- извлечение площади/допов ----
        extracted = extract_info(user_text)
        if getattr(extracted, "area_m2", None):
            mem["area_m2"] = extracted.area_m2
        if getattr(extracted, "extras", None):
            mem["extras"] = extracted.extras

        # эвристика площади: ловим число даже без "кв.м"
        cleaned = PHONE_ANY_RE.sub(" ", user_text)
        nums = [int(n) for n in re.findall(r"\b(\d{1,3})\b", cleaned)]
        nums = [n for n in nums if 1 <= n <= 300]
        if nums and (AREA_HINT_RE.search(cleaned) or detect_price_question(cleaned) or mem.get("asked_area")):
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

        if (hot_intent or hot_fields >= 2 or hot_discount) and not mem.get("hot_notified"):
            mem["hot_notified"] = True
            link = meta.get("chat_url") or meta.get("item_url") or "https://www.avito.ru/profile/messenger"
            self.notify_now(
                "🔥 Горячий интерес (Авито)\n"
                f"Chat ID: {user_id}\n"
                f"Город: {mem.get('city') or '-'}\n"
                f"Площадь: {mem.get('area_m2') or '-'}\n"
                f"Адрес: {mem.get('address') or '-'}\n"
                f"Дата: {mem.get('visit_date') or '-'}\n"
                f"Время: {mem.get('visit_time') or '-'}\n"
                f"Телефон: {mem.get('phone') or '-'}\n"
                f"Ссылка: {link}\n"
                f"Текст: {user_text}"
            )
        # ---- unsupported city short-circuit ----
        if mem.get("unsupported_city_candidate") and not mem.get("city"):
            ans = build_city_not_supported(first, str(mem["unsupported_city_candidate"]))
            ans = sanitize_answer(ans, allow_greet=first)
            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ---- скидки ----
        if detect_discount_mention(user_text):
            mem["measure_offer_pending"] = True
            msg = build_discounts_message(first, mem.get("city"))
            msg = sanitize_answer(msg, allow_greet=first)

            history.add_assistant(msg)
            self._push_dialog(mem, "assistant", msg)

            mem["_started"] = True
            self.mem_store.save(k, mem)

            # для TG можем вернуть маркер под картинку
            if platform == "tg":
                return "__PROMO_IMAGE__\n" + msg
            return msg

        # ---- намерения ----
        price_q = detect_price_question(user_text) or bool(mem.get("calc_only"))
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
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = sanitize_answer(build_need_city(first), allow_greet=first)
            elif not mem.get("area_m2"):
                mem["asked_area"] = True
                ans = sanitize_answer(build_need_area(first, mem["city"]), allow_greet=first)
            else:
                estimate = self.pricing.calculate(
                    city=mem.get("city"),
                    area_m2=mem.get("area_m2"),
                    extras=mem.get("extras") or [],
                )
                if getattr(estimate, "min_price", None) is not None:
                    # после расчёта — предлагаем замер, но если клиент явно "без замера" — спрашиваем мягко
                    ask_measure = not bool(mem.get("calc_only"))
                    mem["measure_offer_pending"] = True
                    ans = build_estimate(
                        int(estimate.min_price),
                        city=str(mem["city"]),
                        area_m2=float(mem["area_m2"]),
                        ask_measure=ask_measure,
                    )
                    ans = sanitize_answer(ans, allow_greet=first)
                else:
                    ans = sanitize_answer(build_need_area(first, mem["city"]), allow_greet=first)

            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ------------------- 2) инфо про замер -------------------
        if info_measure and not mem.get("agreed_measurement"):
            if not mem.get("city"):
                mem["asked_city"] = True
                ans = sanitize_answer(build_need_city(first), allow_greet=first)
            else:
                mem["measure_offer_pending"] = True
                ans = sanitize_answer(build_measure_info(first, mem["city"]), allow_greet=first)

            history.add_assistant(ans)
            self._push_dialog(mem, "assistant", ans)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return ans

        # ------------------- 3) оформление лида на замер -------------------
        lead_flow = self._maybe_create_measure_lead_if_ready(platform, user_id, mem, meta, first=first)
        if lead_flow:
            lead_flow = sanitize_answer(lead_flow, allow_greet=first, allow_phone_echo=True)
            history.add_assistant(lead_flow)
            self._push_dialog(mem, "assistant", lead_flow)
            mem["_started"] = True
            self.mem_store.save(k, mem)
            return lead_flow

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
            estimate = self.pricing.calculate(city=city, area_m2=mem.get("area_m2"), extras=mem.get("extras") or [])

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
        msgs.insert(1, {"role": "system", "content": context})

        try:
            answer = self.ollama.chat(msgs)
        except Exception as e:
            err = str(e).lower()
            if "timed out" in err or "timeout" in err:
                answer = "Похоже, сервис сейчас занят. Попробуйте повторить сообщение через 10–20 секунд."
            else:
                answer = f"Ошибка генерации ответа: {e}"

        answer = sanitize_answer(answer, allow_greet=first)
        history.add_assistant(answer)
        self._push_dialog(mem, "assistant", answer)

        mem["_started"] = True
        self.mem_store.save(k, mem)
        return answer