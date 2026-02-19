# core/app_state.py
import asyncio
import re
import time
import datetime
from difflib import SequenceMatcher
from typing import Optional, Dict, Any, List, Callable, Awaitable

from core.memory_store import FileKVStore
from core.lead_store import LeadStoreTxt

from core.extractor import extract_info
from core.history import ChatHistory
from core.intent import IntentDetector
from core.pricing import PricingEngine
from core.promotions import PromotionManager
from core.response import OllamaClient


SYSTEM_PROMPT = """Ты — менеджер по натяжным потолкам. Общайся по-русски.

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
6) Для замера собери: город, адрес, телефон, дату и время.
7) Не здоровайся повторно, если диалог уже начался.
"""


# ------------------- supported cities -------------------

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


# ------------------- normalization / fuzzy helpers -------------------

def _compress_repeats(s: str) -> str:
    return re.sub(r"(.)\1+", r"\1", s)


_CASE_ENDINGS = (
    "ыми", "ими", "ого", "ему", "ому", "ами", "ями", "ях", "ах", "ью", "ией",
    "ый", "ий", "ая", "яя", "ое", "ее", "ую", "юю", "ым", "им", "ом", "ем", "ых", "их",
    "а", "я", "у", "ю", "е", "и", "о"
)


def _stem_ru_word(w: str) -> str:
    w = w.lower()
    w = w.replace("ё", "е").replace("—", "-").replace("–", "-")
    w = re.sub(r"[^a-zа-я\-]+", "", w, flags=re.IGNORECASE)
    w = _compress_repeats(w)
    for suf in _CASE_ENDINGS:
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            w = w[:-len(suf)]
            break
    return w


def _norm_phrase(phrase: str) -> str:
    phrase = phrase.replace("ё", "е").replace("—", "-").replace("–", "-")
    phrase = re.sub(r"\s+", " ", phrase).strip()
    phrase = phrase.replace("-", " ")
    words = [w for w in phrase.split() if w]
    words = [_stem_ru_word(w) for w in words if w]
    words = [w for w in words if w]
    return " ".join(words).strip()


NORM_CITIES = [(city, _norm_phrase(city)) for city in SUPPORTED_CITIES]


def extract_city(text: str) -> Optional[str]:
    tlow = (text or "").lower()
    if re.search(r"(?<!\w)екб(?!\w)", tlow):
        return "Екатеринбург"

    tnorm = _norm_phrase(text or "")
    if not tnorm:
        return None

    words = tnorm.split()
    windows: List[str] = []
    for n in (1, 2, 3):
        for i in range(0, len(words) - n + 1):
            windows.append(" ".join(words[i:i + n]))

    best_city = None
    best_score = 0.0

    for city, cnorm in NORM_CITIES:
        if not cnorm:
            continue
        for w in windows:
            score = SequenceMatcher(None, w, cnorm).ratio()
            if score > best_score:
                best_score = score
                best_city = city

    if best_city and best_score >= 0.86:
        return best_city
    return None


# ------------------- discounts -------------------

DISCOUNT_RE = re.compile(
    r"\b(скидк\w*|акци\w*|подар\w*|промокод\w*|купон\w*|бонус\w*|распродаж\w*)\b",
    re.IGNORECASE
)

PROMO_DISCOUNTS_TEXT = (
    "На каждый второй потолок (меньший по площади) полотно идет в подарок😇🌸\n"
    "Если кто-то из ваших близких участник СВО или работник оборонного предприятия, то и 3е полотно будет в подарок! 🥰\n"
    "Также скидка на освещение будет от нашего отдела до 50% 😊\n\n"
    "Все индивидуальные предложения специалист рассмотрит с Вами по месту ☀️📝"
)


def detect_discount_mention(text: str) -> bool:
    return bool(DISCOUNT_RE.search(text or ""))


# ------------------- parsing helpers -------------------

PHONE_RE = re.compile(r"(?<!\d)(?:\+7|7|8)\s*\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
PHONE_ANY_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s\(\)]{8,}\d)(?!\d)")

AREA_HINT_RE = re.compile(r"\b(кв\.?\s?м|квм|м2|м²|квадрат\w*|площад\w*)\b", re.IGNORECASE)

ADDRESS_RE = re.compile(r"([А-ЯЁA-Zа-яёa-z\-\s\.,]{3,})\s+(\d{1,4}[а-яa-z]?)", re.IGNORECASE)
ADDRESS_HINT_RE = re.compile(
    r"\b(адрес|ул\.?|улиц\w*|пр\-?т|проспект\w*|пер\.?|переулок\w*|шоссе|бульвар\w*|площад\w*|"
    r"дом|д\.|кв\.|квартира|корпус|стр\.|строен\w*|подъезд|этаж)\b",
    re.IGNORECASE
)

TIME_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3])[:.]\d{2}\b")
TIME_PLAIN_H_RE = re.compile(r"^\s*([01]?\d|2[0-3])\s*$")
TIME_H_RE = re.compile(r"\bв\s*([01]?\d|2[0-3])\b", re.IGNORECASE)

DATE_NUM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
)
DATE_WORD_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.IGNORECASE)

MEASURE_DECLINE_RE = re.compile(
    r"\b(без\s+замер\w*|замер\s+не\s+нужен|не\s+нужен\s+замер|не\s+надо\s+замер\w*|"
    r"не\s+хочу\s+замер\w*|не\s+приезжайте|без\s+выезда)\b",
    re.IGNORECASE
)

CALC_ONLY_RE = re.compile(
    r"\b(без\s+замер\w*|просто\s+(посчит|счит|просчит|рассчит)|только\s+расчет|только\s+расч[её]т|"
    r"предварит\w*\s+расчет|предварит\w*\s+расч[её]т)\b",
    re.IGNORECASE
)

PHONE_REFUSAL_RE = re.compile(
    r"\b(номер\s+не\s+хочу|не\s+хочу\s+оставлять|без\s+номера|не\s+оставлю|не\s+буду\s+оставлять|"
    r"не\s+звоните|звонить\s+не\s+надо|без\s+звонков|не\s+нужно\s+звонить)\b",
    re.IGNORECASE
)

AFFIRM_RE = re.compile(r"\b(да|давайте|ок|хорошо|можно|запишите|записывайте|хочу|согласен|согласна)\b", re.IGNORECASE)
NEG_RE = re.compile(r"\b(нет|не\s*надо|не\s*нужно|потом|позже)\b", re.IGNORECASE)

MEASURE_BOOK_TRIG_RE = re.compile(
    r"\b(запиш|записат|давайте\s+замер|на\s+замер|выехать|когда\s+сможете|когда\s+приедете|"
    r"завтра\s+можете|сегодня\s+можете)\b",
    re.IGNORECASE
)

MEASURE_INFO_TRIG_RE = re.compile(
    r"\b(выезжа\w*|приезжа\w*|делаете\s+замер|замер\s+бесплат\w*|сколько\s+стоит\s+замер)\b",
    re.IGNORECASE
)


def detect_affirm(text: str) -> bool:
    return bool(AFFIRM_RE.search(text or "")) and not bool(re.search(r"\bне\b", (text or "").lower()))


def detect_neg(text: str) -> bool:
    return bool(NEG_RE.search(text or ""))


def detect_measurement_decline(text: str) -> bool:
    return bool(MEASURE_DECLINE_RE.search(text or ""))


def detect_calc_only(text: str) -> bool:
    return bool(CALC_ONLY_RE.search(text or ""))


def detect_phone_refusal(text: str) -> bool:
    return bool(PHONE_REFUSAL_RE.search(text or ""))


def detect_measurement_booking_intent(text: str) -> bool:
    if detect_measurement_decline(text):
        return False
    return bool(MEASURE_BOOK_TRIG_RE.search(text or ""))


def detect_measurement_info_question(text: str) -> bool:
    if detect_measurement_decline(text):
        return False
    return bool(MEASURE_INFO_TRIG_RE.search(text or ""))


def detect_measurement_cost_question(text: str) -> bool:
    low = (text or "").lower()
    return ("сколько стоит замер" in low) or ("замер бесплат" in low) or ("это бесплатно" in low)


def detect_price_question(text: str) -> bool:
    low = (text or "").lower()
    triggers = [
        "сколько стоит", "стоимость", "цена", "по чем", "почем",
        "просчитать", "рассчитать", "посчитать", "посчитайте",
        "примерно", "ориентир", "сколько выйдет", "предварительно"
    ]
    return any(t in low for t in triggers)


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


def extract_address(text: str) -> Optional[str]:
    t = (text or "").strip()
    if not t:
        return None

    low = t.lower()

    # дата/время => не адрес
    if TIME_HHMM_RE.search(t) or DATE_NUM_RE.search(t) or DATE_WORD_RE.search(low):
        return None

    # площадь => не адрес
    if AREA_HINT_RE.search(t):
        return None

    # есть маркер адреса, но нет цифр — не берём
    if ADDRESS_HINT_RE.search(t) and not re.search(r"\d", t):
        return None

    m = ADDRESS_RE.search(t)
    if m:
        street = m.group(1).strip().lower().strip(" ,.-")
        if street in ("в", "во", "на", "к", "ко"):
            return None
        return t.strip()

    return None


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

    m = TIME_H_RE.search(text or "")
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


# ------------------- sanitizer -------------------

GREET_RE = re.compile(
    r"^\s*(здравствуйте|добрый день|добрый вечер|доброе утро|привет|приветствую)[\s!\.,:;-]*",
    re.IGNORECASE
)

BAD_WAIT_RE = re.compile(r"(?i)\b(жд[её]м\s+вас|приходите|ожидаем\s+вас|встреча\s+ждет|встреча\s+жд[её]т)\b")
BAD_I_RE = re.compile(r"(?i)\bя\s+(приеду|выех\w*|проведу\s+замер|замерю)\b")
BAD_CALL_RE = re.compile(r"(?i)\b(позвоню|позвоним|созвон|позвоните|звоните|наберите)\b[^\n]*")
BAD_WE_MASTER_RE = re.compile(r"(?i)\bмы\s+мастер\b")
BAD_SOON_RE = re.compile(r"(?i)\bскоро\s+отвечу\b")


def sanitize_answer(answer: str, allow_greet: bool, allow_phone_echo: bool = False) -> str:
    if not answer:
        return answer
    s = answer.strip()

    if not allow_greet:
        s = GREET_RE.sub("", s, count=1).strip()

    s = BAD_WE_MASTER_RE.sub("мастер", s)
    s = BAD_WAIT_RE.sub("мастер приедет", s)
    s = BAD_I_RE.sub("мастер приедет", s)
    s = BAD_SOON_RE.sub("", s)

    # убрать любые "позвоню/позвоните..." — чтобы не было выдуманного созвона
    s = BAD_CALL_RE.sub("", s).strip()

    # убрать любые телефоны из ответа (кроме подтверждения лида)
    if not allow_phone_echo:
        s = PHONE_ANY_RE.sub("", s)

    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    return s


# ------------------- text builders -------------------

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


def build_need_area(first: bool, city: str) -> str:
    return (
        f"{t_hello(first)}{city} — понял(а).\n"
        "Чтобы назвать ориентир по стоимости, подскажите площадь (м²). Можно примерно."
    )


def build_discounts_message(first: bool, city: Optional[str]) -> str:
    city_line = f"В {city} работаем.\n" if city else ""
    return (
        f"{t_hello(first)}{city_line}"
        "У нас сейчас есть такие скидки:\n\n"
        f"{PROMO_DISCOUNTS_TEXT}\n\n"
        "Если хотите — подскажите город и площадь (м²), сориентирую по стоимости.\n"
        "Замер бесплатный — мастер приедет с каталогами и образцами."
    )


def build_estimate(min_price: int) -> str:
    return (
        f"Ориентир по стоимости: от {min_price} ₽ ✅\n"
        "Точная цена зависит от углов, светильников и выбранного профиля/материала.\n"
        "Если захотите уточнить точнее — замер бесплатный: мастер приедет с каталогами и образцами. Записать вас?"
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
    Адаптеры (tg/vk/avito/max) просто вызывают generate_reply().
    """

    def __init__(self, model: str, ollama_timeout: int = 240):
        self.ollama_timeout = ollama_timeout
        self.ollama = OllamaClient(model=model)

        self.pricing = PricingEngine("data/pricing_rules.json")
        self.promos = PromotionManager("data/promotions.json")
        self.intents = IntentDetector()

        self.histories: Dict[str, ChatHistory] = {}
        self.first_message: Dict[str, bool] = {}

        self.mem_store = FileKVStore(dir_path="data/memory")
        self.leads = LeadStoreTxt(path="data/leads.txt")

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

    def get_history(self, platform: str, user_id: str) -> ChatHistory:
        k = self._key(platform, user_id)
        if k not in self.histories:
            self.histories[k] = ChatHistory(SYSTEM_PROMPT, max_messages=16)
            self.first_message[k] = True
        return self.histories[k]

    def reset_all(self, platform: str, user_id: str) -> None:
        k = self._key(platform, user_id)
        self.histories[k] = ChatHistory(SYSTEM_PROMPT, max_messages=16)
        self.first_message[k] = True
        self.mem_store.reset(k)

    # ---------- lead helpers ----------

    def _get_lead_file_path(self, append_result) -> str:
        if isinstance(append_result, str) and append_result:
            return append_result
        for attr in ("last_path", "last_file_path", "last_filename", "last_file"):
            p = getattr(self.leads, attr, None)
            if isinstance(p, str) and p:
                return p
        return ""

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

    def _maybe_create_measure_lead_if_ready(self, platform: str, user_id: str, mem: Dict[str, Any], meta: Dict[str, Any], first: bool) -> Optional[str]:
        if not mem.get("agreed_measurement"):
            return None
        if mem.get("lead_created"):
            return None

        msg = self._ask_next_measure_field(mem, first=first)
        if msg:
            self.mem_store.save(self._key(platform, user_id), mem)
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
            "areas": mem.get("areas"),
            "extras": mem.get("extras"),
            "address": mem.get("address"),
            "visit_date": resolve_relative_date(mem.get("visit_date") or ""),
            "visit_time": mem.get("visit_time"),
            "phone": mem.get("phone"),
        }

        append_result = self.leads.append(lead)
        lead_file_path = self._get_lead_file_path(append_result)

        mem["lead_created"] = True
        self.mem_store.save(self._key(platform, user_id), mem)

        uname = f"@{lead['username']}" if lead.get("username") else "-"
        lead_text = (
            "🆕 Новая заявка на бесплатный замер\n"
            f"Платформа: {lead['platform']}\n"
            f"User ID: {lead['user_id']}\n"
            f"Username: {uname}\n"
            f"Имя: {lead.get('name') or '-'}\n"
            f"Город: {lead.get('city') or '-'}\n"
            f"Адрес: {lead.get('address') or '-'}\n"
            f"Дата: {lead.get('visit_date') or '-'}\n"
            f"Время: {lead.get('visit_time') or '-'}\n"
            f"Телефон: {lead.get('phone') or '-'}\n"
            f"Площадь: {lead.get('area_m2') or lead.get('areas') or '-'}\n"
            f"Допы: {lead.get('extras') or '-'}"
        )
        self.notify_now(lead_text)

        if lead_file_path:
            subject = f"Заявка на замер: {lead.get('city')} / {lead.get('visit_date')} {lead.get('visit_time')}"
            body = lead_text + "\n\nФайл заявки во вложении."
            self.send_email_now(subject, body, lead_file_path)

        return build_lead_confirmation(mem)

    def _maybe_create_hot_refusal_lead(self, platform: str, user_id: str, mem: Dict[str, Any], meta: Dict[str, Any]) -> None:
        """
        Горячий лид: отказ от замера, но интерес к цене/расчёту. Телефон НЕ обязателен.
        """
        if not mem.get("hot_refusal_lead"):
            return
        if mem.get("hot_refusal_lead_created"):
            return
        if not mem.get("city") or not mem.get("area_m2"):
            return

        lead = {
            "ts": int(time.time()),
            "platform": platform,
            "user_id": user_id,
            "username": meta.get("username", ""),
            "name": meta.get("name", ""),
            "lead_kind": "hot_refusal",
            "status": "refused_measurement_high_interest",
            "city": mem.get("city"),
            "area_m2": mem.get("area_m2"),
            "extras": mem.get("extras"),
            "phone": mem.get("phone"),
            "note": "Клиент отказался от замера, но просил ориентир/расчёт.",
        }

        append_result = self.leads.append(lead)
        lead_file_path = self._get_lead_file_path(append_result)

        mem["hot_refusal_lead_created"] = True
        self.mem_store.save(self._key(platform, user_id), mem)

        uname = f"@{lead['username']}" if lead.get("username") else "-"
        phone_txt = lead.get("phone") or "не оставил"
        lead_text = (
            "🔥 Горячий интерес (без замера)\n"
            f"Платформа: {lead['platform']}\n"
            f"User ID: {lead['user_id']}\n"
            f"Username: {uname}\n"
            f"Имя: {lead.get('name') or '-'}\n"
            f"Город: {lead.get('city') or '-'}\n"
            f"Телефон: {phone_txt}\n"
            f"Площадь: {lead.get('area_m2') or '-'}\n"
            f"Допы: {lead.get('extras') or '-'}\n"
            f"Комментарий: {lead.get('note')}"
        )
        self.notify_now(lead_text)

        if lead_file_path:
            subject = f"Горячий лид (без замера): {lead.get('city')} / {phone_txt}"
            body = lead_text + "\n\nФайл заявки во вложении."
            self.send_email_now(subject, body, lead_file_path)

    # ---------- public API ----------

    def generate_reply(self, platform: str, user_id: str, user_text: str, meta: Optional[Dict[str, Any]] = None) -> str:
        meta = meta or {}
        history = self.get_history(platform, user_id)
        k = self._key(platform, user_id)
        first = bool(self.first_message.get(k, True))

        mem: Dict[str, Any] = self.mem_store.load(k)

        # --- извлечение ---
        extracted = extract_info(user_text)

        if getattr(extracted, "area_m2", None):
            mem["area_m2"] = extracted.area_m2
        if getattr(extracted, "extras", None):
            mem["extras"] = extracted.extras

        # доп. эвристика площади: ловим число даже без "кв.м"
        cleaned = PHONE_ANY_RE.sub(" ", user_text or "")
        nums = [int(n) for n in re.findall(r"\b(\d{1,3})\b", cleaned)]
        nums = [n for n in nums if 1 <= n <= 300]
        if nums and (AREA_HINT_RE.search(cleaned) or detect_price_question(cleaned) or mem.get("asked_area")):
            mem["area_m2"] = float(max(nums))

        c = extract_city(user_text or "")
        if c:
            mem["city"] = c

        if detect_phone_refusal(user_text or ""):
            mem["no_phone"] = True
        ph = extract_phone(user_text or "")
        if ph:
            mem["phone"] = ph
            mem.pop("no_phone", None)

        addr = extract_address(user_text or "")
        if addr:
            mem["address"] = addr

        vdate = extract_visit_date(user_text or "")
        if vdate:
            mem["visit_date"] = vdate

        vt = extract_visit_time(user_text or "")
        if vt:
            mem["visit_time"] = vt

        # --- СКИДКИ/АКЦИИ: текст + (в TG) картинка ---
        if detect_discount_mention(user_text or ""):
            mem["measure_offer_pending"] = True
            self.mem_store.save(k, mem)

            msg = build_discounts_message(first, mem.get("city"))
            self.first_message[k] = False

            # Для Telegram: вернём маркер, адаптер отправит data/tg.png
            if platform == "tg":
                return "__PROMO_IMAGE__\n" + msg

            return msg

        # --- намерения ---
        price_q = detect_price_question(user_text or "")
        book_measure = detect_measurement_booking_intent(user_text or "")
        info_measure = detect_measurement_info_question(user_text or "") or detect_measurement_cost_question(user_text or "")

        # отказ от замера / только расчёт
        if detect_measurement_decline(user_text or "") or detect_calc_only(user_text or ""):
            mem["calc_only"] = True
            mem["hot_refusal_lead"] = True
            mem.pop("agreed_measurement", None)

        # если ранее предложили замер и клиент прислал "да/дата/время/адрес"
        if mem.get("measure_offer_pending") and not mem.get("agreed_measurement"):
            if detect_affirm(user_text or "") or book_measure or addr or vdate or vt:
                mem["agreed_measurement"] = True
                mem.pop("measure_offer_pending", None)
                mem.pop("calc_only", None)

        # явное желание записаться на замер
        if book_measure and not mem.get("calc_only"):
            mem["agreed_measurement"] = True

        # авто-согласие, если клиент сам присылает поля заявки (кроме режима "только расчёт")
        details_count = sum([
            1 if mem.get("address") else 0,
            1 if mem.get("visit_date") else 0,
            1 if mem.get("visit_time") else 0,
            1 if mem.get("phone") else 0,
        ])
        if details_count >= 2 and not mem.get("calc_only"):
            mem["agreed_measurement"] = True

        # ------------------- 1) расчёт (без телефона) -------------------
        if price_q or mem.get("calc_only"):
            if not mem.get("city"):
                mem["asked_city"] = True
                self.mem_store.save(k, mem)
                self.first_message[k] = False
                return sanitize_answer(build_need_city(first), allow_greet=first)

            if not mem.get("area_m2"):
                mem["asked_area"] = True
                self.mem_store.save(k, mem)
                self.first_message[k] = False
                return sanitize_answer(build_need_area(first, mem["city"]), allow_greet=first)

            estimate = self.pricing.calculate(
                city=mem.get("city"),
                area_m2=mem.get("area_m2"),
                extras=mem.get("extras") or []
            )
            self.mem_store.save(k, mem)

            if getattr(estimate, "min_price", None) is not None:
                # после расчёта — ВСЕГДА предлагаем замер
                mem["measure_offer_pending"] = True
                self.mem_store.save(k, mem)

                # если он "без замера" — создаём горячий лид
                if mem.get("calc_only"):
                    self._maybe_create_hot_refusal_lead(platform, user_id, mem, meta)

                ans = build_estimate(int(estimate.min_price))
                self.first_message[k] = False
                return sanitize_answer(ans, allow_greet=first)

        # ------------------- 2) инфо про замер/выезд (не анкета сразу) -------------------
        if info_measure and not mem.get("agreed_measurement"):
            if not mem.get("city"):
                mem["asked_city"] = True
                self.mem_store.save(k, mem)
                self.first_message[k] = False
                return sanitize_answer(build_need_city(first), allow_greet=first)

            mem["measure_offer_pending"] = True
            self.mem_store.save(k, mem)
            self.first_message[k] = False
            return sanitize_answer(build_measure_info(first, mem["city"]), allow_greet=first)

        # ------------------- 3) оформление лида на замер -------------------
        lead_flow = self._maybe_create_measure_lead_if_ready(platform, user_id, mem, meta, first=first)
        if lead_flow:
            history.add_user(user_text)
            history.add_assistant(lead_flow)
            self.first_message[k] = False
            return sanitize_answer(lead_flow, allow_greet=first, allow_phone_echo=True)

        # ------------------- 4) старт / обычное общение -------------------
        if first and not mem.get("city") and not price_q and not info_measure and not book_measure:
            self.mem_store.save(k, mem)
            self.first_message[k] = False
            return sanitize_answer(build_welcome(first=True), allow_greet=True)

        # fallback: LLM (с сильной чисткой)
        city = mem.get("city")
        promo = self.promos.get_promo(city) if (city and first) else ""
        estimate = self.pricing.calculate(city=city, area_m2=mem.get("area_m2"), extras=mem.get("extras") or [])

        context_parts = [f"Город клиента: {mem.get('city')}"]
        if mem.get("area_m2"):
            context_parts.append(f"Площадь (из памяти): {mem['area_m2']} м²")
        if mem.get("extras"):
            context_parts.append(f"Допы (из памяти): {mem['extras']}")
        if getattr(estimate, "min_price", None) is not None:
            context_parts.append(f"Оценка: от {estimate.min_price} ₽ (ориентир, не точная цена)")
        if promo:
            context_parts.append(f"Акция: {promo}")
        context_parts.append(f"Сообщение клиента: {user_text}")
        context = "\n".join(context_parts)

        history.add_user(user_text)
        msgs = history.to_ollama_messages()
        msgs.insert(1, {"role": "system", "content": context})

        try:
            answer = self.ollama.chat(msgs)
        except Exception as e:
            err = str(e)
            if "timed out" in err.lower():
                answer = "Похоже, сервис сейчас занят 🤖 Попробуйте повторить сообщение через 10–20 секунд."
            else:
                answer = f"Ошибка генерации ответа: {e}"

        answer = sanitize_answer(answer, allow_greet=first)
        history.add_assistant(answer)
        self.first_message[k] = False
        self.mem_store.save(k, mem)
        return answer
#