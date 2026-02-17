# core/app_state.py
import asyncio
import re
import time
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

Правила:
1) НЕ называй точную итоговую цену. Только примерный диапазон.
2) Замер ВСЕГДА бесплатный. Замерщик приезжает с каталогами и примерами работ.
3) Если нет площади — попроси площадь (м²) и город.
4) Будь дружелюбным и коротким: 3–7 предложений.
5) Если есть акция — можно упомянуть в первом ответе.
6) НИКОГДА не говори, что ты лично приедешь или "ждёшь клиента". Ты оформляешь заявку, мастер/диспетчер подтвердит.
7) Для замера ОБЯЗАТЕЛЬНО собери: город, адрес, телефон, дату и время.
8) Не здоровайся повторно, если диалог уже начался.
9) Если клиент спросил "сколько стоит замер" — отвечай: замер бесплатный.
10) Если клиент спросил цену: в первый раз вежливо предложи замер, при повторе — можно дать примерный диапазон.
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
    tlow = text.lower()
    if re.search(r"(?<!\w)екб(?!\w)", tlow):
        return "Екатеринбург"

    tnorm = _norm_phrase(text)
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


# ------------------- parsing helpers -------------------

PHONE_RE = re.compile(r"(\+7|8)\s*\(?\d{3}\)?[\s\-]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")
AREA_HINT_RE = re.compile(r"\b(кв\.?\s?м|квм|м2|м²)\b", re.IGNORECASE)

ADDRESS_RE = re.compile(r"([А-ЯЁA-Zа-яёa-z\-\s]{3,})\s+(\d{1,4}[а-яa-z]?)", re.IGNORECASE)
ADDRESS_HINT_RE = re.compile(
    r"\b(адрес|ул\.?|улиц\w*|пр\-?т|проспект\w*|пер\.?|переулок\w*|шоссе|бульвар\w*|площад\w*|"
    r"дом|д\.|кв\.|квартира|корпус|стр\.|строен\w*|подъезд|этаж)\b",
    re.IGNORECASE
)

TIME_HHMM_RE = re.compile(r"\b([01]?\d|2[0-3]):\d{2}\b")
TIME_H_RE = re.compile(r"\bв\s*([01]?\d|2[0-3])\b")
TIME_WORD_RE = re.compile(r"\bв\s*час\b", re.IGNORECASE)

DATE_NUM_RE = re.compile(r"\b(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\b")
MONTHS = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря"
)
DATE_WORD_RE = re.compile(r"\b(\d{1,2})\s+(" + "|".join(MONTHS) + r")\b", re.IGNORECASE)


def extract_phone(text: str) -> Optional[str]:
    m = PHONE_RE.search(text)
    if not m:
        return None
    phone = re.sub(r"[^\d+]", "", m.group(0))
    if phone.startswith("8") and len(phone) == 11:
        phone = "+7" + phone[1:]
    return phone


def extract_address(text: str) -> Optional[str]:
    t = text.strip()
    if not t:
        return None

    low = t.lower()

    # защита: дата/время => не адрес
    if TIME_HHMM_RE.search(t) or DATE_NUM_RE.search(t) or DATE_WORD_RE.search(low):
        return None
    if "завтра" in low or "сегодня" in low or "после" in low:
        return None

    if AREA_HINT_RE.search(t):
        return None

    if ADDRESS_HINT_RE.search(t):
        return t

    m = ADDRESS_RE.search(t)
    if m and len(t) <= 70:
        street = m.group(1).strip().lower()
        house = m.group(2).strip()

        if street in ("в", "во", "на", "к", "ко"):
            return None

        # дом <= 31 без маркеров адреса часто путается с датой
        try:
            hn = int(re.sub(r"\D", "", house))
            if hn <= 31 and not ADDRESS_HINT_RE.search(t):
                return None
        except Exception:
            pass

        return t

    return None


def extract_visit_time(text: str) -> Optional[str]:
    low = text.lower()

    m = TIME_HHMM_RE.search(text)
    if m:
        return m.group(0)

    m = TIME_H_RE.search(text)
    if m:
        hh = int(m.group(1))
        return f"{hh:02d}:00"

    if TIME_WORD_RE.search(text):
        return "в час"

    if "днем" in low or "днём" in low:
        return "днем"
    if "вечером" in low:
        return "вечером"

    if "после" in low:
        return text.strip()

    return None


def extract_visit_date(text: str) -> Optional[str]:
    low = text.lower()
    if "сегодня" in low:
        return "сегодня"
    if "завтра" in low:
        return "завтра"

    m = DATE_NUM_RE.search(text)
    if m:
        dd, mm, yy = m.group(1), m.group(2), m.group(3)
        if yy:
            return f"{dd}.{mm}.{yy}"
        return f"{dd}.{mm}"

    m = DATE_WORD_RE.search(text)
    if m:
        return f"{m.group(1)} {m.group(2)}"

    return None


def detect_measurement_interest(text: str) -> bool:
    low = text.lower()
    triggers = [
        "на замер", "замер", "выезд", "когда сможете", "когда приедете",
        "можете приехать", "давайте замер", "запишите", "записаться",
        "сколько стоит замер", "это бесплатно", "бесплатный замер"
    ]
    return any(t in low for t in triggers)


def detect_measurement_cost_question(text: str) -> bool:
    low = text.lower()
    return ("сколько стоит замер" in low) or ("это бесплатно" in low) or ("замер бесплатный" in low)


def detect_price_question(text: str) -> bool:
    low = text.lower()
    triggers = [
        "сколько стоит", "стоимость", "цена", "по чем", "почем",
        "просчитать", "рассчитать", "примерно выйдет", "бюджет"
    ]
    return any(t in low for t in triggers)


def needs_city_now(text: str) -> bool:
    return detect_price_question(text) or detect_measurement_interest(text)


# ------------------- greeting sanitizer -------------------

GREET_RE = re.compile(
    r"^\s*(здравствуйте|добрый день|добрый вечер|доброе утро|привет|приветствую)[\s!\.,:;-]*",
    re.IGNORECASE
)


def sanitize_answer(answer: str, allow_greet: bool) -> str:
    if not answer:
        return answer
    if allow_greet:
        return answer.strip()
    answer = GREET_RE.sub("", answer, count=1)
    return answer.strip()


# ------------------- text builders -------------------

def build_measurement_pitch() -> str:
    return (
        "Замер у нас бесплатный ✅\n"
        "Замерщик выезжает с каталогами и примерами работ — подберём материал и цвет под ваш бюджет.\n"
        "Если хотите — запишу на удобные дату и время."
    )


def build_lead_confirmation(mem: Dict[str, Any]) -> str:
    return (
        "Ваша заявка на замер принята!\n\n"
        f"Город: {mem.get('city')}\n"
        f"Адрес: {mem.get('address')}\n"
        f"Телефон: {mem.get('phone')}\n"
        f"Удобное время для замера: {mem.get('visit_date')} в {mem.get('visit_time')}\n\n"
        "Замер бесплатный. Мастер приедет с каталогами и примерами работ.\n"
        "Мастер/диспетчер подтвердит все детали. Благодарю за сотрудничество!"
    )


def build_context(user_text: str, estimate, estimate_details: str, promo: str, mem: Dict[str, Any]) -> str:
    parts = [f"Город клиента: {mem.get('city')}"]

    if mem.get("area_m2"):
        parts.append(f"Площадь (из памяти): {mem['area_m2']} м²")
    if mem.get("areas"):
        parts.append(f"Площади помещений (из памяти): {mem['areas']}")
    if mem.get("extras"):
        parts.append(f"Допы (из памяти): {mem['extras']}")
    if mem.get("visit_date"):
        parts.append(f"Дата замера (из памяти): {mem['visit_date']}")
    if mem.get("visit_time"):
        parts.append(f"Время замера (из памяти): {mem['visit_time']}")
    if mem.get("address"):
        parts.append(f"Адрес (из памяти): {mem['address']}")
    if mem.get("phone"):
        parts.append(f"Телефон (из памяти): {mem['phone']}")

    if getattr(estimate, "min_price", None) is not None:
        parts.append(f"Оценка: примерно {estimate.min_price}–{estimate.max_price} ₽ (не точная цена)")
        if estimate_details:
            parts.append(f"Расчёт (для себя): {estimate_details}")
    else:
        parts.append("Оценка: нет данных по площади")

    if promo:
        parts.append(f"Акция: {promo}")

    parts.append(f"Сообщение клиента: {user_text}")
    return "\n".join(parts)


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

        # мгновенная отправка в TG колл-центра (из to_thread)
        self._loop = None
        self._notify_coro = None

        # мгновенная отправка email (из to_thread)
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
        """
        Вызывается из потока (generate_reply -> to_thread),
        поэтому планируем async-отправку в основном loop.
        """
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

    # ---------- lead flow ----------

    def _ask_missing(self, mem: Dict[str, Any], missing: List[str]) -> str:
        asked_key_map = {
            "город": "asked_city",
            "адрес": "asked_address",
            "телефон": "asked_phone",
            "дата": "asked_date",
            "время": "asked_time",
        }

        lines = [
            "Отлично, запишу на бесплатный замер ✅",
            "Замер бесплатный, мастер приедет с каталогами и примерами работ."
        ]

        for item in missing:
            asked_key = asked_key_map.get(item)
            asked_before = bool(mem.get(asked_key)) if asked_key else False

            if item == "город":
                lines.append(
                    "Уточните город, пожалуйста — чтобы правильно оформить заявку."
                    if asked_before else
                    "В каком вы городе?"
                )
                mem["asked_city"] = True

            elif item == "адрес":
                lines.append(
                    "Напишите адрес ещё раз, пожалуйста (улица, дом, квартира)."
                    if asked_before else
                    "Подскажите адрес (улица, дом, квартира)."
                )
                mem["asked_address"] = True

            elif item == "телефон":
                lines.append(
                    "Пожалуйста, напишите номер телефона — без него не смогу оформить заявку."
                    if asked_before else
                    "Уточните номер телефона для подтверждения заявки."
                )
                mem["asked_phone"] = True

            elif item == "дата":
                lines.append(
                    "Нужна дата замера (например: 16 февраля или 16.02). Напишите, пожалуйста."
                    if asked_before else
                    "На какую дату записываем замер? (например: 16 февраля или 16.02)"
                )
                mem["asked_date"] = True

            elif item == "время":
                lines.append(
                    "И ещё время (например: 16:00). Напишите, пожалуйста."
                    if asked_before else
                    "Какое удобное время? (например: 16:00)"
                )
                mem["asked_time"] = True

        return "\n".join(lines)

    def _get_lead_file_path(self, append_result) -> str:
        """
        Пытаемся получить путь к сформированному txt-файлу лида.
        Совместимо с разными версиями LeadStoreTxt.
        """
        if isinstance(append_result, str) and append_result:
            return append_result

        # часто делают атрибуты типа last_path
        for attr in ("last_path", "last_file_path", "last_filename", "last_file"):
            p = getattr(self.leads, attr, None)
            if isinstance(p, str) and p:
                return p

        return ""

    def _maybe_create_lead_if_ready(self, platform: str, user_id: str, mem: Dict[str, Any], meta: Dict[str, Any]) -> Optional[str]:
        if not mem.get("agreed_measurement"):
            return None
        if mem.get("lead_created"):
            return None

        missing: List[str] = []
        if not mem.get("city"):
            missing.append("город")
        if not mem.get("address"):
            missing.append("адрес")
        if not mem.get("phone"):
            missing.append("телефон")
        if not mem.get("visit_date"):
            missing.append("дата")
        if not mem.get("visit_time"):
            missing.append("время")

        if missing:
            msg = self._ask_missing(mem, missing)
            # важно: сохранить флаги asked_* чтобы формулировки менялись
            self.mem_store.save(self._key(platform, user_id), mem)
            return msg

        lead = {
            "ts": int(time.time()),
            "platform": platform,
            "user_id": user_id,
            "username": meta.get("username", ""),
            "name": meta.get("name", ""),
            "city": mem.get("city"),
            "area_m2": mem.get("area_m2"),
            "areas": mem.get("areas"),
            "extras": mem.get("extras"),
            "address": mem.get("address"),
            "visit_date": mem.get("visit_date"),
            "visit_time": mem.get("visit_time"),
            "phone": mem.get("phone"),
        }

        append_result = self.leads.append(lead)  # может вернуть путь к файлу
        lead_file_path = self._get_lead_file_path(append_result)

        mem["lead_created"] = True
        self.mem_store.save(self._key(platform, user_id), mem)

        # --- уведомление в TG колл-центра ---
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
            f"Площади: {lead.get('areas') or lead.get('area_m2') or '-'}\n"
            f"Допы: {lead.get('extras') or '-'}"
        )
        self.notify_now(lead_text)

        # --- email: отправляем файл и чистим его внутри email_sender ---
        if lead_file_path:
            subject = f"Заявка на замер: {lead.get('city')} / {lead.get('visit_date')} {lead.get('visit_time')}"
            body = lead_text + "\n\nФайл заявки во вложении."
            self.send_email_now(subject, body, lead_file_path)

        return build_lead_confirmation(mem)

    # ---------- LLM call ----------

    def _ollama_chat(self, msgs):
        return self.ollama.chat(msgs)

    # ---------- public API ----------

    def generate_reply(self, platform: str, user_id: str, user_text: str, meta: Optional[Dict[str, Any]] = None) -> str:
        meta = meta or {}
        history = self.get_history(platform, user_id)
        k = self._key(platform, user_id)

        mem: Dict[str, Any] = self.mem_store.load(k)

        _intent = self.intents.detect(user_text)
        extracted = extract_info(user_text)

        # --- площади ---
        if getattr(extracted, "area_m2", None):
            mem["area_m2"] = extracted.area_m2

        nums = re.findall(r"\b(\d{1,3})\b", user_text)
        if "и" in user_text and len(nums) >= 2:
            areas = [int(n) for n in nums if int(n) >= 10]
            if len(areas) >= 2:
                mem["areas"] = areas[:5]
                mem["area_m2"] = sum(areas[:5])

        if getattr(extracted, "extras", None):
            mem["extras"] = extracted.extras

        # --- город/телефон/адрес/дата/время ---
        c = extract_city(user_text)
        if c and c != mem.get("city"):
            mem["city"] = c

        ph = extract_phone(user_text)
        if ph:
            mem["phone"] = ph

        addr = extract_address(user_text)
        if addr:
            mem["address"] = addr

        vdate = extract_visit_date(user_text)
        if vdate:
            mem["visit_date"] = vdate

        vt = extract_visit_time(user_text)
        if vt:
            mem["visit_time"] = vt

        # --- замер ---
        if detect_measurement_interest(user_text):
            mem["agreed_measurement"] = True

        if detect_measurement_cost_question(user_text):
            mem.setdefault("agreed_measurement", True)

        # --- цена: 1-й раз не считаем, 2-й раз можно ---
        if detect_price_question(user_text):
            if not mem.get("price_requested_once"):
                mem["price_requested_once"] = True
                self.mem_store.save(k, mem)
                return (
                    build_measurement_pitch()
                    + "\n\nЕсли всё же нужен предварительный расчёт — напишите город и площадь (м²)."
                )

        self.mem_store.save(k, mem)

        # если нужен город, но его нет — спросим
        if needs_city_now(user_text) and not mem.get("city"):
            if not mem.get("asked_city"):
                mem["asked_city"] = True
                self.mem_store.save(k, mem)
                return "Подскажите, пожалуйста, в каком вы городе?"
            return "Уточните город, пожалуйста (например: Ижевск, Верхняя Пышма, Екатеринбург)."

        # оформление лида
        lead_flow = self._maybe_create_lead_if_ready(platform, user_id, mem, meta)
        if lead_flow:
            history.add_user(user_text)
            history.add_assistant(lead_flow)
            return lead_flow

        # обычный режим
        city = mem.get("city")
        promo = self.promos.get_promo(city) if (city and self.first_message.get(k, True)) else ""

        area_for_calc = mem.get("area_m2")
        extras_for_calc = mem.get("extras") or []
        estimate = self.pricing.calculate(city=city, area_m2=area_for_calc, extras=extras_for_calc)

        context = build_context(
            user_text=user_text,
            estimate=estimate,
            estimate_details=getattr(estimate, "details", ""),
            promo=promo,
            mem=mem
        )

        history.add_user(user_text)

        msgs = history.to_ollama_messages()
        msgs.insert(1, {"role": "system", "content": context})

        try:
            answer = self._ollama_chat(msgs)
        except Exception as e:
            err = str(e)
            if "timed out" in err.lower():
                answer = (
                    "Секунду — модель сейчас прогружается/занята 🤖\n"
                    "Повторите сообщение через 10–20 секунд.\n"
                )
            else:
                answer = f"Ошибка генерации ответа: {e}"

        allow_greet = bool(self.first_message.get(k, True))
        answer = sanitize_answer(answer, allow_greet=allow_greet)

        history.add_assistant(answer)
        self.first_message[k] = False
        return answer
