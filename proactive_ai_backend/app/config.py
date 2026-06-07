from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # 服务名，用于日志、指标 label、健康检查响应中标识当前进程。
    # 多副本部署时保持一致；不同子系统之间应区分。
    app_name: str = "proactive-ai-backend"

    # 运行环境标识：dev | staging | prod。
    # 用于日志/告警的环境维度过滤，也可在代码中切换调试行为。
    app_env: str = "dev"

    # 模型推理 Provider 选择：
    #   "mock"      —— 不依赖任何外部 Key，本地伪造响应，便于离线开发与单测
    #   "azure"     —— Azure OpenAI（沿用根目录 app.py 同一套 AZURE_OPENAI_* 环境变量）
    #   "openai"    —— 走 OpenAI 公共/兼容协议的提供方
    #   "anthropic" —— 预留接入位
    # 该值会被环境变量 PROVIDER 覆盖（见 providers.build_provider）。
    # 若未显式指定，build_provider 会在检测到 AZURE_OPENAI_API_KEY 时自动用 azure。
    provider: str = "mock"

    # ---- OpenAI 公共云 ----
    openai_api_key: str = ""
    openai_model: str = "gpt-4o-mini"
    openai_base_url: str = ""  # 留空走官方域名；可指向兼容网关

    # ---- Azure OpenAI（与 app.py 共用同名环境变量）----
    # AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_VERSION / AZURE_OPENAI_DEPLOYMENT
    # pydantic-settings 字段名按下划线小写匹配 ENV，env_prefix="" 时自动支持大小写。
    azure_openai_api_key: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_endpoint: str = ""  # 兼容你 app.py 里两个变量名
    azure_openai_api_version: str = "2025-01-01-preview"
    azure_openai_deployment: str = "gpt-41_milky"

    # ---- Anthropic ----
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-3-5-sonnet-latest"

    # Token 限速（令牌桶）：保护下游配额，避免突发把账号打爆
    # tokens_per_minute 是平均速率；burst 是瞬时桶容量
    provider_tokens_per_minute: int = 60_000
    provider_burst_tokens: int = 8_000

    # 分布式工作流：Redis 连接串，留空则退回到进程内队列
    redis_url: str = ""
    workflow_queue_key: str = "wf:queue"
    workflow_state_prefix: str = "wf:state:"

    # OpenTelemetry：留空则不导出
    otlp_endpoint: str = ""        # 例如 "http://otel-collector:4317"
    otlp_service_name: str = "proactive-ai-backend"

    # Storage
    sqlite_path: str = "./proactive_ai.db"

    # ---- 推理性能调优 ----

    # 推理响应缓存的存活时间（秒）。
    # 命中相同 prompt+user 的请求会直接返回缓存，降低延迟与 token 成本。
    # 设过大易出陈旧结果，设过小命中率低。
    cache_ttl_seconds: int = 60

    # 推理响应缓存的最大条目数（LRU 淘汰）。
    # 控制内存占用上限；高并发场景应配合外部缓存（如 Redis）。
    cache_max_items: int = 1024

    # 微批处理一次最多合并多少个并发推理请求。
    # 越大吞吐越高、单请求延迟也越高；需要与下游 provider 的 batch 能力匹配。
    batch_max_size: int = 8

    # 微批处理的等待窗口（毫秒）。
    # 在该窗口内尽量凑齐 batch_max_size；窗口越大批量越满，但首请求延迟越高。
    batch_window_ms: int = 25

    # 单次下游推理调用的硬超时（毫秒）。
    # 超过即视为失败，触发重试/熔断/降级；用来保护接口 P99 延迟。
    request_timeout_ms: int = 8000

    # ---- 可靠性策略 ----

    # 单次下游调用允许的最大尝试次数（含首次）。
    # 仅对幂等调用使用更大值；非幂等调用建议 1-2。
    retry_max_attempts: int = 3

    # 重试退避的基准延迟（毫秒），实际使用指数退避 + 抖动：
    # base * 2^(attempt-1) + jitter，避免雪崩式同步重试。
    retry_base_delay_ms: int = 100

    # 熔断器打开阈值：连续失败次数达到该值后切到 OPEN 状态。
    # 触发后短时间内拒绝请求或走 fallback，保护下游不被打挂。
    circuit_failure_threshold: int = 5

    # 熔断器从 OPEN 进入 HALF-OPEN 的冷却时间（秒）。
    # 冷却结束后允许少量探测请求，成功则恢复 CLOSED。
    circuit_reset_seconds: int = 30

    # ---- 工作流编排 ----

    # 单个工作流步骤的最大执行时间（毫秒），超时算失败并记录事故。
    # 控制单步对端依赖延迟，避免长流程被某一步拖死。
    workflow_step_timeout_ms: int = 10000

    # 单个工作流允许的最大步骤数，防止恶意/异常请求构造超长任务链耗尽资源。
    workflow_max_steps: int = 20

    # ---- 上下文记忆 ----

    # 每次推理向 prompt 注入的最近消息条数，控制上下文长度上限。
    # 设太大会增加 token 成本与延迟，设太小会失去对话连续性。
    memory_window_messages: int = 20

    # ---- 摘要（滚动压缩长对话） ----

    # 当 session 累计消息数超过该阈值时，触发后台摘要器把旧消息压缩成 summary。
    # 越大触发越少，prompt 越长；越小越频繁压缩，节省 token 但增加额外推理。
    summary_trigger_messages: int = 30

    # 触发摘要时，最近多少条原始消息保留不参与压缩。
    # 保留近端的高保真上下文，远端则用摘要文本顶替。
    summary_keep_recent: int = 10

    # ---- 语义记忆（事实/偏好） ----

    # 每次推理从 semantic_facts 中检索注入 prompt 的最大条数。
    # 控制 RAG 注入预算，避免淹没近期上下文。
    semantic_top_k: int = 3

    # Pydantic Settings 加载配置：
    # - env_file=".env" 允许通过环境变量文件覆盖以上字段
    # - extra="ignore" 忽略 .env 中未声明的额外字段，避免启动失败
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")


# 全局单例：业务模块通过 `from .config import settings` 引用。
# 不要在运行时修改字段，配置变更通过重启进程生效。
settings = Settings()
