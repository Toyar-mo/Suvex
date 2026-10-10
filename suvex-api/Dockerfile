# Suvex API 镜像。
#
# 单阶段而不是多阶段：这是一个纯 Python 服务，没有编译产物需要
# 从构建阶段挑出来。多阶段在这里只会让 Dockerfile 更长、
# 而"哪些文件进了镜像"更难读。真正要紧的是**依赖层与代码层分开** ——
# requirements.txt 先 COPY，这样改代码不会让依赖重装。

FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE：容器里不需要 .pyc，写了只是多一层可写层。
# PYTHONUNBUFFERED：不加的话日志会被缓冲，`docker compose logs` 看不到
#   启动过程 —— 而排查"起不来"时最需要的恰恰是那几行。
# PIP_NO_CACHE_DIR：pip 的下载缓存留在镜像里没有任何用处，纯占体积。
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl 只用于 HEALTHCHECK。用 python -c 也能探活，但那要每次起一个解释器
# （几百毫秒），而 healthcheck 是每 10 秒跑一次的。
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# 依赖层。改 app/ 里的代码不会让这一层失效。
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# 应用层。alembic.ini 与 alembic/ 必须进镜像 ——
# 容器的启动命令里有 `alembic upgrade head`（见 docker-compose.yml）。
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

# 不以 root 跑。这件事在容器里尤其重要：应用的默认配置允许
# 用户提供目标路径与仓库地址，而那些输入最终会交给容器内的工具去处理。
RUN useradd --create-home --uid 10001 suvex \
    && chown -R suvex:suvex /app
USER suvex

EXPOSE 8000

# 探活用 /api/v1/health（契约 §3.8）。start-period 给迁移留时间 ——
# 迁移没跑完之前 /health 会失败，而那属于正常启动过程，不是故障。
HEALTHCHECK --interval=10s --timeout=3s --start-period=25s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/api/v1/health || exit 1

# ★ 迁移在启动命令里，不在应用代码里。理由见 app/core/bootstrap.py：
# 藏在启动流程里的迁移，运维看不到它发生了，失败了日志归属也错。
# 写成 `&&` 而不是 `;` —— 迁移失败就不该起服务（半套表结构的服务
# 会在第一个业务请求上以"代码 bug"的样子炸掉）。
#
# `--host 0.0.0.0` 是容器必须的（默认 127.0.0.1 只有容器内部能访问）。
# 不加 `--proxy-headers`：本组合没有反代。若将来前面加了 nginx，
# 要同时加 `--forwarded-allow-ips=<反代地址>`，否则 X-Forwarded-For 不可信
# （而登录限流与授权留痕都依赖来源 IP）。
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
