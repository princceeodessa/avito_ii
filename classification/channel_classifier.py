# classification/channel_classifier.py

from enum import Enum


class Channel(str, Enum):
    AVITO = "avito"
    TELEGRAM = "telegram"
    VK = "vk"
    WEB = "web"


class ChannelClassifier:

    @staticmethod
    def detect(source: str) -> Channel:
        source = source.lower()

        if "avito" in source:
            return Channel.AVITO

        if "telegram" in source:
            return Channel.TELEGRAM

        if "vk" in source:
            return Channel.VK

        return Channel.WEB
