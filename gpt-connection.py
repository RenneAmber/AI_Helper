
# app.py
"""
Flask 后端（增强版）：调用 Azure OpenAI，提供“低幻觉”回答。
特性：
- 反幻觉：System Prompt + 严格 JSON Schema + 两阶段重试 + 置信度阈值 + 引用校验
- 稳定性：请求超时、指数退避重试、错误分类（429/5xx）、输入长度限制
- 安全性：Markdown -> HTML 安全清洗（bleach）、CORS 限制、SQLite WAL
- 工程化：统一 JSON 错误、结构化日志（request_id、latency、token usage）
- 可配置：温度/采样、max tokens、CORS 允许来源、置信度阈值等可通过环境变量设置
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

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
CORS(app, resources={r"/chat": {"origins": allowed_origin_list}})

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s [%(request_id)s]: %(message)s",
)
logger = logging.getLogger("chat-app")

DB_PATH = os.getenv("DB_PATH", "chat_history.db")

# 运行参数
MAX_INPUT_CHARS = int(os.getenv("MAX_INPUT_CHARS", "5000"))  # 输入长度上限（字符）
MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.6"))   # 置信度门槛
RETRY_MAX = int(os.getenv("RETRY_MAX", "2"))                 # 调用重试次数（不含首次）
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "15"))  # 单次请求超时（秒）

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

# AzureOpenAI 客户端
client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT,
)

# -----------------------------------------------------------------------------#
# DB 初始化（WAL 提升并发写入；保留最小字段）
# -----------------------------------------------------------------------------#
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
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
# 主接口：POST /chat
# 请求体：
# {
#   "message": "必填",
#   "context": "可选（若传则作为唯一依据）"
# }
# 响应体：
# {
#   "json": {answer,sources,confidence},
#   "markdown": "...",
#   "html": "...",
#   "request_id": "...",
#   "usage": {...}
# }
# -----------------------------------------------------------------------------#
@app.route("/chat", methods=["POST"])
def chat() -> Tuple[Any, int]:
    payload: Dict[str, Any] = request.get_json(silent=True) or {}
    user_input = (payload.get("message") or "").strip()
    context = (payload.get("context") or "").strip()

    if not user_input:
        return jsonify({"error": "message is required", "request_id": g.request_id}), 400
    if len(user_input) > MAX_INPUT_CHARS:
        return jsonify({"error": "message too long", "request_id": g.request_id}), 400
    if len(context) > 200_000:  # 简单保护，防止巨量上下文
        return jsonify({"error": "context too long", "request_id": g.request_id}), 400

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
