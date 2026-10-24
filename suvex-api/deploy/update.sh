#!/usr/bin/env bash
# Suvex 增量更新脚本（Linux / systemd + 宿主 venv）
#
# ## 为什么需要它
#
# `install.sh` 是**首次安装**用的：建用户、写配置、建库、装服务，全部
# "先查后做"。它也能重跑，但它不解决「线上已经在跑、我只想上这次改动」
# 这个场景 —— 而手工做这件事时最容易犯的错是：
#
#   只把这次改过的几个文件 scp 上去。
#
# 这个错在 2026-10-25 真实发生过：v0.89 的改动横跨 `settings/router.py`、
# `scans/scheduler.py`、`kernel/schemas.py` 三个文件，手工部署只覆盖了
# `settings/dispatch.py`（它 v0.22 就存在，所以看起来"部署成功了"）。
# 结果线上跑的仍是 v0.22 的提示文案，`/test` 端点返回
# "真实发送尚未接入" —— 而前后端都显示正常，没有任何地方报错。
#
# 所以本脚本的第一原则是：**整包覆盖，不猜哪些文件改过**。
#
# ## 做法
#
#   1. 从调用者给的 tar 包（`git archive` 产物）解出 app/prompts/alembic
#   2. 备份现有三个目录到带时间戳的位置
#   3. 清 `__pycache__`（否则旧 .pyc 可能被优先加载）
#   4. 整目录覆盖
#   5. **逐文件 md5 回验**：与 tar 包里的清单比对，任何不一致直接失败
#   6. `alembic upgrade head`（unit 里也有，但显式跑一次能立刻看到迁移错误）
#   7. 重启服务 + 健康检查 + 近 30 秒 error 计数
#
# 任何一步失败都**不改回滚之外的东西**，并把备份路径打出来。
#
# ## 用法
#
#   # 本地
#   git archive --format=tar HEAD suvex-api/app suvex-api/prompts suvex-api/alembic \
#     | tar x -C /tmp/deploy   # 或直接打包
#
#   # 目标机
#   sudo bash update.sh /tmp/deploy_v090.tgz
#   sudo bash update.sh /tmp/deploy_v090.tgz --dry-run
#   sudo bash update.sh /tmp/deploy_v090.tgz --no-restart
#
# 退出码：0 成功；2 参数错误；3 前置条件不满足；4 校验失败；5 迁移失败。

set -euo pipefail

# ── 参数 ────────────────────────────────────────────────────
SUPEX_HOME="${SUPEX_HOME:-/opt/suvex}"
API_DIR="$SUPEX_HOME/suvex-api"
TARBALL=""
DRY_RUN=0
NO_RESTART=0
KEEP_BACKUPS=3
HEALTH_PATH="${HEALTH_PATH:-/api/v1/health}"
HEALTH_PORT="${HEALTH_PORT:-8000}"

usage() {
  sed -n '3,45p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "用法：sudo bash update.sh <tar包> [--dry-run] [--no-restart]"
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    -h|--help)    usage ;;
    -*)           echo "不认识的参数：$1" >&2; usage ;;
    *)            TARBALL="$1" ;;
  esac
  shift
done

[ -n "$TARBALL" ] || usage

# ── 输出 ────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=""; C_DIM=""; C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""
fi

STEP_NO=0
STEP_TOTAL=7

step() { STEP_NO=$((STEP_NO + 1)); printf '\n%s[%d/%d] %s%s\n' "$C_STEP" "$STEP_NO" "$STEP_TOTAL" "$1" "$C_RESET"; }
ok()   { printf '  %s✓%s %s\n' "$C_OK" "$C_RESET" "$1"; }
warn() { printf '  %s!%s %s\n' "$C_WARN" "$C_RESET" "$1"; }
die()  { printf '\n%s✗ %s%s\n' "$C_ERR" "$2" "$C_RESET" >&2; exit "$1"; }

run() {
  if [ "$DRY_RUN" -eq 1 ]; then
    printf '  %s$ %s%s\n' "$C_DIM" "$*" "$C_RESET"
  else
    "$@"
  fi
}

# ── 0. 前置检查 ─────────────────────────────────────────────
step "前置检查"

[ "$(id -u)" -eq 0 ] || die 3 "需要 root（要写 $API_DIR 与重启 systemd 服务）"
[ -f "$TARBALL" ] || die 3 "找不到 tar 包：$TARBALL"
[ -d "$API_DIR" ] || die 3 "找不到 $API_DIR —— 这个脚本只做增量更新，首次安装请用 install.sh"
[ -x "$API_DIR/.venv/bin/python" ] || die 3 "$API_DIR/.venv/bin/python 不存在或不可执行"

TARBALL="$(readlink -f "$TARBALL")"
ok "tar 包：$TARBALL（$(du -h "$TARBALL" | cut -f1)，md5 $(md5sum "$TARBALL" | cut -d' ' -f1)）"
ok "目标：$API_DIR"

if command -v systemctl >/dev/null 2>&1 && systemctl is-active --quiet suvex-api 2>/dev/null; then
  ok "服务当前 active"
else
  warn "服务当前不是 active —— 更新后会被拉起来"
fi

# ── 1. 解包到临时目录 ───────────────────────────────────────
step "解包"

STAGE="$(mktemp -d /tmp/suvex_update.XXXXXX)"
trap 'rm -rf "$STAGE"' EXIT

run tar xzf "$TARBALL" -C "$STAGE"

# 兼容两种打包结构：tar 包里直接是 app/，或者外面套了一层 suvex-api/
if [ -d "$STAGE/suvex-api/app" ]; then
  SRC="$STAGE/suvex-api"
elif [ -d "$STAGE/app" ]; then
  SRC="$STAGE"
else
  die 3 "tar 包里既没有 app/ 也没有 suvex-api/app/ —— 检查打包命令"
fi

[ -d "$SRC/prompts" ] || warn "tar 包里没有 prompts/ —— 提示词不会更新"
[ -d "$SRC/alembic" ] || warn "tar 包里没有 alembic/ —— 迁移不会更新"

FILE_COUNT="$(find "$SRC" -type f | wc -l)"
ok "解出 $FILE_COUNT 个文件到 $SRC"

if [ "$DRY_RUN" -eq 1 ]; then
  echo
  warn "--dry-run：以下步骤只打印不执行"
fi

# ── 2. 备份 ─────────────────────────────────────────────────
step "备份现有代码"

TS="$(date +%Y%m%d_%H%M%S)"
BACKUP="$SUPEX_HOME/backup_$TS"

if [ "$DRY_RUN" -eq 0 ]; then
  mkdir -p "$BACKUP"
  for d in app prompts alembic; do
    [ -d "$API_DIR/$d" ] && cp -a "$API_DIR/$d" "$BACKUP/$d"
  done
  ok "备份到 $BACKUP（$(du -sh "$BACKUP" | cut -f1)）"
else
  run mkdir -p "$BACKUP"
  run cp -a "$API_DIR/app" "$BACKUP/app"
fi

# 只留最近 N 份，别把磁盘吃满
if [ "$DRY_RUN" -eq 0 ]; then
  OLD="$(ls -1d "$SUPEX_HOME"/backup_* 2>/dev/null | sort | head -n -"$KEEP_BACKUPS" || true)"
  if [ -n "$OLD" ]; then
    echo "$OLD" | while read -r p; do rm -rf "$p"; done
    ok "清理旧备份，保留最近 $KEEP_BACKUPS 份"
  fi
fi

# ── 3. 清 __pycache__ ───────────────────────────────────────
step "清 __pycache__"

# 不清的话，覆盖后仍可能加载旧 .pyc —— Python 按 mtime+size 判断是否
# 重新编译，而 `cp -a` 保留了源文件的 mtime，恰好可能"看起来没变"。
if [ "$DRY_RUN" -eq 0 ]; then
  N_PYC="$(find "$API_DIR" -name __pycache__ -type d 2>/dev/null | wc -l)"
  find "$API_DIR" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
  ok "删除 $N_PYC 个 __pycache__ 目录"
else
  run find "$API_DIR" -name __pycache__ -type d -prune -exec rm -rf {} +
fi

# ── 4. 覆盖 ─────────────────────────────────────────────────
step "覆盖代码"

for d in app prompts alembic; do
  if [ -d "$SRC/$d" ]; then
    run cp -a "$SRC/$d/." "$API_DIR/$d/"
    ok "$d/ 已覆盖"
  else
    warn "$d/ 不在包里，跳过"
  fi
done

# ── 5. md5 回验（本脚本的核心）──────────────────────────────
step "md5 回验"

# 逐个文件比对 tar 包与目标目录。这是"漏拷文件"唯一的可靠防线 ——
# 只 diff 目录树不够，因为源与目标都在同一台机上时容易自我印证。
if [ "$DRY_RUN" -eq 0 ]; then
  MISMATCH=0
  CHECKED=0
  while IFS= read -r f; do
    rel="${f#"$SRC"/}"
    dst="$API_DIR/$rel"
    if [ ! -f "$dst" ]; then
      printf '  %s✗ 缺失：%s%s\n' "$C_ERR" "$rel" "$C_RESET"
      MISMATCH=$((MISMATCH + 1))
      continue
    fi
    a="$(md5sum "$f" | cut -d' ' -f1)"
    b="$(md5sum "$dst" | cut -d' ' -f1)"
    if [ "$a" != "$b" ]; then
      printf '  %s✗ 不一致：%s%s\n' "$C_ERR" "$rel" "$C_RESET"
      MISMATCH=$((MISMATCH + 1))
    fi
    CHECKED=$((CHECKED + 1))
  done < <(find "$SRC" -type f \( -name '*.py' -o -name '*.md' -o -name '*.ini' -o -name '*.json' -o -name '*.sql' -o -name '*.txt' \) ! -path '*__pycache__*')

  if [ "$MISMATCH" -gt 0 ]; then
    die 4 "md5 回验失败：$MISMATCH 个文件不一致（已检查 $CHECKED）。备份在 $BACKUP，回滚：cp -a $BACKUP/* $API_DIR/"
  fi
  ok "回验通过：$CHECKED 个文件逐字一致"
else
  run md5sum --check "(逐文件比对 $FILE_COUNT 个文件)"
fi

# ── 6. 迁移 ─────────────────────────────────────────────────
step "数据库迁移"

# unit 的 ExecStart 里也有 alembic upgrade head，重启时还会跑一次。
# 这里显式跑是为了**在重启前**就把迁移错误暴露出来 —— 否则失败发生在
# 服务启动时，日志混在 uvicorn 的输出里，看起来像应用起不来。
DB_URL="${SUPEX_DATABASE_URL:-}"
if [ -z "$DB_URL" ] && [ -f /etc/suvex/api.env ]; then
  DB_URL="$(grep -E '^SUPEX_DATABASE_URL=' /etc/suvex/api.env | head -1 | cut -d= -f2- || true)"
fi
if [ -z "$DB_URL" ]; then
  warn "读不到 SUPEX_DATABASE_URL —— 跳过显式迁移（重启时 unit 会跑）"
else
  if [ "$DRY_RUN" -eq 1 ]; then
    run env PYTHONPATH="$API_DIR" SUPEX_DATABASE_URL="$DB_URL" \
      "$API_DIR/.venv/bin/python" -m alembic -c "$API_DIR/alembic.ini" upgrade head
  else
    if ! ( cd "$API_DIR" && PYTHONPATH="$API_DIR" SUPEX_DATABASE_URL="$DB_URL" \
           "$API_DIR/.venv/bin/python" -m alembic -c "$API_DIR/alembic.ini" upgrade head 2>&1 | tail -20 ); then
      die 5 "迁移失败。代码已更新但表结构是旧的 —— 备份在 $BACKUP"
    fi
    ok "alembic upgrade head 完成"
  fi
fi

# ── 7. 重启与验证 ───────────────────────────────────────────
step "重启与验证"

if [ "$NO_RESTART" -eq 1 ]; then
  warn "--no-restart：服务未重启，改动尚未生效"
else
  run systemctl restart suvex-api
  if [ "$DRY_RUN" -eq 0 ]; then
    sleep 6
    STATE="$(systemctl is-active suvex-api || true)"
    if [ "$STATE" = "active" ]; then
      ok "服务 active"
    else
      die 5 "服务未进入 active（当前：$STATE）—— journalctl -u suvex-api -n 100"
    fi

    CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$HEALTH_PORT$HEALTH_PATH" || echo 000)"
    if [ "$CODE" = "200" ]; then
      ok "健康检查 $HEALTH_PATH → 200"
    else
      die 5 "健康检查 $HEALTH_PATH → $CODE"
    fi

    # 只看 error/traceback 不够：`relation ... does not exist` 这类
    # 结构性错误不带 error 字样，但每轮 tick 都刷。所以把两样都数一遍。
    ERRS="$(journalctl -u suvex-api --since '-30sec' --no-pager 2>/dev/null | grep -icE 'error|traceback|does not exist' || true)"
    if [ "$ERRS" -eq 0 ]; then
      ok "近 30 秒无 error / traceback / does not exist"
    else
      warn "近 30 秒有 $ERRS 行可疑日志 —— journalctl -u suvex-api -n 50"
    fi
  fi
fi

# ── 结尾 ────────────────────────────────────────────────────
echo
if [ "$DRY_RUN" -eq 1 ]; then
  printf '%s（--dry-run，什么都没改）%s\n' "$C_DIM" "$C_RESET"
else
  printf '%s✓ 更新完成%s\n' "$C_OK" "$C_RESET"
  printf '  备份：%s\n' "$BACKUP"
  printf '  回滚：%scp -a %s/app/. %s/app/ && systemctl restart suvex-api%s\n' \
    "$C_DIM" "$BACKUP" "$API_DIR" "$C_RESET"
fi
