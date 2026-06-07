"""
Decision Copilot OpenAI Tools (Function Calling)
Supports both Azure OpenAI and standard OpenAI.
"""

import json
from typing import Any, Dict, List
from openai import OpenAI, AzureOpenAI
from app.config import get_settings


settings = get_settings()


class CopilotTools:
    """Decision Copilot å·¥å…·é›†ï¼Œæ”¯æŒ OpenAI Function Calling"""
    
    def __init__(self):
        if settings.azure_openai_api_key and settings.azure_openai_endpoint:
            self.client = AzureOpenAI(
                api_key=settings.azure_openai_api_key,
                azure_endpoint=settings.azure_openai_endpoint,
                api_version=settings.azure_openai_api_version,
            )
            self.model = settings.azure_openai_deployment
        else:
            self.client = OpenAI(api_key=settings.openai_api_key)
            self.model = "gpt-4o"
    
    # ============ Tool Definitions ============
    
    @staticmethod
    def get_tool_definitions() -> List[Dict[str, Any]]:
        """èŽ·å–æ‰€æœ‰ tool å®šä¹‰ï¼ˆç”¨äºŽ OpenAI function_callingï¼‰"""
        return [
            {
                "type": "function",
                "function": {
                    "name": "clarify_context",
                    "description": "æ¾„æ¸…ç”¨æˆ·é—®é¢˜çš„ç›®æ ‡ã€çº¦æŸã€æ¶‰ä¼—å’ŒæˆåŠŸæ ‡å‡†",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "refined_problem": {
                                "type": "string",
                                "description": "æ¾„æ¸…åŽçš„é—®é¢˜æè¿°"
                            },
                            "objectives": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "å†³ç­–çš„ä¸»è¦ç›®æ ‡"
                            },
                            "constraints": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "æ—¶é—´ã€èµ„æºã€æŠ€æœ¯ç­‰çº¦æŸ"
                            },
                            "stakeholders": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "ç›¸å…³æ–¹"
                            },
                            "success_criteria": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "æˆåŠŸçš„è¡¡é‡æ ‡å‡†"
                            },
                            "assumptions": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "å…³é”®å‡è®¾"
                            }
                        },
                        "required": ["refined_problem", "objectives", "constraints", "success_criteria"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "generate_options",
                    "description": "åŸºäºŽæ¾„æ¸…çš„ä¸Šä¸‹æ–‡ï¼Œç”Ÿæˆ 3-5 ä¸ªå¯é€‰æ–¹æ¡ˆ",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "options": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "description": {"type": "string"},
                                        "effort": {"type": "string", "enum": ["low", "medium", "high"]},
                                        "timeline": {"type": "string"},
                                        "key_assumption": {"type": "string"}
                                    },
                                    "required": ["name", "description"]
                                },
                                "description": "ç”Ÿæˆçš„æ–¹æ¡ˆåˆ—è¡¨ï¼ˆ3-5ä¸ªï¼‰"
                            }
                        },
                        "required": ["options"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "evaluate_options",
                    "description": "å¯¹æ¯ä¸ªæ–¹æ¡ˆè¿›è¡Œæ·±åº¦è¯„ä¼°ï¼Œåˆ†æžä¼˜ç¼ºç‚¹å’Œé£Žé™©",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "evaluations": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "option_name": {"type": "string"},
                                        "pros": {"type": "array", "items": {"type": "string"}},
                                        "cons": {"type": "array", "items": {"type": "string"}},
                                        "risks": {"type": "array", "items": {"type": "string"}},
                                        "score": {"type": "number", "minimum": 0, "maximum": 1},
                                        "score_confidence": {"type": "string", "enum": ["high", "medium", "low", "very_low"]},
                                        "rationale": {"type": "string"}
                                    },
                                    "required": ["option_name", "pros", "cons", "risks", "score"]
                                }
                            }
                        },
                        "required": ["evaluations"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "rank_and_recommend",
                    "description": "åŸºäºŽè¯„ä¼°ï¼ŒæŽ’åºæ–¹æ¡ˆå¹¶ç»™å‡ºä¸»æŽ¨è",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "ranked_options": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "æŒ‰æŽ¨èåº¦æŽ’åºçš„æ–¹æ¡ˆåç§°åˆ—è¡¨"
                            },
                            "primary_recommendation": {"type": "string", "description": "é¦–é€‰æŽ¨è"},
                            "confidence": {"type": "string", "enum": ["high", "medium", "low", "very_low"], "description": "æ•´ä½“æŽ¨èçš„ç½®ä¿¡åº¦ç­‰çº§"},
                            "confidence_score": {"type": "number", "minimum": 0, "maximum": 1, "description": "ç½®ä¿¡åº¦åˆ†å€¼ 0-1"},
                            "recommendation_rationale": {"type": "string"},
                            "key_risks": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "é¦–é€‰æŽ¨èçš„å…³é”®é£Žé™©"
                            },
                            "mitigation_strategies": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "é£Žé™©ç¼“è§£ç­–ç•¥"
                            },
                            "next_steps_to_strengthen": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "æå‡å†³ç­–è´¨é‡çš„å…·ä½“è¡ŒåŠ¨ï¼ˆéªŒè¯å‡è®¾ã€æ”¶é›†æ•°æ®ç­‰ï¼‰"
                            }
                        },
                        "required": ["primary_recommendation", "confidence", "recommendation_rationale"]
                    }
                }
            }
        ]
    
    # ============ Tool Executions ============
    
    def clarify_context(self, problem_statement: str, domain: str) -> Dict[str, Any]:
        """ä½¿ç”¨ OpenAI æ¾„æ¸…é—®é¢˜ä¸Šä¸‹æ–‡"""
        prompt = f"""
You are an expert decision analyst. Your task is to clarify the following decision problem:

**Domain**: {domain}
**Problem Statement**: {problem_statement}

Please analyze the problem and clarify:
1. The refined problem statement
2. Core objectives (what does the decision maker really want?)
3. Key constraints (time, budget, technical, organizational)
4. Relevant stakeholders
5. Success criteria
6. Key assumptions

Call the 'clarify_context' function with your analysis.
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=CopilotTools.get_tool_definitions()[:1],  # Just clarify_context
            tool_choice="required"
        )
        
        # Extract function call result
        tool_use = response.choices[0].message.tool_calls[0]
        return json.loads(tool_use.function.arguments)
    
    def generate_options(self, clarified_context: Dict[str, Any], domain: str) -> Dict[str, Any]:
        """ç”Ÿæˆå¤šä¸ªå¯é€‰æ–¹æ¡ˆ"""
        prompt = f"""
You are an expert decision analyst. Based on the following clarified decision context:

**Domain**: {domain}
**Objectives**: {', '.join(clarified_context.get('objectives', []))}
**Constraints**: {', '.join(clarified_context.get('constraints', []))}
**Success Criteria**: {', '.join(clarified_context.get('success_criteria', []))}

Generate 3-5 distinct viable options. Each should:
- Address the objectives
- Respect the constraints
- Be feasible within the timeframe
- Have clear tradeoffs

Call the 'generate_options' function with your options.
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=CopilotTools.get_tool_definitions()[1:2],  # Just generate_options
            tool_choice="required"
        )
        
        tool_use = response.choices[0].message.tool_calls[0]
        return json.loads(tool_use.function.arguments)
    
    def evaluate_options(self, clarified_context: Dict[str, Any], options: List[str], 
                         criteria: Dict[str, float], domain: str) -> Dict[str, Any]:
        """è¯„ä¼°æ‰€æœ‰æ–¹æ¡ˆ"""
        criteria_str = "\n".join([f"- {k}: weight {v}" for k, v in criteria.items()])
        
        prompt = f"""
You are an expert decision analyst. Evaluate the following options against the given criteria:

**Domain**: {domain}
**Objectives**: {', '.join(clarified_context.get('objectives', []))}
**Constraints**: {', '.join(clarified_context.get('constraints', []))}

**Evaluation Criteria**:
{criteria_str}

**Options to Evaluate**:
{chr(10).join(f'- {opt}' for opt in options)}

For each option, analyze:
1. Pros (how it addresses objectives)
2. Cons (drawbacks and tradeoffs)
3. Risks (potential problems, failure modes)
4. Score (0-1, weighted by criteria)
5. Rationale (why this score)

Call the 'evaluate_options' function with your analysis.
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=CopilotTools.get_tool_definitions()[2:3],  # Just evaluate_options
            tool_choice="required"
        )
        
        tool_use = response.choices[0].message.tool_calls[0]
        return json.loads(tool_use.function.arguments)
    
    def rank_and_recommend(self, clarified_context: Dict[str, Any], evaluations: Dict[str, Any],
                           criteria: Dict[str, float], domain: str) -> Dict[str, Any]:
        """åŸºäºŽè¯„ä¼°ç»“æžœæŽ¨èæœ€ä½³æ–¹æ¡ˆ"""
        evals_str = "\n".join([
            f"- {e['option_name']}: score {e['score']}, risks: {', '.join(e['risks'][:2])}"
            for e in evaluations.get('evaluations', [])
        ])
        
        prompt = f"""
You are an expert decision analyst. Based on the evaluations below, provide your recommendation:

**Domain**: {domain}
**Objectives**: {', '.join(clarified_context.get('objectives', []))}
**Success Criteria**: {', '.join(clarified_context.get('success_criteria', []))}

**Option Scores**:
{evals_str}

Recommendation Guidelines:
- Choose the option with the best risk-adjusted score
- Consider implementation complexity
- Ensure it aligns with all constraints and objectives
- Be honest about remaining risks

Call the 'rank_and_recommend' function with your recommendation and risk mitigation strategies.
"""
        
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            tools=CopilotTools.get_tool_definitions()[3:4],  # Just rank_and_recommend
            tool_choice="required"
        )
        
        tool_use = response.choices[0].message.tool_calls[0]
        return json.loads(tool_use.function.arguments)
    
    def extract_assumptions(self, clarified_context: Dict[str, Any], domain: str) -> Dict[str, Any]:
        """
        æ˜¾æ€§åŒ–å…³é”®å‡è®¾
        è¿™æ˜¯æ–°å¢žçš„æ–¹æ³•ï¼Œæ”¯æŒ"æ— è¯æ®å‹å¥½æ¨¡å¼"
        """
        prompt = f"""
You are an expert decision analyst. Based on the clarified context below, identify and extract the KEY ASSUMPTIONS being made.

**Domain**: {domain}
**Objectives**: {', '.join(clarified_context.get('objectives', []))}
**Constraints**: {', '.join(clarified_context.get('constraints', []))}

Extract 3-5 key assumptions. For each assumption:
1. State it clearly (e.g., "Team size < 5 people")
2. Justify why you're making it
3. Rate your confidence (high/medium/low/very_low)
4. Indicate if it's verifiable and how
5. Assess impact if wrong (low/medium/high)

Return as JSON with "assumptions" array. Each assumption must have:
- statement
- justification
- confidence (high/medium/low/very_low)
- can_be_verified (bool)
- how_to_verify (string or null)
- impact_if_wrong (low/medium/high)
"""
        
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7,
                response_format={"type": "json_object"}
            )
            
            result = json.loads(response.choices[0].message.content)
            return result
        except Exception as e:
            print(f"Error extracting assumptions: {e}")
            # Return empty assumptions gracefully
            return {"assumptions": []}

