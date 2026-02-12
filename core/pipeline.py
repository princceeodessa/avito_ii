from core.intent import IntentDetector
from core.router import MessageRouter
from core.prompt_builder import PromptBuilder


class MessagePipeline:

    def __init__(self, llm, pricing, promotions, history):
        self.llm = llm
        self.intent_detector = IntentDetector(llm)
        self.router = MessageRouter()
        self.prompt_builder = PromptBuilder()
        self.pricing = pricing
        self.promotions = promotions
        self.history = history

    async def process(self, message: str, city: str, platform: str):

        promo_text = None

        # 1. Если первое сообщение — показываем акцию
        if self.history.is_empty():
            promo = self.promotions.get_promo(platform)
            if promo:
                promo_text = promo["text"]

        # 2. Intent
        intent = await self.intent_detector.detect(message)

        # 3. Router
        route = self.router.route(intent)

        # 4. Pricing
        pricing_context = None
        extracted = self.pricing.extract_data(message)

        if extracted["area"]:
            total = self.pricing.calculate(
                city=city,
                area=extracted["area"],
                extras=extracted["extras"]
            )

            pricing_context = f"""
Площадь: {extracted['area']} м2
Примерная стоимость: {total} руб
Обязательно скажи, что это ориентировочная стоимость.
"""

        # 5. История
        self.history.add_user(message)
        history_context = self.history.build_context()

        # 6. Строим промпт
        prompt = self.prompt_builder.build(
            message=message,
            route=route,
            history=history_context,
            pricing_context=pricing_context,
            promo_text=promo_text
        )

        # 7. Генерация
        response = await self.llm.generate(prompt)

        # 8. Сохраняем ответ
        self.history.add_assistant(response)

        return {
            "intent": intent,
            "response": response.strip()
        }
