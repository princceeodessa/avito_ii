# core/knowledge_base.py
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


_NON_WORD_RE = re.compile(r"[^0-9a-zа-яё\s]+", re.IGNORECASE)


def _norm(s: str) -> str:
    s = (s or "").lower().replace("ё", "е")
    s = _NON_WORD_RE.sub(" ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


@dataclass
class KBEntry:
    id: str
    triggers: List[str]
    answer: str
    service: str = "any"   # any|ceiling|soundproof
    escalate: bool = False # if True -> create support request
    direct: bool = False   # if True -> can be answered without LLM


class KnowledgeBase:
    """Lightweight keyword-based knowledge base.

    Keeps answers short and safe. We use it to:
    - improve answer quality on common questions
    - decide when to escalate to a human
    - provide context snippets to the LLM
    """

    def __init__(self, path: str):
        self.path = str(path)
        self.entries: List[KBEntry] = []
        self._load()

    def _load(self) -> None:
        p = Path(self.path)
        if not p.exists():
            self.entries = []
            return
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            out: List[KBEntry] = []
            if isinstance(data, list):
                for it in data:
                    if not isinstance(it, dict):
                        continue
                    eid = str(it.get("id") or "").strip()
                    if not eid:
                        continue
                    triggers = it.get("triggers") or []
                    if not isinstance(triggers, list) or not triggers:
                        continue
                    answer = str(it.get("answer") or "").strip()
                    if not answer:
                        continue
                    service = str(it.get("service") or "any").strip().lower()
                    if service not in ("any", "ceiling", "soundproof"):
                        service = "any"
                    out.append(
                        KBEntry(
                            id=eid,
                            triggers=[_norm(x) for x in triggers if isinstance(x, str) and x.strip()],
                            answer=answer,
                            service=service,
                            escalate=bool(it.get("escalate") or False),
                            direct=bool(it.get("direct") or False),
                        )
                    )
            self.entries = out
        except Exception:
            self.entries = []

    def select(self, user_text: str, service: str = "any", k: int = 2) -> List[KBEntry]:
        if not self.entries or not (user_text or "").strip():
            return []
        service = (service or "any").lower().strip()
        q = _norm(user_text)
        if not q:
            return []

        scored: List[Tuple[float, KBEntry]] = []
        for e in self.entries:
            if e.service != "any" and service != "any" and e.service != service:
                continue
            hits = 0
            for t in e.triggers:
                if t and t in q:
                    hits += 1
            if hits <= 0:
                continue
            score = hits / max(1, len(e.triggers))
            # small boost for longer trigger hits
            score += min(0.15, 0.03 * hits)
            scored.append((score, e))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[: min(k, len(scored))]]

