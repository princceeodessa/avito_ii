# core/fewshot.py

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё\s]+", re.IGNORECASE)


def _norm_text(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = _NON_WORD_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _tokenize(s: str) -> List[str]:
    s = _norm_text(s)
    if not s:
        return []
    toks = s.split()
    # простой стоп-лист, чтобы совпадения были чуть “смысловее”
    stop = {
        "и", "а", "но", "в", "во", "на", "по", "к", "ко", "у", "за",
        "ли", "же", "то", "это", "мы", "вы", "я", "он", "она", "они",
        "мне", "вам", "нас", "вас", "есть", "нужно", "надо", "можно",
        "пожалуйста", "спасибо",
    }
    return [t for t in toks if t not in stop and len(t) > 1]


def _jaccard(a: List[str], b: List[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


@dataclass
class FewShotExample:
    user_text: str
    messages: List[Dict[str, str]]


class FewShotManager:
    """Лёгкий few-shot без внешних зависимостей.

    Хранит примеры в JSON (список объектов с ключом `messages`).
    Выбирает несколько наиболее похожих на текущее сообщение клиента.
    """

    def __init__(self, path: str):
        self.path = str(path)
        self.examples: List[FewShotExample] = []
        self._load()

    def _load(self) -> None:
        p = Path(self.path)
        if not p.exists():
            self.examples = []
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out: List[FewShotExample] = []
            if isinstance(data, list):
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    msgs = it.get("messages")
                    if not isinstance(msgs, list) or not msgs:
                        continue

                    # ожидаем, что первый turn — user
                    first_user = None
                    for m in msgs:
                        if isinstance(m, dict) and m.get("role") == "user" and isinstance(m.get("content"), str):
                            first_user = m.get("content")
                            break
                    if not first_user:
                        continue

                    norm_msgs = []
                    ok = True
                    for m in msgs:
                        if not isinstance(m, dict):
                            ok = False
                            break
                        role = m.get("role")
                        content = m.get("content")
                        if role not in ("user", "assistant"):
                            ok = False
                            break
                        if not isinstance(content, str) or not content.strip():
                            ok = False
                            break
                        norm_msgs.append({"role": role, "content": content.strip()})
                    if not ok:
                        continue

                    out.append(FewShotExample(user_text=first_user.strip(), messages=norm_msgs))

            self.examples = out
        except Exception:
            self.examples = []

    def select(self, user_text: str, mem: Optional[Dict[str, Any]] = None, k: int = 4) -> List[Dict[str, str]]:
        """Возвращает сообщения few-shot (user/assistant), которые вставляются в начало контекста."""

        if not self.examples or not (user_text or "").strip() or k <= 0:
            return []

        mem = mem or {}
        service = mem.get("service") or ("soundproof" if mem.get("soundproof_pending") else "ceiling")
        service_hint = "sound" if service == "soundproof" else "ceiling"

        q_tokens = _tokenize(user_text)
        scored: List[Tuple[float, FewShotExample]] = []

        for ex in self.examples:
            ex_tokens = _tokenize(ex.user_text)
            score = _jaccard(q_tokens, ex_tokens)

            # небольшой эвристический буст по направлению
            ex_low = _norm_text(ex.user_text)
            if service_hint == "sound" and ("шумо" in ex_low or "звуко" in ex_low):
                score += 0.10
            if service_hint == "ceiling" and ("потол" in ex_low or "замер" in ex_low):
                score += 0.03

            if score > 0:
                scored.append((score, ex))

        scored.sort(key=lambda x: x[0], reverse=True)
        picked = [ex for _, ex in scored[: min(len(scored), k)]]

        # если совсем ничего не нашлось — вернём 1–2 самых “универсальных” примера
        if not picked:
            picked = self.examples[: min(2, len(self.examples))]

        out: List[Dict[str, str]] = []
        for ex in picked:
            out.extend(ex.messages)
        return out
