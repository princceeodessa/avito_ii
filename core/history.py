from dataclasses import dataclass
from typing import List, Dict

@dataclass
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str

class ChatHistory:
    def __init__(self, system_prompt: str, max_messages: int = 20):
        self.max_messages = max_messages
        self.messages: List[ChatMessage] = [ChatMessage("system", system_prompt)]

    def add_user(self, text: str):
        self.messages.append(ChatMessage("user", text))
        self._trim()

    def add_assistant(self, text: str):
        self.messages.append(ChatMessage("assistant", text))
        self._trim()

    def _trim(self):
        # оставляем system + последние N сообщений
        if len(self.messages) <= 1 + self.max_messages:
            return
        system = self.messages[0]
        tail = self.messages[-self.max_messages:]
        self.messages = [system] + tail

    def to_ollama_messages(self) -> List[Dict[str, str]]:
        return [{"role": m.role, "content": m.content} for m in self.messages]
