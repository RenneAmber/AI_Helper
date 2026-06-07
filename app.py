
# app.py
"""
Flask 后端（增强版）：调用 Azure OpenAI，提供“低幻觉”回答。
特性：
- 反幻觉：System Prompt + 严格 JSON Schema + 两阶段重试 + 置信度阈值 + 引用校验
- 稳定性：请求超时、指数退避重试、错误分类（429/5xx）、输入长度限制
- 安全性：Markdown -> HTML 安全清洗（bleach）、CORS 限制、SQLite WAL
- 工程化：统一 JSON 错误、结构化日志（request_id、latency、token usage）
- 可配置：温度/采样、max tokens、CORS 允许来源、置信度阈值等可通过环境变量设置

新手阅读提示：
1) 先看 /chat 路由：这是所有请求的统一入口。
2) 在 /chat 内看“医疗分支”和“RAG 分支”如何分流。
3) 最后看 /svc/* mock 路由与 /ingest，理解联调与数据入库。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from tool_client import ToolClient
from medical_agent import medical_agent_step
import numpy as np
from flask import Flask, jsonify, request, send_from_directory, g
from flask_cors import CORS
from openai import AzureOpenAI, APIError, RateLimitError, APITimeoutError, APIConnectionError
import markdown as md
import bleach

# -----------------------------------------------------------------------------#
# 基础配置 & 日志
# -----------------------------------------------------------------------------#
app = Flask(__name__)

# CORS 限制：默认仅允许本地前端；用逗号分隔多个源
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000")
allowed_origin_list = [o.strip() for o in ALLOWED_ORIGINS.split(",") if o.strip()]
CORS(app, resources={
    r"/chat": {"origins": allowed_origin_list},
    r"/medical/chat": {"origins": allowed_origin_list},
    r"/ingest": {"origins": allowed_origin_list},
})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s",
)
from flask import has_request_context

class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # 如果是请求内，拿 g.request_id；否则给默认值
        if has_request_context():
            record.request_id = getattr(g, "request_id", "-")
        else:
            record.request_id = "-"
        return True
# 在 logging.basicConfig(...) 之后加
request_id_filter = RequestIdFilter()

root_logger = logging.getLogger()  # root
for h in root_logger.handlers:
    h.addFilter(request_id_filter)

# werkzeug 自己用的 logger 也加上
werk_logger = logging.getLogger("werkzeug")
for h in werk_logger.handlers:
    h.addFilter(request_id_filter)

logger = logging.getLogger("chat-app")

DB_PATH = os.getenv("DB_PATH", "chat_history.db")

# 运行参数
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "5000"))  # 输入长度上限（字符）
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))   # 置信度门槛
RETRY_MAX = int(os.getenv("RETRY_MAX", "2"))                 # 调用重试次数（不含首次）
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))  # 单次请求超时（秒）
REG_SERVICE_URL = os.getenv("REG_SERVICE_URL", "http://localhost:5000/svc")      # 先走同进程 mock
QUERY_SERVICE_URL = os.getenv("QUERY_SERVICE_URL", "http://localhost:5000/svc")
INTERPRET_SERVICE_URL = os.getenv("INTERPRET_SERVICE_URL", "http://localhost:5000/svc")
EMR_SERVICE_URL = os.getenv("EMR_SERVICE_URL", "http://localhost:5000/svc")
CHRONIC_DISEASE_SERVICE_URL = os.getenv("CHRONIC_DISEASE_SERVICE_URL", "http://localhost:5000/svc")

tool_client = ToolClient(
    registration_url=REG_SERVICE_URL,
    query_url=QUERY_SERVICE_URL,
    interpret_url=INTERPRET_SERVICE_URL,
    emr_url=EMR_SERVICE_URL,
    chronic_disease_url=CHRONIC_DISEASE_SERVICE_URL,
    timeout=REQUEST_TIMEOUT,
    retry_max=RETRY_MAX,
)

# 采样参数
TEMPERATURE = float(os.getenv("OPENAI_TEMPERATURE", "0.2"))
TOP_P = float(os.getenv("OPENAI_TOP_P", "0.9"))
MAX_TOKENS = int(os.getenv("OPENAI_MAX_TOKENS", "1024"))

# Azure OpenAI 环境
AZURE_OPENAI_API_KEY = os.getenv("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.getenv("AZURE_OPENAI_API_ENDPOINT") or os.getenv("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.getenv("AZURE_OPENAI_API_VERSION", "2025-01-01-preview")
DEPLOYMENT_NAME = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-41_milky")  # 你的部署名

# Embedding 部署名（务必在 Azure OpenAI 上单独创建 embedding 部署，如 text-embedding-3-large/small）
EMBED_DEPLOYMENT = os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-3-large")

# RAG 参数
CHUNK_SIZE = int(os.getenv("RAG_CHUNK_SIZE", "500"))      # 粗略按字符切（简化实现；生产建议按 tokens）
CHUNK_OVERLAP = int(os.getenv("RAG_CHUNK_OVERLAP", "100"))
TOP_K = int(os.getenv("RAG_TOP_K", "4"))
SIM_THRESHOLD = float(os.getenv("RAG_SIM_THRESHOLD", "0.0"))  # 设 >0 可过滤低相似度

print("=== Env (probe) ===")
print("AZURE_OPENAI_API_KEY exists:", "Yes" if AZURE_OPENAI_API_KEY else "No")
print("AZURE_OPENAI_ENDPOINT:", AZURE_OPENAI_ENDPOINT)
print("AZURE_OPENAI_API_VERSION:", AZURE_OPENAI_API_VERSION)
print("ALLOWED_ORIGINS:", allowed_origin_list)

# 导入并执行数据库迁移（新功能所需的表）
try:
    from db_migrate import migrate_db
    migrate_db()
    logger.info("✓ 数据库迁移完成")
except Exception as e:
    logger.warning(f"数据库迁移失败（非关键）: {e}")

# AzureOpenAI 客户端
client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

def load_session_state(session_id: str) -> Dict[str, Any]:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute("SELECT state_json FROM agent_sessions WHERE session_id=?", (session_id,))
        row = c.fetchone()
        if not row:
            return {}
        return json.loads(row[0])
    except Exception:
        return {}
    finally:
        conn.close()

def save_session_state(session_id: str, state: Dict[str, Any]) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO agent_sessions (session_id, state_json, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
              state_json=excluded.state_json,
              updated_at=excluded.updated_at
            """,
            (session_id, json.dumps(state, ensure_ascii=False), datetime.now().isoformat()),
        )
        conn.commit()
    finally:
        conn.close()

# -----------------------------------------------------------------------------#
# DB 初始化（WAL 提升并发写入；保留最小字段）
# -----------------------------------------------------------------------------#
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")

        # 原 chat 表
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS chat (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                question   TEXT NOT NULL,
                answer_md  TEXT NOT NULL,
                answer_json TEXT,
                usage_prompt_tokens INTEGER,
                usage_completion_tokens INTEGER,
                usage_total_tokens INTEGER,
                confidence REAL,
                timestamp TEXT NOT NULL
            )
            """
        )

        # RAG 文档片段表（你 /ingest 需要）
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS rag_docs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                content TEXT NOT NULL,
                embedding BLOB NOT NULL
            )
            """
        )

        # 医疗 Agent 会话表（多轮追问需要）
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_sessions (
                session_id TEXT PRIMARY KEY,
                state_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        conn.commit()
    finally:
        conn.close()

# -----------------------------------------------------------------------------#
# 请求上下文：request_id & 计时
# -----------------------------------------------------------------------------#
@app.before_request
def before_request():
    g.request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())
    g.start_ts = time.time()

@app.after_request
def after_request(resp):
    latency_ms = int((time.time() - getattr(g, "start_ts", time.time())) * 1000)
    resp.headers["X-Request-Id"] = getattr(g, "request_id", "")
    resp.headers["X-Latency-ms"] = str(latency_ms)
    return resp

def log_info(msg: str, **kwargs):
    extra = {"request_id": getattr(g, "request_id", "-")}
    logger.info(msg + " " + json.dumps(kwargs, ensure_ascii=False), extra=extra)

def log_warn(msg: str, **kwargs):
    extra = {"request_id": getattr(g, "request_id", "-")}
    logger.warning(msg + " " + json.dumps(kwargs, ensure_ascii=False), extra=extra)

def log_error(msg: str, **kwargs):
    extra = {"request_id": getattr(g, "request_id", "-")}
    logger.error(msg + " " + json.dumps(kwargs, ensure_ascii=False), extra=extra)

# -----------------------------------------------------------------------------#
# 错误处理（统一 JSON）
# -----------------------------------------------------------------------------#
@app.errorhandler(404)
def handle_404(e):
    return jsonify({"error": "Not Found", "detail": str(e), "request_id": g.request_id}), 404

@app.errorhandler(405)
def handle_405(_):
    return jsonify({"error": "Method Not Allowed", "request_id": g.request_id}), 405

@app.errorhandler(Exception)
def handle_exception(e):
    log_error("Unhandled exception", detail=str(e))
    return jsonify({"error": "Internal Server Error", "detail": str(e), "request_id": g.request_id}), 500

# -----------------------------------------------------------------------------#
# 静态首页（可选）
# -----------------------------------------------------------------------------#
@app.route("/")
def index():
    return send_from_directory(".", "index.html")

# -----------------------------------------------------------------------------#
# 安全 Markdown 渲染
# -----------------------------------------------------------------------------#
def render_markdown_safe(markdown_text: str) -> str:
    html = md.markdown(
        markdown_text or "",
        extensions=["fenced_code", "codehilite", "tables", "toc", "sane_lists", "smarty"],
    )
    allowed_tags = bleach.sanitizer.ALLOWED_TAGS.union({
        "p", "pre", "code", "blockquote", "hr",
        "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "strong", "em", "del",
        "table", "thead", "tbody", "tr", "th", "td",
    })
    allowed_attrs = {
        "*": ["class"],
        "a": ["href", "title", "target", "rel"],
        "img": ["src", "alt", "title"],
        "code": ["class"],
    }
    cleaned = bleach.clean(
        html,
        tags=allowed_tags,
        attributes=allowed_attrs,
        protocols=["http", "https", "mailto"],
        strip=True,
    )
    try:
        callbacks = getattr(bleach.linkifier, "DEFAULT_CALLBACKS", None)
        if callbacks:
            cleaned = bleach.linkify(cleaned, callbacks=callbacks, skip_pre=True)
        else:
            cleaned = bleach.linkify(cleaned, skip_pre=True)
    except Exception:
        pass
    return cleaned

# -----------------------------------------------------------------------------#
# 反幻觉：System Prompt 与模板
# -----------------------------------------------------------------------------#
SYSTEM_PROMPT_BASE = (
    "你是一个严谨可信的企业知识助手。必须遵守：\n"
    "1) 只基于“提供的上下文”与“用户问题”回答；若资料不足以支持结论，必须回答“我不知道”。\n"
    "2) 禁止编造事实；所有关键结论必须能在上下文中找到依据。\n"
    "3) 输出严格 JSON，且可被 json.loads 解析，不得输出任何多余字符。\n"
    "4) JSON 字段：answer(string), sources(array[string]), confidence(number, 0~1)。"
)

# 用户未提供上下文时，进一步收紧（尽可能拒答而非乱编）
SYSTEM_PROMPT_NO_CONTEXT_APPEND = (
    "\n注意：当前未提供任何上下文资料，你不得使用外部知识。若无法回答，必须输出“我不知道”。"
)

# 用户提供上下文时，强调引用
INSTRUCTION_TEMPLATE = (
    "请基于以下上下文回答用户问题。若上下文不足以支持答案，直接回答“我不知道”。\n"
    "要求：\n"
    "- answer：简明扼要；\n"
    "- sources：填写使用到的上下文片段的来源标识（可用自定义字符串/文件名/URL/段落号等）；\n"
    "- confidence：0~1 的置信度评分（仅在依据充分时给出 >=0.6）。\n\n"
    "[上下文开始]\n{context}\n[上下文结束]\n\n[用户问题]\n{question}"
)

# 第二阶段更严格模板（首次不合格时触发）
STRICT_APPEND = (
    "\n请严格遵守以上规则，若资料不足，必须回答“我不知道”，且 sources 需为空数组，confidence 需小于 0.6。"
)

# -----------------------------------------------------------------------------#
# OpenAI 调用封装（超时、重试、错误分类）
# -----------------------------------------------------------------------------#
def call_azure_openai(messages: List[Dict[str, str]]) -> Tuple[str, Dict[str, int]]:
    """
    返回 (model_content, usage_dict)
    usage_dict 包含 prompt_tokens, completion_tokens, total_tokens（若可用）
    """
    last_err: Optional[str] = None
    for attempt in range(RETRY_MAX + 1):
        try:
            resp = client.chat.completions.create(
                model=DEPLOYMENT_NAME,          # 部署名
                messages=messages,
                temperature=TEMPERATURE,
                top_p=TOP_P,
                max_tokens=MAX_TOKENS,
                timeout=REQUEST_TIMEOUT,
            )
            content = resp.choices[0].message.content or ""
            usage = getattr(resp, "usage", None)
            usage_dict = {
                "prompt_tokens": getattr(usage, "prompt_tokens", None) or 0,
                "completion_tokens": getattr(usage, "completion_tokens", None) or 0,
                "total_tokens": getattr(usage, "total_tokens", None) or 0,
            }
            return content, usage_dict
        except RateLimitError as e:
            last_err = f"429 RateLimit: {e}"
            # 指数退避
            time.sleep(min(2 ** attempt, 8))
        except (APITimeoutError, APIConnectionError) as e:
            last_err = f"Timeout/Connection: {e}"
            time.sleep(min(2 ** attempt, 8))
        except APIError as e:
            # 5xx 尝试重试，其它直接报错
            status = getattr(e, "status_code", None)
            if status and 500 <= status < 600:
                last_err = f"ServerError {status}: {e}"
                time.sleep(min(2 ** attempt, 8))
            else:
                raise
        except Exception as e:
            # 其它异常不重试
            raise
    raise RuntimeError(last_err or "OpenAI call failed")


# -----------------------------------------------------------------------------#
# Embedding 工具：切分 / 向量化 / 余弦相似度 / 存取
# -----------------------------------------------------------------------------#
def simple_overlap_chunks(text: str, size: int, overlap: int) -> List[str]:
    """按字符粗略切分（生产建议按 token 切分或语义切分）"""
    text = text.strip()
    if not text:
        return []
    chunks = []
    i = 0
    while i < len(text):
        chunk = text[i:i + size]
        chunks.append(chunk)
        if i + size >= len(text):
            break
        i += max(1, size - overlap)
    return chunks

def embed_texts(texts: List[str]) -> np.ndarray:
    """调用 Azure OpenAI Embeddings，返回 shape=(N, D) 的 np.float32 数组"""
    if not texts:
        return np.zeros((0, 1), dtype=np.float32)
    resp = client.embeddings.create(
        model=EMBED_DEPLOYMENT,   # 部署名
        input=texts,
        timeout=REQUEST_TIMEOUT,
    )
    vecs = [np.array(d.embedding, dtype=np.float32) for d in resp.data]
    return np.vstack(vecs)

def cosine_sim_matrix(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """计算余弦相似度矩阵 a·b / (|a||b|)"""
    if a.size == 0 or b.size == 0:
        return np.zeros((a.shape[0], b.shape[0]), dtype=np.float32)
    a_norm = a / (np.linalg.norm(a, axis=1, keepdims=True) + 1e-9)
    b_norm = b / (np.linalg.norm(b, axis=1, keepdims=True) + 1e-9)
    return np.dot(a_norm, b_norm.T)

def save_chunks_with_embeddings(source: str, chunks: List[str], embeddings: np.ndarray) -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        c = conn.cursor()
        for idx, (chunk, vec) in enumerate(zip(chunks, embeddings)):
            c.execute(
                "INSERT INTO rag_docs (source, chunk_index, content, embedding) VALUES (?, ?, ?, ?)",
                (source, idx, chunk, vec.tobytes()),
            )
        conn.commit()
    finally:
        conn.close()

def load_all_embeddings() -> Tuple[List[Dict[str, Any]], np.ndarray]:
    """从 rag_docs 读出所有片段与向量"""
    conn = sqlite3.connect(DB_PATH)
    rows = []
    vectors = []
    try:
        c = conn.cursor()
        c.execute("SELECT id, source, chunk_index, content, embedding FROM rag_docs")
        for rid, source, chunk_idx, content, emb_blob in c.fetchall():
            vec = np.frombuffer(emb_blob, dtype=np.float32)
            rows.append({"id": rid, "source": source, "chunk_index": chunk_idx, "content": content})
            vectors.append(vec)
    finally:
        conn.close()
    if vectors:
        vectors = np.vstack(vectors)
    else:
        vectors = np.zeros((0, 1), dtype=np.float32)
    return rows, vectors

def search_similar_chunks(query: str, top_k: int = TOP_K, threshold: float = SIM_THRESHOLD) -> List[Dict[str, Any]]:
    """对 query 计算 embedding，检索最相似的 K 个片段（含 source/content/sim）"""
    rows, mat = load_all_embeddings()
    if len(rows) == 0:
        return []
    qvec = embed_texts([query])
    sims = cosine_sim_matrix(qvec, mat)[0]  # shape=(N,)
    order = np.argsort(-sims)
    results = []
    for idx in order[:top_k]:
        sim = float(sims[idx])
        if sim < threshold:
            continue
        row = rows[idx]
        results.append({
            "source": f'{row["source"]}#chunk{row["chunk_index"]}',
            "content": row["content"],
            "similarity": sim,
        })
    return results

# -----------------------------------------------------------------------------#
# JSON 解析与校验
# -----------------------------------------------------------------------------#
def parse_answer_json(s: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(s)

        # 假设 data 已经是 parse 后的字典
        answer = data.get("answer", "")
        sources = data.get("sources", [])
        confidence = float(data.get("confidence", 0.0))

        # 方式1：结构化日志（推荐）
        log_info("final answer", answer=answer, sources_count=len(sources), confidence=confidence)

        # 方式2：简单打印到 stdout
        print(f"answer: {answer}")
        print(f"sources: {sources}")
        print(f"confidence: {confidence:.2f}")

        if not isinstance(data, dict):
            return None
        # 基础字段校验
        if "answer" not in data or "sources" not in data or "confidence" not in data:
            return None
        if not isinstance(data["answer"], str):
            return None
        if not isinstance(data["sources"], list):
            return None
        if not isinstance(data["confidence"], (int, float)):
            return None
        # 归一化
        data["confidence"] = max(0.0, min(1.0, float(data["confidence"])))
        # sources 统一为字符串数组
        data["sources"] = [str(x) for x in data["sources"]]
        return data
    except Exception:
        return None

# -----------------------------------------------------------------------------#
# 医疗意图关键词（快速路由）
# -----------------------------------------------------------------------------#
MEDICAL_KEYWORDS = {
    "挂号", "预约", "门诊", "就诊", "科室",
    "报告", "化验", "检查结果", "化验单", "影像",
    "解读", "分析报告", "看报告", "查报告",
    "哪些医生", "可挂号", "排班", "号源", "医生列表",
    "胸闷", "胸痛", "呼吸困难", "心悸", "头晕", "发热", "腹痛",
    "高血压", "糖尿病", "冠心病", "慢病", "慢病管家",
    "建档", "档案", "随访", "复查", "复诊", "配药", "提醒", "预警",
    "病历", "病例", "主诉", "症状",
}

MEDICAL_MODULES: Dict[str, Dict[str, Any]] = {
    "INTELLIGENT_APPOINTMENT": {
        "label": "智能预约问诊",
        "agent": "appointment_intake_agent",
        "keywords": ["胸闷", "胸痛", "呼吸困难", "主诉", "症状", "挂号", "预约", "门诊"],
    },
    "CHRONIC_DISEASE": {
        "label": "慢病管理",
        "agent": "chronic_disease_agent",
        "keywords": ["慢病", "高血压", "糖尿病", "冠心病", "随访", "复查", "提醒", "预警", "建档"],
    },
    "REPORT_QUERY": {
        "label": "报告查询",
        "agent": "report_query_agent",
        "keywords": ["查报告", "报告", "化验", "检查结果", "影像", "医生列表", "排班", "号源"],
    },
    "REPORT_INTERPRET": {
        "label": "报告解读",
        "agent": "report_interpret_agent",
        "keywords": ["解读", "分析报告", "看报告", "解释结果"],
    },
    "GENERAL_MEDICAL": {
        "label": "综合医疗咨询",
        "agent": "general_medical_agent",
        "keywords": [],
    },
}

SWITCH_PAT = re.compile(r"(?:切换|转到|进入)\s*(?:到)?\s*([\u4e00-\u9fffA-Za-z_]+)")

ACUTE_APPOINTMENT_KEYWORDS = ["胸闷", "胸痛", "呼吸困难", "高热", "发烧", "39", "40", "急诊", "想挂", "挂心内科", "挂号"]
CHRONIC_EXPLICIT_KEYWORDS = ["慢病", "慢病管理", "随访", "复查", "复诊", "提醒", "建档", "档案", "预警"]


def _is_strong_module_signal(text: str, module_key: str) -> bool:
    normalized = (text or "").strip()
    if not normalized:
        return False

    if module_key == "INTELLIGENT_APPOINTMENT":
        return any(word in normalized for word in ACUTE_APPOINTMENT_KEYWORDS)
    if module_key == "CHRONIC_DISEASE":
        return any(word in normalized for word in CHRONIC_EXPLICIT_KEYWORDS)
    if module_key == "REPORT_INTERPRET":
        return any(word in normalized for word in ["解读", "分析报告", "看报告", "解释结果"])
    if module_key == "REPORT_QUERY":
        return any(word in normalized for word in ["查报告", "报告", "化验", "影像", "医生列表", "排班"])
    return False


def detect_medical_module(text: str) -> Optional[str]:
    normalized = (text or "").strip()
    if not normalized:
        return None

    # 急性症状 + 挂号诉求优先归到智能预约问诊，避免被“高血压”误分到慢病管理。
    if _is_strong_module_signal(normalized, "INTELLIGENT_APPOINTMENT"):
        return "INTELLIGENT_APPOINTMENT"
    if any(word in normalized for word in MEDICAL_MODULES["REPORT_INTERPRET"]["keywords"]):
        return "REPORT_INTERPRET"
    if _is_strong_module_signal(normalized, "CHRONIC_DISEASE"):
        return "CHRONIC_DISEASE"
    if any(word in normalized for word in MEDICAL_MODULES["INTELLIGENT_APPOINTMENT"]["keywords"]):
        return "INTELLIGENT_APPOINTMENT"
    if any(word in normalized for word in MEDICAL_MODULES["REPORT_QUERY"]["keywords"]):
        return "REPORT_QUERY"
    return None


def _module_meta(module_key: str) -> Dict[str, str]:
    module = MEDICAL_MODULES.get(module_key) or MEDICAL_MODULES["GENERAL_MEDICAL"]
    return {
        "module_key": module_key,
        "module_label": module["label"],
        "agent": module["agent"],
    }


def _resolve_session_module(user_text: str, known: Dict[str, Any]) -> Tuple[str, Optional[str]]:
    """返回 (active_module, switch_suggestion)。"""
    active_module = str(known.get("active_module") or "").strip() or "GENERAL_MEDICAL"
    detected_module = detect_medical_module(user_text)
    switch_suggestion: Optional[str] = None

    switch_match = SWITCH_PAT.search(user_text or "")
    if switch_match:
        explicit_target = detect_medical_module(switch_match.group(1))
        if explicit_target:
            active_module = explicit_target
            return active_module, None

    if active_module == "GENERAL_MEDICAL" and detected_module:
        active_module = detected_module
        return active_module, None

    if detected_module and detected_module != active_module:
        # 对于强信号请求自动切换，减少用户额外操作。
        if _is_strong_module_signal(user_text, detected_module):
            return detected_module, None

        current_label = _module_meta(active_module)["module_label"]
        target_label = _module_meta(detected_module)["module_label"]
        switch_suggestion = (
            f"当前模块为【{current_label}】。"
            f"检测到你可能想使用【{target_label}】；如需切换，请回复“切换到{target_label}”。"
        )

    return active_module, switch_suggestion

def is_medical_intent(text: str) -> bool:
    """关键词和常见生命体征快速判断是否为医疗任务。"""
    if any(kw in text for kw in MEDICAL_KEYWORDS):
        return True
    if re.search(r"\b\d{2,3}\s*/\s*\d{2,3}\b", text):
        return True
    if re.search(r"血糖\s*\d+(?:\.\d+)?", text):
        return True
    return False

# -----------------------------------------------------------------------------#
# 主接口：POST /chat
# 请求体：
# {
#   "message": "必填",
#   "context": "可选（若传则作为唯一依据）",
#   "session_id": "可选（医疗多轮追问用）",
#   "patient_id": "可选"
# }
# 响应体（通用）：
# {
#   "json": {answer,sources,confidence},
#   "markdown": "...",
#   "html": "...",
#   "request_id": "...",
#   "usage": {...}
# }
# 响应体（医疗路由时额外字段）：
# {
#   "type": "clarification|final",
#   "session_id": "...",
#   "routed_to": "medical_agent"
# }
# -----------------------------------------------------------------------------#
@app.route("/chat", methods=["POST"])
def chat() -> Tuple[Any, int]:
    # /chat 是统一入口：先做输入校验，再决定走医疗 Agent 还是通用 RAG。
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    user_input = (payload.get("message") or "").strip()
    context = (payload.get("context") or "").strip()

    if not user_input:
        return jsonify({"error": "message is required", "request_id": g.request_id}), 400
    if len(user_input) > MAX_INPUT_CHARS:
        return jsonify({"error": "message too long", "request_id": g.request_id}), 400
    if len(context) > 200_000:  # 简单保护，防止巨量上下文
        return jsonify({"error": "context too long", "request_id": g.request_id}), 400

    # ── 医疗意图自动路由 ──────────────────────────────────────────────────────
    # 条件1：关键词触发；条件2：已有活跃会话（追问回复不含关键词但必须继续走 Agent）
    session_id = (payload.get("session_id") or "").strip()
    known = load_session_state(session_id) if session_id else {}
    has_active_session = bool(session_id and known)
    if is_medical_intent(user_input) or has_active_session:
        if not session_id:
            session_id = str(uuid.uuid4())
        patient_id = (payload.get("patient_id") or "").strip()
        if patient_id:
            known["patient_id"] = patient_id

        active_module, switch_suggestion = _resolve_session_module(user_input, known)
        module_meta = _module_meta(active_module)
        known["active_module"] = active_module
        known["active_agent"] = module_meta["agent"]
        if switch_suggestion:
            known["module_switch_suggestion"] = switch_suggestion
        else:
            known.pop("module_switch_suggestion", None)

        try:
            agent_result, usage = medical_agent_step(
                message=user_input,
                known=known,
                call_llm=call_azure_openai,
                tool_client=tool_client,
            )
        except Exception as ex:
            log_error("medical agent auto-route failed", error=str(ex))
            return jsonify({"error": "medical agent failed", "detail": str(ex), "request_id": g.request_id}), 500

        agent_result.session_state["active_module"] = active_module
        agent_result.session_state["active_agent"] = module_meta["agent"]
        if switch_suggestion:
            agent_result.session_state["module_switch_suggestion"] = switch_suggestion
        else:
            agent_result.session_state.pop("module_switch_suggestion", None)

        save_session_state(session_id, agent_result.session_state)
        answer_md = agent_result.response_json.get("answer", "") or "我不知道"
        answer_html = render_markdown_safe(answer_md)
        log_info("medical auto-route", type=agent_result.type, session_id=session_id)

        return jsonify({
            "type": agent_result.type,          # clarification | final
            "session_id": session_id,
            "json": agent_result.response_json,
            "markdown": answer_md,
            "html": answer_html,
            "request_id": g.request_id,
            "usage": usage,
            "routed_to": "medical_agent",       # 供前端区分路由
            "current_module": active_module,
            "current_module_label": module_meta["module_label"],
            "current_agent": module_meta["agent"],
            "module_switch_suggestion": switch_suggestion,
        }), 200
    # ── 通用 RAG 链路（原逻辑不变） ──────────────────────────────────────────
    # 只有在未命中医疗路由时才进入这里。

    # 组装 System Prompt
    system_prompt = SYSTEM_PROMPT_BASE + (SYSTEM_PROMPT_NO_CONTEXT_APPEND if not context else "")

    # 第一阶段提示词
    instruction = INSTRUCTION_TEMPLATE.format(context=context if context else "(无)", question=user_input)

    # --- 阶段一：正常回答 ---
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": instruction},
    ]

    t0 = time.time()
    try:
        content, usage = call_azure_openai(messages)
    except Exception as ex:
        log_error("OpenAI call failed (phase1)", error=str(ex))
        return jsonify({"error": "OpenAI call failed", "detail": str(ex), "request_id": g.request_id}), 502

    data = parse_answer_json(content)

    # --- 阶段二：若不合格，进入严格重试 ---
    need_strict = False
    if not data:
        need_strict = True
        reason = "json_parse_failed"
    else:
        # 引用与置信度检查：sources 为空或置信度过低，则进入更严格模板
        if (len(data.get("sources", [])) == 0 and context) or (data.get("confidence", 0.0) < MIN_CONFIDENCE and context):
            need_strict = True
            reason = "low_conf_or_empty_sources"

    if need_strict:
        messages_strict = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": instruction + STRICT_APPEND},
        ]
        try:
            content2, usage2 = call_azure_openai(messages_strict)
            usage = {  # 累加 usage（粗略）
                "prompt_tokens": usage.get("prompt_tokens", 0) + usage2.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0) + usage2.get("completion_tokens", 0),
                "total_tokens": usage.get("total_tokens", 0) + usage2.get("total_tokens", 0),
            }
            data2 = parse_answer_json(content2)
            if data2:
                data = data2
                content = content2
            else:
                log_warn("Strict retry still invalid JSON", reason=reason)
        except Exception as ex:
            log_warn("Strict retry failed", reason=reason, error=str(ex))

    # 若仍然没有合格 JSON，则给出保底拒答
    if not data:
        data = {
            "answer": "我不知道",
            "sources": [],
            "confidence": 0.0
        }
        content = json.dumps(data, ensure_ascii=False)

    latency_ms = int((time.time() - t0) * 1000)

    # 将 answer（纯文本）同时渲染为 Markdown & HTML（这里假定 answer 已是 Markdown）
    answer_md = data.get("answer", "") or "我不知道"
    answer_html = render_markdown_safe(answer_md)

    # 入库（错误不阻断）
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO chat (
                request_id, question, answer_md, answer_json,
                usage_prompt_tokens, usage_completion_tokens, usage_total_tokens,
                confidence, timestamp
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                g.request_id,
                user_input,
                answer_md,
                json.dumps(data, ensure_ascii=False),
                usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0),
                usage.get("total_tokens", 0),
                float(data.get("confidence", 0.0)),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
    except Exception as ex:
        log_warn("DB insert failed", error=str(ex))
    finally:
        try:
            conn.close()
        except Exception:
            pass

    # 可观测性日志
    log_info(
        "chat handled",
        latency_ms=latency_ms,
        usage=usage,
        confidence=float(data.get("confidence", 0.0)),
        sources=len(data.get("sources", [])),
        has_context=bool(context),
    )

    return (
        jsonify({
            "json": data,              # 机器可读
            "markdown": answer_md,     # 人类可读
            "html": answer_html,       # 前端安全展示
            "request_id": g.request_id,
            "usage": usage,
        }),
        200,
    )

@app.route("/medical/chat", methods=["POST"])
def medical_chat():
    payload = request.get_json(silent=True) or {}
    user_input = (payload.get("message") or "").strip()
    session_id = (payload.get("session_id") or "").strip() or str(uuid.uuid4())
    patient_id = (payload.get("patient_id") or "").strip()  # 可选

    if not user_input:
        return jsonify({"error": "message is required", "request_id": g.request_id}), 400
    if len(user_input) > MAX_INPUT_CHARS:
        return jsonify({"error": "message too long", "request_id": g.request_id}), 400

    # 读取会话状态
    known = load_session_state(session_id)
    if patient_id:
        known["patient_id"] = patient_id

    active_module, switch_suggestion = _resolve_session_module(user_input, known)
    module_meta = _module_meta(active_module)
    known["active_module"] = active_module
    known["active_agent"] = module_meta["agent"]
    if switch_suggestion:
        known["module_switch_suggestion"] = switch_suggestion
    else:
        known.pop("module_switch_suggestion", None)

    # 复用你已有的 call_azure_openai（把函数当作 call_llm 传进去）
    def call_llm(messages):
        return call_azure_openai(messages)

    try:
        agent_result, usage = medical_agent_step(
            message=user_input,
            known=known,
            call_llm=call_llm,
            tool_client=tool_client,
        )
    except Exception as ex:
        log_error("medical agent failed", error=str(ex))
        return jsonify({"error": "medical agent failed", "detail": str(ex), "request_id": g.request_id}), 500

    agent_result.session_state["active_module"] = active_module
    agent_result.session_state["active_agent"] = module_meta["agent"]
    if switch_suggestion:
        agent_result.session_state["module_switch_suggestion"] = switch_suggestion
    else:
        agent_result.session_state.pop("module_switch_suggestion", None)

    # 保存会话状态（用于下一轮补参）
    save_session_state(session_id, agent_result.session_state)

    # 输出（沿用你原来的 markdown/html 渲染）
    answer_md = agent_result.response_json.get("answer", "") or "我不知道"
    answer_html = render_markdown_safe(answer_md)

    return jsonify({
        "type": agent_result.type,         # clarification / final
        "session_id": session_id,
        "json": agent_result.response_json,
        "markdown": answer_md,
        "html": answer_html,
        "request_id": g.request_id,
        "usage": usage,
        "routed_to": "medical_agent",
        "current_module": active_module,
        "current_module_label": module_meta["module_label"],
        "current_agent": module_meta["agent"],
        "module_switch_suggestion": switch_suggestion,
    }), 200


def _svc_extract_bp(text: str) -> Optional[str]:
    match = re.search(r"(\d{2,3})\s*/\s*(\d{2,3})", text)
    if not match:
        return None
    return f"{match.group(1)}/{match.group(2)}"


def _svc_detect_severity(req: Dict[str, Any]) -> Dict[str, Any]:
    complaint = str(req.get("chief_complaint") or "")
    symptoms = req.get("symptoms") or []
    joined = " ".join([complaint, *[str(item) for item in symptoms]])
    bp = ((req.get("vital_signs") or {}) if isinstance(req.get("vital_signs"), dict) else {})
    score = 25
    reasons: List[str] = []

    if any(item in joined for item in ["胸痛", "胸闷", "呼吸困难"]):
        score = max(score, 85)
        reasons.append("存在心肺高风险症状")
    if any(item in joined for item in ["头晕", "发热", "咳嗽", "腹痛"]):
        score = max(score, 45)
        reasons.append("存在需要就诊评估的症状")

    bp_text = bp.get("bp") or _svc_extract_bp(joined)
    if bp_text:
        try:
            systolic, diastolic = map(int, bp_text.split("/"))
            if systolic >= 180 or diastolic >= 110:
                score = max(score, 90)
                reasons.append(f"血压危急值 {bp_text}")
            elif systolic >= 160 or diastolic >= 100:
                score = max(score, 70)
                reasons.append(f"血压明显升高 {bp_text}")
        except ValueError:
            pass

    level = "WHITE"
    if score >= 81:
        level = "RED"
    elif score >= 51:
        level = "ORANGE"
    elif score >= 21:
        level = "YELLOW"
    return {"level": level, "score": score, "reasons": reasons or ["需要门诊进一步评估"]}


def _svc_recommend_department(req: Dict[str, Any]) -> str:
    complaint = str(req.get("chief_complaint") or "")
    symptoms = req.get("symptoms") or []
    joined = " ".join([complaint, *[str(item) for item in symptoms]])
    if any(item in joined for item in ["胸闷", "胸痛", "心悸", "高血压"]):
        return "心内科"
    if any(item in joined for item in ["糖尿病", "血糖"]):
        return "内分泌科"
    if any(item in joined for item in ["咳嗽", "呼吸困难"]):
        return "呼吸内科"
    return "普通内科"


def _svc_chronic_reminders(disease_name: str) -> List[Dict[str, Any]]:
    today = datetime.now().date()
    base_checks = {
        "高血压": ["血压", "血清肌酐", "尿蛋白"],
        "糖尿病": ["空腹血糖", "HbA1c", "尿微量白蛋白"],
        "冠心病": ["ECG", "肌钙蛋白", "血脂全项"],
    }
    checks = base_checks.get(disease_name, ["门诊复查"])
    return [
        {
            "reminder_type": "CHECKUP",
            "title": f"{disease_name}定期复查",
            "description": f"建议按时复查：{', '.join(checks)}",
            "due_date": str(today),
            "delivery_channels": ["SMS", "APP", "VOICE"],
        },
        {
            "reminder_type": "MEDICATION",
            "title": f"{disease_name}续方提醒",
            "description": "请确认常用药物余量，避免断药。",
            "due_date": str(today),
            "delivery_channels": ["SMS", "APP"],
        },
    ]

# ---------------------------
# Mock microservices（MVP）
# 默认单进程跑通 demo，之后再把这些 URL 指到真实服务即可
# ---------------------------
@app.route("/svc/register", methods=["POST"])
def svc_register():
    req = request.get_json(silent=True) or {}
    # TODO: 替换成真实号源系统
    return jsonify({
        "status": "CONFIRMED",
        "registration_id": "R-MOCK-0001",
        "scheduled_time": req.get("preferred_time"),
        "location": "门诊楼2层A区",
        "notes": "请提前30分钟到院取号"
    }), 200

@app.route("/svc/query", methods=["POST"])
def svc_query():
    req = request.get_json(silent=True) or {}
    qt = req.get("query_type", "LAB_REPORT")
    hospital = req.get("hospital", "")
    department = req.get("department", "")

    if qt == "DOCTOR_LIST":
        # TODO: 替换成真实号源排班查询
        doctors = [
            {"name": "张伟", "title": "主任医师", "department": department or "心内科",
             "available_times": ["2026-04-03 08:00", "2026-04-03 10:00", "2026-04-03 14:00"]},
            {"name": "李敏", "title": "副主任医师", "department": department or "心内科",
             "available_times": ["2026-04-03 09:00", "2026-04-04 08:30"]},
            {"name": "王芳", "title": "主治医师", "department": department or "心内科",
             "available_times": ["2026-04-03 11:00", "2026-04-03 15:00"]},
        ]
        return jsonify({
            "hospital": hospital or "上海瑞金北院",
            "department": department or "心内科",
            "doctors": doctors,
            "note": "以上为模拟数据，实际号源请以医院官网/APP为准"
        }), 200

    if qt == "LAB_REPORT":
        return jsonify({
            "items": [
                {
                    "report_id": "LAB-MOCK-20260315",
                    "report_type": "LIPID_PANEL",
                    "report_time": "2026-03-15T09:12:00+08:00",
                    "results": [
                        {"name": "LDL-C", "value": 4.1, "unit": "mmol/L", "ref": "<3.4", "flag": "HIGH"},
                        {"name": "TG",    "value": 2.0, "unit": "mmol/L", "ref": "<1.7", "flag": "HIGH"}
                    ]
                }
            ]
        }), 200

    # REG_RECORD / VISIT_RECORD / IMAGING / 其他
    return jsonify({"items": [], "note": f"暂无 {qt} 类型的模拟数据"}), 200

@app.route("/svc/interpret", methods=["POST"])
def svc_interpret():
    req = request.get_json(silent=True) or {}
    report = req.get("report")
    # MVP：如果没传 report，就从 report_id 做一个 mock
    if not report:
        report = {"report_type": "UNKNOWN", "results": []}

    # 这里只做“信息解释”，不诊断
    return jsonify({
        "summary": "报告存在异常指标（如有 HIGH 标记）。建议结合病史咨询医生。",
        "highlights": ["异常项请关注 HIGH/LOW 标记及参考范围"],
        "suggestions": [
            "如出现胸痛、气促、持续不适等症状，请及时就医",
            "以医院报告结论与医生解读为准"
        ],
        "disclaimer": "仅供信息参考，不构成诊断或治疗建议"
    }), 200


@app.route("/svc/emr/intake", methods=["POST"])
def svc_emr_intake():
    req = request.get_json(silent=True) or {}
    severity = _svc_detect_severity(req)
    department = _svc_recommend_department(req)
    return jsonify({
        "status": "STRUCTURED",
        "emr_id": f"EMR-MOCK-{uuid.uuid4().hex[:8].upper()}",
        "structured_data": {
            "chief_complaint": req.get("chief_complaint") or "未提供",
            "symptoms": req.get("symptoms") or [],
            "medical_history": req.get("medical_history") or [],
            "vital_signs": req.get("vital_signs") or {},
            "preliminary_assessment": "系统已生成结构化初诊摘要，供医生问诊前参考。",
        },
        "severity": severity,
        "recommended_dept": department,
        "recommended_doctor_level": "EXPERT" if severity["level"] == "RED" else "SPECIALIST",
        "suggested_tests": ["血常规", "生化全项", "ECG"],
    }), 200


@app.route("/svc/chronic/intake", methods=["POST"])
def svc_chronic_intake():
    req = request.get_json(silent=True) or {}
    disease_name = req.get("disease_name") or "慢病"
    return jsonify({
        "status": "RECORDED",
        "record_id": f"CDR-MOCK-{uuid.uuid4().hex[:8].upper()}",
        "patient_id": req.get("patient_id") or "P001",
        "disease_name": disease_name,
        "diagnosis_date": req.get("diagnosis_date") or datetime.now().date().isoformat(),
        "message": f"已建立{disease_name}随访档案，并生成后续提醒计划。",
    }), 200


@app.route("/svc/chronic/generate-reminders", methods=["POST"])
def svc_chronic_generate_reminders():
    req = request.get_json(silent=True) or {}
    disease_name = req.get("disease_name") or "慢病"
    return jsonify({
        "patient_id": req.get("patient_id") or "P001",
        "disease_name": disease_name,
        "reminders": _svc_chronic_reminders(disease_name),
    }), 200


@app.route("/svc/chronic/check-urgent-warning", methods=["POST"])
def svc_chronic_check_warning():
    req = request.get_json(silent=True) or {}
    systolic = req.get("systolic")
    diastolic = req.get("diastolic")
    blood_glucose = req.get("blood_glucose")
    if systolic and diastolic and (int(systolic) >= 180 or int(diastolic) >= 110):
        return jsonify({
            "level": "URGENT",
            "message": f"当前血压 {systolic}/{diastolic}，建议立即就医或急诊评估。",
            "actions": ["立即复测血压", "联系医生", "必要时前往急诊"],
        }), 200
    if blood_glucose and float(blood_glucose) >= 11.1:
        return jsonify({
            "level": "URGENT",
            "message": f"当前血糖 {blood_glucose} mmol/L，建议尽快联系医生。",
            "actions": ["复测血糖", "联系医生", "注意补水"],
        }), 200
    return jsonify({
        "status": "no_warning",
        "message": "当前未触发紧急预警，但建议按计划复查。",
    }), 200
# -----------------------------------------------------------------------------#
# 路由：文档入库（/ingest）
# POST body: { "source": "文档名或URL", "text": "原文长文本" }
# 将 text 切分 -> embedding -> 存到 rag_docs
# -----------------------------------------------------------------------------#
@app.route("/ingest", methods=["POST"])
def ingest():
    payload = request.get_json(silent=True) or {}
    source = (payload.get("source") or "").strip()
    text = (payload.get("text") or "").strip()
    if not source or not text:
        return jsonify({"error": "source and text are required", "request_id": g.request_id}), 400

    # 切分
    chunks = simple_overlap_chunks(text, CHUNK_SIZE, CHUNK_OVERLAP)
    if not chunks:
        return jsonify({"error": "no chunks produced", "request_id": g.request_id}), 400

    # 向量化
    try:
        vecs = embed_texts(chunks)
    except Exception as ex:
        log_error("Embedding failed", error=str(ex))
        return jsonify({"error": "Embedding failed", "detail": str(ex), "request_id": g.request_id}), 502

    # 保存
    save_chunks_with_embeddings(source, chunks, vecs)
    log_info("ingested", source=source, chunks=len(chunks))

    return jsonify({"ok": True, "source": source, "chunks": len(chunks), "request_id": g.request_id}), 200

# -----------------------------------------------------------------------------#
# 入口
# -----------------------------------------------------------------------------#
if __name__ == "__main__":
    if not AZURE_OPENAI_API_KEY or not AZURE_OPENAI_ENDPOINT:
        logger.warning("Missing required environment variables: AZURE_OPENAI_API_KEY/AZURE_OPENAI_ENDPOINT")
    init_db()
    # 生产环境建议：gunicorn/uvicorn 部署；关闭 debug
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "5000")))
