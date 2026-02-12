from core.intent import IntentDetector
from core.prompt_builder import PromptBuilder
from core.router import MessageRouter


class MessagePipeline:

    def __init__(self, llm, pricing, promotions, history):
        self.llm = llm
        self.intent_detector = IntentDetector(llm)
        self.router = MessageRouter()
        self.prompt_builder = PromptBuilder()
        self.pricing = pricing
        self.promotions = promotions
        self.history = history
