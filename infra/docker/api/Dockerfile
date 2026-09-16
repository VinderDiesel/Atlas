# Atlas HTTP API 镜像（serving/api.py，ADR-0012）
#
# 构建：docker compose up -d --build atlas-api（context = 仓库根，见 compose args）
#
# 运行约束（ADR-0012 / README Known Limitations #28）：
#   - WORKDIR=/app（仓库根）：SemanticModel 加载语义层 YAML、快照 meta 路径均
#     相对仓库根解析，进程 cwd 必须是 /app
#   - uvicorn workers=1：会话/轮数/身份指纹已入 SQLite checkpoint（ADR-0020，设
#     ATLAS_CHECKPOINT_DB 时落盘），不再分裂；仍为进程内态的是限流桶、审计写与
#     SQLite 单写者（决策 ⑧，KL #28）
#   - 快照身份 ATLAS_GIT_SHA：镜像无 .git（context 排除），由构建参数 GIT_SHA
#     注入。**无默认值**（ADR-0019 决策 ④）——任何硬编码 sha 都会随 HEAD 前进变成
#     谎言，实测曾因 Dockerfile 与 compose 两处默认值不同而静默绑到无零售表的快照。
#     注入点在 Makefile（`make up` 求值并 export）；直接敲 compose 命令时需
#     `--build-arg GIT_SHA=$(git rev-parse --short HEAD)`，否则本文件下方的
#     构建守卫直接失败（响亮失败优于静默错绑）。
#     data/snapshots/<sha>.meta.json 随 COPY 进镜像，运行时按
#     ATLAS_SNAPSHOT_SHA > HEAD > 最新已锁 三级解析绑定（决策 ①）
#   - /ask 只读链不变：eval/runner.execute_sql 连 DORIS_HOST（compose 注入）
#     + Guard 预算；写面（loader/dwd）不入本容器
#   - 前端静态面（ADR-0018 决策 ②⑤）：frontend/dist 由本文件 node 阶段构建后
#     COPY 进最终层；dist 缺失时 SPA 条件挂载跳过并 warning，API 照常起
#
# ---- 构建阶段 1/2：前端产物（ADR-0018 决策 ⑤）----
# 本 stage 必须在最终 stage 之前定义：docker 默认构建「最后一个 stage」，故
# 「最终镜像」= 文件尾的 python 层；node/npm 只存在于本 stage，不进最终镜像。
# npm ci 用 lockfile 精确重建（与本地 make ui-check / .github/workflows/ui.yml 同口径）。
FROM node:20-alpine AS frontend
WORKDIR /app/frontend
# 依赖层与源码分层：package*.json 未变则 npm ci 走缓存
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

# ---- 构建阶段 2/2：API 运行时（最终镜像）----
FROM python:3.11-slim

# 身份守卫（ADR-0019 决策 ④）：放在依赖层之前，误构建立刻失败，不先花几分钟装包。
# （多阶段后 frontend stage 先于本 stage 执行——冷构建失败会多等其完成，缓存命中
# 时近零；顺序取舍登记于 ADR-0018 P0b 落地注记。）
# ARG 在整个 stage 内有效，故末尾的 ENV 直接引用，无需重复声明。
ARG GIT_SHA
RUN test -n "${GIT_SHA}" || { echo "ERROR: build arg GIT_SHA is required（镜像无 .git，身份必须显式注入）——用 make up，或 docker compose build --build-arg GIT_SHA=<git rev-parse --short HEAD 的输出> atlas-api" >&2; exit 1; }

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

# 前端产物（ADR-0018 决策 ⑤）：宿主 frontend/dist 已被 .dockerignore 排除出构建
# 上下文，此处的 dist 来自 frontend stage 的镜像内构建（宿主产物与镜像产物不混用）。
COPY --from=frontend /app/frontend/dist ./frontend/dist

# 身份落地在此而非文件头：ENV 本身是层，放在 `uv sync` 之前会让每次换身份都重装
# 依赖；ARG 已在文件头声明并被守卫校验过非空，这里只取值（决策 ④）。
ENV ATLAS_GIT_SHA=${GIT_SHA}

EXPOSE 8000
# 单进程（workers=1）+ 0.0.0.0（容器网络）
CMD [".venv/bin/uvicorn", "serving.api:app", "--host", "0.0.0.0", "--port", "8000"]
