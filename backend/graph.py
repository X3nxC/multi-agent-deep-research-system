from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import logging
from urllib.parse import urlparse
from typing import Any, Literal

from langchain.chat_models import init_chat_model
from langchain_community.retrievers import ArxivRetriever, TavilySearchAPIRetriever
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel, Field

from .prompts import (
    COORDINATOR_PROMPT,
    PLANNER_PROMPT,
    REPORTER_PROMPT,
    REPORTER_REVISION_PROMPT,
    RESEARCHER_SYNTHESIS_PROMPT,
)
from .schemas import (
    DeepResearchContext,
    DeepResearchState,
    PlannerState,
    ReporterState,
    ResearchTask,
    ResearchWorkerState,
    ReviewDecision,
    SourceDoc,
    TaskFinding,
)

logger = logging.getLogger("backend.graph")

TAVILY_MAX_QUERY_CHARS = 400
TAVILY_QUESTION_BUDGET = 320
TAVILY_KEYWORD_BUDGET = 64


def _normalize_base_url(base_url: str | None) -> str | None:
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.netloc in {"api.deepseek.com", "api.openai.com"} and not parsed.path.rstrip("/"):
        return f"{base_url.rstrip('/')}/v1"
    return base_url


def _build_chat_model(model_name: str, runtime: Runtime[DeepResearchContext]):
    kwargs: dict[str, Any] = {
        "model": model_name,
        "model_provider": runtime.context.llm_provider,
    }
    if runtime.context.llm_api_key:
        kwargs["api_key"] = runtime.context.llm_api_key
    normalized_base_url = _normalize_base_url(runtime.context.llm_base_url)
    if normalized_base_url:
        kwargs["base_url"] = normalized_base_url
    return init_chat_model(**kwargs)


def _as_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text") or item.get("content") or ""
                if text:
                    parts.append(str(text))
        return "\n".join(part for part in parts if part).strip()
    return str(content)


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _normalize_query_text(text: str) -> str:
    return " ".join((text or "").split()).strip()


def _build_tavily_query(task: ResearchTask) -> str:
    question = _clip(_normalize_query_text(task.get("question", "")), TAVILY_QUESTION_BUDGET)
    keywords = [
        _normalize_query_text(item)
        for item in task.get("keywords", [])[:6]
        if _normalize_query_text(item)
    ]
    keyword_text = _clip(", ".join(keywords), TAVILY_KEYWORD_BUDGET)

    query = question
    if keyword_text:
        query = f"{question} | keywords: {keyword_text}"
    return _clip(query, TAVILY_MAX_QUERY_CHARS)


def _build_arxiv_query(task: ResearchTask) -> str:
    return _normalize_query_text(task.get("question", ""))


def _format_arxiv_warning(exc: Exception) -> str:
    message = _normalize_query_text(str(exc))
    lower = message.lower()
    if "429" in lower or "too many requests" in lower:
        return (
            "arXiv retrieval hit rate limits (HTTP 429). "
            "The system continued with Tavily results only for this task."
        )
    return (
        "arXiv retrieval failed for this task. "
        "The system continued with Tavily results only."
    )


def _normalize_doc(doc: Any, source: Literal["tavily", "arxiv"], query: str, max_chars: int) -> SourceDoc:
    metadata = getattr(doc, "metadata", {}) or {}
    title = (
        metadata.get("title")
        or metadata.get("Title")
        or metadata.get("source")
        or metadata.get("Entry ID")
        or f"{source} result"
    )
    url = (
        metadata.get("url")
        or metadata.get("source")
        or metadata.get("Entry ID")
        or metadata.get("Source")
        or ""
    )
    published = metadata.get("published") or metadata.get("Published")
    score = metadata.get("score")
    snippet = _clip(getattr(doc, "page_content", "") or "", max_chars)
    return {
        "source": source,
        "title": str(title),
        "url": str(url),
        "snippet": snippet,
        "published": str(published) if published is not None else None,
        "score": float(score) if isinstance(score, (int, float)) else None,
        "query_used": query,
    }


def _format_docs_for_prompt(docs: list[SourceDoc]) -> str:
    if not docs:
        return "No evidence retrieved."

    lines: list[str] = []
    for idx, doc in enumerate(docs, start=1):
        lines.append(
            "\n".join(
                [
                    f"[{idx}] source={doc['source']}",
                    f"title={doc['title']}",
                    f"url={doc['url']}",
                    f"published={doc['published']}",
                    f"query_used={doc['query_used']}",
                    f"snippet={doc['snippet']}",
                ]
            )
        )
    return "\n\n".join(lines)


def _format_references(docs: list[SourceDoc]) -> list[str]:
    refs: list[str] = []
    seen: set[tuple[str, str]] = set()
    for doc in docs:
        key = (doc["title"], doc["url"])
        if key in seen:
            continue
        seen.add(key)
        if doc["url"]:
            refs.append(f"- [{doc['source']}] {doc['title']} — {doc['url']}")
        else:
            refs.append(f"- [{doc['source']}] {doc['title']}")
    return refs


class PlannedTaskModel(BaseModel):
    task_id: str = Field(description="Stable short identifier, e.g. task_1")
    title: str = Field(description="Short human-readable title")
    question: str = Field(description="Specific research question for retrieval")
    objective: str = Field(description="Why this task matters")
    keywords: list[str] = Field(default_factory=list)
    sources: list[Literal["tavily", "arxiv"]] = Field(
        description="Relevant sources for this task"
    )


class PlanResult(BaseModel):
    plan: list[PlannedTaskModel]


class CoordinatorDecisionModel(BaseModel):
    request_type: Literal["research", "non_research"] = Field(
        description="Whether the request should enter the deep research workflow."
    )
    research_brief: str = Field(
        default="",
        description="Markdown research brief for downstream agents when request_type is research.",
    )
    coordinator_response: str = Field(
        default="",
        description="Direct user-facing reply when request_type is non_research.",
    )


class TaskFindingModel(BaseModel):
    summary_md: str = Field(description="Compact task-level markdown summary")
    key_points: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


def _fallback_plan(query: str) -> list[ResearchTask]:
    return [
        {
            "task_id": "task_1",
            "title": "Background and framing",
            "question": f"What is the current background, terminology, and framing for: {query}?",
            "objective": "Establish baseline understanding and terminology.",
            "keywords": query.split()[:8],
            "sources": ["tavily", "arxiv"],
        },
        {
            "task_id": "task_2",
            "title": "Evidence and recent developments",
            "question": f"What are the most relevant evidence, comparisons, and recent developments related to: {query}?",
            "objective": "Collect substantive evidence and recent signals.",
            "keywords": query.split()[:8],
            "sources": ["tavily", "arxiv"],
        },
    ]


async def coordinator_brief_node(
    state: DeepResearchState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    model = _build_chat_model(runtime.context.coordinator_model, runtime)
    coordinator = model.with_structured_output(CoordinatorDecisionModel)
    result = await coordinator.ainvoke(
        [
            {
                "role": "system",
                "content": COORDINATOR_PROMPT.format(
                    language=runtime.context.report_language
                ),
            },
            {"role": "user", "content": state["query"]},
        ]
    )
    return {
        "request_type": result.request_type,
        "research_brief": _as_text(result.research_brief).strip(),
        "coordinator_response": _as_text(result.coordinator_response).strip(),
    }


async def non_research_response_node(
    state: DeepResearchState,
    _runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    response = str(state.get("coordinator_response") or "").strip()
    if not response:
        response = "I can help with research-oriented requests. Ask me to investigate a topic, compare options, or gather evidence."
    return {"final_report": response}


async def planner_node(
    state: PlannerState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    model = _build_chat_model(runtime.context.planner_model, runtime)
    planner = model.with_structured_output(PlanResult)
    result = await planner.ainvoke(
        [
            {
                "role": "system",
                "content": PLANNER_PROMPT.format(
                    max_tasks=runtime.context.max_plan_tasks,
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Original query:\n{state['query']}\n\n"
                    f"Research brief:\n{state['research_brief']}"
                ),
            },
        ]
    )

    if not result.plan:
        return {"plan": _fallback_plan(state["query"])}

    plan = [task.model_dump() for task in result.plan[: runtime.context.max_plan_tasks]]
    return {"plan": plan}


async def planner_agent_node(
    state: DeepResearchState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    result = await build_planner_graph().ainvoke(
        {
            "query": state["query"],
            "research_brief": state["research_brief"],
        },
        context=runtime.context,
    )
    return {"plan": result["plan"]}


async def prepare_source_queries_node(
    state: ResearchWorkerState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    task = state["task"]
    return {
        "source_queries": {
            "tavily": _build_tavily_query(task),
            "arxiv": _build_arxiv_query(task),
        }
    }


def route_sources(state: ResearchWorkerState) -> list[Send]:
    sends: list[Send] = []
    source_queries = state.get("source_queries", {})
    for source in state["task"].get("sources", []):
        if source == "tavily":
            sends.append(
                Send(
                    "search_tavily",
                    {
                        "task": state["task"],
                        "source_queries": source_queries,
                    },
                )
            )
        elif source == "arxiv":
            sends.append(
                Send(
                    "search_arxiv",
                    {
                        "task": state["task"],
                        "source_queries": source_queries,
                    },
                )
            )
    if sends:
        return sends

    return [
        Send(
            "search_tavily",
            {
                "task": state["task"],
                "source_queries": source_queries,
            },
        )
    ]


async def search_tavily_node(
    state: ResearchWorkerState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    query = state["source_queries"].get("tavily", state["task"]["question"])
    retriever = TavilySearchAPIRetriever(
        k=runtime.context.max_tavily_results,
        api_key=runtime.context.tavily_api_key,
    )
    docs = await retriever.ainvoke(query)
    normalized = [
        _normalize_doc(doc, "tavily", query, runtime.context.max_snippet_chars)
        for doc in docs
    ]
    return {"raw_docs": normalized, "retrieval_warnings": []}


async def search_arxiv_node(
    state: ResearchWorkerState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    query = state["source_queries"].get("arxiv", state["task"]["question"])
    try:
        retriever = ArxivRetriever(
            load_max_docs=runtime.context.max_arxiv_results,
            load_all_available_meta=True,
        )
        docs = await retriever.ainvoke(query)
        normalized = [
            _normalize_doc(doc, "arxiv", query, runtime.context.max_snippet_chars)
            for doc in docs
        ]
        return {"raw_docs": normalized, "retrieval_warnings": []}
    except Exception as exc:
        warning = _format_arxiv_warning(exc)
        logger.warning("arXiv retrieval degraded to Tavily-only mode: %s", exc)
        return {"raw_docs": [], "retrieval_warnings": [warning]}


async def synthesize_task_finding_node(
    state: ResearchWorkerState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    task = state["task"]
    docs = state.get("raw_docs", [])
    retrieval_warnings = list(dict.fromkeys(state.get("retrieval_warnings", [])))

    if not docs:
        key_points = ["No retrievable evidence was returned."]
        open_questions = ["Broaden keywords or add another retrieval source."]
        if retrieval_warnings:
            key_points.extend(retrieval_warnings)
            open_questions.extend(retrieval_warnings)
        finding: TaskFinding = {
            "task_id": task["task_id"],
            "title": task["title"],
            "question": task["question"],
            "summary_md": (
                f"### {task['title']}\n\n"
                "No evidence was retrieved from the configured sources for this task."
            ),
            "key_points": key_points,
            "open_questions": open_questions,
            "confidence": "low",
            "references": [],
        }
        return {"finding": finding}

    model = _build_chat_model(runtime.context.researcher_model, runtime)
    summarizer = model.with_structured_output(TaskFindingModel)
    evidence_blob = _format_docs_for_prompt(docs)
    result = await summarizer.ainvoke(
        [
            {
                "role": "system",
                "content": RESEARCHER_SYNTHESIS_PROMPT.format(
                    language=runtime.context.report_language
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task title: {task['title']}\n"
                    f"Task question: {task['question']}\n"
                    f"Task objective: {task['objective']}\n\n"
                    f"Evidence:\n{evidence_blob}"
                ),
            },
        ]
    )

    finding = {
        "task_id": task["task_id"],
        "title": task["title"],
        "question": task["question"],
        "summary_md": result.summary_md,
        "key_points": result.key_points,
        "open_questions": list(dict.fromkeys([*result.open_questions, *retrieval_warnings])),
        "confidence": result.confidence,
        "references": _format_references(docs),
    }
    return {"finding": finding}


async def researcher_agent_node(
    state: dict[str, Any],
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    task: ResearchTask = state["task"]
    result = await build_researcher_graph().ainvoke(
        {
            "task": task,
            "source_queries": {},
            "raw_docs": [],
            "retrieval_warnings": [],
        },
        context=runtime.context,
    )
    return {"findings": [result["finding"]]}


async def reporter_node(
    state: ReporterState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    model = _build_chat_model(runtime.context.reporter_model, runtime)
    findings_block = []
    for finding in state.get("findings", []):
        findings_block.append(
            "\n".join(
                [
                    f"Task ID: {finding['task_id']}",
                    f"Title: {finding['title']}",
                    f"Question: {finding['question']}",
                    f"Confidence: {finding['confidence']}",
                    f"Summary:\n{finding['summary_md']}",
                    "Key points:",
                    *[f"- {point}" for point in finding.get("key_points", [])],
                    "Open questions:",
                    *[f"- {question}" for question in finding.get("open_questions", [])],
                    "References:",
                    *finding.get("references", []),
                ]
            )
        )

    findings_text = "\n\n---\n\n".join(findings_block) or "No task findings were produced."
    plan_text = "\n".join(
        f"- {task['task_id']}: {task['title']} ({', '.join(task['sources'])})"
        for task in state.get("plan", [])
    ) or "- No explicit plan available"

    if state.get("review_feedback"):
        prompt = REPORTER_REVISION_PROMPT.format(
            language=runtime.context.report_language,
        )
        user_content = (
            f"Original query:\n{state['query']}\n\n"
            f"Research brief:\n{state['research_brief']}\n\n"
            f"Plan:\n{plan_text}\n\n"
            f"Current draft:\n{state['draft_report']}\n\n"
            f"Human review feedback:\n{state['review_feedback']}\n\n"
            f"Supporting findings:\n{findings_text}"
        )
    else:
        prompt = REPORTER_PROMPT.format(
            language=runtime.context.report_language,
        )
        user_content = (
            f"Original query:\n{state['query']}\n\n"
            f"Research brief:\n{state['research_brief']}\n\n"
            f"Plan:\n{plan_text}\n\n"
            f"Findings:\n{findings_text}"
        )

    response = await model.ainvoke(
        [
            {"role": "system", "content": prompt},
            {"role": "user", "content": user_content},
        ]
    )
    return {"draft_report": _as_text(response.content)}


async def reporter_agent_node(
    state: DeepResearchState,
    runtime: Runtime[DeepResearchContext],
) -> dict[str, Any]:
    result = await build_reporter_graph().ainvoke(
        {
            "query": state["query"],
            "research_brief": state["research_brief"],
            "plan": state.get("plan", []),
            "findings": state.get("findings", []),
            "draft_report": state.get("draft_report", ""),
            "review_feedback": state.get("review_feedback"),
        },
        context=runtime.context,
    )
    return {"draft_report": result["draft_report"]}


def review_node(
    state: DeepResearchState,
    runtime: Runtime[DeepResearchContext],
) -> Command:
    payload = {
        "instruction": (
            "Review the Markdown draft. Resume with one of: "
            "{'action': 'approve'} | {'action': 'edit', 'edited_markdown': '...'} | "
            "{'action': 'revise', 'feedback': '...'}"
        ),
        "review_round": state.get("review_round", 0),
        "draft_report": state["draft_report"],
    }
    decision = interrupt(payload)
    action = (decision or {}).get("action", "approve")

    if action == "edit":
        edited_markdown = (decision or {}).get("edited_markdown", state["draft_report"])
        return Command(
            update={
                "draft_report": edited_markdown,
                "review_feedback": None,
            },
            goto="finalize",
        )

    if action == "revise":
        review_round = int(state.get("review_round", 0)) + 1
        feedback = (decision or {}).get("feedback", "Please revise the report.")
        if review_round > runtime.context.max_review_loops:
            return Command(
                update={
                    "review_feedback": feedback,
                    "review_round": review_round,
                },
                goto="finalize",
            )
        return Command(
            update={
                "review_feedback": feedback,
                "review_round": review_round,
            },
            goto="reporter_agent",
        )

    return Command(
        update={"review_feedback": None},
        goto="finalize",
    )


def finalize_node(state: DeepResearchState) -> dict[str, Any]:
    return {"final_report": state["draft_report"]}


def route_research_tasks(state: DeepResearchState) -> list[Send]:
    tasks = state.get("plan", [])
    if not tasks:
        tasks = _fallback_plan(state["query"])

    return [Send("researcher_agent", {"task": task}) for task in tasks]


@lru_cache(maxsize=1)
def build_coordinator_graph():
    builder = StateGraph(DeepResearchState, context_schema=DeepResearchContext)
    builder.add_node("coordinator", coordinator_brief_node)
    builder.add_edge(START, "coordinator")
    builder.add_edge("coordinator", END)
    return builder.compile()


@lru_cache(maxsize=1)
def build_planner_graph():
    builder = StateGraph(PlannerState, context_schema=DeepResearchContext)
    builder.add_node("planner", planner_node)
    builder.add_edge(START, "planner")
    builder.add_edge("planner", END)
    return builder.compile()


@lru_cache(maxsize=1)
def build_researcher_graph():
    builder = StateGraph(ResearchWorkerState, context_schema=DeepResearchContext)
    builder.add_node("prepare_queries", prepare_source_queries_node)
    builder.add_node("search_tavily", search_tavily_node)
    builder.add_node("search_arxiv", search_arxiv_node)
    builder.add_node("synthesize_finding", synthesize_task_finding_node)

    builder.add_edge(START, "prepare_queries")
    builder.add_conditional_edges(
        "prepare_queries",
        route_sources,
        ["search_tavily", "search_arxiv"],
    )
    builder.add_edge("search_tavily", "synthesize_finding")
    builder.add_edge("search_arxiv", "synthesize_finding")
    builder.add_edge("synthesize_finding", END)
    return builder.compile()


@lru_cache(maxsize=1)
def build_reporter_graph():
    builder = StateGraph(ReporterState, context_schema=DeepResearchContext)
    builder.add_node("reporter", reporter_node)
    builder.add_edge(START, "reporter")
    builder.add_edge("reporter", END)
    return builder.compile()


def build_deep_research_graph(checkpointer: Any | None = None):
    def route_after_coordinator(state: DeepResearchState) -> str:
        if state.get("request_type") == "non_research":
            return "non_research_response"
        return "planner_agent"

    builder = StateGraph(DeepResearchState, context_schema=DeepResearchContext)
    builder.add_node("coordinator_brief", coordinator_brief_node)
    builder.add_node("non_research_response", non_research_response_node)
    builder.add_node("planner_agent", planner_agent_node)
    builder.add_node("researcher_agent", researcher_agent_node)
    builder.add_node("reporter_agent", reporter_agent_node)
    builder.add_node("human_review", review_node)
    builder.add_node("finalize", finalize_node)

    builder.add_edge(START, "coordinator_brief")
    builder.add_conditional_edges(
        "coordinator_brief",
        route_after_coordinator,
        ["non_research_response", "planner_agent"],
    )
    builder.add_edge("non_research_response", END)
    builder.add_conditional_edges(
        "planner_agent",
        route_research_tasks,
        ["researcher_agent"],
    )
    builder.add_edge("researcher_agent", "reporter_agent")
    builder.add_edge("reporter_agent", "human_review")
    builder.add_edge("finalize", END)

    return builder.compile(checkpointer=checkpointer)


def dedupe_references(report: str) -> str:
    """Optional helper for downstream post-processing.

    The Reporter agent is instructed to deduplicate references itself, but keeping a
    deterministic helper is useful for tests or external pipelines.
    """
    lines = report.splitlines()
    out: list[str] = []
    in_refs = False
    seen: OrderedDict[str, None] = OrderedDict()

    for line in lines:
        if line.strip() == "## References":
            in_refs = True
            out.append(line)
            continue

        if in_refs:
            if line.startswith("## "):
                for ref in seen.keys():
                    out.append(ref)
                seen.clear()
                in_refs = False
                out.append(line)
                continue
            if line.strip().startswith("-"):
                seen.setdefault(line, None)
                continue
        out.append(line)

    if in_refs:
        for ref in seen.keys():
            out.append(ref)

    return "\n".join(out)
