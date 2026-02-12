from typing import Optional
from core.router import RouteConfig


class PromptBuilder:

    SYSTEM_BASE = """
Ты менеджер по продаже натяжных потолков.

Правила:
- Отвечай только на русском языке.
- Никогда не называй итоговую точную стоимость.
- Стоимость только ориентировочная.
- Скидок нет.
- Всегда предлагай бесплатный замер.
- Будь вежливым и продающим.
"""

    EMPATHY_RULE = """
Если клиент недоволен:
- извинись
- прояви понимание
- предложи решение
"""

    def build(
        self,
        message: str,
        route: RouteConfig,
        history: str,
        pricing_context: Optional[str] = None,
        promo_text: Optional[str] = None
    ) -> str:

        system_block = self.SYSTEM_BASE

        if route.use_empathy:
            system_block += "\n" + self.EMPATHY_RULE

        promo_block = ""
        if promo_text:
            promo_block = f"\nСообщи клиенту об акции:\n{promo_text}\n"

        pricing_block = ""
        if pricing_context:
            pricing_block = f"\nДанные для расчета:\n{pricing_context}\n"

        prompt = f"""
{system_block}

{promo_block}

История диалога:
{history}

{pricing_block}

Сообщение клиента:
{message}

Ответ менеджера:
"""

        return prompt.strip()
