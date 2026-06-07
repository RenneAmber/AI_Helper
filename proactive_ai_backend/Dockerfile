# =============================================================================
# proactive-ai-backend 镜像
# 目标：可在 Docker / Kubernetes 中运行的最小生产镜像
# =============================================================================

# 基础镜像：使用官方 slim 版本，体积小、漏洞面小，足够跑 FastAPI/uvicorn
FROM python:3.12-slim

# 运行期环境变量：
#   PYTHONDONTWRITEBYTECODE=1  不在容器里写 .pyc，减少层和 IO
#   PYTHONUNBUFFERED=1         让 print/日志立刻 flush，方便 docker logs 实时看到
#   PIP_NO_CACHE_DIR=1         不缓存 pip 包，缩小镜像
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# 工作目录：所有后续 COPY / CMD 默认相对路径都基于这里
WORKDIR /srv

# 先只拷依赖清单，再安装依赖。这一层只在 requirements.txt 变化时才会重建，
# 大幅提升日常代码改动的构建速度（Docker layer cache）
COPY requirements.txt /srv/requirements.txt
RUN pip install --upgrade pip && pip install -r /srv/requirements.txt

# 再拷应用代码。应用代码改动频繁，所以放在依赖层之后
COPY app /srv/app

# 暴露 API 端口（仅声明用途，实际端口由 docker run -p / k8s Service 决定）
EXPOSE 8100

# 容器级健康检查：
# 让 Docker / 编排器（如 docker compose）能感知到容器内进程是否健康。
# K8s 中通常用 livenessProbe / readinessProbe 取代，但保留这条对纯 Docker 跑时有用。
HEALTHCHECK --interval=20s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request, sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/healthz', timeout=2).status == 200 else 1)" \
    || exit 1

# 启动命令：
#   uvicorn 直接运行 ASGI 应用
#   --host 0.0.0.0 让容器外可访问
#   --workers 2    多进程提高吞吐；在 K8s 中也可以改成 1 worker + 多 replica
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100", "--workers", "2"]
