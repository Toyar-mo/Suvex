# Suvex API 镜像。
#
# 单阶段而不是多阶段：这是一个纯 Python 服务，没有编译产物需要
# 从构建阶段挑出来。多阶段在这里只会让 Dockerfile 更长、
# 而"哪些文件进了镜像"更难读。真正要紧的是**依赖层与代码层分开** ——
# requirements.txt 先 COPY，这样改代码不会让依赖重装。
#
# ══════════════════════════════════════════════════════════════
# ⚠️ 这个镜像**跑不了扫描** —— 它是一个"控制面"镜像
# ══════════════════════════════════════════════════════════════
#
# 2026-10-10 在真机上实测（`docker compose up --build`）确认的边界：
#
#   · 镜像能构建、容器能起来、迁移能跑通、`/health` 返回 200 ✅
#   · 但容器里**既没有 `strix`，也没有 `docker` CLI** ❌
#
# 后果：`POST /scans` 能成功受理，但扫描会**立即失败**，日志里是
#
#     扫描执行后端不可用：找不到 strix 可执行文件（SUPEX_STRIX_COMMAND='strix'）
#
# 所以这个镜像适合的场景是：**只看界面、管理历史数据、跑 CTF2 的
# 平台同步**（那些不需要执行后端）。要真的跑扫描，两条路：
#
#   1. **宿主机部署**（推荐，也是当前 Kali 上在跑的方式）：
#      systemd + venv，strix 装在独立 venv 里，docker CLI 在宿主上。
#      见 `deploy/README.md`。
#   2. **给这个镜像补执行后端**：装 docker CLI（`docker.io-cli` 或
#      静态二进制）+ 把宿主 docker socket 挂进来 + 装 strix。
#      但把 docker socket 挂进容器**等于给容器 root** ——
#      那是知情后的选择，不是顺手能加的。
#
# 不在这份 Dockerfile 里默认装它们，是因为那会让镜像体积翻几倍
# （strix 带 80 多个包：litellm、openai-agents、docker、boto3…），
# 而大多数用这个镜像的场景并不需要执行后端。
# ══════════════════════════════════════════════════════════════

# ── 基础镜像钉到 digest，不用浮动 tag ────────────────────────
#
# `python:3.12-slim` 是一个**会移动**的 tag：同一个 Dockerfile 今天构建
# 与三个月后构建，拿到的是不同的基础镜像（不同 glibc、不同系统库）。
# 对企业审计要的"可复现构建"来说，那是一个真实的缺口 ——
# 而 `requirements.lock` 只锁住了 Python 包，锁不住这一层。
#
# ⚠️ digest 对应 `linux/amd64`。要在别的架构上构建（如 arm64 服务器），
#    得换成那个架构的 digest，否则 Docker 会报"找不到匹配的 manifest"。
#    取值：`docker inspect python:3.12-slim --format '{{index .RepoDigests 0}}'`
#    更新方式：定期执行上面那条命令，把新 digest 替换到这里，然后
#    **推一次 CI 确认全绿** —— 不要手工改完就以为没事。
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

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
#
# ★ 用 `requirements.lock` 而不是 `requirements.txt`（2026-10-11 补）。
#
# `requirements.txt` 是**范围约束**（`fastapi>=0.115,<1.0`），所以同一个
# commit 在不同时间构建会装到不同版本 —— 而"三个月前能跑，现在装出
# 一个新版本就炸"是真实存在的失败模式。
#
# `requirements.lock` 由 `pip-compile --generate-hashes` 在**目标平台**
# （linux/amd64 + Python 3.12，就是这个镜像）生成，含 35 个包的精确版本
# 与 sha256 哈希；`--require-hashes` 会校验每一个下载物。
#
# ⚠️ **锁文件必须在 Linux 上生成**。Windows 拿到的是 `win_amd64` 的
#    wheel，哈希与文件名都与 Linux 不同 —— 在 Windows 上生成的锁文件
#    拿到这里会装不上，而报错是"找不到满足条件的版本"，看起来像
#    依赖写错了。重新生成的命令见 requirements.txt 头部。
COPY requirements.txt requirements.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements.lock

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
