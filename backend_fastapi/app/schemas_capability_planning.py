from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CapabilityScenario(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    summary: str = Field(min_length=5, max_length=300)
    priority: int = Field(ge=1, le=10)


class CapabilityPlanRequest(BaseModel):
    project_name: str = Field(min_length=2, max_length=120)
    audience: Literal["interviewer", "business", "tech_lead", "mixed"] = "interviewer"
    focus: Literal["technical_depth", "business_value", "balanced"] = "balanced"
    timeline_days: int = Field(default=5, ge=1, le=30)
    scenarios: list[CapabilityScenario] = Field(min_length=1, max_length=8)
    constraints: list[str] = Field(default_factory=list, max_length=12)


class StepSummary(BaseModel):
    step_no: int
    title: str
    status: Literal["done", "pending"]
    output: str


class UserStory(BaseModel):
    scenario: str
    user_story: str
    acceptance: list[str]
    responsibility_mapping: list[str]


class ArchitectureBlock(BaseModel):
    name: str
    responsibility: str
    interfaces: list[str]


class DataFlowStep(BaseModel):
    stage: str
    input: str
    process: str
    output: str
    observable: str


class SearchStrategy(BaseModel):
    retrieval_layers: list[str]
    rerank_policy: str
    fallback_policy: str
    prompt_principles: list[str]


class ApiContract(BaseModel):
    route: str
    method: str
    purpose: str
    required_fields: list[str]


class OpsChecklist(BaseModel):
    item: str
    metric: str
    alert_rule: str


class CapabilityPlanResponse(BaseModel):
    project_name: str
    step_status: list[StepSummary]
    goal_statement: str
    in_scope: list[str]
    out_of_scope: list[str]
    user_stories: list[UserStory]
    architecture_blocks: list[ArchitectureBlock]
    data_intelligence_flow: list[DataFlowStep]
    agent_search_strategy: SearchStrategy
    api_contracts: list[ApiContract]
    production_ops_checklist: list[OpsChecklist]
    next_inputs_needed: list[str]
