from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip())
    normalized = normalized.strip("-._")
    return normalized or "session"


class SessionLogStore:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def append(
        self,
        *,
        session_id: str,
        source: str,
        kind: str,
        message: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "timestamp": utc_now(),
            "session_id": session_id,
            "source": source,
            "kind": kind,
            "message": message,
            "metadata": metadata or {},
        }
        line = json.dumps(entry, ensure_ascii=False)
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(f"{line}\n")
        return entry

    def load_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        entries: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                entries.append(payload)
        return entries


class CompletedResearchStore:
    def __init__(self, root_dir: str) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()

    def save_final_report(
        self,
        *,
        session_id: str,
        final_report: str,
    ) -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        prefix = f"{timestamp}_{_slugify(session_id)}"
        with self._lock:
            report_dir = self._next_available_dir(prefix)
            report_dir.mkdir(parents=True, exist_ok=False)
            report_path = report_dir / "final_report.md"
            report_path.write_text(final_report, encoding="utf-8")
        return report_path

    def _next_available_dir(self, prefix: str) -> Path:
        candidate = self.root_dir / prefix
        if not candidate.exists():
            return candidate
        suffix = 2
        while True:
            candidate = self.root_dir / f"{prefix}_{suffix}"
            if not candidate.exists():
                return candidate
            suffix += 1
