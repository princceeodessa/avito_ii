import ollama


class ResponseGenerator:

    def __init__(self, model_name: str = "qwen2.5:7b"):
        self.model_name = model_name

    async def generate(self, prompt: str) -> str:
        response = ollama.chat(
            model=self.model_name,
            messages=[{"role": "user", "content": prompt}]
        )
        return response["message"]["content"]
