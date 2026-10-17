#!/usr/bin/env bash
# Suvex 一键部署脚本（Linux / systemd + 宿主 venv）
#
# 覆盖 deploy/README.md 的一～六节：建用户 → 放代码 → 装依赖 → 写配置
# → 建库 → 装服务 → 前端产物 → 反代。**做完跑第七节验收**（本脚本末尾会提示）。
#
# ## 为什么必须有这个脚本，而不是再写一份步骤文档
#
# 手工步骤的失败模式不是"漏写一行"，是**顺序与幂等**：重复执行会把
# `useradd` 报成失败、把 `api.env` 覆盖成空密钥、把已经建好的库再建一次。
# 脚本把每步都写成"先查后做"，第二次跑的结果与第一次一致。
#
# ## 为什么是 --dry-run 默认安全
#
# 部署脚本最贵的错误是不可回滚的那几个：动数据库、覆盖密钥、改反代。
# `--dry-run` 打印全部命令与将要写入的内容而不执行，先在目标机上跑一遍
# 看清单，再去掉这个参数。
#
# ## 用法
#
#   sudo bash install.sh --dry-run              # 先看清单
#   sudo bash install.sh                        # 真装
#   sudo bash install.sh --skip-db --skip-web   # 只更新后端
#
# 退出码：0 成功；2 参数错误；3 前置条件不满足；4 某一步执行失败。

set -euo pipefail

# ── 默认参数 ────────────────────────────────────────────────
SUPEX_USER="suvex"
SUPEX_HOME="/opt/suvex"
SUPEX_ENV_FILE="/etc/suvex/api.env"
SUPEX_DB_NAME="suvex"
SUPEX_DB_USER="suvex"
SUPEX_REPO="${SUPEX_REPO:-}"
SUPEX_DOMAIN="${SUPEX_DOMAIN:-}"
SUPEX_PYTHON="${SUPEX_PYTHON:-}"   # 留空则自动探测（见下）
PYTHON_BIN=""
DRY_RUN=0
SKIP_DB=0
SKIP_WEB=0
SKIP_SERVICE=0
FORCE_ENV=0

# ── 输出 ────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=""; C_DIM=""; C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""
fi

STEP_NO=0
STEP_TOTAL=10

step() {
  STEP_NO=$((STEP_NO + 1))
  printf '\n%s[%d/%d] %s%s\n' "$C_STEP" "$STEP_NO" "$STEP_TOTAL" "$1" "$C_RESET"
}
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$1"; }
# done_：真的执行过才报 ✓。dry-run 下一个字节都没落地，报 ✓ 会让人把
# "将要做什么"读成"已经做了什么"，而 dry-run 的全部价值就是执行前看懂清单。
done_() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s~%s %s %s(dry-run 未执行)%s\n' "$C_DIM" "$C_RESET" "$1" "$C_DIM" "$C_RESET"
  else
    printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$1"
  fi
}
skip() { printf '  %s·%s %s\n' "$C_DIM" "$C_RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_RESET" "$1"; }
die()  { printf '\n%s✗ %s%s\n' "$C_ERR" "$1" "$C_RESET" >&2; exit "${2:-4}"; }

# run：唯一的执行出口。dry-run 下只打印。
run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ %s%s\n' "$C_DIM" "$*" "$C_RESET"
  else
    "$@"
  fi
}

# runsh：需要 shell 语义（重定向 / heredoc / 管道）时用。
runsh() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ %s%s\n' "$C_DIM" "$1" "$C_RESET"
  else
    bash -c "$1"
  fi
}

usage() {
  cat <<'EOF'
Suvex 一键部署

  sudo bash install.sh [选项]

选项：
  --dry-run              只打印将要执行的命令与写入的内容，不执行
  --repo <地址>          源码仓库地址（也可用环境变量 SUPEX_REPO）
  --domain <域名>        对外域名，写进 api.env 的 CORS 与 nginx server_name
  --user <名字>          运行用户，默认 suvex
  --home <路径>          安装根目录，默认 /opt/suvex
  --python <解释器>      默认 python3.12
  --skip-db              跳过建库（库已存在时用）
  --skip-web             跳过前端构建与反代
  --skip-service         完全不动 systemd 单元（不复制、不启动）
  --env-file <路径>      api.env 路径，默认 /etc/suvex/api.env
                         （隔离演练/试装务必改掉它，见下方警告）
  --force-env            已存在 api.env 时依然重写（⚠️ 会换掉加密密钥）
  -h, --help             显示本帮助

首次部署至少要给 --repo 与 --domain。

⚠️ 在已有服务的机器上试装，请务必同时给 --home、--env-file、--skip-service，
   否则会写到生产路径：--home 换了但 --env-file 没换，就是典型的半隔离。
EOF
}

# ── 参数解析 ────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --repo)       SUPEX_REPO="${2:-}"; shift ;;
    --domain)     SUPEX_DOMAIN="${2:-}"; shift ;;
    --user)       SUPEX_USER="${2:-}"; shift ;;
    --home)       SUPEX_HOME="${2:-}"; shift ;;
    --python)     SUPEX_PYTHON="${2:-}"; shift ;;
    --skip-db)    SKIP_DB=1 ;;
    --skip-web)   SKIP_WEB=1 ;;
    --skip-service) SKIP_SERVICE=1 ;;
    --env-file)   SUPEX_ENV_FILE="${2:-}"; shift ;;
    --force-env)  FORCE_ENV=1 ;;
    -h|--help)    usage; exit 0 ;;
    *)            printf '未知参数：%s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ── 前置条件 ────────────────────────────────────────────────
step "检查前置条件"

if [ "$(id -u)" -ne 0 ]; then
  die "必须以 root 运行（sudo bash install.sh）—— 要建用户、写 /etc、装 systemd 单元" 3
fi
ok "root"

case "$(uname -s)" in
  Linux) ok "Linux ($(uname -r))" ;;
  *) die "只支持 Linux：扫描依赖 docker + systemd，Windows / macOS 上的失败会在运行期才暴露" 3 ;;
esac

if ! command -v systemctl >/dev/null 2>&1; then
  die "找不到 systemctl —— 本脚本按 systemd 部署；容器里请用 docker-compose.yml（但它跑不了扫描）" 3
fi
ok "systemd"

# docker：README 第一节就写明它是**真的依赖**，不是可选。
# 没有它服务能起来，但每次扫描都失败，且失败长成"目标不可达"的样子。
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "docker $(docker version --format '{{.Server.Version}}' 2>/dev/null || echo '(已就绪)')"
  else
    warn "docker 已安装但 daemon 不可用 —— 服务能起来，扫描会全部失败"
  fi
else
  die "找不到 docker。扫描靠它起沙箱容器；没有 docker 请先装（本脚本不代装系统包）" 3
fi

# 解释器探测。CI 与 Dockerfile 都钉 3.12，但发行版未必提供 3.12
# （Kali Rolling 只有 3.13 是常态）。策略：显式指定优先，否则按
# 3.12 → python3 的顺序找，拿到后**校验 >= 3.12**，不满足就报清楚。
PY_VER_OK='import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'
if [ -n "$SUPEX_PYTHON" ]; then
  CANDIDATES="$SUPEX_PYTHON"
else
  CANDIDATES="python3.12 python3.13 python3"
fi

PYTHON_BIN=""
for c in $CANDIDATES; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c "$PY_VER_OK" >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v "$c")"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  die "找不到 >= 3.12 的 Python（试过：$CANDIDATES）。用 --python 指定" 3
fi
ok "$("$PYTHON_BIN" -V 2>&1) → $PYTHON_BIN"
if ! "$PYTHON_BIN" -c 'import sys; sys.exit(0 if sys.version_info[:2]==(3,12) else 1)' >/dev/null 2>&1; then
  warn "CI 与 Dockerfile 钉 3.12，本机是 $("$PYTHON_BIN" -V 2>&1 | cut -d' ' -f2)。"
  warn "已实测该版本能装 requirements.lock（universal wheel），但线上口径仍以 3.12 为准。"
fi

if command -v psql >/dev/null 2>&1; then
  ok "psql"
else
  warn "找不到 psql —— 建库这一步会失败；如用远程库请加 --skip-db 并自行建库"
fi

if [ -z "$SUPEX_REPO" ] && [ ! -d "$SUPEX_HOME/src" ]; then
  die "首次部署需要 --repo <仓库地址>（或把代码预放到 $SUPEX_HOME/src）" 2
fi
if [ -z "$SUPEX_DOMAIN" ]; then
  die "需要 --domain <域名>：它决定 api.env 的 CORS 与 nginx 的 server_name" 2
fi
ok "域名 $SUPEX_DOMAIN"

# 落点自检：把将要写的三个绝对路径先亮出来。试装事故就出在
# 「--home 换成了 /tmp/x，但 env 与 unit 还是 /etc 与 /etc/systemd」——
# 目录隔离了、生产配置没隔离。这里不做拦截，只让眼睛看见。
printf '\n  落点：代码 %s  配置 %s  单元 %s\n' \
  "$SUPEX_HOME" "$SUPEX_ENV_FILE" \
  "$([ "$SKIP_SERVICE" -eq 1 ] && echo '(不改)' || echo /etc/systemd/system/suvex-api.service)"
if [ "$SUPEX_HOME" != "/opt/suvex" ] && [ "$SUPEX_ENV_FILE" = "/etc/suvex/api.env" ] && [ "$FORCE_ENV" -eq 1 ]; then
  warn "半隔离：代码装到 $SUPEX_HOME，配置却要覆盖生产的 /etc/suvex/api.env"
  warn "试装请加 --env-file $SUPEX_HOME/api.env；真要改生产配置则忽略本提示"
fi

# 磁盘：扫描产物只增不减（README 第十一节），装之前先看够不够。
AVAIL_GB=$(df -Pk "$(dirname "$SUPEX_HOME")" 2>/dev/null | awk 'NR==2 {printf "%d", $4/1024/1024}')
if [ -n "${AVAIL_GB:-}" ]; then
  if [ "$AVAIL_GB" -lt 20 ]; then
    warn "可用磁盘仅 ${AVAIL_GB} GB。扫描产物约 6 MB/次且无自动清理，建议 ≥20 GB"
  else
    ok "可用磁盘 ${AVAIL_GB} GB"
  fi
fi

# ── 1. 用户与目录 ───────────────────────────────────────────
step "创建运行用户与目录"

if id -u "$SUPEX_USER" >/dev/null 2>&1; then
  skip "用户 $SUPEX_USER 已存在"
else
  run useradd --system --create-home --shell /usr/sbin/nologin "$SUPEX_USER"
  done_ "已创建系统用户 $SUPEX_USER（非 root）"
fi

# docker 组：沙箱要通过 docker socket 起容器。这是**等价 root** 的权限，
# 脚本不替你决定，只如实说。
if getent group docker >/dev/null 2>&1; then
  if id -nG "$SUPEX_USER" 2>/dev/null | tr ' ' '\n' | grep -qx docker; then
    skip "$SUPEX_USER 已在 docker 组"
  else
    run usermod -aG docker "$SUPEX_USER"
    warn "$SUPEX_USER 已加入 docker 组 —— 该组权限等价 root，知情后再上线"
  fi
fi

run mkdir -p "$SUPEX_HOME" "$(dirname "$SUPEX_ENV_FILE")"
if id -u "$SUPEX_USER" >/dev/null 2>&1; then
  run chown "$SUPEX_USER:$SUPEX_USER" "$SUPEX_HOME"
fi
done_ "目录 $SUPEX_HOME 与 $(dirname "$SUPEX_ENV_FILE") 就绪"

# ── 2. 代码 ─────────────────────────────────────────────────
step "拉取代码"

if [ -d "$SUPEX_HOME/src/.git" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ cd %s/src && sudo -u %s git pull --ff-only%s\n' "$C_DIM" "$SUPEX_HOME" "$SUPEX_USER" "$C_RESET"
  else
    # 两步都可能挡路，都实测踩过：
    # ① 属主不对 → git 以 "dubious ownership" 硬失败；这里只对这条命令放行 safe.directory，
    #    不写全局 git 配置。
    # ② 属主若是 root（cp/scp 搬进来的代码），suvex 连 .git/FETCH_HEAD 都写不了，
    #    pull 报 "Permission denied"。运行目录最终都归 suvex，这里先统一属主。
    chown -R "$SUPEX_USER:$SUPEX_USER" "$SUPEX_HOME/src"
    if sudo -u "$SUPEX_USER" git -c safe.directory="$SUPEX_HOME/src" -C "$SUPEX_HOME/src" rev-parse --abbrev-ref '@{u}' >/dev/null 2>&1; then
      sudo -u "$SUPEX_USER" git -c safe.directory="$SUPEX_HOME/src" -C "$SUPEX_HOME/src" pull --ff-only
    else
      warn "src 是 git 仓库但没有上游分支 —— 跳过更新，直接用现有代码"
    fi
  fi
  done_ "已有代码，已更新"
elif [ -d "$SUPEX_HOME/src/suvex-api" ]; then
  skip "$SUPEX_HOME/src 已有代码但不是 git 仓库 —— 跳过拉取，直接用现有文件"
elif [ -n "$SUPEX_REPO" ]; then
  run sudo -u "$SUPEX_USER" git clone --depth 1 "$SUPEX_REPO" "$SUPEX_HOME/src"
  done_ "已克隆到 $SUPEX_HOME/src"
else
  die "$SUPEX_HOME/src 不存在且未给 --repo" 3
fi

# 源码目录 → 运行目录。用 cp -r 而不是软链：unit 的 WorkingDirectory
# 指向运行目录，且升级时只替换 app/ 不碰 .venv。
# 注意用 "$src/." 而不是 "$src"：目标目录已存在时 `cp -r src dst` 会套成
# dst/src，第二次跑就多一层 —— 这是重复部署最常见的静默错位。
for sub in suvex-api suvex-web; do
  if [ -d "$SUPEX_HOME/src/$sub" ]; then
    runsh "mkdir -p '$SUPEX_HOME/$sub' && cp -a '$SUPEX_HOME/src/$sub/.' '$SUPEX_HOME/$sub/'"
    runsh "chown -R '$SUPEX_USER:$SUPEX_USER' '$SUPEX_HOME/$sub'"
  fi
done
done_ "源码已放到 $SUPEX_HOME"

# ── 3. 后端依赖 ─────────────────────────────────────────────
step "安装后端依赖"

API_DIR="$SUPEX_HOME/suvex-api"
# dry-run 下第 3 步只打印、没真拷贝，所以目录确实不存在 —— 此时不判死。
if [ ! -d "$API_DIR" ] && [ "$DRY_RUN" -eq 0 ]; then
  die "找不到 $API_DIR —— 代码没有放对位置" 4
fi

if [ -x "$API_DIR/.venv/bin/python" ]; then
  skip "虚拟环境已存在"
else
  run sudo -u "$SUPEX_USER" "$PYTHON_BIN" -m venv "$API_DIR/.venv"
  done_ "已创建虚拟环境"
fi

# 装的是**锁文件**不是 requirements.txt：后者是范围约束，同一 commit
# 在不同时间装会得到不同版本。--require-hashes 会逐个校验 sha256。
if [ -f "$API_DIR/requirements.lock" ]; then
  run sudo -u "$SUPEX_USER" "$API_DIR/.venv/bin/pip" install --quiet --upgrade pip
  run sudo -u "$SUPEX_USER" "$API_DIR/.venv/bin/pip" install --require-hashes -r "$API_DIR/requirements.lock"
  done_ "已按 requirements.lock 安装（精确版本 + sha256 校验）"
elif [ "$DRY_RUN" -eq 0 ]; then
  die "$API_DIR/requirements.lock 不存在 —— 它是在 Linux 上生成的，别用 Windows 版覆盖" 4
else
  printf '  %s$ pip install --require-hashes -r %s/requirements.lock%s\n' "$C_DIM" "$API_DIR" "$C_RESET"
fi

# ── 4. 配置文件 ─────────────────────────────────────────────
step "写入配置 $SUPEX_ENV_FILE"

GEN_KEY=""
if [ ! -e "$SUPEX_ENV_FILE" ] || [ "$FORCE_ENV" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ %s/suvex-api/.venv/bin/python -m app.core.crypto   # 生成 Fernet 密钥%s\n' "$C_DIM" "$SUPEX_HOME" "$C_RESET"
    GEN_KEY="<运行时生成>"
  else
    # 必须在 $API_DIR 下跑：-m app.core.crypto 靠 cwd 找得到 app 包。
    # 依赖这一点的话，从任意目录调用脚本都会失败（实测踩过）。
    GEN_ERR="$(mktemp)"
    GEN_KEY="$(cd "$API_DIR" && sudo -u "$SUPEX_USER" "$API_DIR/.venv/bin/python" -m app.core.crypto 2>"$GEN_ERR" || true)"
  fi
  if [ -z "$GEN_KEY" ] && [ "$DRY_RUN" -eq 0 ]; then
    printf '%s\n' "$(cat "${GEN_ERR:-/dev/null}" 2>/dev/null | tail -5)" >&2
    die "生成加密密钥失败（cd $API_DIR && .venv/bin/python -m app.core.crypto）" 4
  fi

  DB_URL="postgresql+psycopg://$SUPEX_DB_USER:<强口令>@127.0.0.1:5432/$SUPEX_DB_NAME"

  # ⚠️ 不写引号：systemd 的 EnvironmentFile 与 shell 语法不同，
  #    KEY="value" 会把引号算进值里，布尔解析随即失败。
  ENV_BODY="# Suvex 运行时配置（由 deploy/install.sh 生成）
# ⚠️ 本文件含加密密钥：chmod 0640 + root:$SUPEX_USER；不要进版本库、不要走聊天工具传。
# ⚠️ 值一律不加引号。

SUPEX_ENV=prod
SUPEX_LOG_LEVEL=info

# ★ 数据库口令必须自己改掉下面这个占位符；脚本不生成口令，也不猜你的库口令。
SUPEX_DATABASE_URL=$DB_URL

# ★ provider 凭据的加密密钥。**必须与数据库一起备份** —— 丢了它，
#   库里所有 provider 的 API key 都解不开，只能重新申请。
SUPEX_SECRET_KEY=$GEN_KEY

# ★ 生产必须 true，否则启动自检会拒绝启动
SUPEX_COOKIE_SECURE=true

# 前端与 API 同源（nginx 一个 server 块托管两者）
SUPEX_CORS_ORIGINS=https://$SUPEX_DOMAIN

SUPEX_SCAN_AUTHORIZATION_STATEMENT=我有权对此目标进行安全测试
"

  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ cat > %s <<"EOF"%s\n' "$C_DIM" "$SUPEX_ENV_FILE" "$C_RESET"
    printf '%s\n' "$ENV_BODY" | sed 's/^/  | /'
    printf '  %s\n' "$C_DIM$C_RESET"
  else
    # 覆盖已有配置前先留一份：加密密钥换了就再也解不开旧凭据，
    # 那份备份是唯一的退路。
    if [ -e "$SUPEX_ENV_FILE" ]; then
      BAK="$SUPEX_ENV_FILE.bak.$(date +%Y%m%d%H%M%S)"
      cp -p "$SUPEX_ENV_FILE" "$BAK"
      chmod 0600 "$BAK"
      warn "原配置已备份到 $BAK（0640→0600）"
    fi
    printf '%s' "$ENV_BODY" > "$SUPEX_ENV_FILE"
    chown "root:$SUPEX_USER" "$SUPEX_ENV_FILE"
    chmod 0640 "$SUPEX_ENV_FILE"
    # 只统计有效行数、不打印内容（文件里有密钥）。
    ENV_LINES="$(grep -cvE '^[[:space:]]*(#|$)' "$SUPEX_ENV_FILE" || true)"
    ok "已生成 $SUPEX_ENV_FILE（root:$SUPEX_USER 0640，$ENV_LINES 行有效配置）"
  fi
  if [ "$FORCE_ENV" -eq 1 ]; then
    warn "--force-env：加密密钥已更换，旧 provider 凭据将无法解密"
  fi
else
  skip "$SUPEX_ENV_FILE 已存在（要重写加 --force-env，⚠️ 会换掉加密密钥）"
fi

# 占位符检查：漏改口令是最常见的"装完了但连不上库"。
if [ "$DRY_RUN" -eq 0 ] && grep -q '<强口令>' "$SUPEX_ENV_FILE" 2>/dev/null; then
  warn "api.env 里仍有 <强口令> 占位符 —— 改成真实口令后再启动服务"
fi

# ── 5. 数据库 ───────────────────────────────────────────────
step "准备 PostgreSQL 数据库"

if [ "$SKIP_DB" -eq 1 ]; then
  skip "--skip-db：跳过"
elif ! command -v psql >/dev/null 2>&1; then
  skip "无 psql，跳过（请自行建库后重启服务）"
else
  # LC_COLLATE 'C'：跨机器排序一致。跟随 locale 会让 ORDER BY 结果
  # 随 glibc 版本变化，症状是"同一份数据分页顺序不同"，极难怀疑到 locale。
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname=%s"%s\n' "$C_DIM" "'$SUPEX_DB_USER'" "$C_RESET"
    printf '  %s$ sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname=%s"%s\n' "$C_DIM" "'$SUPEX_DB_NAME'" "$C_RESET"
    printf '  %s$ CREATE USER / CREATE DATABASE ... ENCODING UTF8 LC_COLLATE C%s\n' "$C_DIM" "$C_RESET"
  else
    if [ "$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$SUPEX_DB_USER'" 2>/dev/null || true)" = "1" ]; then
      skip "数据库用户 $SUPEX_DB_USER 已存在"
    else
      runsh "sudo -u postgres psql -c \"CREATE USER $SUPEX_DB_USER WITH PASSWORD 'CHANGE_ME';\""
      warn "数据库用户 $SUPEX_DB_USER 口令为占位符 CHANGE_ME，请改掉并与 api.env 保持一致"
    fi
    if [ "$(sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$SUPEX_DB_NAME'" 2>/dev/null || true)" = "1" ]; then
      skip "数据库 $SUPEX_DB_NAME 已存在"
    else
      runsh "sudo -u postgres psql -c \"CREATE DATABASE $SUPEX_DB_NAME OWNER $SUPEX_DB_USER ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;\""
      done_ "已创建数据库 $SUPEX_DB_NAME（LC_COLLATE=C）"
    fi
  fi
fi

# 迁移不在这里跑：它在 unit 的 ExecStart 里（alembic upgrade head &&），
# 跟服务同生共死。手工跑一次反而会在"迁移成功但服务没起"时留下半套表结构。

# ── 6. systemd 服务 ─────────────────────────────────────────
step "安装 systemd 服务"

UNIT_SRC="$API_DIR/deploy/suvex-api.service"
if [ ! -f "$UNIT_SRC" ] && [ "$DRY_RUN" -eq 0 ]; then
  die "找不到 $UNIT_SRC" 4
fi
if [ "$SKIP_SERVICE" -eq 1 ]; then
  # 本分支**一个字节都不往 /etc/systemd 写**。原实现是"只装文件不启动"，
  # 但 cp 一样会覆盖线上单元 —— 试装把生产 unit 换掉的坑真踩过一次，
  # 且 systemctl 看起来毫无异常（进程还持着旧环境变量）。
  skip "--skip-service：完全不碰 systemd 单元（不复制、不 daemon-reload、不启动）"
else
  run cp "$UNIT_SRC" /etc/systemd/system/suvex-api.service
  run systemctl daemon-reload
  run systemctl enable --now suvex-api
  done_ "suvex-api 已启用并启动"

  if [ "$DRY_RUN" -eq 0 ]; then
    sleep 2
    if systemctl is-active --quiet suvex-api; then
      ok "当前状态 active"
    else
      warn "服务未进入 active —— 看 journalctl -u suvex-api -n 100"
    fi
  fi
fi

# ── 7. 前端与反代 ───────────────────────────────────────────
step "构建前端并配置反向代理"

WEB_DIR="$SUPEX_HOME/suvex-web"
if [ "$SKIP_WEB" -eq 1 ]; then
  skip "--skip-web：跳过"
elif [ ! -d "$WEB_DIR" ]; then
  skip "无 suvex-web 目录，跳过"
else
  if command -v npm >/dev/null 2>&1; then
    run sudo -u "$SUPEX_USER" bash -lc "cd '$WEB_DIR' && npm ci && npm run build"
    done_ "前端产物已生成（dist/）"
  else
    warn "找不到 npm —— 前端未构建"
  fi

  NGINX_CONF="$API_DIR/deploy/nginx.conf"
  if [ -f "$NGINX_CONF" ] && command -v nginx >/dev/null 2>&1; then
    run cp "$NGINX_CONF" /etc/nginx/conf.d/suvex.conf
    # 只替换 server_name：其它三处易错点（SSE 的 proxy_buffering off、
    # proxy_read_timeout 要大于心跳、X-Forwarded-For 信任边界）在文件头注释里，
    # 脚本不替人改 —— 改错了症状分别是"活动页空白""反复重连""IP 可伪造"。
    if [ "$DRY_RUN" -eq 0 ]; then
      sed -i "s/server_name .*;/server_name $SUPEX_DOMAIN;/" /etc/nginx/conf.d/suvex.conf
    fi
    run nginx -t
    run systemctl reload nginx
    done_ "nginx 已接管 $SUPEX_DOMAIN（证书路径仍需你确认）"
  else
    skip "无 nginx 或 nginx.conf，跳过反代"
  fi
fi

# ── 8. 健康检查 ─────────────────────────────────────────────
step "健康检查"

if [ "$SKIP_SERVICE" -eq 1 ]; then
  # --skip-service 下本脚本没起任何进程，8000 端口上若是活的，那是**别人**在跑。
  # 把它当成"本次安装成功"的证据是错的，这里只说清事实。
  skip "--skip-service：本次未启动服务，跳过（8000 端口若通，那是机器上已有的进程）"
elif [ "$DRY_RUN" -eq 1 ]; then
  printf '  %s$ curl -fsS http://127.0.0.1:8000/api/v1/health%s\n' "$C_DIM" "$C_RESET"
else
  HEALTH=""
  for _ in 1 2 3 4 5 6 7 8 9 10; do
    HEALTH="$(curl -fsS --max-time 2 http://127.0.0.1:8000/api/v1/health 2>/dev/null || true)"
    [ -n "$HEALTH" ] && break
    sleep 1
  done
  if [ -n "$HEALTH" ]; then
    ok "health: $HEALTH"
  else
    warn "健康检查未通过 —— journalctl -u suvex-api -n 100，先找 bootstrap 那几行"
  fi
fi

# ── 9. 收尾提示 ─────────────────────────────────────────────
step "完成"

cat <<EOF

  装完了，但**还有一步不能省**（deploy/README.md 第七节第 4 条）：
  浏览器登录 → 新建一次扫描 → 目标填自己的测试地址 → 看事件流。

  健康检查只能证明"进程活着、端口通"。docker 权限、沙箱网络、
  镜像拉取这三类失败在那三步里一个都不会暴露，只在真扫描时才现形。

  接下来必做：
    1. 改掉 api.env 里的数据库口令占位符，systemctl restart suvex-api
    2. 确认 nginx 证书路径（deploy/nginx.conf 头部三处易错点）
    3. 备份两样东西：数据库 + api.env（后者丢了凭据全部解不开）
    4. 挂上磁盘清理定时任务（扫描产物只增不减）

  看状态：
    systemctl status suvex-api
    journalctl -u suvex-api -n 50 --no-pager

EOF

if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s(dry-run：以上命令一条都没执行)%s\n' "$C_WARN" "$C_RESET"
fi
