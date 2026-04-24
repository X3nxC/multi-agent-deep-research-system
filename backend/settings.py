from __future__ import annotations

from functools import lru_cache

from dotenv import load_dotenv
from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .schemas import DeepResearchContext


load_dotenv()


class Settings(BaseSettings):
    openai_api_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("OPENAI_API_KEY", "DEEPSEEK_API_KEY"),
    )
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias=AliasChoices(
            "OPENAI_BASE_URL",
            "OPENAI_API_BASE_URL",
            "OPENA_API_BASE_URL",
            "DEEPSEEK_BASE_URL",
        ),
    )

    coordinator_model: str = Field(
        default="gpt-5.4-mini",
        alias="COORDINATOR_MODEL",
    )
    worker_model: str = Field(
        default="gpt-5.4-mini",
        alias="WORKER_MODEL",
    )

    tavily_api_key: str | None = Field(default=None, alias="TAVILY_API_KEY")

    app_host: str = Field(default="127.0.0.1", alias="APP_HOST")
    app_port: int = Field(default=8000, alias="APP_PORT")
    public_base_url: str | None = Field(default=None, alias="PUBLIC_BASE_URL")

    session_log_file: str = Field(
        default="data/session_log.jsonl",
        alias="SESSION_LOG_FILE",
    )
    completed_reports_dir: str = Field(
        default="data/reports",
        alias="COMPLETED_REPORTS_DIR",
    )

    report_language: str = Field(default="en-US", alias="REPORT_LANGUAGE")
    max_plan_tasks: int = Field(default=4, alias="MAX_PLAN_TASKS")
    max_tavily_results: int = Field(default=5, alias="MAX_TAVILY_RESULTS")
    max_arxiv_results: int = Field(default=3, alias="MAX_ARXIV_RESULTS")
    max_snippet_chars: int = Field(default=1200, alias="MAX_SNIPPET_CHARS")
    max_review_loops: int = Field(default=2, alias="MAX_REVIEW_LOOPS")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    error_log_file: str = Field(
        default="data/backend-errors.log",
        alias="ERROR_LOG_FILE",
    )

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    def to_graph_context(self) -> DeepResearchContext:
        return DeepResearchContext(
            llm_provider="openai",
            llm_api_key=self.openai_api_key,
            llm_base_url=self.openai_base_url,
            coordinator_model=self.coordinator_model,
            planner_model=self.worker_model,
            researcher_model=self.worker_model,
            reporter_model=self.worker_model,
            report_language=self.report_language,
            max_plan_tasks=self.max_plan_tasks,
            max_tavily_results=self.max_tavily_results,
            max_arxiv_results=self.max_arxiv_results,
            max_snippet_chars=self.max_snippet_chars,
            max_review_loops=self.max_review_loops,
            tavily_api_key=self.tavily_api_key,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
