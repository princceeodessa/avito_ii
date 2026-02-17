import os
import time
from typing import Dict, Any


class LeadStoreTxt:
    def __init__(self, path: str = "data/leads.txt"):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)

    def append(self, lead: Dict[str, Any]) -> None:
        ts = lead.get("ts") or int(time.time())
        line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts))}] "
            f"platform={lead.get('platform','?')} "
            f"user_id={lead.get('user_id','?')} "
            f"username={lead.get('username','-')} "
            f"name={lead.get('name','-')} "
            f"city={lead.get('city','?')} "
            f"area_m2={lead.get('area_m2','?')} "
            f"extras={lead.get('extras','-')} "
            f"visit_time={lead.get('visit_time','?')} "
            f"address={lead.get('address','?')} "
            f"phone={lead.get('phone','?')}\n"
        )
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)
