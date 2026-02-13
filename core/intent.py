import re
from dataclasses import dataclass

@dataclass
class IntentResult:
    intent: str
    confidence: float

class IntentDetector:
    PRICE = "price"
    BOOKING = "booking"
    COMPLAINT = "complaint"
    GENERAL = "general"

    def detect(self, text: str) -> IntentResult:
        t = text.lower()

        if re.search(r"\b(дорого|жалоб|плох|обман|верните|не\s+доволен|мошенн)\b", t):
            return IntentResult(self.COMPLAINT, 0.85)

        if re.search(r"\b(сколько|цена|стоим|стоить|прайс|расч(е|ё)т|м2|кв\.?\s*м)\b", t):
            return IntentResult(self.PRICE, 0.8)

        if re.search(r"\b(когда|запис|замер|приех|встреч|контакт|телефон|адрес)\b", t):
            return IntentResult(self.BOOKING, 0.8)

        return IntentResult(self.GENERAL, 0.6)
