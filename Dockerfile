# ---------- 构建阶段 ----------
FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
COPY pyproject.toml README.md ./
COPY reviewbot ./reviewbot
COPY main.py ./

RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install ".[worker]"

# ---------- 运行阶段 ----------
FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    CRB_DB_PATH=/data/reviews.db \
    CRB_LOG_JSON=true \
    CRB_HOST=0.0.0.0

# 上面显式把容器内监听设为 0.0.0.0（否则容器外无法访问）。
# 注意：容器内 0.0.0.0 是否等于"对外暴露"取决于端口映射与网络；
# 若把 8000 映射到公网，请务必同时设置 CRB_API_KEY，见 README「安全」一节。

# 以非 root 运行；/data 用于 SQLite 持久化
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data /app \
 && chown -R appuser:appuser /data /app

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=appuser:appuser reviewbot ./reviewbot
COPY --chown=appuser:appuser main.py pyproject.toml README.md ./

USER appuser
EXPOSE 8000

# 健康检查直接打业务健康端点，避免只看端口是否监听
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/v1/livez', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
