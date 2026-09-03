# Atlas HTTP API 镜像（serving/api.py，ADR-0012）
#
# 构建：docker compose up -d --build atlas-api（context = 仓库根，见 compose args）
#
# 运行约束（ADR-0012 / README Known Limitations #28）：
#   - WORKDIR=/app（仓库根）：SemanticModel 加载语义层 YAML、快照 meta 路径均
#     相对仓库根解析，进程 cwd 必须是 /app
#   - uvicorn workers=1：checkpointer MemorySaver 与 DataAgent._session_turns 是
#     进程内状态，多 worker = 会话分裂（重启即失，KL #28）
#   - 快照身份 ATLAS_GIT_SHA：镜像无 .git（context 排除），评测绑定键由构建
#     参数注入——GIT_SHA 默认 = 当前数据装载 commit（docker-compose.yml 注释），
#     data/snapshots/<sha>.meta.json 随 COPY 进镜像，/ask 才可绑定快照
#   - /ask 只读链不变：eval/runner.execute_sql 连 DORIS_HOST（compose 注入）
#     + Guard 预算；写面（loader/dwd）不入本容器
FROM python:3.11-slim

# uv（官方多阶段拷贝；同步走 pyproject 内 tsinghua 默认源，与本地同口径）
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

ENV PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# 先装依赖（pyproject/uv.lock 变更才失效该层），再 COPY 源码（频繁变更不重装）
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY . .

ARG GIT_SHA=7d48dcb
ENV ATLAS_GIT_SHA=${GIT_SHA}

EXPOSE 8000
# 单进程（workers=1）+ 0.0.0.0（容器网络）
CMD [".venv/bin/uvicorn", "serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
