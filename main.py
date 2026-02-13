from core.history import ChatHistory
from core.intent import IntentDetector
from core.extractor import extract_info
from core.pricing import PricingEngine
from core.promotions import PromotionManager
from core.response import OllamaClient


SYSTEM_PROMPT = """Ты — менеджер по натяжным потолкам. Общайся по-русски.
Правила:
1) Не называй точную итоговую цену. Можно давать только примерный диапазон.
2) Всегда предлагай бесплатный замер.
3) Если пользователь не указал площадь — попроси площадь (м²) и город.
4) Будь дружелюбным и коротким: 3–7 предложений.
5) Если есть акция — можно упомянуть в первом ответе.
"""

def build_context(user_text: str, city: str, estimate, estimate_details: str, promo: str) -> str:
    parts = []
    parts.append(f"Город клиента: {city}")
    if estimate.min_price is not None:
        parts.append(f"Оценка: примерно {estimate.min_price}–{estimate.max_price} ₽ (не точная цена)")
        if estimate_details:
            parts.append(f"Расчёт (для себя): {estimate_details}")
    else:
        parts.append("Оценка: нет данных по площади")
    if promo:
        parts.append(f"Акция: {promo}")
    parts.append(f"Сообщение клиента: {user_text}")
    return "\n".join(parts)

def main():
    city_default = "default"
    model = "qwen2.5:3b" # поменяй на свою модель в Ollama
    ollama = OllamaClient(model=model)

    pricing = PricingEngine("data/pricing_rules.json")
    promos = PromotionManager("data/promotions.json")
    intents = IntentDetector()

    history = ChatHistory(SYSTEM_PROMPT, max_messages=16)

    first_message = True

    print("Avito AI Bot (console). Напиши сообщение клиента, 'exit' для выхода.\n")

    while True:
        user_text = input("Клиент: ").strip()
        if not user_text:
            continue
        if user_text.lower() in ("exit", "quit", "выход"):
            break

        intent = intents.detect(user_text)
        extracted = extract_info(user_text)

        city = city_default  # пока так; позже можно извлекать город из текста
        promo = promos.get_promo(city) if first_message else ""

        estimate = pricing.calculate(city=city, area_m2=extracted.area_m2, extras=extracted.extras)

        context = build_context(
            user_text=user_text,
            city=city,
            estimate=estimate,
            estimate_details=estimate.details,
            promo=promo
        )

        # Добавляем user-message (с контекстом) в историю
        history.add_user(context)

        try:
            answer = ollama.chat(history.to_ollama_messages())
        except Exception as e:
            answer = f"Ошибка генерации ответа: {e}"

        history.add_assistant(answer)
        first_message = False

        print(f"\nБот: {answer}\n")

if __name__ == "__main__":
    main()
