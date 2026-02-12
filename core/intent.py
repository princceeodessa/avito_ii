from typing import Literal

IntentType = Literal[
    "price_question",
    "booking",
    "complaint",
    "general"
]


class IntentDetector:

    def __init__(self, llm):
        self.llm = llm

    async def detect(self, message: str) -> IntentType:

        prompt = f"""
Ты классификатор диалогов по продаже натяжных потолков.

Категории:
- price_question (если спрашивают цену, расчет, стоимость)
- booking (если хотят записаться на замер)
- complaint (если недовольство)
- general (все остальное)

Ответь ТОЛЬКО названием категории.

Сообщение:
"{message}"
"""

        response = await self.llm.generate(prompt)
        response = response.strip().lower().split()[0]

        allowed = {"price_question", "booking", "complaint", "general"}

        if response not in allowed:
            return "general"

        return response
