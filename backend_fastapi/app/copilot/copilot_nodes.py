"""
Decision Copilot 简化版 - 核心理念：快速澄清 + 显性化假设 + 轻量推荐

这不追求"完美结论"，而是"快速产生可迭代的决策记录"
"""

import json
from datetime import datetime
from typing import Any, Dict, List
from app.copilot.copilot_state import (
    CopilotState, ClarifiedContext, DecisionOption, Assumption, ConfidenceLevel
)
from app.copilot.copilot_tools import CopilotTools


class CopilotNodes:
    """Decision Copilot 工作流节点（简化版）"""

    CONFIDENCE_TO_SCORE = {
        ConfidenceLevel.HIGH: 0.90,
        ConfidenceLevel.MEDIUM: 0.75,
        ConfidenceLevel.LOW: 0.55,
        ConfidenceLevel.VERY_LOW: 0.35,
    }

    def __init__(self, tools: CopilotTools):
        self.tools = tools

    def _log_audit(self, state: CopilotState, action: str, details: Dict[str, Any]) -> None:
        """记录审计日志"""
        state.audit_trail.append({
            "timestamp": datetime.utcnow().isoformat(),
            "action": action,
            "details": details
        })

    def _build_ranked_recommendations(self, options: List[DecisionOption], ranked_names: List[str]) -> List[Dict[str, Any]]:
        """构建带置信度分值的多答案排序列表。"""
        by_name = {opt.name: opt for opt in options}

        # Respect model ranking order first, then append any omitted options by score.
        ordered: List[DecisionOption] = [by_name[name] for name in ranked_names if name in by_name]
        remaining = [opt for opt in options if opt.name not in {o.name for o in ordered}]
        remaining.sort(key=lambda item: item.score, reverse=True)
        ordered.extend(remaining)

        ranked_recommendations: List[Dict[str, Any]] = []
        for idx, opt in enumerate(ordered, 1):
            conf_score = self.CONFIDENCE_TO_SCORE.get(opt.score_confidence, 0.35)
            # Blend model score with score-confidence for a stable ranking confidence score.
            ranking_confidence_score = round((float(opt.score) * 0.7) + (conf_score * 0.3), 3)
            ranked_recommendations.append({
                "rank": idx,
                "option_id": opt.id,
                "option_name": opt.name,
                "score": round(float(opt.score), 3),
                "score_confidence": opt.score_confidence.value,
                "confidence_score": ranking_confidence_score,
                "rationale": opt.rationale,
            })

        return ranked_recommendations

    async def clarify_context_node(self, state: CopilotState) -> CopilotState:
        """节点 1：澄清上下文（快速版）"""
        print(f"[ClarifyContext] Processing: {state.problem_statement[:80]}...")

        try:
            result = self.tools.clarify_context(state.problem_statement, state.domain)

            clarified = ClarifiedContext(
                original_problem=state.problem_statement,
                refined_problem=result.get("refined_problem", state.problem_statement),
                objectives=result.get("objectives", []),
                constraints=result.get("constraints", []),
                stakeholders=result.get("stakeholders", []),
                success_criteria=result.get("success_criteria", []),
                timeline=result.get("timeline"),
                budget_or_scope=result.get("budget_or_scope"),
            )

            self._log_audit(state, "clarify_context", {
                "objectives": len(clarified.objectives),
                "constraints": len(clarified.constraints)
            })

            state.clarified_context = clarified
            return state
        except Exception as e:
            self._log_audit(state, "clarify_context_error", {"error": str(e)})
            raise

    async def extract_assumptions_node(self, state: CopilotState) -> CopilotState:
        """节点 2：显性化假设（核心创新）- 把隐性假设变显性"""
        print(f"[ExtractAssumptions] Identifying assumptions...")

        if not state.clarified_context:
            raise ValueError("clarified_context not set")

        try:
            result = self.tools.extract_assumptions(
                state.clarified_context.dict(),
                state.domain
            )

            assumptions = []
            for i, assumption_data in enumerate(result.get("assumptions", []), 1):
                assumption = Assumption(
                    id=f"a_{i}",
                    statement=assumption_data.get("statement", ""),
                    justification=assumption_data.get("justification", ""),
                    confidence=ConfidenceLevel(assumption_data.get("confidence", "low")),
                    can_be_verified=assumption_data.get("can_be_verified", True),
                    how_to_verify=assumption_data.get("how_to_verify"),
                    impact_if_wrong=assumption_data.get("impact_if_wrong", "medium")
                )
                assumptions.append(assumption)

            self._log_audit(state, "extract_assumptions", {
                "num_assumptions": len(assumptions),
                "high_confidence_count": sum(1 for a in assumptions if a.confidence == ConfidenceLevel.HIGH),
                "low_confidence_count": sum(1 for a in assumptions if a.confidence in [ConfidenceLevel.LOW, ConfidenceLevel.VERY_LOW])
            })

            state.explicit_assumptions = assumptions
            return state
        except Exception as e:
            self._log_audit(state, "extract_assumptions_error", {"error": str(e)})
            state.explicit_assumptions = []
            return state

    async def generate_options_node(self, state: CopilotState) -> CopilotState:
        """节点 3：生成方案（每个标注依赖的假设）"""
        print(f"[GenerateOptions] Generating options...")

        if not state.clarified_context:
            raise ValueError("clarified_context not set")

        try:
            result = self.tools.generate_options(
                state.clarified_context.dict(),
                state.domain
            )

            options = []
            for i, opt_data in enumerate(result.get("options", []), 1):
                option = DecisionOption(
                    id=f"option_{i}",
                    name=opt_data.get("name", f"Option {i}"),
                    description=opt_data.get("description", ""),
                    estimated_effort=opt_data.get("effort"),
                    timeline=opt_data.get("timeline"),
                    assumption_ids=opt_data.get("assumption_ids", [])
                )
                options.append(option)

            self._log_audit(state, "generate_options", {
                "num_options": len(options),
                "option_names": [opt.name for opt in options]
            })

            state.generated_options = options
            return state
        except Exception as e:
            self._log_audit(state, "generate_options_error", {"error": str(e)})
            raise

    async def evaluate_options_node(self, state: CopilotState) -> CopilotState:
        """节点 4：轻量评估（强调置信度而不是权威性）"""
        print(f"[EvaluateOptions] Evaluating {len(state.generated_options)} options...")

        if not state.generated_options or not state.clarified_context:
            raise ValueError("generated_options or clarified_context not set")

        try:
            option_names = [opt.name for opt in state.generated_options]
            result = self.tools.evaluate_options(
                state.clarified_context.dict(),
                option_names,
                state.evaluation_criteria,
                state.domain
            )

            eval_map = {e["option_name"]: e for e in result.get("evaluations", [])}

            updated_options = []
            for opt in state.generated_options:
                eval_data = eval_map.get(opt.name, {})
                opt.pros = eval_data.get("pros", [])
                opt.cons = eval_data.get("cons", [])
                opt.risks = eval_data.get("risks", [])
                opt.score = eval_data.get("score", 0.0)
                opt.rationale = eval_data.get("rationale", "")
                opt.score_confidence = ConfidenceLevel(eval_data.get("score_confidence", "low"))
                updated_options.append(opt)

            self._log_audit(state, "evaluate_options", {
                "num_evaluated": len(updated_options),
                "avg_score": sum(opt.score for opt in updated_options) / len(updated_options) if updated_options else 0,
                "low_confidence_options": sum(
                    1 for opt in updated_options
                    if opt.score_confidence in [ConfidenceLevel.LOW, ConfidenceLevel.VERY_LOW]
                )
            })

            state.generated_options = updated_options
            return state
        except Exception as e:
            self._log_audit(state, "evaluate_options_error", {"error": str(e)})
            raise

    async def rank_and_recommend_node(self, state: CopilotState) -> CopilotState:
        """节点 5：推荐（关键：明确指出如何提升置信度）"""
        print(f"[RankAndRecommend] Recommending...")

        if not state.generated_options or not state.clarified_context:
            raise ValueError("generated_options or clarified_context not set")

        try:
            evaluations = {
                "evaluations": [
                    {
                        "option_name": opt.name,
                        "pros": opt.pros,
                        "cons": opt.cons,
                        "risks": opt.risks,
                        "score": opt.score,
                        "rationale": opt.rationale,
                        "score_confidence": opt.score_confidence.value
                    }
                    for opt in state.generated_options
                ]
            }

            result = self.tools.rank_and_recommend(
                state.clarified_context.dict(),
                evaluations,
                state.evaluation_criteria,
                state.domain
            )

            primary_name = result.get("primary_recommendation")
            primary = next(
                (opt for opt in state.generated_options if opt.name == primary_name),
                None
            )

            ranked_names = result.get("ranked_options", [])
            alternatives = [
                opt for opt in state.generated_options
                if opt.name in ranked_names and opt.name != primary_name
            ]
            ranked_recommendations = self._build_ranked_recommendations(state.generated_options, ranked_names)

            next_steps = result.get("next_steps_to_strengthen", [])

            self._log_audit(state, "rank_and_recommend", {
                "primary": primary_name,
                "confidence": result.get("confidence", "low")
            })

            state.primary_recommendation = primary
            state.recommendation_confidence = ConfidenceLevel(result.get("confidence", "low"))
            state.recommendation_confidence_score = result.get("confidence_score", 0.5)
            state.recommendation_rationale = result.get("recommendation_rationale", "")
            state.ranked_recommendations = ranked_recommendations
            state.alternative_recommendations = alternatives
            state.key_risks = [{"risk": r} for r in result.get("key_risks", [])]
            state.mitigation_strategies = result.get("mitigation_strategies", [])
            state.next_steps_to_strengthen = next_steps

            return state
        except Exception as e:
            self._log_audit(state, "rank_and_recommend_error", {"error": str(e)})
            raise

    async def save_record_node(self, state: CopilotState) -> CopilotState:
        """节点 6：保存决策记录（作为可迭代的 Draft）"""
        print(f"[SaveRecord] Saving decision {state.decision_id}...")

        try:
            now = datetime.utcnow().isoformat()
            state.updated_at = now
            if not state.created_at:
                state.created_at = now

            state.status = "draft"

            self._log_audit(state, "save_record", {
                "decision_id": state.decision_id,
                "status": state.status
            })

            return state
        except Exception as e:
            self._log_audit(state, "save_record_error", {"error": str(e)})
            raise
