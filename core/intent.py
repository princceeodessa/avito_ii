# core/intent.py

from typing import Literal

IntentType = Literal[
    "product_question",
    "price_question",
    "complaint",
    "positive_feedback",
    "general_question"
]


class IntentDetector:

    def __init__(self, llm):
        self.llm = llm

    async def detect(self, message: str) -> IntentType:

        prompt = f"""
Ты классификатор сообщений службы поддержки.

Категории:
- product_question
- price_question
- complaint
- positive_feedback
- general_question

Ответь ТОЛЬКО названием категории.

Сообщение:
"{message}"
"""

        response = await self.llm.generate(prompt)
        response = response.strip().lower().split()[0]

        allowed = {
            "product_question",
            "price_question",
            "complaint",
            "positive_feedback",
            "general_question"
        }

        if response not in allowed:
            return "general_question"

        return response
