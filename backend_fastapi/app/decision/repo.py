from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from datetime import datetime
from typing import List

from app.decision.models import DecisionEvent, ToolRun, Decision, EvidenceItem


class DecisionRepo:
    """决策系统数据库操作封装"""
    
    def __init__(self, session: AsyncSession):
        self.session = session
    
    async def add_event(
        self,
        event_id: str,
        decision_id: str,
        event_type: str,
        node_name: str,
        status: str,
        payload_json: str
    ):
        """记录决策执行事件"""
        self.session.add(DecisionEvent(
            event_id=event_id,
            decision_id=decision_id,
            event_type=event_type,
            node_name=node_name,
            status=status,
            payload_json=payload_json
        ))
        await self.session.commit()
    
    async def add_tool_run(
        self,
        run_id: str,
        decision_id: str,
        tool_name: str,
        status: str,
        started_at: datetime,
        input_hash: str
    ):
        """记录工具执行开始"""
        self.session.add(ToolRun(
            run_id=run_id,
            decision_id=decision_id,
            tool_name=tool_name,
            status=status,
            started_at=started_at,
            input_hash=input_hash
        ))
        await self.session.commit()
    
    async def finish_tool_run_success(
        self,
        run_id: str,
        ended_at: datetime,
        output_hash: str
    ):
        """标记工具执行成功"""
        tr = await self.session.get(ToolRun, run_id)
        if tr:
            tr.status = "success"
            tr.ended_at = ended_at
            tr.output_hash = output_hash
            await self.session.commit()
    
    async def finish_tool_run_failure(
        self,
        run_id: str,
        ended_at: datetime,
        error_code: str,
        error_message: str
    ):
        """标记工具执行失败"""
        tr = await self.session.get(ToolRun, run_id)
        if tr:
            tr.status = "failure"
            tr.ended_at = ended_at
            tr.error_code = error_code
            tr.error_message = error_message
            await self.session.commit()
    
    async def get_events(self, decision_id: str) -> List[DecisionEvent]:
        """获取决策的所有事件（按时间排序）"""
        q = select(DecisionEvent).where(
            DecisionEvent.decision_id == decision_id
        ).order_by(DecisionEvent.created_at.asc())
        res = await self.session.execute(q)
        return [row[0] for row in res.all()]
    
    async def get_tool_runs(self, decision_id: str) -> List[ToolRun]:
        """获取决策的所有工具执行记录"""
        q = select(ToolRun).where(
            ToolRun.decision_id == decision_id
        ).order_by(ToolRun.started_at.asc())
        res = await self.session.execute(q)
        return [row[0] for row in res.all()]
    
    async def get_evidence_items(self, decision_id: str) -> List[EvidenceItem]:
        """获取决策的所有证据项"""
        q = select(EvidenceItem).where(
            EvidenceItem.decision_id == decision_id
        ).order_by(EvidenceItem.retrieved_at.asc())
        res = await self.session.execute(q)
        return [row[0] for row in res.all()]
    
    async def create_decision(self, decision: Decision):
        """创建决策记录"""
        self.session.add(decision)
        await self.session.commit()
    
    async def get_decision(self, decision_id: str) -> Decision:
        """获取决策记录"""
        return await self.session.get(Decision, decision_id)
    
    async def update_decision(self, decision: Decision):
        """更新决策记录"""
        await self.session.merge(decision)
        await self.session.commit()
    
    async def add_evidence_item(self, evidence: EvidenceItem):
        """添加证据项"""
        self.session.add(evidence)
        await self.session.commit()
