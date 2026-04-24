from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class WorkerAvailabilityTracker:
    def __init__(self, initial_addresses: dict[str, str | None]) -> None:
        self._lock = RLock()
        self._workers: dict[str, dict[str, Any]] = {}
        for worker_name, address in initial_addresses.items():
            self._workers[worker_name] = {
                "worker_name": worker_name,
                "address": address,
                "available": False,
                "last_checked_at": None,
                "last_error": "Worker has not been probed yet.",
            }

    def record_probe(
        self,
        worker_name: str,
        *,
        address: str | None,
        available: bool,
        error: str | None,
    ) -> None:
        with self._lock:
            worker = self._workers.setdefault(
                worker_name,
                {
                    "worker_name": worker_name,
                    "address": address,
                    "available": available,
                    "last_checked_at": None,
                    "last_error": error,
                },
            )
            worker["address"] = address
            worker["available"] = available
            worker["last_checked_at"] = utc_now()
            worker["last_error"] = error

    def snapshot(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return deepcopy(self._workers)
