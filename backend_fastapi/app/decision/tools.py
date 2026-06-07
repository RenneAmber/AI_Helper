from typing import Dict, Any, List
from datetime import datetime


TRAINING_CASES: List[Dict[str, Any]] = [
    {
        "name": "solo_hotel",
        "keywords": ["\u4e00\u4e2a\u4eba", "\u5bbe\u9986", "\u9152\u5e97", "\u4f4f\u5bbf", "\u4f4f\u5e97", "hotel", "motel", "hostel", "solo trip"],
        "recommendation": "PROCEED",
        "confidence": 0.72,
        "rationale": "\u5355\u4eba\u4f4f\u5bbe\u9986\u5728\u5b89\u5168\u548c\u4fbf\u5229\u6027\u4e0a\u901a\u5e38\u53ef\u884c\uff0c\u9700\u4f18\u5148\u7b5b\u9009\u9ad8\u8bc4\u5206\u4e0e\u6b63\u89c4\u5546\u5708\u9152\u5e97\u3002",
        "uncertainties": ["\u76ee\u7684\u5730\u6cbb\u5b89\u5dee\u5f02", "\u9884\u7b97\u4e0a\u9650", "\u591c\u95f4\u51fa\u884c\u9700\u6c42"],
        "next_steps": ["\u4f18\u5148\u9009\u62e9\u8bc4\u52064.5+\u4e14\u8fd1\u5730\u94c1\u7684\u9152\u5e97", "\u786e\u8ba424\u5c0f\u65f6\u524d\u53f0\u4e0e\u5973\u6027\u5355\u72ec\u5165\u4f4f\u5b89\u5168\u63aa\u65bd"],
        "tags": ["travel", "hotel", "solo", "rec:PROCEED", "cf:0.72"],
    },
    {
        "name": "travel_new_zealand",
        "keywords": ["\u65b0\u897f\u5170", "new zealand", "nz", "\u65c5\u884c", "\u65c5\u6e38", "trip", "travel"],
        "recommendation": "PROCEED_WITH_GUARDRAILS",
        "confidence": 0.7,
        "rationale": "\u65b0\u897f\u5170\u65c5\u884c\u6536\u76ca\u9ad8\uff0c\u4f46\u6210\u672c\u548c\u884c\u7a0b\u957f\u5ea6\u654f\u611f\uff0c\u5efa\u8bae\u5728\u9884\u7b97\u548c\u65f6\u95f4\u53ef\u63a7\u65f6\u63a8\u8fdb\u3002",
        "uncertainties": ["\u673a\u7968\u4e0e\u4f4f\u5bbf\u6ce2\u52a8", "\u7b7e\u8bc1\u4e0e\u5047\u671f\u7a97\u53e3", "\u5929\u6c14\u5b63\u8282\u56e0\u7d20"],
        "next_steps": ["\u5148\u505a7-10\u5929\u884c\u7a0b\u9884\u7b97\u8868", "\u9501\u5b9a\u53ef\u9000\u6539\u673a\u9152\u540e\u518d\u786e\u8ba4"],
        "tags": ["travel", "international", "budget", "rec:PROCEED_WITH_GUARDRAILS", "cf:0.70"],
    },
    {
        "name": "feature_flag_rollout",
        "keywords": ["feature flag", "\u7070\u5ea6", "\u53d1\u5e03", "\u4e0a\u7ebf", "10%", "\u56de\u6eda"],
        "recommendation": "PROCEED_WITH_GUARDRAILS",
        "confidence": 0.78,
        "rationale": "\u529f\u80fd\u7070\u5ea6\u53d1\u5e03\u5efa\u8bae\u91c7\u7528\u9010\u6b65\u653e\u91cf\u7b56\u7565\uff0c\u5e76\u9884\u7f6e\u56de\u6eda\u9608\u503c\u4e0e\u76d1\u63a7\u544a\u8b66\u3002",
        "uncertainties": ["\u5173\u952e\u6307\u6807\u6ce2\u52a8\u9608\u503c\u662f\u5426\u5b9a\u4e49\u5145\u5206", "\u5f02\u5e38\u6062\u590dSLA"],
        "next_steps": ["\u8bbe\u7f6e5%-10%-25%\u5206\u9636\u6bb5\u653e\u91cf", "\u914d\u7f6e\u9519\u8bef\u7387\u548c\u8f6c\u5316\u7387\u53cc\u9608\u503c\u81ea\u52a8\u56de\u6eda"],
        "tags": ["engineering", "release", "risk_control", "rec:PROCEED_WITH_GUARDRAILS", "cf:0.78"],
    },
    {
        "name": "legacy_refactor",
        "keywords": ["\u91cd\u6784", "legacy", "\u83dc\u5355\u5c42", "\u65e7\u7cfb\u7edf", "menu layer", "refactor"],
        "recommendation": "NEEDS_REVIEW",
        "confidence": 0.61,
        "rationale": "\u65e7\u7cfb\u7edf\u91cd\u6784\u6536\u76ca\u53ef\u80fd\u8f83\u9ad8\uff0c\u4f46\u9700\u8981\u5148\u786e\u8ba4\u56de\u5f52\u8303\u56f4\u3001\u4f9d\u8d56\u8026\u5408\u5ea6\u548c\u8fc1\u79fb\u7a97\u53e3\u3002",
        "uncertainties": ["\u6f5c\u5728\u56de\u5f52\u9762\u5927\u5c0f", "\u4f9d\u8d56\u7cfb\u7edf\u8054\u8c03\u6210\u672c", "\u662f\u5426\u5177\u5907\u7070\u5ea6\u5207\u6362\u80fd\u529b"],
        "next_steps": ["\u5148\u5b8c\u6210\u6a21\u5757\u4f9d\u8d56\u76d8\u70b9\u548c\u98ce\u9669\u6e05\u5355", "\u62c6\u6210\u53ef\u56de\u6eda\u7684\u5c0f\u6b65\u91cd\u6784\u8ba1\u5212\u518d\u8bc4\u5ba1"],
        "tags": ["engineering", "refactor", "legacy", "rec:NEEDS_REVIEW", "cf:0.61"],
    },
    {
        "name": "fallback_generic",
        "keywords": [],
        "recommendation": "NEEDS_REVIEW",
        "confidence": 0.55,
        "rationale": "\u5df2\u8fdb\u5165\u51b3\u7b56\u6d41\u7a0b\uff0c\u4f46\u5f53\u524d\u8bad\u7ec3\u6837\u4f8b\u672a\u8986\u76d6\u8be5\u573a\u666f\uff0c\u9700\u8981\u8865\u5145\u4e1a\u52a1\u8bc1\u636e\u540e\u518d\u5b9a\u6848\u3002",
        "uncertainties": ["\u6837\u4f8b\u8986\u76d6\u4e0d\u8db3", "\u4e1a\u52a1\u7ea6\u675f\u5c1a\u672a\u91cf\u5316"],
        "next_steps": ["\u8865\u5145\u8be5\u9886\u57df\u5386\u53f2\u6848\u4f8b", "\u589e\u52a0\u5173\u952e\u7ea6\u675f\u548c\u8bc4\u4ef7\u6307\u6807"],
        "tags": ["generic", "rec:NEEDS_REVIEW", "cf:0.55"],
    },
]


def _match_training_case(query: str) -> Dict[str, Any]:
    lowered = query.lower()
    for case in TRAINING_CASES:
        keywords = case.get("keywords", [])
        if keywords and any(word.lower() in lowered for word in keywords):
            return case
    return next(case for case in TRAINING_CASES if case["name"] == "fallback_generic")


async def fake_retriever(query: str) -> Dict[str, Any]:
    """
    示例工具：文档检索
    生产环境替换为真实的 RAG / 日志查询 / SharePoint 接口
    """
    case = _match_training_case(query)
    return {
        "kind": "doc",
        "sourceType": "training_data",
        "uri": f"local://training-cases/{case['name']}",
        "title": f"Training case: {case['name']}",
        "trainingCase": {
            "name": case["name"],
            "recommendation": case["recommendation"],
            "confidence": case["confidence"],
            "rationale": case["rationale"],
            "uncertainties": case["uncertainties"],
            "nextSteps": case["next_steps"],
        },
        "retrievedAt": datetime.utcnow().isoformat(),
        "quote": (
            f"[\u8bad\u7ec3\u6837\u4f8b:{case['name']}] \u95ee\u9898:{query}\u3002"
            f"\u5efa\u8bae:{case['recommendation']}\uff0c\u4f9d\u636e:{case['rationale']}"
        ),
        "signals": {
            "recencyDays": 0,
            "reliability": "high",
            "relevance": "high"
        },
        "tags": case["tags"],
    }


async def fake_log_query(query: str) -> Dict[str, Any]:
    """
    示例工具：日志查询
    """
    return {
        "kind": "log",
        "sourceType": "audit_log",
        "uri": "local://logs",
        "title": "Audit Logs",
        "retrievedAt": datetime.utcnow().isoformat(),
        "quote": f"Log entries matching: {query}",
        "signals": {
            "recencyDays": 0,
            "reliability": "high",
            "relevance": "medium"
        },
        "tags": ["logs"]
    }
