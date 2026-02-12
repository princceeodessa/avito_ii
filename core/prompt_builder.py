# core/prompt_builder.py

from typing import Optional
from core.router import RouteConfig


class PromptBuilder:

    SYSTEM_BASE = """
Ты профессиональный оператор поддержки Avito.
Отвечай кратко, вежливо и по делу.
Никогда не выдумывай информацию.
Если данных недостаточно — попроси уточнение.
"""

    EMPATHY_RULE = """
Если клиент выражает недовольство:
- извинись
- прояви понимание
- предложи решение
"""

    def build(
        self,
        message: str,
        route: RouteConfig,
        context: Optional[str] = None
    ) -> str:

        system_block = self.SYSTEM_BASE

        if route.use_empathy:
            system_block += "\n" + self.EMPATHY_RULE

        context_block = ""
        if context:
            context_block = f"\nКонтекст:\n{context}\n"

        prompt = f"""
{system_block}

{context_block}

Сообщение клиента:
{message}

Ответ:
"""

        return prompt.strip()
