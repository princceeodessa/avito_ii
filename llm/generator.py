# llm/generator.py

import httpx


class LLMGenerator:
    """
    Универсальный async LLM клиент.
    """

    def __init__(
        self,
        model: str = "phi3",
        base_url: str = "http://localhost:11434",
        temperature: float = 0.2,
        timeout: int = 60,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.temperature = temperature
        self.timeout = timeout

    async def generate(self, prompt: str) -> str:
        url = f"{self.base_url}/api/generate"

        payload = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": self.temperature,
            }
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(url, json=payload)
                response.raise_for_status()

            data = response.json()
            return data.get("response", "").strip()

        except Exception:
            return "Произошла ошибка при формировании ответа."
