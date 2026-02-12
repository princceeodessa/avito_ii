# core/pipeline.py

from core.intent import IntentDetector
from core.router import MessageRouter
from core.prompt_builder import PromptBuilder


class MessagePipeline:

    def __init__(self, llm):
        self.llm = llm
        self.intent_detector = IntentDetector(llm)
        self.router = MessageRouter()
        self.prompt_builder = PromptBuilder()

    async def process(self, message: str, context: str = "") -> dict:

        # 1. Определяем intent
        intent = await self.intent_detector.detect(message)

        # 2. Определяем стратегию
        route_config = self.router.route(intent)

        # 3. Строим промпт
        prompt = self.prompt_builder.build(
            message=message,
            route=route_config,
            context=context
        )

        # 4. Генерируем ответ
        response = await self.llm.generate(prompt)

        return {
            "intent": intent,
            "route": route_config,
            "response": response.strip()
        }
