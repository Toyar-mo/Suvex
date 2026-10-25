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
# ## 前端产物：为什么用 rsync --delete 而不是 rm -rf + cp
#
# 2026-10-25 真实事故：手工发布前端时先 `rm -rf dist/*` 再拷新产物，
# 而 `dist` 是 **bind mount**（`/root/suvex/suvex-web/dist` 挂到
# `/opt/suvex/suvex-web/dist` 给 nginx 用）。`rm -rf` 清空了挂载点**里面**
# 的内容，却因为"设备忙"删不掉目录本身 —— 于是 dist 变成空目录，
# nginx 对所有请求回 **403**（目录存在、无 index.html、无 autoindex）。
#
# 这个失败模式的迷惑之处：`rm -rf` 报了错但没有 `set -e` 兜住（它在
# `&&` 链或 `||` 里），而**目录还在**，所以检验 `[ -d "$dist" ]` 会通过。
# 站点坏掉，而部署脚本说成功。
#
# 所以前端发布改成（四步，任一步失败都不留半套现场）：
#   · 解包到 `mktemp -d` 的临时 stage，`rsync -a` 摊平（tar 包顶层即 dist 内容）；
#   · 结构校验（index.html 存在 + assets 里有 index-<hash>.js）**在动现场之前**做，
#     不过就直接删 stage 退出，现场完好；
#   · 备份旧产物到 `$SUPEX_HOME/backup_web_<时间戳>/`，备份里没有 index.html
#     也当场停（说明现场本来就不完整，先人工看一眼）；
#   · 切换用 `rsync -a --delete "$WEB_SRC/" "$WEB_DIST/"` —— 只动**挂载点里**
#     的内容，不碰挂载点本身，天然避开"删不掉目录"的陷阱；
#   · 切换后再验三条（见下），任一不过走 `web_rollback()` 从备份还原。
#
# 备份留在 `backup_web_*` 里不自动删：回滚点用时才在，事后人工清理。
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
#   # 同时发布前端产物（tar 包内顶层是 dist/ 的内容）
#   sudo bash update.sh /tmp/deploy_v090.tgz --web /tmp/dist_v090.tgz
#
# 退出码：0 成功；2 参数错误；3 前置条件不满足；4 校验失败；5 迁移失败；
#         6 前端发布失败（已回滚）。

set -euo pipefail

# ── 参数 ────────────────────────────────────────────────────
#
# ⚠️ SUPEX_HOME **没有默认值**，必须从实际安装探测出来。
#
# 曾经写成 `SUPEX_HOME="${SUPEX_HOME:-/opt/suvex}"`，那是 `install.sh` 的默认
# 安装根 —— 但"默认安装根"和"这台机器的实际安装根"是两件事。2026-10-25
# 实测：本机（Kali）后端实际在 `/root/suvex/suvex-api`（unit 的
# `WorkingDirectory` 就这么写的），而 `/opt/suvex` 下根本没有 `suvex-api`。
# 于是脚本在任何参数下都死在第一步：
#
#     ✗ 找不到 /opt/suvex/suvex-api —— 这个脚本只做增量更新
#
# 这个错误**看起来**很正常（像是"你没装过"），所以很容易被判定为
# "环境没准备好"而不是"脚本写错了" —— 而真相是脚本假设错了。
#
# 所以改成从**运行时的事实**出发，按可信度依次取：
#
#   1. 显式传入的 SUPEX_HOME（想覆盖时用）
#   2. systemd unit 的 WorkingDirectory —— 服务实际在哪儿跑，这是最硬的证据
#   3. /etc/suvex/api.env 里的 PYTHONPATH（少数安装方式会写）
#   4. 都探测不到 → **明确失败并列出怎么覆盖**，不猜
#
# 第 4 条是关键：猜错的代价是"在错误的位置写入/备份"，比直接失败更糟。
SUPEX_HOME="${SUPEX_HOME:-}"
API_DIR=""
TARBALL=""
# WEB_TARBALL 刻意**不初始化**：下面对 `--web=` 传空值做检查时，
# 需要用 `${WEB_TARBALL+x}` 区分「没传 --web」与「传了但值为空」。
WEB_DIST=""
DRY_RUN=0
NO_RESTART=0
KEEP_BACKUPS=3
HEALTH_PATH="${HEALTH_PATH:-/api/v1/health}"
HEALTH_PORT="${HEALTH_PORT:-8000}"
# 前端由 nginx 提供，端口与后端的 uvicorn **不是同一个**。
# 本机实测：8080 是 nginx 站点（server_name suvex.asia，SPA fallback），
# 80 是 nginx 默认站（try_files → 404）。所以默认打 8080。
WEB_PORT="${WEB_PORT:-8080}"

usage() {
  sed -n '3,66p' "$0" | sed 's/^# \{0,1\}//'
  echo
  echo "用法：sudo bash update.sh <tar包> [--web <dist包>] [--dry-run] [--no-restart]"
  exit 2
}

while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run)    DRY_RUN=1 ;;
    --no-restart) NO_RESTART=1 ;;
    # `--web` 后面必须跟路径：漏了会造成"静默不发布前端"，是最难查的失败。
    # 两种漏法都要拦：
    #   ① 它是最后一个参数         → `--web` 后面什么都没有
    #   ② 后面跟的是另一个选项     → 例如 `--web --no-restart`，
    #      不拦的话会把 "--no-restart" 当成包路径，然后报"找不到文件"，
    #      而人会以为自己确实传了包（错误信息指向错误的地方）。
    --web)
      if [ $# -lt 2 ] || [ "${2#-}" != "$2" ]; then
        echo "--web 后面需要一个 dist 包路径（当前：${2:-<空>}）" >&2
        usage
      fi
      WEB_TARBALL="$2"; shift
      ;;
    --web=*)      WEB_TARBALL="${1#--web=}" ;;
    -h|--help)    usage ;;
    -*)           echo "不认识的参数：$1" >&2; usage ;;
    *)            TARBALL="$1" ;;
  esac
  shift
done

[ -n "$TARBALL" ] || usage

# `--web=` （等号形式传空）也要拦
if [ -n "${WEB_TARBALL+x}" ] && [ -z "$WEB_TARBALL" ]; then
  echo "--web 后面需要一个 dist 包路径" >&2
  usage
fi

# ── 输出 ────────────────────────────────────────────────────
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_OK=$'\033[32m'
  C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_STEP=$'\033[36m'
else
  C_RESET=""; C_DIM=""; C_OK=""; C_WARN=""; C_ERR=""; C_STEP=""
fi

STEP_NO=0
# 步骤总数从脚本自身数出来，不手写 —— 手写的那次就写错了（7 vs 实际 8），
# 打印成 `[8/7]`，加一步之后还会继续漂。
STEP_TOTAL="$(grep -c '^step "' "$0" 2>/dev/null || echo 0)"
[ "$STEP_TOTAL" -gt 0 ] || STEP_TOTAL=8

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

# ── 探测安装根（必须在 die 定义之后：探测失败要用它报错）────────
#
# 见文件头「SUPEX_HOME 没有默认值」那段：默认值 `/opt/suvex` 是 install.sh
# 的约定，不是这台机器的事实。本机实际在 /root/suvex —— 硬编码的默认值
# 会让脚本在所有参数下都死在第一步，而错误信息看起来像"你没装过"。
detect_home() {
  # 1. 显式传入优先（同时校验它真的像一个安装根）
  if [ -n "$SUPEX_HOME" ]; then
    [ -d "$SUPEX_HOME/suvex-api" ] || die 3 \
      "SUPEX_HOME=$SUPEX_HOME 下没有 suvex-api/ —— 传错目录了？"
    printf '%s' "$SUPEX_HOME"
    return 0
  fi

  # 2. systemd unit 的 WorkingDirectory —— 服务实际在哪儿跑，最硬的证据。
  #    优先用它而不是"猜"，因为更新脚本的目的就是"让线上那份代码变新"，
  #    而线上那份代码就是 unit 指向的那份。
  if command -v systemctl >/dev/null 2>&1; then
    local wd
    wd="$(systemctl show -p WorkingDirectory --value suvex-api 2>/dev/null || true)"
    if [ -n "$wd" ] && [ -d "$wd" ] && [ "${wd##*/}" = "suvex-api" ]; then
      printf '%s' "${wd%/suvex-api}"
      return 0
    fi
  fi

  # 3. /etc/suvex/api.env 的 PYTHONPATH（部分安装方式写这个）
  if [ -f /etc/suvex/api.env ]; then
    local pp
    pp="$(grep -E '^PYTHONPATH=' /etc/suvex/api.env 2>/dev/null | head -1 | cut -d= -f2- || true)"
    if [ -n "$pp" ] && [ -d "$pp" ] && [ "${pp##*/}" = "suvex-api" ]; then
      printf '%s' "${pp%/suvex-api}"
      return 0
    fi
  fi

  # 4. 探测不到就**明确失败**，不猜。
  #    猜错的代价是"在错误的位置写入并按错误的位置做备份"，比直接失败更糟。
  die 3 "探测不到安装根。请显式指定：
    sudo SUPEX_HOME=/path/to/parent bash update.sh <tar包>
  （安装根 = suvex-api/ 的**上一级**目录。本机 unit 的 WorkingDirectory 是
    /root/suvex/suvex-api，所以安装根是 /root/suvex。）"
}

SUPEX_HOME="$(detect_home)"
API_DIR="$SUPEX_HOME/suvex-api"

# 前端产物目录：install.sh 与 nginx 模板约定的位置是 $SUPEX_HOME/suvex-web/dist。
# 只在前端包存在时才用（后端更新不碰它）。
WEB_DIST="$SUPEX_HOME/suvex-web/dist"

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

# ⚠️ 解包**在 dry-run 下也真执行**。
#
# 最初写成 `run tar xzf ...`，于是 dry-run 时只打印命令、STAGE 是空的，
# 后面每一步的「有没有 app/」「有几个文件」全是基于空目录的假结论 ——
# dry-run 就失去了意义（它该告诉你"这次会改什么"，而不是"我什么都没看"）。
#
# 解到 mktemp 出来的临时目录里，不碰 $API_DIR，所以真执行是无害的。
tar xzf "$TARBALL" -C "$STAGE" || die 3 "解包失败：$TARBALL（是 tar.gz 吗）"

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

# ★ 2026-10-25 实测纠正：拷贝范围与 md5 回验范围必须来自**同一份清单**。
#
# 原实现里第 4 步硬编码 `for d in app prompts alembic`，第 5 步却
# `find "$SRC" -type f  \( -name '*.py' -o ... \)` 遍历**整包**。
# 只要包里带了 tests/、docs/、scripts/、README.md 等从不部署的东西，
# 回验就会把它们全报成「不一致 / 缺失」，然后 die 4 在迁移前中止部署。
#
# 实测：`git archive HEAD` 打全树（440 文件）跑真机部署 → 88 个文件报不一致。
# 不是文件坏了，是**校验口径与被拷贝口径脱节**。以前手工打包恰好只装那三个
# 目录，所以这个缺陷一直没暴露。
DEPLOY_DIRS="app prompts alembic"

for d in $DEPLOY_DIRS; do
  if [ -d "$SRC/$d" ]; then
    run cp -a "$SRC/$d/." "$API_DIR/$d/"
    ok "$d/ 已覆盖"
  else
    warn "$d/ 不在包里，跳过"
  fi
done

# 包里除这三个目录外的东西**不会**被部署。明确说出来，不静默：
IGNORED_DIRS=""
for e in "$SRC"/*/; do
  [ -d "$e" ] || continue
  b="$(basename "$e")"
  case " $DEPLOY_DIRS " in *" $b "*) ;; *) IGNORED_DIRS="$IGNORED_DIRS $b" ;; esac
done
IGNORED_FILES="$(find "$SRC" -maxdepth 1 -mindepth 1 -type f 2>/dev/null | wc -l)"
if [ -n "$IGNORED_DIRS" ] || [ "${IGNORED_FILES:-0}" -gt 0 ]; then
  warn "包里另有内容不参与部署（只更新 $DEPLOY_DIRS）：${IGNORED_DIRS:- （无目录）}，另有 $IGNORED_FILES 个顶层文件"
fi

# ── 5. md5 回验（本脚本的核心）──────────────────────────────
step "md5 回验"

# 逐个文件比对 tar 包与目标目录。这是"漏拷文件"唯一的可靠防线 ——
# 只 diff 目录树不够，因为源与目标都在同一台机上时容易自我印证。
# 范围就是上面实际拷贝过的 DEPLOY_DIRS，一个不多一个不少。
if [ "$DRY_RUN" -eq 0 ]; then
  MISMATCH=0
  CHECKED=0
  for d in $DEPLOY_DIRS; do
    [ -d "$SRC/$d" ] || continue
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
    done < <(find "$SRC/$d" -type f ! -path '*__pycache__*')
  done

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

# ── 7. 前端产物（可选）─────────────────────────────────────
step "前端产物"

if [ -z "${WEB_TARBALL:-}" ]; then
  warn "没传 --web，前端不做任何改动"
else
  [ -f "$WEB_TARBALL" ] || die 6 "找不到前端包：$WEB_TARBALL"
  WEB_TARBALL="$(readlink -f "$WEB_TARBALL")"

  # dist 可能是 **bind mount**（nginx 从 /opt 看，代码在 /root，两边是同一个 inode）。
  #
  # ⚠️ 2026-10-25 实测纠正：原来的判断是 `mountpoint -q "$WEB_DIST"`，而那
  # **永远返回 false** —— 因为 `WEB_DIST` 是 bind mount 的**源**，挂载点在
  # `/opt/suvex/suvex-web/dist`。真机上 `findmnt` 的输出是：
  #
  #   /opt/suvex/suvex-web/dist  /dev/sda1[/root/suvex/suvex-web/dist]  ext4
  #
  # 于是脚本一直打印「不是挂载点」，与实际拓扑相反 —— 提示错不可怕，
  # 可怕的是它让人以为「这里可以直接 rm -rf」，而那次 403 事故正是这么来的。
  #
  # 所以这里查的是「这条路径是否被当作 bind mount 的源引用过」，
  # 并把真实形态如实报出来，不再猜。
  WEB_IS_MOUNT=0
  if mountpoint -q "$WEB_DIST" 2>/dev/null; then
    WEB_IS_MOUNT=1
    ok "$WEB_DIST 是挂载点（只替换内容，保留目录）"
  elif grep -qF "[/$WEB_DIST]" /proc/self/mountinfo 2>/dev/null \
       || awk -v d="$WEB_DIST" '$0 ~ / - / { for (i=1;i<=NF;i++) if ($i == d) found=1 } END{exit !found}' /proc/self/mountinfo 2>/dev/null; then
    WEB_IS_MOUNT=1
    ok "$WEB_DIST 是 bind mount 的源（挂载点另有其处；只替换内容，保留目录）"
  else
    ok "$WEB_DIST 既不是挂载点也不是 bind mount 源（直接替换内容）"
  fi

  # 无论哪种形态，都只用 rsync 动内容、绝不删目录本身。
  # 这里显式记一笔，防止后来的人看到"不是挂载点"就写 rm -rf。
  [ "$WEB_IS_MOUNT" -eq 1 ] || true

  WEB_STAGE="$(mktemp -d /tmp/suvex_web.XXXXXX)"
  # 注意：不能把 WEB_STAGE 的清理挂在 EXIT trap 上 —— 原来的 trap 已经
  # 被 STAGE 用了（`trap 'rm -rf "$STAGE"' EXIT` 重复设置会覆盖掉前者）。
  # 这里手工清理，成功失败都清。
  tar xzf "$WEB_TARBALL" -C "$WEB_STAGE" || { rm -rf "$WEB_STAGE"; die 6 "解包失败：$WEB_TARBALL（是 tar.gz 吗）"; }

  # 兼容两种打包结构：包内直接是 index.html，或套了一层 dist/
  if [ -f "$WEB_STAGE/index.html" ]; then
    WEB_SRC="$WEB_STAGE"
  elif [ -f "$WEB_STAGE/dist/index.html" ]; then
    WEB_SRC="$WEB_STAGE/dist"
  else
    rm -rf "$WEB_STAGE"
    die 6 "前端包里找不到 index.html（既不在顶层也不在 dist/ 下）—— 检查打包命令"
  fi

  # ── ★ 结构校验必须在切换之前 ──
  #
  # 事故里最要命的一点是"空目录也算通过"：`[ -d "$dist" ]` 在
  # 内容被清空后依然为真。所以这里断言的是**内容**，不是目录存在。
  for f in index.html; do
    [ -s "$WEB_SRC/$f" ] || { rm -rf "$WEB_STAGE"; die 6 "前端产物缺 $f 或为空 —— 拒绝发布（宁可不发，不发空站）"; }
  done
  ASSET_N="$(find "$WEB_SRC/assets" -type f 2>/dev/null | wc -l || true)"
  [ "$ASSET_N" -gt 0 ] || { rm -rf "$WEB_STAGE"; die 6 "前端产物里 assets/ 一个文件都没有 —— 拒绝发布"; }

  # 带哈希的产物命名必须是 index-<hash>.js，否则说明构建走了非预期路径
  MAIN_JS="$(find "$WEB_SRC/assets" -maxdepth 1 -name 'index-*.js' -type f | head -1)"
  [ -n "$MAIN_JS" ] || { rm -rf "$WEB_STAGE"; die 6 "assets/ 里没有 index-<hash>.js —— 产物结构不对"; }

  ok "新产物：index.html $(wc -c < "$WEB_SRC/index.html") 字节，assets/ $ASSET_N 个文件"

  if [ "$DRY_RUN" -eq 1 ]; then
    run rsync -a --checksum --delete "$WEB_SRC/" "$WEB_DIST/"
    rm -rf "$WEB_STAGE"
  else
    WEB_BACKUP="$SUPEX_HOME/backup_web_$(date +%Y%m%d_%H%M%S)"
    mkdir -p "$WEB_BACKUP"
    # 备份失败就必须在**动现场之前**停下 —— 否则后面出事没有回滚点。
    if ! rsync -a --checksum "$WEB_DIST/" "$WEB_BACKUP/"; then
      rm -rf "$WEB_STAGE"
      die 6 "旧产物备份失败，未做任何改动（现场完好）"
    fi
    # 备份里必须真有 index.html，否则"回滚点"是个空壳
    if [ ! -s "$WEB_BACKUP/index.html" ]; then
      rm -rf "$WEB_STAGE"
      die 6 "旧产物备份里没有 index.html —— 现场本来就不完整，先人工看一下 $WEB_DIST"
    fi
    ok "旧产物备份到 $WEB_BACKUP（$(du -sh "$WEB_BACKUP" | cut -f1)）"

    # ── 从这里开始现场可能被改动，任何失败都必须回滚 ──
    #
    # 2026-10-25 事故的形态是「先清空、再失败」，留下空目录。
    # 光靠"用 rsync 不用 rm"不够：rsync 自己也可能中途失败（磁盘满、
    # 悬空链接、权限错误），那时 dist 里是**半套产物** —— 比空目录更难查。
    # 所以把回滚做成**唯一出口**，不在每个分支里重复写。
    #
    # ★ --checksum 不是优化，是正确性要求。
    # rsync 默认按「大小 + mtime」判等。两套产物里同名同大小的文件
    # （`index.html` 常见，回滚点与现场经常只差几十字节、同一秒生成）
    # 会被判成"没变"而**跳过不拷** —— 发布时留下旧内容，回滚时不还原。
    # 两者都是静默的：rsync 退出码 0，脚本报成功。
    # 实测复现：`echo KEEP3 > dst/index.html` 与 `echo KEEP4 > src/index.html`
    # 同秒生成，`rsync -a src/ dst/` 后 dst 仍是 KEEP3。
    web_rollback() {
      local reason="$1"
      rm -rf "$WEB_STAGE"
      if rsync -a --checksum --delete "$WEB_BACKUP/" "$WEB_DIST/"; then
        die 6 "$reason —— 已从 $WEB_BACKUP 回滚，现场恢复为旧产物"
      else
        die 6 "$reason —— 且自动回滚失败！立即人工恢复：rsync -a --checksum --delete $WEB_BACKUP/ $WEB_DIST/"
      fi
    }

    # ★ 关键：rsync 只动挂载点里的内容，不碰挂载点自身。
    #   这一条就是"rm -rf 清空挂载点"事故的根治办法。
    if ! rsync -a --checksum --delete "$WEB_SRC/" "$WEB_DIST/"; then
      web_rollback "rsync 发布失败"
    fi

    # 切换后验三条：入口文件在、数量对、主 JS 是带哈希的那一个。
    # 任何一条不过都回滚 —— 不留"看起来发布成功但站点已坏"的状态。
    if [ ! -s "$WEB_DIST/index.html" ]; then
      web_rollback "发布后 $WEB_DIST/index.html 缺失"
    fi

    AFTER_N="$(find "$WEB_DIST/assets" -type f 2>/dev/null | wc -l || true)"
    if [ "$AFTER_N" -ne "$ASSET_N" ]; then
      web_rollback "发布后 assets 数量不符（期望 $ASSET_N，实际 $AFTER_N）"
    fi

    # 主 JS 必须真的到了目标（防止"rsync 在拷 assets 时失败但退出码为 0"
    # 这种极端情形；也顺带抓 index.html 引用的是构建残留的旧哈希）
    MAIN_JS_NAME="$(basename "$MAIN_JS")"
    if [ ! -s "$WEB_DIST/assets/$MAIN_JS_NAME" ]; then
      web_rollback "发布后主 bundle $MAIN_JS_NAME 不在目标目录"
    fi

    rm -rf "$WEB_STAGE"
    ok "已发布到 $WEB_DIST（assets/ $AFTER_N 个文件，回滚点 $WEB_BACKUP）"

    # 线上取一次 index.html，确认 nginx 真的能读到新产物。
    # 这一步能抓到"dist 没错但 nginx root 指向别处"这种更难的问题。
    #
    # ★ 必须打 nginx 的端口，不是 uvicorn 的端口。
    # 这两个是不同的监听：后端 $HEALTH_PORT（默认 8000），前端由 nginx 提供
    # （本机实测 8080，server_name suvex.asia）。早先这里写的是 $HEALTH_PORT，
    # 于是每次部署都必然报「前端根路径 → 404」，纯属误报，而真正的 404 会被
    # 这条固定的假警报淹没。
    WEB_CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$WEB_PORT/" 2>/dev/null)" || WEB_CODE=000
    if [ "$WEB_CODE" = "200" ] || [ "$WEB_CODE" = "301" ] || [ "$WEB_CODE" = "302" ]; then
      ok "前端根路径（端口 $WEB_PORT）→ $WEB_CODE"
    else
      warn "前端根路径（端口 $WEB_PORT）→ $WEB_CODE（nginx root 是否指向 $WEB_DIST？或改 WEB_PORT=）"
    fi
  fi
fi

# ── 8. 重启与验证 ───────────────────────────────────────────
step "重启与验证"

if [ "$NO_RESTART" -eq 1 ]; then
  warn "--no-restart：服务未重启，改动尚未生效"
else
  run systemctl restart suvex-api
  if [ "$DRY_RUN" -eq 0 ]; then
    # ★ 不能写死 sleep 6。本机实测：restart 到 /health 真的回 200 需要 8 秒
    # （先 alembic upgrade 再 uvicorn 起来，还要跑保留期清理、启两个调度器）。
    # 写死 6 秒的结果就是每次部署都在「还没起来」时探测，报一个假的
    # `健康检查 → 000`；而真正的启动失败会被这条固定的假警报掩盖。
    #
    # 改成轮询到就绪，上限由 WAIT_READY_SEC 控制；超时才判失败。
    WAIT_READY_SEC="${WAIT_READY_SEC:-60}"
    STATE=""
    READY=0
    WAITED=0
    while [ "$WAITED" -lt "$WAIT_READY_SEC" ]; do
      STATE="$(systemctl is-active suvex-api 2>/dev/null || true)"
      if [ "$STATE" = "active" ]; then
        CODE="$(curl -s -o /dev/null -w '%{http_code}' \
          "http://127.0.0.1:$HEALTH_PORT$HEALTH_PATH" 2>/dev/null)" || CODE=000
        if [ "$CODE" = "200" ]; then
          READY=1
          break
        fi
      elif [ "$STATE" = "failed" ]; then
        # 已经注定失败，不必再等满上限
        die 5 "服务进入 failed 状态 —— journalctl -u suvex-api -n 100"
      fi
      sleep 1
      WAITED=$((WAITED + 1))
    done

    if [ "$STATE" = "active" ]; then
      ok "服务 active"
    else
      die 5 "服务未进入 active（当前：$STATE）—— journalctl -u suvex-api -n 100"
    fi

    # ★ curl 的 `-w '%{http_code}'` 在连接失败时自己就会输出 `000`，
    # 再挂 `|| echo 000` 会变成 `000000`（两次输出拼在一起）。
    # 实测就是这样拿到 "→ 000000" 的，看起来像某种神秘状态码。
    # 正确写法：只用 -w，让 curl 自己给 000。
    if [ "$READY" -eq 1 ]; then
      ok "健康检查 $HEALTH_PATH → 200（就绪耗时 ${WAITED}s）"
    else
      die 5 "健康检查 $HEALTH_PATH 在 ${WAIT_READY_SEC}s 内未返回 200（最后：$CODE）"
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
