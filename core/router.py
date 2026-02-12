# core/router.py

from dataclasses import dataclass
from core.intent import IntentType


@dataclass
class RouteConfig:
    use_empathy: bool = False
    escalate: bool = False


class MessageRouter:

    def route(self, intent: IntentType) -> RouteConfig:

        if intent == "complaint":
            return RouteConfig(use_empathy=True)

        if intent == "positive_feedback":
            return RouteConfig(use_empathy=False)

        return RouteConfig()
