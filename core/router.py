from dataclasses import dataclass
from core.intent import IntentType


@dataclass
class RouteConfig:
    use_empathy: bool = False
    force_booking_offer: bool = True


class MessageRouter:

    def route(self, intent: IntentType) -> RouteConfig:

        if intent == "complaint":
            return RouteConfig(use_empathy=True)

        if intent == "booking":
            return RouteConfig(force_booking_offer=False)

        return RouteConfig()
from dataclasses import dataclass
from core.intent import IntentType


@dataclass
class RouteConfig:
    use_empathy: bool = False
    force_booking_offer: bool = True


class MessageRouter:

    def route(self, intent: IntentType) -> RouteConfig:

        if intent == "complaint":
            return RouteConfig(use_empathy=True)

        if intent == "booking":
            return RouteConfig(force_booking_offer=False)

        return RouteConfig()
