import asyncio
from pathlib import Path

from core.generator import ResponseGenerator
from core.history import ChatHistory
from core.pricing import PricingEngine
from core.promotions import PromotionManager


CITY_DEFAULT = "Ижевск"
PLATFORM_DEFAULT = "avito"  # avito / vk


class CeilingBot:

    def __init__(self, model_name: str = "mistral"):
        self.history = ChatHistory()
        self.generator = ResponseGenerator(model_name=model_name)
        self.pricing = PricingEngine("data/pricing_rules.json")
        self.promotions = PromotionManager("data/promotions.json")

    async def process_message(self, user_message: str, city: str, platform: str):

        # 1️⃣ если первое сообщение — отправляем акцию
        if self.history.is_empty():
            promo = self.promotions.get_promotion(platform)
            print(f"\n📢 Акция:\n{promo['text']}")
            print(f"🖼 Картинка: {promo['image']}\n")

        # 2️⃣ проверяем есть ли площадь
        parsed = self.pricing.extract_data(user_message)

        pricing_context = ""
        if parsed.get("area"):
            estimate = self.pricing.calculate_estimate(
                city=city,
                area=parsed["area"],
                extras=parsed.get("extras", {})
            )

            pricing_context = f"""
Примерный расчёт:
Площадь: {parsed["area"]} м²
Примерная стоимость: {estimate} руб.
Важно: сообщи, что это ориентировочная стоимость.
Обязательно предложи бесплатный замер.
"""

        # 3️⃣ добавляем сообщение в историю
        self.history.add_user_message(user_message)

        # 4️⃣ формируем полный контекст
        system_context = f"""
Ты менеджер по продаже натяжных потолков.

Правила:
- Отвечай только на русском языке
- Не давай точную итоговую стоимость
- Стоимость только ориентировочная
- Скидок нет
- Всегда предлагай записаться на бесплатный замер
- Будь вежливым и продающим

{pricing_context}
"""

        full_prompt = self.history.build_prompt(system_context)

        # 5️⃣ генерируем ответ
        response = await self.generator.generate(full_prompt)

        # 6️⃣ сохраняем ответ
        self.history.add_bot_message(response)

        return response


# ==========================
# 🧪 Консольное тестирование
# ==========================

async def main():
    bot = CeilingBot(model_name="mistral")

    city = CITY_DEFAULT
    platform = PLATFORM_DEFAULT

    print("🤖 Бот запущен. Напишите сообщение (exit для выхода)\n")

    while True:
        user_input = input("Вы: ")

        if user_input.lower() in ["exit", "quit"]:
            break

        response = await bot.process_message(
            user_message=user_input,
            city=city,
            platform=platform
        )

        print(f"\nБот: {response}\n")


if __name__ == "__main__":
    asyncio.run(main())
