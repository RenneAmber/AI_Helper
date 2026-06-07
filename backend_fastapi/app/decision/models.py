from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Index, Float
from sqlalchemy.orm import declarative_base
from datetime import datetime

Base = declarative_base()


class Decision(Base):
    """决策记录主表"""
    __tablename__ = "decisions"
    
    decision_id = Column(String(128), primary_key=True)
    schema_version = Column(String(32), nullable=False, default="decision_record.v1")
    title = Column(String(255), nullable=False)
    question = Column(Text, nullable=False)
    domain = Column(String(64), nullable=False, default="engineering")
    status = Column(String(32), nullable=False, default="draft")  # draft, running, final, aborted
    requester_user_id = Column(String(128), nullable=False)
    
    context_json = Column(Text, nullable=False, default="{}")
    criteria_json = Column(Text, nullable=False, default="[]")
    plan_json = Column(Text, nullable=False, default="{}")
    analysis_json = Column(Text, nullable=False, default="{}")
    decision_json = Column(Text, nullable=False, default="{}")
    followup_json = Column(Text, nullable=False, default="{}")
    
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class EvidenceItem(Base):
    """证据项目表"""
    __tablename__ = "evidence_items"
    
    evidence_id = Column(String(128), primary_key=True)
    decision_id = Column(String(128), ForeignKey("decisions.decision_id"), nullable=False)
    kind = Column(String(64), nullable=False)  # doc, log, metric, etc
    source_type = Column(String(64), nullable=False)  # internal_doc, log, db, etc
    source_uri = Column(Text, nullable=False)
    source_title = Column(Text)
    retrieved_at = Column(DateTime, nullable=False)
    quote = Column(Text, nullable=False)
    signals_json = Column(Text, nullable=False)  # recencyDays, reliability, relevance
    tags_json = Column(Text, nullable=False, default="[]")
    content_hash = Column(String(128), nullable=False)


class ToolRun(Base):
    """工具执行记录表"""
    __tablename__ = "tool_runs"
    
    run_id = Column(String(128), primary_key=True)
    decision_id = Column(String(128), ForeignKey("decisions.decision_id"), nullable=False)
    tool_name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)  # running, success, failure
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime)
    input_hash = Column(String(128), nullable=False)
    output_hash = Column(String(128))
    error_code = Column(String(128))
    error_message = Column(Text)


class DecisionEvent(Base):
    """决策执行事件日志表"""
    __tablename__ = "decision_events"
    
    event_id = Column(String(128), primary_key=True)
    decision_id = Column(String(128), ForeignKey("decisions.decision_id"), nullable=False)
    event_type = Column(String(64), nullable=False)  # NODE_START, NODE_END, TOOL_RUN
    node_name = Column(String(128), nullable=False)
    status = Column(String(32), nullable=False)  # success, failure
    payload_json = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# Indexes
Index("idx_decisions_created_at", Decision.created_at)
Index("idx_decisions_domain", Decision.domain)
Index("idx_decisions_status", Decision.status)
Index("idx_evidence_decision_id", EvidenceItem.decision_id)
Index("idx_tool_runs_decision_id", ToolRun.decision_id)
Index("idx_tool_runs_status", ToolRun.status)
Index("idx_events_decision_id", DecisionEvent.decision_id)
Index("idx_events_created_at", DecisionEvent.created_at)
Index("idx_events_node_name", DecisionEvent.node_name)
