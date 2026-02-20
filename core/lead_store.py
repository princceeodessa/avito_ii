# core/lead_store.py
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, Optional


class LeadStoreTxt:
    """
    Пишем:
    1) data/leads.txt (короткая строка)
    2) data/leads/<lead_...>.json (полная карточка)  -> возвращаем путь (для email/вложений)
    """

    def __init__(self, path: str = "data/leads.txt", leads_dir: str = "data/leads") -> None:
        self.path = path
        self.leads_dir = leads_dir
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        os.makedirs(leads_dir, exist_ok=True)

        self.last_path: Optional[str] = None

    @staticmethod
    def _safe(s: str) -> str:
        s = (s or "").strip()
        s = re.sub(r"\s+", "_", s)
        s = re.sub(r"[^0-9A-Za-zА-Яа-я_\-]+", "", s)
        return s[:80] or "lead"

    def append(self, lead: Dict[str, Any]) -> str:
        ts = int(lead.get("ts") or time.time())
        platform = self._safe(str(lead.get("platform", "unknown")))
        user_id = self._safe(str(lead.get("user_id", "unknown")))
        city = self._safe(str(lead.get("city", "")))

        # 1) txt line
        line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}] "
            f"platform={lead.get('platform','?')} "
            f"user_id={lead.get('user_id','?')} "
            f"username={lead.get('username','-')} "
            f"name={lead.get('name','-')} "
            f"lead_kind={lead.get('lead_kind','-')} "
            f"city={lead.get('city','?')} "
            f"area_m2={lead.get('area_m2','?')} "
            f"extras={lead.get('extras','-')} "
            f"visit_date={lead.get('visit_date','-')} "
            f"visit_time={lead.get('visit_time','-')} "
            f"address={lead.get('address','-')} "
            f"phone={lead.get('phone','-')}\n"
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)

        # 2) json card
        fname = f"lead_{ts}_{platform}_{user_id}_{city}.json"
        fpath = str(Path(self.leads_dir) / fname)
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump(lead, f, ensure_ascii=False, indent=2)

        self.last_path = fpath
        return fpath