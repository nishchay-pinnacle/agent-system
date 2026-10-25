"""Small durable elapsed-time ledger, independent of research revisions."""

import json
import time
from pathlib import Path

from agent_system.company_intelligence.artifacts import now, read_json, save_json
from agent_system.company_intelligence.coordination import Document


class ActiveClock:
    def __init__(self, directory: Path, doc: Document) -> None:
        self.path = directory / "timing.json"
        self.total = doc.usage.get("active_seconds", 0)
        if self.path.exists():
            saved = read_json(self.path)
            prior = float(str(saved["total_seconds"]))
            # An interrupted active interval is conservatively charged once, up to
            # the next ledger boundary; offline time is never charged.
            uncertain = 1.0 if saved.get("active") else 0.0
            self.total = max(self.total, prior + uncertain)
        self.last = time.monotonic()
        self.flushed = self.last
        self.active = False
        doc.usage["active_seconds"] = self.total

    def tick(self, doc: Document, active: bool) -> None:
        current = time.monotonic()
        if self.active:
            self.total += max(0, current - self.last)
        self.last, self.active = current, active
        doc.usage["active_seconds"] = self.total
        if current - self.flushed >= 1:
            self.flush()

    def flush(self) -> None:
        save_json(
            self.path,
            {
                "version": 1,
                "total_seconds": self.total,
                "active": self.active,
                "updated_at": now(),
            },
        )
        self.flushed = self.last

    def close(self, doc: Document) -> None:
        self.tick(doc, False)
        self.flush()


def research_signature(doc: Document) -> str:
    payload = doc.model_dump(mode="json", exclude={"revision", "findings"})
    usage = payload.get("usage")
    if isinstance(usage, dict):
        usage.pop("active_seconds", None)
    return json.dumps(payload, sort_keys=True)
