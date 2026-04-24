from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ResearchTask(BaseModel):
    id: str
    question: str
    search_queries: list[str] = Field(default_factory=list)
    source_hints: list[str] = Field(default_factory=list)
    done_criteria: str = ""
    status: str = "pending"


class SearchResult(BaseModel):
    id: str
    source: str
    title: str
    url: str
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class EvidenceClaim(BaseModel):
    claim: str
    source_ids: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"] = "medium"
    gaps: str = ""
    task_id: str


class SessionCommand(BaseModel):
    type: Literal["ack", "user_message", "accept", "reject", "refresh", "crash"]
    content: str | None = None
    feedback: str | None = None
    style_instructions: str | None = None


class SessionState(BaseModel):
    session_id: str
    acknowledged: bool = False
    status: str = "idle"
    workflow_revision: int = 0
    query: str | None = None
    style_instructions: str | None = None
    draft_report: str | None = None
    final_report: str | None = None
    tasks: list[ResearchTask] = Field(default_factory=list)
    results: list[SearchResult] = Field(default_factory=list)
    evidence: list[EvidenceClaim] = Field(default_factory=list)
    feedback: list[str] = Field(default_factory=list)
    progress_events: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    command_history: list[SessionCommand] = Field(default_factory=list)
    active_job: str | None = None
    research_inflight: int = 0
    updated_at: str = Field(default_factory=utc_now)


class SessionStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._sessions: dict[str, SessionState] = {}

    def ensure_session(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.setdefault(
                session_id, SessionState(session_id=session_id)
            )
            return session.model_copy(deep=True).model_dump()

    def get_by_reference(self, session_id: str | None) -> dict[str, Any] | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            return session.model_copy(deep=True).model_dump() if session else None

    def mutate(
        self,
        session_id: str,
        updater: Callable[[SessionState], None],
    ) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.setdefault(
                session_id, SessionState(session_id=session_id)
            )
            updater(session)
            session.updated_at = utc_now()
            snapshot = session.model_copy(deep=True).model_dump()
        return snapshot


class SessionEventBroker:
    def __init__(self, *, max_events_per_session: int = 500) -> None:
        self._max_events_per_session = max_events_per_session
        self._lock = asyncio.Lock()
        self._events: dict[str, list[dict[str, Any]]] = {}
        self._conditions: dict[str, asyncio.Condition] = {}
        self._next_offset: dict[str, int] = {}

    async def publish(
        self, session_id: str, event: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        async with self._lock:
            condition = self._conditions.setdefault(session_id, asyncio.Condition())
            offset = self._next_offset.get(session_id, 0)
            self._next_offset[session_id] = offset + 1
            event_payload = deepcopy(payload)
            event_payload["offset"] = offset
            item = {
                "offset": offset,
                "event": event,
                "payload": event_payload,
            }
            events = self._events.setdefault(session_id, [])
            events.append(item)
            if len(events) > self._max_events_per_session:
                del events[: len(events) - self._max_events_per_session]
        async with condition:
            condition.notify_all()
        return deepcopy(item)

    async def wait_for_events(
        self,
        session_id: str,
        *,
        after_offset: int,
        timeout_seconds: float = 15.0,
    ) -> list[dict[str, Any]]:
        async with self._lock:
            condition = self._conditions.setdefault(session_id, asyncio.Condition())
            available = [
                deepcopy(item)
                for item in self._events.get(session_id, [])
                if item["offset"] > after_offset
            ]
        if available:
            return available

        try:
            async with condition:
                await asyncio.wait_for(condition.wait(), timeout=timeout_seconds)
        except TimeoutError:
            return []

        async with self._lock:
            return [
                deepcopy(item)
                for item in self._events.get(session_id, [])
                if item["offset"] > after_offset
            ]

    async def latest_offset(self, session_id: str) -> int:
        async with self._lock:
            return self._next_offset.get(session_id, 0) - 1
