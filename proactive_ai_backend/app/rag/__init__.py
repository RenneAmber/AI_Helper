"""Aido RAG 子系统：embedder / store / chunker / service。

设计目标
--------
1. **零外部服务依赖即可启动**——mock embedder + SQLite + numpy
2. **可平滑升级到真模型**——同一个 `Embedder` 协议，env 决定 mock / azure / openai
3. **接口与 LangChain VectorStore 高度对齐**——日后切 LangGraph + LangChain 不痛
4. **多租户**——所有读写按 user_id 隔离
"""
