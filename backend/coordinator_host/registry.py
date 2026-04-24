from copy import deepcopy
from threading import RLock
from typing import Any


COORDINATOR_CAPABILITIES = [
    "natural_language_status",
    "session_management",
    "review_gate",
]
COORDINATOR_ROLE_DESCRIPTION = (
    "Host-managed coordinator layer. It explains current system state to the user, "
    "accepts review decisions and feedback, and reflects host scheduling progress."
)
PLANNER_CAPABILITIES = ["task_decomposition", "plan_revision"]
RESEARCHER_CAPABILITIES = ["multi_source_retrieval", "evidence_synthesis"]
REPORTER_CAPABILITIES = ["markdown_report_generation", "final_report_packaging"]


class SessionRegistry:
    def __init__(
        self,
        coordinator_address: str | None = None,
        planner_address: str | None = None,
        researcher_address: str | None = None,
        reporter_address: str | None = None,
    ) -> None:
        self.coordinator_address = coordinator_address
        self.planner_address = planner_address
        self.researcher_address = researcher_address
        self.reporter_address = reporter_address
        self._worker_addresses = {
            "planner": planner_address,
            "researcher": researcher_address,
            "reporter": reporter_address,
        }
        self._lock = RLock()
        self._sessions: dict[str, dict[str, Any]] = {}

    def ensure_session(self, session_id: str) -> None:
        with self._lock:
            if session_id in self._sessions:
                return
            self._sessions[session_id] = {
                "session_id": session_id,
                "agents": {
                    "coordinator": self._agent_template(
                        name="coordinator",
                        role_group="coordinator",
                        role=COORDINATOR_ROLE_DESCRIPTION,
                        capabilities=COORDINATOR_CAPABILITIES,
                        address=self.coordinator_address,
                        managed_by="coordinator_host",
                    ),
                    "planner": self._agent_template(
                        name="planner",
                        role_group="planner",
                        role="Breaks the research request into concrete tasks.",
                        capabilities=PLANNER_CAPABILITIES,
                        address=self._worker_addresses["planner"],
                        managed_by="coordinator_host",
                    ),
                    "researcher": self._agent_template(
                        name="researcher",
                        role_group="researcher",
                        role="Retrieves sources and extracts evidence.",
                        capabilities=RESEARCHER_CAPABILITIES,
                        address=self._worker_addresses["researcher"],
                        managed_by="coordinator_host",
                    ),
                    "reporter": self._agent_template(
                        name="reporter",
                        role_group="reporter",
                        role="Builds the Markdown report from approved evidence.",
                        capabilities=REPORTER_CAPABILITIES,
                        address=self._worker_addresses["reporter"],
                        managed_by="coordinator_host",
                    ),
                },
                "links": {
                    "coordinator": ["planner", "researcher", "reporter"],
                    "planner": ["researcher", "reporter"],
                    "researcher": ["planner", "reporter"],
                    "reporter": ["coordinator"],
                },
            }

    def _agent_template(
        self,
        *,
        name: str,
        role_group: str,
        role: str,
        capabilities: list[str],
        address: str | None,
        managed_by: str,
    ) -> dict[str, Any]:
        return {
            "name": name,
            "role_group": role_group,
            "role": role,
            "capabilities": list(capabilities),
            "address": address,
            "state": "suspended",
            "last_event": "idle",
            "details": {
                "managed_by": managed_by,
                "activity": "idle",
            },
        }

    def update_agent(
        self,
        session_id: str,
        agent_name: str,
        *,
        state: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.ensure_session(session_id)
        with self._lock:
            agent = self._sessions[session_id]["agents"][agent_name]
            agent["state"] = state
            base_details = {
                "managed_by": agent.get("details", {}).get("managed_by", "coordinator_host"),
                "activity": "idle",
            }
            base_details.update(details or {})
            if message.strip():
                base_details["user_message"] = message
            agent["last_event"] = message.strip() or base_details.get("activity", state)
            agent["details"] = base_details
            return deepcopy(agent)

    def reset_agents(self, session_id: str, *, include_coordinator: bool = False) -> dict[str, Any]:
        self.ensure_session(session_id)
        with self._lock:
            agents = self._sessions[session_id]["agents"]
            for agent_name, agent in agents.items():
                if agent_name == "coordinator" and not include_coordinator:
                    continue
                managed_by = agent.get("details", {}).get("managed_by", "coordinator_host")
                agent["state"] = "suspended"
                agent["last_event"] = "idle"
                agent["details"] = {
                    "managed_by": managed_by,
                    "activity": "idle",
                }
            return deepcopy(self._sessions[session_id])

    def snapshot(self, session_id: str) -> dict[str, Any] | None:
        with self._lock:
            session = self._sessions.get(session_id)
            return deepcopy(session) if session is not None else None

    def worker_addresses(self) -> dict[str, str | None]:
        with self._lock:
            return deepcopy(self._worker_addresses)
