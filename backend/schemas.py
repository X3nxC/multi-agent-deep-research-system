from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
import operator

from typing_extensions import NotRequired, TypedDict


SourceName = Literal["tavily", "arxiv"]
ConfidenceLevel = Literal["low", "medium", "high"]
ReviewActionName = Literal["approve", "edit", "revise"]


class ResearchTask(TypedDict):
    task_id: str
    title: str
    question: str
    objective: str
    keywords: list[str]
    sources: list[SourceName]


class SourceDoc(TypedDict):
    source: SourceName
    title: str
    url: str
    snippet: str
    published: str | None
    score: float | None
    query_used: str


class TaskFinding(TypedDict):
    task_id: str
    title: str
    question: str
    summary_md: str
    key_points: list[str]
    open_questions: list[str]
    confidence: ConfidenceLevel
    references: list[str]


class DeepResearchState(TypedDict):
    query: str
    research_brief: str
    request_type: NotRequired[Literal["research", "non_research"]]
    coordinator_response: NotRequired[str]
    plan: list[ResearchTask]
    findings: Annotated[list[TaskFinding], operator.add]
    draft_report: str
    final_report: str
    review_feedback: str | None
    review_round: int


class PlannerState(TypedDict):
    query: str
    research_brief: str
    plan: list[ResearchTask]


class ResearchWorkerState(TypedDict):
    task: ResearchTask
    source_queries: dict[str, str]
    raw_docs: Annotated[list[SourceDoc], operator.add]
    retrieval_warnings: Annotated[list[str], operator.add]
    finding: TaskFinding


class ReporterState(TypedDict):
    query: str
    research_brief: str
    plan: list[ResearchTask]
    findings: list[TaskFinding]
    draft_report: str
    review_feedback: str | None


class ReviewDecision(TypedDict):
    action: ReviewActionName
    feedback: NotRequired[str]
    edited_markdown: NotRequired[str]


@dataclass
class DeepResearchContext:
    llm_provider: str = "openai"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    coordinator_model: str = "openai:gpt-5.4-mini"
    planner_model: str = "openai:gpt-5.4-mini"
    researcher_model: str = "openai:gpt-5.4-mini"
    reporter_model: str = "openai:gpt-5.4"
    report_language: str = "en-US"
    max_plan_tasks: int = 4
    max_tavily_results: int = 5
    max_arxiv_results: int = 3
    max_snippet_chars: int = 1200
    max_review_loops: int = 2
    tavily_topic: str = "general"
    tavily_api_key: str | None = None
