from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from typing import Any, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from .graph import (
    build_coordinator_graph,
    build_planner_graph,
    build_reporter_graph,
    build_researcher_graph,
    dedupe_references,
)
from .schemas import DeepResearchContext, ResearchTask as GraphResearchTask
from .settings import Settings, get_settings

from .coordinator_host.memory import (
    EvidenceClaim,
    ResearchTask,
    SearchResult,
    SessionCommand,
    SessionEventBroker,
    SessionStore,
)
from .coordinator_host.persistence import CompletedResearchStore, SessionLogStore
from .coordinator_host.registry import SessionRegistry
from .coordinator_host.worker_availability import WorkerAvailabilityTracker


logger = logging.getLogger("backend.service")


class GraphHostService:
    def __init__(
        self,
        *,
        settings: Settings,
        session_logs: SessionLogStore,
        completed_reports: CompletedResearchStore,
        crash_exit: Callable[[int], None],
    ) -> None:
        self.settings = settings
        self.session_logs = session_logs
        self.completed_reports = completed_reports
        self.store = SessionStore()
        base_url = (
            settings.public_base_url
            or f"http://{settings.app_host}:{settings.app_port}"
        ).rstrip("/")
        self.registry = SessionRegistry(
            coordinator_address=base_url,
            planner_address="inproc://langgraph/planner",
            researcher_address="inproc://langgraph/researcher",
            reporter_address="inproc://langgraph/reporter",
        )
        self.worker_availability = WorkerAvailabilityTracker(
            self.registry.worker_addresses()
        )
        self.event_broker = SessionEventBroker()
        self.graph_context = settings.to_graph_context()
        self.coordinator_graph = build_coordinator_graph()
        self.planner_graph = build_planner_graph()
        self.researcher_graph = build_researcher_graph()
        self.reporter_graph = build_reporter_graph()
        self._crash_exit = crash_exit
        self._session_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._graph_runtime_state: dict[str, dict[str, Any]] = {}
        self._mark_workers_available()

    def ensure_session(self, session_id: str) -> None:
        self.store.ensure_session(session_id)
        self.registry.ensure_session(session_id)

    def session_state(self, session_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        return self.store.get_by_reference(session_id) or {
            "session_id": session_id,
            "status": "idle",
        }

    def agent_snapshot(self, session_id: str) -> dict[str, Any]:
        self.ensure_session(session_id)
        snapshot = self.registry.snapshot(session_id)
        return snapshot or {"session_id": session_id, "agents": {}, "links": {}}

    async def handle_command(
        self, session_id: str, command: SessionCommand
    ) -> dict[str, Any]:
        self.ensure_session(session_id)

        if command.type == "ack":
            self.store.mutate(
                session_id,
                lambda session: setattr(session, "acknowledged", True),
            )
            await self.publish_session_snapshot(session_id)
            await self.publish_agent_snapshot(session_id)
            return {
                "accepted": True,
                "session_id": session_id,
                "command": command.model_dump(),
            }

        self.store.mutate(
            session_id,
            lambda session: session.command_history.append(command),
        )
        self._record_user_command(session_id, command)

        if command.type == "refresh":
            await self.publish_session_snapshot(session_id)
            await self.publish_agent_snapshot(session_id)
            return {
                "accepted": True,
                "session_id": session_id,
                "command": command.model_dump(),
            }

        if command.type == "crash":
            reason = (
                command.feedback or command.content or "intentional backend crash"
            ).strip()
            await self.trigger_crash(reason=reason, session_id=session_id)
            return {
                "accepted": True,
                "session_id": session_id,
                "command": command.model_dump(),
            }

        await self.publish_agent_state(
            session_id,
            "coordinator",
            state="running",
            message="coordinator is processing the latest command",
            details={"activity": "coordinating"},
        )

        if command.type == "user_message":
            result = await self._tool_start_research(
                query=(command.content or "").strip(),
                style_instructions=command.style_instructions,
                session_id=session_id,
                previous_draft=None,
            )
        elif command.type == "accept":
            result = await self._tool_approve_review(
                session_id=session_id,
                feedback=command.feedback,
            )
        elif command.type == "reject":
            result = await self._tool_request_revision(
                session_id=session_id,
                feedback=command.feedback or "",
            )
        else:
            raise HTTPException(status_code=400, detail=f"unsupported command: {command.type}")

        if self.session_state(session_id).get("status") == "idle":
            await self.publish_agent_state(
                session_id,
                "coordinator",
                state="suspended",
                message="coordinator is idle",
                details={"activity": "idle"},
            )
        return result

    async def trigger_crash(
        self, *, reason: str, session_id: str | None = None
    ) -> None:
        logger.error(
            "Intentional backend crash requested; session_id=%s reason=%s",
            session_id,
            reason,
        )
        if session_id:
            await self.publish_message(
                session_id, "coordinator", f"backend crash requested: {reason}"
            )

        def _exit() -> None:
            self._crash_exit(1)

        timer = threading.Timer(0.05, _exit)
        timer.daemon = True
        timer.start()

    async def publish_session_snapshot(self, session_id: str) -> None:
        session = self.session_state(session_id)
        await self.event_broker.publish(
            session_id,
            "progress",
            {
                "event_type": "session",
                "session_id": session_id,
                "session_status": session.get("status", "idle"),
                "data": self._session_view(session),
            },
        )

    async def publish_agent_snapshot(self, session_id: str) -> None:
        snapshot = self.agent_snapshot(session_id)
        await self.event_broker.publish(
            session_id,
            "snapshot",
            {
                "session": self.session_state(session_id),
                "agents": {"agents": snapshot.get("agents", {})},
            },
        )

    async def publish_message(
        self, session_id: str, agent_name: str, message: str
    ) -> None:
        if not message.strip():
            return
        self.session_logs.append(
            session_id=session_id,
            source=agent_name,
            kind="message",
            message=message,
        )
        self.store.mutate(
            session_id,
            lambda session: session.progress_events.append(f"{agent_name}: {message}"),
        )
        await self.event_broker.publish(
            session_id,
            "progress",
            {
                "event_type": "message",
                "session_id": session_id,
                "agent_name": agent_name,
                "message": message,
            },
        )

    async def publish_agent_state(
        self,
        session_id: str,
        agent_name: str,
        *,
        state: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        agent = self.registry.update_agent(
            session_id,
            agent_name,
            state=state,
            message=message,
            details=details or {},
        )
        self.session_logs.append(
            session_id=session_id,
            source=agent_name,
            kind="state",
            message=f"state: {state} | {message}",
            metadata={"details": agent.get("details", {})},
        )
        await self.event_broker.publish(
            session_id,
            "progress",
            {
                "event_type": "agent",
                "session_id": session_id,
                "agent_name": agent_name,
                "agent_state": agent.get("state", state),
                "message": message,
                "data": agent.get("details", {}),
            },
        )

    async def set_session_error(self, session_id: str, message: str) -> None:
        self.session_logs.append(
            session_id=session_id,
            source="error",
            kind="error",
            message=message,
        )
        self.store.mutate(
            session_id,
            lambda session: (
                session.errors.clear(),
                session.errors.append(message),
                setattr(session, "status", "error"),
                setattr(session, "active_job", None),
            ),
        )
        await self.event_broker.publish(
            session_id,
            "terminal",
            {
                "session_id": session_id,
                "session_status": "error",
                "data": self._session_view(self.session_state(session_id)),
            },
        )

    def snapshot_event(self, session_id: str) -> dict[str, Any]:
        return {
            "session": self.session_state(session_id),
            "agents": {"agents": self.agent_snapshot(session_id).get("agents", {})},
        }

    async def _tool_start_research(
        self,
        query: str,
        style_instructions: str | None = None,
        session_id: str | None = None,
        previous_draft: str | None = None,
    ) -> dict[str, Any]:
        if not session_id:
            raise ValueError("session_id is required for start_research.")
        if not query.strip():
            raise HTTPException(status_code=400, detail="query is required")
        self.ensure_session(session_id)
        if not self.reserve_job(session_id, "research"):
            return self._tool_result(
                session_id=session_id,
                result={
                    "accepted": False,
                    "reason": "session already has an active job",
                },
            )

        self.store.mutate(
            session_id,
            lambda session: (
                setattr(session, "acknowledged", True),
                setattr(session, "query", query.strip()),
                setattr(session, "style_instructions", style_instructions),
                setattr(session, "status", "queued"),
                setattr(session, "draft_report", None),
                setattr(session, "final_report", None),
                setattr(session, "tasks", []),
                setattr(session, "results", []),
                setattr(session, "evidence", []),
                setattr(session, "errors", []),
                setattr(session, "progress_events", []),
            ),
        )
        self.registry.reset_agents(session_id, include_coordinator=False)
        await self.publish_session_snapshot(session_id)
        await self.publish_agent_snapshot(session_id)
        workflow_revision = self._bump_workflow_revision(session_id)
        feedback_history = list(self.session_state(session_id).get("feedback") or [])
        self._spawn_stage(
            session_id=session_id,
            stage_name="research",
            failed_agent="coordinator",
            workflow_revision=workflow_revision,
            coroutine=self._run_research_workflow(
                session_id=session_id,
                query=query.strip(),
                feedback_history=feedback_history,
                style_instructions=style_instructions,
                previous_draft=previous_draft,
                workflow_revision=workflow_revision,
            ),
        )
        return self._tool_result(
            session_id=session_id,
            result={"accepted": True, "queued": True, "action": "start_research"},
        )

    async def _tool_approve_review(
        self,
        session_id: str,
        feedback: str | None = None,
    ) -> dict[str, Any]:
        self.ensure_session(session_id)
        session = self.session_state(session_id)
        if session.get("status") != "awaiting_review":
            return self._tool_result(
                session_id=session_id,
                result={"accepted": False, "reason": "session is not awaiting review"},
            )
        if not self.reserve_job(session_id, "finalize"):
            return self._tool_result(
                session_id=session_id,
                result={
                    "accepted": False,
                    "reason": "session already has an active job",
                },
            )
        self.store.mutate(
            session_id,
            lambda session: setattr(session, "status", "finalizing"),
        )
        if feedback and feedback.strip():
            self.store.mutate(
                session_id,
                lambda session: session.feedback.append(feedback.strip()),
            )
        await self.publish_session_snapshot(session_id)
        workflow_revision = self._current_workflow_revision(session_id)
        self._spawn_stage(
            session_id=session_id,
            stage_name="finalize",
            failed_agent="reporter",
            workflow_revision=workflow_revision,
            coroutine=self._run_finalize_workflow(
                session_id=session_id,
                approval_feedback=feedback.strip() if feedback and feedback.strip() else None,
                workflow_revision=workflow_revision,
            ),
        )
        return self._tool_result(
            session_id=session_id,
            result={"accepted": True, "queued": True, "action": "approve_review"},
        )

    async def _tool_request_revision(
        self, session_id: str, feedback: str
    ) -> dict[str, Any]:
        self.ensure_session(session_id)
        session = self.session_state(session_id)
        if session.get("status") != "awaiting_review":
            return self._tool_result(
                session_id=session_id,
                result={"accepted": False, "reason": "session is not awaiting review"},
            )
        if not self.reserve_job(session_id, "revision"):
            return self._tool_result(
                session_id=session_id,
                result={
                    "accepted": False,
                    "reason": "session already has an active job",
                },
            )

        normalized_feedback = (
            feedback.strip() or "Please revise the report based on the latest review."
        )
        current_draft = session.get("draft_report")
        self.store.mutate(
            session_id,
            lambda session: (
                session.feedback.append(normalized_feedback),
                setattr(session, "status", "queued"),
            ),
        )
        self.registry.reset_agents(session_id, include_coordinator=False)
        await self.publish_session_snapshot(session_id)
        await self.publish_agent_snapshot(session_id)
        workflow_revision = self._bump_workflow_revision(session_id)
        self._spawn_stage(
            session_id=session_id,
            stage_name="revision",
            failed_agent="coordinator",
            workflow_revision=workflow_revision,
            coroutine=self._run_research_workflow(
                session_id=session_id,
                query=str(session.get("query") or "").strip(),
                feedback_history=list(self.session_state(session_id).get("feedback") or []),
                style_instructions=session.get("style_instructions"),
                previous_draft=str(current_draft or "").strip() or None,
                workflow_revision=workflow_revision,
            ),
        )
        return self._tool_result(
            session_id=session_id,
            result={"accepted": True, "queued": True, "action": "request_revision"},
        )

    def _tool_result(
        self, *, session_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "session_state": self.session_state(session_id),
            "registry_snapshot": self.agent_snapshot(session_id),
            "result": result,
        }

    async def _run_research_workflow(
        self,
        *,
        session_id: str,
        query: str,
        feedback_history: list[str],
        style_instructions: str | None,
        previous_draft: str | None,
        workflow_revision: int,
    ) -> None:
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        if not self.graph_context.llm_api_key:
            raise RuntimeError(
                "OPENAI_API_KEY is required to run the LangGraph backend."
            )

        self.store.mutate(
            session_id,
            lambda session: (
                setattr(session, "acknowledged", True),
                setattr(session, "query", query),
                setattr(session, "style_instructions", style_instructions),
                setattr(session, "status", "planning"),
                setattr(session, "draft_report", None),
                setattr(session, "final_report", None),
                setattr(session, "tasks", []),
                setattr(session, "results", []),
                setattr(session, "evidence", []),
                setattr(session, "errors", []),
                setattr(session, "progress_events", []),
                setattr(session, "feedback", list(feedback_history)),
                setattr(session, "research_inflight", 0),
            ),
        )
        await self.publish_session_snapshot(session_id)
        await self.publish_message(
            session_id,
            "coordinator",
            "Coordinator is assessing whether the request needs a research workflow.",
        )
        await self.publish_agent_state(
            session_id,
            "coordinator",
            state="running",
            message="coordinator is assessing the request",
            details={"activity": "triaging"},
        )
        coordinator_result = await self.coordinator_graph.ainvoke(
            {
                "query": query,
                "research_brief": "",
            },
            context=self.graph_context,
        )
        request_type = str(coordinator_result.get("request_type") or "research").strip()
        coordinator_response = str(
            coordinator_result.get("coordinator_response") or ""
        ).strip()
        if request_type == "non_research":
            if not coordinator_response:
                coordinator_response = (
                    "I can help with research-oriented requests. Ask me to investigate a topic, "
                    "compare options, or gather evidence."
                )
            self.store.mutate(
                session_id,
                lambda session: (
                    setattr(session, "status", "idle"),
                    setattr(session, "draft_report", None),
                    setattr(session, "final_report", None),
                    setattr(session, "active_job", None),
                    setattr(session, "research_inflight", 0),
                ),
            )
            await self.publish_message(
                session_id,
                "coordinator",
                coordinator_response,
            )
            await self.publish_agent_state(
                session_id,
                "coordinator",
                state="suspended",
                message="request handled directly without research",
                details={"activity": "direct_response"},
            )
            await self.publish_session_snapshot(session_id)
            await self.event_broker.publish(
                session_id,
                "terminal",
                {
                    "session_id": session_id,
                    "session_status": "idle",
                    "data": self._session_view(self.session_state(session_id)),
                },
            )
            return
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        research_brief = self._compose_research_brief(
            base_brief=str(coordinator_result.get("research_brief") or "").strip(),
            style_instructions=style_instructions,
            feedback_history=feedback_history,
        )
        await self.publish_message(
            session_id,
            "coordinator",
            "Research brief is ready. Planner is creating the task list.",
        )
        await self.publish_agent_state(
            session_id,
            "coordinator",
            state="suspended",
            message="research brief prepared",
            details={"activity": "idle"},
        )

        await self.publish_agent_state(
            session_id,
            "planner",
            state="running",
            message="planner is creating the research plan",
            details={"activity": "planning"},
        )
        planner_result = await self.planner_graph.ainvoke(
            {
                "query": query,
                "research_brief": research_brief,
                "plan": [],
            },
            context=self.graph_context,
        )
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        plan = list(planner_result.get("plan") or [])
        session_tasks = [self._to_session_task(task) for task in plan]
        self.store.mutate(
            session_id,
            lambda session: (
                setattr(session, "tasks", session_tasks),
                setattr(session, "status", "researching"),
                setattr(session, "research_inflight", len(session_tasks)),
            ),
        )
        await self.publish_session_snapshot(session_id)
        await self.publish_message(
            session_id,
            "planner",
            self._format_plan_summary(plan),
        )
        await self.publish_agent_state(
            session_id,
            "planner",
            state="suspended",
            message="planner produced a task list",
            details={"activity": "idle"},
        )
        await self.publish_agent_state(
            session_id,
            "researcher",
            state="running",
            message="researcher is gathering evidence",
            details={"activity": "researching"},
        )

        findings: list[dict[str, Any]] = []
        raw_docs: list[dict[str, Any]] = []
        retrieval_warnings: list[str] = []
        task_results = await asyncio.gather(
            *[
                self._run_single_research_task(
                    session_id=session_id,
                    task=task,
                    workflow_revision=workflow_revision,
                )
                for task in plan
            ]
        )
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        for finding, docs, task_warnings in task_results:
            findings.append(finding)
            raw_docs.extend(docs)
            retrieval_warnings.extend(task_warnings)

        self.store.mutate(
            session_id,
            lambda session: (
                setattr(session, "results", self._to_search_results(raw_docs)),
                setattr(session, "evidence", self._to_evidence_claims(findings)),
                setattr(session, "status", "drafting"),
            ),
        )
        await self.publish_session_snapshot(session_id)
        await self.publish_agent_state(
            session_id,
            "researcher",
            state="suspended",
            message="researcher finished gathering evidence",
            details={"activity": "idle"},
        )
        if retrieval_warnings:
            await self.publish_message(
                session_id,
                "coordinator",
                "Some retrieval sources were degraded during research. "
                "The workflow continued with the remaining available sources.",
            )
        await self.publish_agent_state(
            session_id,
            "reporter",
            state="running",
            message="reporter is drafting the report",
            details={"activity": "drafting"},
        )

        latest_feedback = feedback_history[-1] if feedback_history and previous_draft else None
        reporter_result = await self.reporter_graph.ainvoke(
            {
                "query": query,
                "research_brief": research_brief,
                "plan": plan,
                "findings": findings,
                "draft_report": previous_draft or "",
                "review_feedback": latest_feedback,
            },
            context=self.graph_context,
        )
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        draft_report = dedupe_references(
            str(reporter_result.get("draft_report") or "").strip()
        )
        self._graph_runtime_state[session_id] = {
            "query": query,
            "research_brief": research_brief,
            "plan": plan,
            "findings": findings,
            "draft_report": draft_report,
        }
        self.store.mutate(
            session_id,
            lambda session: (
                setattr(session, "draft_report", draft_report),
                setattr(session, "final_report", None),
                setattr(session, "status", "awaiting_review"),
                setattr(session, "active_job", None),
                setattr(session, "research_inflight", 0),
            ),
        )
        await self.publish_agent_state(
            session_id,
            "reporter",
            state="suspended",
            message="draft report is ready for review",
            details={"activity": "awaiting_review"},
        )
        await self.publish_message(
            session_id,
            "coordinator",
            "Draft report is ready. Use /accept [opinion] or /reject [opinion].",
        )
        await self.publish_agent_state(
            session_id,
            "coordinator",
            state="suspended",
            message="awaiting human review",
            details={"activity": "awaiting_review"},
        )
        await self.publish_session_snapshot(session_id)
        await self.event_broker.publish(
            session_id,
            "terminal",
            {
                "session_id": session_id,
                "session_status": "awaiting_review",
                "data": self._session_view(self.session_state(session_id)),
            },
        )

    async def _run_single_research_task(
        self,
        *,
        session_id: str,
        task: GraphResearchTask,
        workflow_revision: int,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
        if not self._workflow_is_current(session_id, workflow_revision):
            return {}, [], []
        self.store.mutate(
            session_id,
            lambda session: self._set_task_status(session, task["task_id"], "running"),
        )
        await self.publish_session_snapshot(session_id)
        await self.publish_message(
            session_id,
            "researcher",
            f"Researching {task['task_id']}: {task['title']}",
        )
        result = await self.researcher_graph.ainvoke(
            {
                "task": task,
                "source_queries": {},
                "raw_docs": [],
                "retrieval_warnings": [],
            },
            context=self.graph_context,
        )
        if not self._workflow_is_current(session_id, workflow_revision):
            return {}, [], []
        finding = dict(result.get("finding") or {})
        docs = list(result.get("raw_docs") or [])
        retrieval_warnings = list(dict.fromkeys(result.get("retrieval_warnings") or []))
        self.store.mutate(
            session_id,
            lambda session: (
                self._set_task_status(session, task["task_id"], "done"),
                setattr(session, "research_inflight", max(int(session.research_inflight or 0) - 1, 0)),
            ),
        )
        await self.publish_session_snapshot(session_id)
        await self.publish_message(
            session_id,
            "researcher",
            self._format_finding_summary(finding),
        )
        for warning in retrieval_warnings:
            await self.publish_message(
                session_id,
                "coordinator",
                f"{task['task_id']} retrieval note: {warning}",
            )
        return finding, docs, retrieval_warnings

    async def _run_finalize_workflow(
        self,
        *,
        session_id: str,
        approval_feedback: str | None,
        workflow_revision: int,
    ) -> None:
        if not self._workflow_is_current(session_id, workflow_revision):
            return
        session = self.session_state(session_id)
        draft_report = str(session.get("draft_report") or "").strip()
        if not draft_report:
            raise ValueError("cannot finalize an empty draft report")
        runtime_state = dict(self._graph_runtime_state.get(session_id) or {})
        if not runtime_state:
            raise ValueError("missing graph runtime state for finalization")

        await self.publish_agent_state(
            session_id,
            "reporter",
            state="running",
            message="reporter is preparing the final report",
            details={"activity": "finalizing"},
        )

        final_report = draft_report
        if approval_feedback:
            reporter_result = await self.reporter_graph.ainvoke(
                {
                    "query": runtime_state["query"],
                    "research_brief": runtime_state["research_brief"],
                    "plan": runtime_state["plan"],
                    "findings": runtime_state["findings"],
                    "draft_report": draft_report,
                    "review_feedback": approval_feedback,
                },
                context=self.graph_context,
            )
            final_report = str(reporter_result.get("draft_report") or "").strip()

        final_report = dedupe_references(final_report)
        self.store.mutate(
            session_id,
            lambda current: (
                setattr(current, "final_report", final_report),
                setattr(current, "status", "complete"),
                setattr(current, "active_job", None),
            ),
        )
        self.completed_reports.save_final_report(
            session_id=session_id,
            final_report=final_report,
        )
        await self.publish_agent_state(
            session_id,
            "reporter",
            state="suspended",
            message="final report is ready",
            details={"activity": "idle"},
        )
        await self.publish_message(
            session_id,
            "coordinator",
            "Final report approved and published.",
        )
        await self.publish_agent_state(
            session_id,
            "coordinator",
            state="suspended",
            message="final report is ready",
            details={"activity": "idle"},
        )
        await self.publish_session_snapshot(session_id)
        await self.event_broker.publish(
            session_id,
            "terminal",
            {
                "session_id": session_id,
                "session_status": "complete",
                "data": self._session_view(self.session_state(session_id)),
            },
        )

    def reserve_job(self, session_id: str, job_name: str) -> bool:
        reserved = False

        def _reserve(session: Any) -> None:
            nonlocal reserved
            if session.active_job:
                reserved = False
                return
            session.active_job = job_name
            reserved = True

        self.store.mutate(session_id, _reserve)
        return reserved

    def _spawn_stage(
        self,
        *,
        session_id: str,
        stage_name: str,
        failed_agent: str,
        workflow_revision: int,
        coroutine: Any,
    ) -> None:
        async def _runner() -> None:
            try:
                await coroutine
            except Exception as exc:
                logger.exception(
                    "Workflow stage failed; session_id=%s stage=%s agent=%s",
                    session_id,
                    stage_name,
                    failed_agent,
                )
                if not self._workflow_is_current(session_id, workflow_revision):
                    return
                self._invalidate_workflow(session_id)
                await self.publish_agent_state(
                    session_id,
                    failed_agent,
                    state="error",
                    message=str(exc),
                    details={
                        "activity": "error",
                        "error": str(exc),
                        "stage": stage_name,
                    },
                )
                await self.publish_message(
                    session_id,
                    "coordinator",
                    f"{failed_agent} failed during {stage_name}: {exc}",
                )
                await self.publish_agent_state(
                    session_id,
                    "coordinator",
                    state="suspended",
                    message="error handled with guidance",
                    details={"activity": "error_handling"},
                )
                await self.set_session_error(session_id, str(exc))
            finally:
                tasks = self._session_tasks.get(session_id)
                current_task = asyncio.current_task()
                if tasks is not None and current_task is not None:
                    tasks.discard(current_task)
                    if not tasks:
                        self._session_tasks.pop(session_id, None)

        task = asyncio.create_task(_runner(), name=f"{stage_name}:{session_id}")
        self._session_tasks.setdefault(session_id, set()).add(task)

    def _bump_workflow_revision(self, session_id: str) -> int:
        revision = 0

        def _bump(session: Any) -> None:
            nonlocal revision
            session.workflow_revision = int(session.workflow_revision or 0) + 1
            session.research_inflight = 0
            revision = session.workflow_revision

        self.store.mutate(session_id, _bump)
        return revision

    def _current_workflow_revision(self, session_id: str) -> int:
        return int(self.session_state(session_id).get("workflow_revision") or 0)

    def _workflow_is_current(self, session_id: str, workflow_revision: int) -> bool:
        session = self.session_state(session_id)
        return int(session.get("workflow_revision") or 0) == workflow_revision

    def _invalidate_workflow(self, session_id: str) -> None:
        self.store.mutate(
            session_id,
            lambda session: (
                setattr(
                    session,
                    "workflow_revision",
                    int(session.workflow_revision or 0) + 1,
                ),
                setattr(session, "research_inflight", 0),
            ),
        )

    def _set_task_status(self, session: Any, task_id: str, status: str) -> None:
        for item in session.tasks:
            if item.id == task_id:
                item.status = status
                return

    def _compose_research_brief(
        self,
        *,
        base_brief: str,
        style_instructions: str | None,
        feedback_history: list[str],
    ) -> str:
        parts = [base_brief.strip()] if base_brief.strip() else []
        if style_instructions and style_instructions.strip():
            parts.append(
                "## Report Preferences\n"
                f"{style_instructions.strip()}"
            )
        if feedback_history:
            bullet_lines = "\n".join(f"- {item}" for item in feedback_history if item.strip())
            if bullet_lines:
                parts.append(f"## Revision Feedback\n{bullet_lines}")
        return "\n\n".join(parts).strip()

    def _to_session_task(self, task: GraphResearchTask) -> ResearchTask:
        return ResearchTask(
            id=task["task_id"],
            question=task["question"],
            search_queries=[task["question"], *task.get("keywords", [])],
            source_hints=list(task.get("sources", [])),
            done_criteria=task.get("objective", ""),
            status="pending",
        )

    def _to_search_results(self, docs: list[dict[str, Any]]) -> list[SearchResult]:
        results: list[SearchResult] = []
        for idx, doc in enumerate(docs, start=1):
            results.append(
                SearchResult(
                    id=f"{doc.get('source', 'source')}-{idx}",
                    source=str(doc.get("source") or "unknown"),
                    title=str(doc.get("title") or "Untitled source"),
                    url=str(doc.get("url") or ""),
                    content=str(doc.get("snippet") or ""),
                    metadata={
                        "published": doc.get("published"),
                        "score": doc.get("score"),
                        "query_used": doc.get("query_used"),
                    },
                )
            )
        return results

    def _to_evidence_claims(self, findings: list[dict[str, Any]]) -> list[EvidenceClaim]:
        claims: list[EvidenceClaim] = []
        for finding in findings:
            points = list(finding.get("key_points") or [])
            if not points:
                points = [str(finding.get("summary_md") or "").strip()]
            for point in points:
                text = str(point).strip()
                if not text:
                    continue
                claims.append(
                    EvidenceClaim(
                        claim=text,
                        source_ids=[],
                        confidence=str(finding.get("confidence") or "medium"),
                        gaps="; ".join(finding.get("open_questions") or []),
                        task_id=str(finding.get("task_id") or "unknown"),
                    )
                )
        return claims

    def _format_plan_summary(self, plan: list[dict[str, Any]]) -> str:
        if not plan:
            return "Planner returned an empty plan."
        lines = [
            f"- {task['task_id']}: {task['title']} ({', '.join(task.get('sources', []))})"
            for task in plan
        ]
        return "Planned research tasks:\n" + "\n".join(lines)

    def _format_finding_summary(self, finding: dict[str, Any]) -> str:
        title = str(finding.get("title") or finding.get("task_id") or "task")
        confidence = str(finding.get("confidence") or "unknown")
        key_points = list(finding.get("key_points") or [])
        if key_points:
            return f"{title} complete ({confidence} confidence): {key_points[0]}"
        return f"{title} complete ({confidence} confidence)."

    def _session_view(self, session: dict[str, Any]) -> dict[str, Any]:
        return {
            "query": session.get("query"),
            "draft_report": session.get("draft_report"),
            "final_report": session.get("final_report"),
            "errors": session.get("errors") or [],
            "feedback": session.get("feedback") or [],
            "command_history": session.get("command_history") or [],
        }

    def _record_user_command(self, session_id: str, command: SessionCommand) -> None:
        message = self._user_command_text(command)
        if not message:
            return
        self.session_logs.append(
            session_id=session_id,
            source="user",
            kind="command",
            message=message,
        )

    def _user_command_text(self, command: SessionCommand) -> str | None:
        if command.type == "user_message":
            return (command.content or "").strip() or None
        if command.type == "accept":
            suffix = (
                f" {command.feedback.strip()}"
                if command.feedback and command.feedback.strip()
                else ""
            )
            return f"/accept{suffix}"
        if command.type == "reject":
            suffix = (
                f" {command.feedback.strip()}"
                if command.feedback and command.feedback.strip()
                else ""
            )
            return f"/reject{suffix}"
        if command.type == "crash":
            payload = (command.feedback or command.content or "").strip()
            suffix = f" {payload}" if payload else ""
            return f"/crash{suffix}"
        return None

    def _mark_workers_available(self) -> None:
        available = bool(self.settings.openai_api_key)
        missing_key = "OPENAI_API_KEY is not configured." if not available else None
        for worker_name, address in self.registry.worker_addresses().items():
            self.worker_availability.record_probe(
                worker_name,
                address=address,
                available=available,
                error=missing_key,
            )


def create_app(
    settings: Settings | None = None,
    *,
    crash_exit: Callable[[int], None] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    session_logs = SessionLogStore(settings.session_log_file)
    completed_reports = CompletedResearchStore(settings.completed_reports_dir)
    crash_exit = crash_exit or os._exit

    service = GraphHostService(
        settings=settings,
        session_logs=session_logs,
        completed_reports=completed_reports,
        crash_exit=crash_exit,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield

    app = FastAPI(
        title="LangGraph Research Host",
        version="0.2.0",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.service = service
    app.state.store = service.store
    app.state.registry = service.registry
    app.state.worker_availability = service.worker_availability
    app.state.session_logs = session_logs
    app.state.completed_reports = completed_reports

    @app.exception_handler(HTTPException)
    async def http_exception_handler(_request: Any, exc: HTTPException):
        if exc.status_code >= 500:
            logger.error("HTTP exception %s: %s", exc.status_code, exc.detail)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        logger.exception(
            "Unhandled request exception; method=%s path=%s",
            request.method,
            request.url.path,
        )
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "backend"}

    @app.get("/internal/workers/availability")
    async def worker_availability() -> dict[str, Any]:
        return {"workers": service.worker_availability.snapshot()}

    @app.get("/api/research/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return service.session_state(session_id)

    @app.get("/api/research/{session_id}/agents")
    async def get_session_agents(session_id: str) -> dict[str, Any]:
        snapshot = service.agent_snapshot(session_id)
        return {
            "session_id": session_id,
            "agents": snapshot.get("agents", {}),
            "links": snapshot.get("links", {}),
        }

    @app.post("/api/research/{session_id}/command", status_code=202)
    async def post_command(session_id: str, command: SessionCommand) -> dict[str, Any]:
        return await service.handle_command(session_id, command)

    @app.post("/api/debug/crash")
    async def debug_crash(payload: dict[str, Any]) -> dict[str, Any]:
        reason = str(payload.get("reason") or "intentional backend crash").strip()
        session_id = payload.get("session_id")
        await service.trigger_crash(
            reason=reason, session_id=str(session_id) if session_id else None
        )
        raise HTTPException(
            status_code=500, detail="intentional backend crash triggered"
        )

    @app.get("/api/research/{session_id}/stream")
    async def stream_session(session_id: str):
        service.ensure_session(session_id)

        async def event_generator():
            try:
                snapshot = service.snapshot_event(session_id)
                yield {
                    "event": "snapshot",
                    "data": json.dumps(snapshot, ensure_ascii=False),
                }
                after_offset = await service.event_broker.latest_offset(session_id)
                while True:
                    items = await service.event_broker.wait_for_events(
                        session_id,
                        after_offset=after_offset,
                    )
                    if not items:
                        yield {
                            "event": "keepalive",
                            "data": json.dumps(
                                {"session_id": session_id}, ensure_ascii=False
                            ),
                        }
                        continue
                    for item in items:
                        after_offset = item["offset"]
                        yield {
                            "event": item["event"],
                            "data": json.dumps(item["payload"], ensure_ascii=False),
                        }
            except asyncio.CancelledError:
                logger.info("SSE stream cancelled; session_id=%s", session_id)
                raise
            except Exception:
                logger.exception("SSE stream failed; session_id=%s", session_id)
                raise

        return EventSourceResponse(event_generator())

    return app


def create_app_from_env() -> FastAPI:
    return create_app()
