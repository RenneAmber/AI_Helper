"""
RAG 子系统端到端测试 —— mock embedder + 真实 SQLite 临时库，不打外网。

覆盖范围
--------
1. MockEmbedder 输出确定性 & 归一化
2. chunker 剥引用/签名、保留 header
3. store.upsert_chunks + search 排序与 top_k
4. service.ingest_email 端到端：3 封邮件入库 → 查询 → 命中正确邮件
5. service.search 多 source_type 过滤
6. prompt_builder 在 rag_enabled=True 时正确注入 retrieved_context 段
7. delete_user / delete_source 清理
"""

from __future__ import annotations

import asyncio
import os
import tempfile

import numpy as np
import pytest

from app.config import settings
from app.memory import init_db
from app.rag import embeddings as emb_mod
from app.rag import service as rag_service
from app.rag import store as rag_store
from app.rag.chunker import EmailDoc, chunk_email, strip_quotes_and_signature


# ---------- 公共 fixture：临时 SQLite 库 + MockEmbedder 强制 ----------

@pytest.fixture(autouse=True)
async def _tmp_db(monkeypatch):
    tmp_dir = tempfile.mkdtemp(prefix="aido_rag_test_")
    db_path = os.path.join(tmp_dir, "rag_test.db")
    monkeypatch.setattr(settings, "sqlite_path", db_path)
    # 强制 mock，且重置单例
    monkeypatch.setattr(settings, "rag_embedder", "mock")
    emb_mod._reset_for_tests()
    await init_db()
    yield
    emb_mod._reset_for_tests()


# ---------- 1. embedder ----------

@pytest.mark.asyncio
async def test_mock_embedder_is_deterministic_and_normalized():
    e = emb_mod.MockEmbedder()
    a = await e.embed(["hello world"])
    b = await e.embed(["hello world"])
    assert np.allclose(a, b)
    # 归一化：模长 ≈ 1
    assert pytest.approx(float(np.linalg.norm(a[0])), rel=1e-5) == 1.0
    # 不同输入向量不同
    c = await e.embed(["totally different"])
    assert not np.allclose(a, c)


# ---------- 2. chunker ----------

def test_strip_quotes_and_signature():
    body = (
        "你好，这是我要说的事。\n"
        "明天下午 3 点见。\n"
        "\n"
        "-- \n"
        "签名行不应入库\n"
        "On Mon, 2024 Boss <boss@x> wrote:\n"
        "> 老的回复正文也丢弃\n"
    )
    cleaned = strip_quotes_and_signature(body)
    assert "签名行" not in cleaned
    assert "老的回复" not in cleaned
    assert "明天下午 3 点见。" in cleaned


def test_chunk_email_first_chunk_has_header():
    doc = EmailDoc(
        uid="m1",
        subject="项目同步",
        sender="alice@example.com",
        date="2024-09-01",
        body="正文段落一。\n\n正文段落二。",
    )
    chunks = chunk_email(doc)
    assert len(chunks) >= 1
    assert "[Subject] 项目同步" in chunks[0]
    assert "[From] alice@example.com" in chunks[0]


# ---------- 3. store 基础 ----------

@pytest.mark.asyncio
async def test_upsert_and_search_basic():
    e = emb_mod.MockEmbedder()
    texts = ["alpha cat", "beta dog", "gamma fish"]
    vecs = await e.embed(texts)
    chunks = [
        rag_store.Chunk("u1", "note", "n1", i, t, {})
        for i, t in enumerate(texts)
    ]
    n = await rag_store.upsert_chunks(chunks, vecs, embedder_name=e.name, dim=e.dim)
    assert n == 3

    # 查 "alpha cat" 应当排第一（与自身完全一致 score=1.0）
    q = await e.embed(["alpha cat"])
    hits = await rag_store.search("u1", q[0], top_k=2)
    assert len(hits) == 2
    assert hits[0].text == "alpha cat"
    assert pytest.approx(hits[0].score, abs=1e-4) == 1.0


@pytest.mark.asyncio
async def test_search_isolates_by_user_id():
    e = emb_mod.MockEmbedder()
    vecs = await e.embed(["secret note for u1"])
    await rag_store.upsert_chunks(
        [rag_store.Chunk("u1", "note", "x", 0, "secret note for u1", {})],
        vecs, embedder_name=e.name, dim=e.dim,
    )
    q = await e.embed(["secret note for u1"])
    # u2 看不到 u1 的数据
    hits = await rag_store.search("u2", q[0], top_k=5)
    assert hits == []


# ---------- 4. service.ingest_email 端到端 ----------

@pytest.mark.asyncio
async def test_ingest_email_and_retrieve_correct_doc():
    user = "alice"
    emails = [
        {
            "uid": "e1", "subject": "周会通知",
            "from": "boss@x", "date": "2024-09-01",
            "body": "本周三下午 3 点开周会，地点会议室 A。",
        },
        {
            "uid": "e2", "subject": "团建报名",
            "from": "hr@x", "date": "2024-09-02",
            "body": "周五下班后去吃火锅，请回复是否参加。",
        },
        {
            "uid": "e3", "subject": "项目代号 Falcon 启动",
            "from": "pm@x", "date": "2024-09-03",
            "body": "Falcon 项目第一阶段交付 deadline 在 9 月 30 日。",
        },
    ]
    for em in emails:
        n = await rag_service.ingest_email(user, em)
        assert n >= 1

    # 查询 "Falcon" → 应命中 e3
    hits = await rag_service.search(user, "Falcon 项目 deadline", top_k=3)
    assert hits, "应至少命中一条"
    assert hits[0].source_id == "e3"
    # 命中的 metadata 应保留邮件头信息
    assert hits[0].metadata.get("subject") == "项目代号 Falcon 启动"


@pytest.mark.asyncio
async def test_ingest_email_idempotent_replaces_old_chunks():
    user = "alice"
    em = {"uid": "e1", "subject": "v1", "body": "原始正文" * 100}
    n1 = await rag_service.ingest_email(user, em)
    em2 = {"uid": "e1", "subject": "v2", "body": "短了"}
    n2 = await rag_service.ingest_email(user, em2)

    stats = await rag_store.count(user)
    # 重 ingest 后总数应等于第二次的 chunk 数（旧的被清掉）
    assert stats["total"] == n2


# ---------- 5. 多 source_type 过滤 ----------

@pytest.mark.asyncio
async def test_search_filter_by_source_type():
    user = "alice"
    await rag_service.ingest_email(user, {"uid": "e1", "subject": "邮件 alpha", "body": "alpha 邮件正文"})
    await rag_service.ingest_text(user, "note", "n1", "alpha 笔记正文", {"tag": "demo"})

    # 只在 note 里搜
    hits_note = await rag_service.search(user, "alpha", top_k=3, source_types=["note"])
    assert hits_note and all(h.source_type == "note" for h in hits_note)
    # 只在 email 里搜
    hits_email = await rag_service.search(user, "alpha", top_k=3, source_types=["email"])
    assert hits_email and all(h.source_type == "email" for h in hits_email)


# ---------- 6. prompt_builder 注入 ----------

@pytest.mark.asyncio
async def test_prompt_builder_injects_rag_when_enabled(monkeypatch):
    from app import prompt_builder

    user = "alice"
    await rag_service.ingest_email(user, {
        "uid": "e1", "subject": "周会通知", "from": "boss@x",
        "body": "本周三下午 3 点开周会，地点会议室 A。",
    })

    monkeypatch.setattr(settings, "rag_enabled", True)
    monkeypatch.setattr(settings, "rag_top_k", 3)
    prompt = await prompt_builder.build_prompt(
        session_id="s1", user_id=user, user_message="周三的会几点？",
    )
    assert "retrieved_context:" in prompt
    assert "周会" in prompt or "会议室 A" in prompt


@pytest.mark.asyncio
async def test_prompt_builder_skips_rag_when_disabled(monkeypatch):
    from app import prompt_builder

    user = "alice"
    await rag_service.ingest_email(user, {
        "uid": "e1", "subject": "周会通知", "body": "周三 3 点",
    })
    monkeypatch.setattr(settings, "rag_enabled", False)
    prompt = await prompt_builder.build_prompt(
        session_id="s1", user_id=user, user_message="周三的会几点？",
    )
    assert "retrieved_context:" not in prompt


# ---------- 7. 清理 ----------

@pytest.mark.asyncio
async def test_delete_user_wipes_everything():
    user = "bob"
    await rag_service.ingest_email(user, {"uid": "e1", "subject": "x", "body": "x"})
    await rag_service.ingest_text(user, "note", "n1", "y")

    pre = await rag_store.count(user)
    assert pre["total"] >= 2

    deleted = await rag_store.delete_user(user)
    assert deleted >= 2

    post = await rag_store.count(user)
    assert post["total"] == 0
