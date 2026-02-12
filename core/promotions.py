import json


class PromotionManager:

    def __init__(self, path: str):
        with open(path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def get_promo(self, platform: str):
        return self.data.get(platform)
