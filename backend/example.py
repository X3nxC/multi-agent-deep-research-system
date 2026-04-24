from __future__ import annotations

import asyncio
import textwrap

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from backend import DeepResearchContext, build_deep_research_graph


QUERY = "Assess retrieval-augmented generation evaluation methods for production LLM systems."


async def main() -> None:
    graph = build_deep_research_graph(checkpointer=InMemorySaver())
    context = DeepResearchContext(
        coordinator_model="gpt-5.4-mini",
        planner_model="gpt-5.4-mini",
        researcher_model="gpt-5.4-mini",
        reporter_model="gpt-5.4",
        report_language="en-US",
        max_plan_tasks=4,
        max_tavily_results=4,
        max_arxiv_results=3,
    )
    config = {"configurable": {"thread_id": "demo-thread-001"}}

    initial_state = {
        "query": QUERY,
        "research_brief": "",
        "plan": [],
        "findings": [],
        "draft_report": "",
        "final_report": "",
        "review_feedback": None,
        "review_round": 0,
    }

    result = await graph.ainvoke(initial_state, config=config, context=context)

    if "__interrupt__" in result:
        interrupt_payload = result["__interrupt__"][0].value
        print("\n=== HUMAN REVIEW PAYLOAD ===\n")
        print(textwrap.shorten(interrupt_payload["draft_report"], width=1200, placeholder=" ..."))

        review_decision = {
            "action": "approve"
            # can be following actions:
            # "action": "revise", "feedback": "Add a clearer limitations section."
            # "action": "edit", "edited_markdown": "# My manually edited report ..."
        }
        result = await graph.ainvoke(Command(resume=review_decision), config=config, context=context)

    print("\n=== FINAL MARKDOWN REPORT ===\n")
    print(result["final_report"])

    print("\n=== CHECKPOINT HISTORY (LATEST FIRST) ===\n")
    for snapshot in list(graph.get_state_history(config))[:5]:
        cfg = snapshot.config["configurable"]
        print(
            {
                "step": snapshot.metadata.get("step"),
                "checkpoint_ns": cfg.get("checkpoint_ns"),
                "checkpoint_id": cfg.get("checkpoint_id"),
                "next": snapshot.next,
            }
        )


if __name__ == "__main__":
    asyncio.run(main())
