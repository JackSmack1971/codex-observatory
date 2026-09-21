from __future__ import annotations

from typing import Literal, TypeVar

from pydantic import BaseModel, ConfigDict


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


Coverage = Literal["available", "unavailable", "empty", "degraded"]
T = TypeVar("T")


class Page[T](ApiModel):
    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


class Evidence(ApiModel):
    source_class: str = "UNKNOWN"
    fact_type: str = "observed"
    stability: str | None = None
    source_event: str | None = None
    correlation_method: str | None = None
    correlation_confidence: str | None = None


class Metric(ApiModel):
    value: int | float | None
    coverage: Coverage
    evidence: Evidence | None = None


class Session(ApiModel):
    session_id: str | None
    thread_id: str
    name: str | None
    cwd: str | None
    model: str | None
    started_at: str | None
    updated_at: str | None
    status: str | None
    archived: bool | None
    turns: int
    tokens: int | None
    tool_calls: int
    evidence: Evidence


class Thread(Session):
    pass


class Turn(ApiModel):
    turn_id: str
    thread_id: str
    status: str
    started_at: str | None
    completed_at: str | None
    duration_ms: int | None
    evidence: Evidence


class Agent(ApiModel):
    agent_id: str
    parent_agent_id: str | None
    agent_type: str | None
    state: str
    thread_id: str | None
    started_at: str | None
    stopped_at: str | None
    tool_count: int
    evidence: Evidence


class Tool(ApiModel):
    tool: str
    calls: int
    successes: int
    failures: int
    unknown_results: int
    source_breakdown: dict[str, int]
    evidence: Evidence


class Approval(ApiModel):
    approval_id: str
    decision: str | None
    decision_source: str | None
    tool: str | None
    session_id: str | None
    thread_id: str | None
    observed_at: str
    evidence: Evidence


class Skill(ApiModel):
    skill: str
    status: str | None
    thread_id: str | None
    observed_at: str
    evidence: Evidence


class Repository(ApiModel):
    repo_id: str
    root: str
    remote_identity: str | None
    worktrees: list[str]
    evidence: Evidence


class GitSnapshot(ApiModel):
    snapshot_id: str
    repo_id: str
    worktree_id: str
    head_sha: str | None
    head_ref: str | None
    head_state: str
    detached: bool
    clean: bool
    staged: int
    unstaged: int
    untracked: int
    conflicted: int
    insertions: int
    deletions: int
    captured_at: str
    evidence: Evidence


class Event(ApiModel):
    event_id: str
    sequence: int
    event_time: str
    category: str
    name: str
    status: str | None
    session_id: str | None
    thread_id: str | None
    turn_id: str | None
    evidence: Evidence


class ComponentHealth(ApiModel):
    component: str
    status: str
    reason: str | None = None
    last_observed: str | None = None


class Health(ApiModel):
    status: str
    components: list[ComponentHealth]


class ArchiveHealth(ApiModel):
    status: str
    batches: int
    files: int
    rows: int
    verification_failures: int
    small_files: int
    duckdb_status: str


class AdminUsageHealth(ApiModel):
    collector: str
    status: str
    reason: str | None = None
    last_success: str | None = None
    last_error: str | None = None


class AdminCompletionUsage(ApiModel):
    result_identity: str
    revision: int
    source: str
    usage_family: str
    bucket_start: int
    bucket_end: int
    bucket_width: str
    project_id: str | None
    user_id: str | None
    api_key_id: str | None
    model: str | None
    batch: bool | None
    service_tier: str | None
    input_tokens: int
    output_tokens: int
    num_model_requests: int
    input_audio_tokens: int | None
    input_cache_write_tokens: int | None
    input_cached_audio_tokens: int | None
    input_cached_image_tokens: int | None
    input_cached_text_tokens: int | None
    input_cached_tokens: int | None
    input_image_tokens: int | None
    input_text_tokens: int | None
    input_uncached_tokens: int | None
    output_audio_tokens: int | None
    output_image_tokens: int | None
    output_text_tokens: int | None
    request_start: int
    request_end: int
    retrieved_at: str
    adapter_schema_version: str
    first_observed_at: str
    last_observed_at: str


class Overview(ApiModel):
    sessions: Metric
    threads: Metric
    turns: Metric
    events: Metric
    tool_calls: Metric
    tool_failures: Metric
    approvals: Metric
    agents: Metric
    tokens: Metric
    repositories: Metric
    health: Health
    archive: ArchiveHealth
