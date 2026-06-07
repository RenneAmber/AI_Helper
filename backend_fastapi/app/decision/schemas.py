from pydantic import BaseModel, Field
from typing import List, Dict, Optional, Literal
from datetime import datetime


class CriteriaItem(BaseModel):
    key: str
    weight: float


class PlanStep(BaseModel):
    stepId: str
    type: str
    desc: str


class Plan(BaseModel):
    steps: List[PlanStep] = Field(default_factory=list)


class EvidenceSource(BaseModel):
    sourceType: str
    uri: str
    title: Optional[str] = None
    retrievedAt: str  # ISO format


class EvidenceSignals(BaseModel):
    recencyDays: int
    reliability: Literal["high", "medium", "low"]
    relevance: Literal["high", "medium", "low"]


class EvidenceItemModel(BaseModel):
    evidenceId: str
    kind: str
    source: EvidenceSource
    quote: str
    signals: EvidenceSignals
    tags: List[str] = Field(default_factory=list)
    hash: str


class EvidenceConflict(BaseModel):
    conflictId: str
    type: str
    summary: str
    involvedEvidenceIds: List[str]
    resolution: Optional[str] = None


class EvidencePack(BaseModel):
    items: List[EvidenceItemModel] = Field(default_factory=list)
    conflicts: List[EvidenceConflict] = Field(default_factory=list)


class DecisionRationale(BaseModel):
    text: str
    evidenceIds: List[str] = Field(default_factory=list)


class DecisionOutput(BaseModel):
    recommendation: str
    confidence: float
    rationale: List[DecisionRationale] = Field(default_factory=list)
    safetyNotes: List[str] = Field(default_factory=list)
    uncertainties: List[str] = Field(default_factory=list)
    nextSteps: List[str] = Field(default_factory=list)


class FollowUp(BaseModel):
    reviewAfterDays: int = 30
    successMetrics: List[Dict[str, str]] = Field(default_factory=list)
    tasks: List[Dict[str, str]] = Field(default_factory=list)


class DecisionContext(BaseModel):
    system: str
    background: Optional[str] = None
    constraints: List[str] = Field(default_factory=list)
    riskPosture: Literal["low", "medium", "high"] = "low"
    timeHorizonDays: int = 90


class CreateDecisionRequest(BaseModel):
    title: Optional[str] = None
    question: str
    domain: str = "engineering"
    requester: Dict[str, str]
    context: DecisionContext
    criteria: List[CriteriaItem] = Field(default_factory=list)


class DecisionRecord(BaseModel):
    schemaVersion: str = "decision_record.v1"
    decisionId: str
    title: str
    question: str
    domain: str
    status: Literal["draft", "running", "final", "aborted"]
    createdAt: datetime
    updatedAt: datetime
    requester: Dict[str, str]
    context: DecisionContext
    criteria: List[CriteriaItem]
    plan: Plan
    evidencePack: EvidencePack
    analysis: Dict
    decision: DecisionOutput
    followUp: FollowUp


class DecisionEventModel(BaseModel):
    time: str  # ISO format
    type: str  # NODE_START, NODE_END, TOOL_RUN
    node: str
    status: str
    payload: Dict


class DecisionReplayResponse(BaseModel):
    decisionId: str
    events: List[DecisionEventModel]
    toolRuns: List[Dict] = Field(default_factory=list)
    evidenceItems: List[EvidenceItemModel] = Field(default_factory=list)
