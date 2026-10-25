#!/usr/bin/env bash
# 在 Kali 上验证 update.sh 的前端发布：不会把 dist 搞空（2026-10-25 事故的根治验证）。
#
# 事故机制回顾：`rm -rf dist/*` 清空了 **bind mount 挂载点里** 的内容，
# 却因"设备忙"删不掉目录 → nginx 对着空目录回 403，而脚本报成功。
# 关键点：`[ -d "$dist" ]` 在内容清空后依然为真 —— 所以校验必须是"内容"不是"目录"。
#
# 本机真实拓扑（`findmnt -T` 实测）：
#   /opt/suvex/suvex-web/dist  ← bind mount 的**目标**（nginx root）
#   /root/suvex/suvex-web/dist ← 底层目录（同一 inode，写入互相可见）
# 所以 update.sh 写 $SUPEX_HOME/suvex-web/dist 时，两种拓扑都要能工作：
#   ① 写底层目录（本机现状）  ② 写挂载点自身（SUPEX_HOME=/opt/suvex 的情形）
#
# 用法：sudo bash test_update_web.sh <update.sh 路径>
set -uo pipefail

UPDATE_SH="${1:?用法：sudo bash test_update_web.sh <update.sh 路径>}"
[ -f "$UPDATE_SH" ] || { echo "找不到 $UPDATE_SH"; exit 2; }

PASS=0; FAIL=0
pass() { PASS=$((PASS+1)); printf '  [PASS] %s\n' "$*"; }
fail() { FAIL=$((FAIL+1)); printf '  [FAIL] %s\n' "$*"; }

ROOT=/tmp/wtest
cleanup() {
  umount "$ROOT/site/suvex-web/dist" 2>/dev/null || true
  rm -rf "$ROOT"
}
cleanup

# ── 造最小安装骨架 ────────────────────────────────────────
# update.sh 会校验 .venv/bin/python 可执行，且第 6 步会跑 alembic。
# 真跑真 alembic 会连真库 —— 所以用**桩 python**：任何调用都 exit 0。
# 这不是绕过测试，而是把"迁移成功"变成受控前提，好让流程走到第 7 步（前端）。
mkdir -p "$ROOT/site/suvex-api/.venv/bin"
mkdir -p "$ROOT/underlying"      # 底层目录（bind mount 的源）
cat > "$ROOT/site/suvex-api/.venv/bin/python" <<'STUB'
#!/bin/sh
# 测试桩：模拟 `python -m alembic upgrade head` 成功
exit 0
STUB
chmod +x "$ROOT/site/suvex-api/.venv/bin/python"
: > "$ROOT/site/suvex-api/alembic.ini"

# 旧产物放进底层目录
echo 'OLD-INDEX' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'old' > "$ROOT/underlying/assets/index-OLD.js"
echo 'stale' > "$ROOT/underlying/assets/index-STALE.js"

# ★ 把 site/suvex-web/dist 做成 bind mount 的**目标**（源是 underlying）
mkdir -p "$ROOT/site/suvex-web/dist"
mount --bind "$ROOT/underlying" "$ROOT/site/suvex-web/dist" \
  || { echo "bind mount 失败（需要 root / 缺少权限）"; exit 2; }

echo "=== 现场就绪 ==="
mountpoint -q "$ROOT/site/suvex-web/dist" && echo "  dist 是 mountpoint ✓"
echo "  底层：$ROOT/underlying   挂载点：$ROOT/site/suvex-web/dist"
echo "  旧内容：$(find "$ROOT/site/suvex-web/dist" -type f | wc -l) 个文件"

# ── A. 旧做法 rm -rf 确实会清空挂载点（复现事故）────────────
echo
echo "=== A. 复现事故：rm -rf 清空挂载点内容 ==="
rm -rf "$ROOT/site/suvex-web/dist/"* 2>/dev/null || true
AFTER_RM=$(find "$ROOT/site/suvex-web/dist" -type f | wc -l)
if [ "$AFTER_RM" -eq 0 ] && mountpoint -q "$ROOT/site/suvex-web/dist"; then
  pass "rm -rf 之后：内容 0 个、目录仍在 → 这就是 403 现场"
else
  fail "复现不符合预期（剩 $AFTER_RM 个文件）"
fi
# 目录仍存在 —— 证明 `[ -d ]` 这种校验方式形同虚设
[ -d "$ROOT/site/suvex-web/dist" ] \
  && pass "注意：此时 [ -d dist ] 仍为真（所以校验必须看内容）" || fail "目录丢了"

# 恢复旧内容
cp -a "$ROOT/underlying/." "$ROOT/site/suvex-web/dist/" 2>/dev/null || true
# 上面 cp 源和目标是同一 inode，实际没变化；重新铺一遍
rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'OLD-INDEX' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'old' > "$ROOT/underlying/assets/index-OLD.js"
echo 'stale' > "$ROOT/underlying/assets/index-STALE.js"

# ── B. rsync --delete 换内容但保留挂载点 ──────────────────
echo
echo "=== B. 新做法 rsync --delete：内容换新、挂载点保留 ==="
NEWSRC="$ROOT/newsrc"
mkdir -p "$NEWSRC/assets"
echo '<html>NEW</html>' > "$NEWSRC/index.html"
echo 'new' > "$NEWSRC/assets/index-NEWHASH.js"
if rsync -a --delete "$NEWSRC/" "$ROOT/site/suvex-web/dist/"; then
  pass "rsync 成功"
else
  fail "rsync 失败"
fi
mountpoint -q "$ROOT/site/suvex-web/dist" && pass "挂载点仍在" || fail "挂载点丢了"
grep -q 'NEW' "$ROOT/site/suvex-web/dist/index.html" 2>/dev/null \
  && pass "index.html 已换新" || fail "index.html 没换"
[ -f "$ROOT/site/suvex-web/dist/assets/index-OLD.js" ] \
  && fail "旧文件还在" || pass "旧文件已清（--delete 生效）"
# 底层也要能看到（证明写入穿透到 bind mount 的源）
grep -q 'NEW' "$ROOT/underlying/index.html" 2>/dev/null \
  && pass "底层目录同步可见（bind mount 语义正确）" || fail "底层没同步"

# ── 准备包 ───────────────────────────────────────────────
DISTTGZ="$ROOT/dist_v999.tgz"
( cd "$NEWSRC" && tar czf "$DISTTGZ" . )
mkdir -p "$ROOT/fakeback/app"
echo 'x' > "$ROOT/fakeback/app/__init__.py"
( cd "$ROOT/fakeback" && tar czf "$ROOT/backend.tgz" app )

# 清掉 dist 回到"部署前"状态
rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'OLD2' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'o' > "$ROOT/underlying/assets/index-OLD2.js"

run_upd() {  # $1=logfile, 其余=rargs
  local log="$1"; shift
  SUPEX_HOME="$ROOT/site" SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
    bash "$UPDATE_SH" "$ROOT/backend.tgz" "$@" --no-restart > "$log" 2>&1
}

# ── C. 真跑（--web），验证发布成功 ────────────────────────
echo
echo "=== C. 真跑 update.sh --web ==="
run_upd "$ROOT/real.txt" --web "$DISTTGZ"
RC=$?
[ "$RC" -eq 0 ] && pass "退出码 0" || { fail "退出码 $RC"; tail -15 "$ROOT/real.txt" | sed 's/^/    /'; }

mountpoint -q "$ROOT/site/suvex-web/dist" && pass "跑完挂载点仍在" || fail "跑完挂载点丢了"
grep -q 'NEW' "$ROOT/site/suvex-web/dist/index.html" 2>/dev/null \
  && pass "dist/index.html 是新产物" || fail "dist/index.html 没换"
[ -f "$ROOT/site/suvex-web/dist/assets/index-NEWHASH.js" ] \
  && pass "新 asset 到位" || fail "新 asset 缺失"
[ -f "$ROOT/site/suvex-web/dist/assets/index-OLD2.js" ] \
  && fail "旧 asset 残留" || pass "旧 asset 已清"
# 备份目录必须真的含旧产物（否则回滚点是个空壳）
BK=$(find "$ROOT/site" -maxdepth 1 -name 'backup_web_*' -type d | head -1)
if [ -n "$BK" ] && [ -f "$BK/index.html" ]; then
  pass "回滚点含旧产物：$(basename "$BK")"
else
  fail "回滚点缺失或为空（$BK）"
fi
echo "--- 输出（前端相关）---"
grep -E '前端|产物|rsync|挂载|index\.html|assets|备份' "$ROOT/real.txt" | sed 's/^/  /' || true

# ── D. 幂等：再跑一次不应报错，内容不变 ───────────────────
echo
echo "=== D. 幂等：连跑第二次 ==="
run_upd "$ROOT/real2.txt" --web "$DISTTGZ"
RC2=$?
[ "$RC2" -eq 0 ] && pass "第二次也退出 0" || fail "第二次退出码 $RC2"
[ "$(find "$ROOT/site/suvex-web/dist/assets" -name 'index-*.js' | wc -l)" -ge 1 ] \
  && pass "第二次后产物仍在" || fail "第二次后产物丢了"

# ── E. 守卫：坏产物必须被拒，且不得破坏现场 ───────────────
echo
echo "=== E. 守卫：空 assets 必须被拒且不动现场 ==="
BADSRC="$ROOT/badsrc"; mkdir -p "$BADSRC/assets"
echo '<html>x</html>' > "$BADSRC/index.html"
( cd "$BADSRC" && tar czf "$ROOT/bad_assets.tgz" . )
BEFORE_HASH=$(md5sum "$ROOT/site/suvex-web/dist/index.html" | cut -d' ' -f1)
run_upd "$ROOT/bad1.txt" --web "$ROOT/bad_assets.tgz"
RC3=$?
[ "$RC3" -ne 0 ] && pass "空 assets 被拒（退出码 $RC3）" || fail "空 assets 竟然通过"
grep -q '一个文件都没有' "$ROOT/bad1.txt" && pass "错误信息说明了原因" || fail "错误信息不明确"
AFTER_HASH=$(md5sum "$ROOT/site/suvex-web/dist/index.html" | cut -d' ' -f1)
[ "$BEFORE_HASH" = "$AFTER_HASH" ] && pass "被拒后现场未变" || fail "被拒后现场被改了"

echo
echo "=== F. 守卫：缺 index.html 必须被拒 ==="
BADSRC2="$ROOT/badsrc2"; mkdir -p "$BADSRC2/assets"
echo 'x' > "$BADSRC2/assets/index-ABC.js"
( cd "$BADSRC2" && tar czf "$ROOT/bad_noindex.tgz" . )
run_upd "$ROOT/bad2.txt" --web "$ROOT/bad_noindex.tgz"
RC4=$?
[ "$RC4" -ne 0 ] && pass "缺 index.html 被拒（退出码 $RC4）" || fail "缺 index.html 竟然通过"
grep -qE '找不到 index\.html|缺 index\.html' "$ROOT/bad2.txt" \
  && pass "错误信息说明了原因" || fail "错误信息不明确"

echo
echo "=== G. 顶层直接是 index.html 的包也要能认（兼容打包结构） ==="
DISTTGZ2="$ROOT/dist_nested.tgz"
NEST="$ROOT/nestsrc"; mkdir -p "$NEST/dist/assets"
echo '<html>NESTED</html>' > "$NEST/dist/index.html"
echo 'n' > "$NEST/dist/assets/index-NEST1.js"
( cd "$NEST" && tar czf "$DISTTGZ2" dist )
run_upd "$ROOT/nested.txt" --web "$DISTTGZ2"
RC5=$?
[ "$RC5" -eq 0 ] && pass "嵌套 dist/ 结构被正确识别" || fail "嵌套结构失败（退出码 $RC5）"

echo
echo "=== H. 不传 --web 时前端必须一点不动 ==="
BEFORE_H=$(md5sum "$ROOT/site/suvex-web/dist/index.html" | cut -d' ' -f1)
run_upd "$ROOT/noWeb.txt"
RC6=$?
AFTER_H=$(md5sum "$ROOT/site/suvex-web/dist/index.html" | cut -d' ' -f1)
[ "$RC6" -eq 0 ] && pass "不传 --web 正常结束" || fail "不传 --web 退出 $RC6"
[ "$BEFORE_H" = "$AFTER_H" ] && pass "前端未被触碰" || fail "前端被意外改了"
grep -q '没传 --web' "$ROOT/noWeb.txt" && pass "明确提示了跳过" || fail "没提示跳过"

# ── I. ★ 发布中途失败必须回滚（事故的真实形态）─────────────
#
# 2026-10-25 那次的机制**不是** "rm -rf 本身有害" —— `rm -rf dist/*` 之后
# 只要 cp 成功，净效果其实是对的。事故是：**先清空，然后填充失败**，
# 于是留下一个空目录，而脚本的收尾检查看的是 `[ -d dist ]`（仍为真）。
#
# 悬空符号链接试过了，`rsync -a` 会原样保留链接、不报错，所以注入不了失败。
# 改用**受控故障注入**：在 PATH 前面放一个假 rsync，它在"发布"那一次
# （带 --delete 且目标是 dist）先拷一半文件、然后 exit 1 —— 精确复现
# "半套产物 + 非零退出" 这个最难查的状态，看产品会不会自己回滚。
echo
echo "=== I. 发布中途失败（拷一半就崩）→ 必须回滚成旧产物 ==="

FAKEBIN="$ROOT/fakebin"; mkdir -p "$FAKEBIN"
REAL_RSYNC="$(command -v rsync)"
# ★ 必须 export：假脚本是**另一个进程**，看不到本 shell 的变量。
# 之前漏了这行，假脚本里 `exec "$REAL_RSYNC"` 展开成 `exec "" "$@"`
# → "Permission denied" → 假脚本根本没做任何事，于是
#   ① 注入的"失败"其实是 exec 失败（不是我要测的场景）
#   ② 回滚也没真的发生（现场压根没被改过，所以看起来"回滚成功"）
#   ③ 四条断言同时假绿 —— 测试在测自己的 bug，而不是产品
export REAL_RSYNC
MARKER="$ROOT/.injected"
cat > "$FAKEBIN/rsync" <<FAKE
#!/bin/sh
# 假 rsync：**只在第一次**「发布」（--delete + 目标 dist/）时制造
# "拷一半然后失败"，之后一律转给真 rsync。
#
# 为什么必须"只一次"：回滚调用的是同一条 rsync 命令形态
# （rsync -a --delete \$BACKUP/ \$DIST/）。如果假 rsync 永远失败，
# 回滚本身也会失败，而"回滚成功"的断言会靠假脚本碰巧 copied 的文件
# 通过 —— 那是在测假脚本，不是在测产品的回滚逻辑。
LAST=""
for a in "\$@"; do LAST="\$a"; done
case " \$* " in
  *" --delete "*)
    if [ "\$LAST" = "$ROOT/site/suvex-web/dist/" ] && [ ! -f "$MARKER" ]; then
      : > "$MARKER"
      SRC=""
      for a in "\$@"; do
        case "\$a" in -*) continue ;; *) SRC="\$a"; break ;; esac
      done
      # 只拷 index.html，assets 一个都不拷 —— 半套产物
      [ -n "\$SRC" ] && [ -f "\$SRC/index.html" ] \
        && cp "\$SRC/index.html" "$ROOT/site/suvex-web/dist/index.html"
      echo "fake-rsync: 故意在拷 assets 之前失败（仅此一次）" >&2
      exit 23
    fi
    ;;
esac
exec "\$REAL_RSYNC" "\$@"
FAKE
chmod +x "$FAKEBIN/rsync"
rm -f "$MARKER"

# ★★ 正向对照（positive control）：先证明这个假脚本"能转发"。
# 没有这一步，"失败"可能只是假脚本本身坏了 —— 那测试就失去意义。
CTRL="$ROOT/ctrl"; mkdir -p "$CTRL/src" "$CTRL/dst"
echo CTRL > "$CTRL/src/f.txt"
if "$FAKEBIN/rsync" -a "$CTRL/src/" "$CTRL/dst/" 2>/dev/null && [ -f "$CTRL/dst/f.txt" ]; then
  pass "正向对照：假 rsync 能正常转发给真 rsync"
else
  fail "★ 正向对照失败 —— 假 rsync 本身就是坏的，本段所有结论无效"
fi
# 反向对照：确认注入条件真的会触发（用临时 marker 试一次）
echo X > "$CTRL/dst/index.html"
if "$FAKEBIN/rsync" -a --delete "$CTRL/src/" "$ROOT/site/suvex-web/dist/" 2>/dev/null; then
  # 若返回 0 说明注入条件没命中（LAST 参数形态不符），后续断言不可信
  fail "★ 反向对照失败 —— 注入条件没命中，后续'失败捕获'是假的"
else
  pass "反向对照：注入条件命中了（会制造失败）"
fi
rm -f "$MARKER"   # 对照用掉了 marker，清掉给正式注入用

# 造"发布前"的稳定现场
rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'KEEP-ME' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'k' > "$ROOT/underlying/assets/index-KEEP.js"
PREV_IDX=$(cat "$ROOT/site/suvex-web/dist/index.html")
PREV_N=$(find "$ROOT/site/suvex-web/dist/assets" -type f | wc -l)
pass "注入前现场：index.html='$PREV_IDX'，assets=$PREV_N"

# 新产物（内容与旧的不同，好在失败后区分是否被污染）
NEWSRC2="$ROOT/newsrc2"; mkdir -p "$NEWSRC2/assets"
echo '<html>SHOULD-NOT-LAND</html>' > "$NEWSRC2/index.html"
echo 'n2' > "$NEWSRC2/assets/index-NEW2.js"
( cd "$NEWSRC2" && tar czf "$ROOT/dist2.tgz" . )

PATH="$FAKEBIN:$PATH" SUPEX_HOME="$ROOT/site" \
  SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web "$ROOT/dist2.tgz" --no-restart \
  > "$ROOT/inject.txt" 2>&1
RC7=$?

[ "$RC7" -ne 0 ] && pass "发布失败被捕获（退出码 $RC7）" || fail "发布失败竟然返回 0"
grep -q '回滚' "$ROOT/inject.txt" && pass "输出里提到了回滚" || fail "没提回滚"
NOW_IDX=$(cat "$ROOT/site/suvex-web/dist/index.html" 2>/dev/null || echo MISSING)
NOW_N=$(find "$ROOT/site/suvex-web/dist/assets" -type f 2>/dev/null | wc -l)
[ "$NOW_IDX" = "$PREV_IDX" ] \
  && pass "回滚成功：index.html 恢复为 '$PREV_IDX'" \
  || fail "★ index.html 未回滚（期望 '$PREV_IDX'，实际 '$NOW_IDX'）"
[ "$NOW_N" -eq "$PREV_N" ] \
  && pass "回滚成功：assets 数量恢复（$PREV_N）" \
  || fail "★ assets 未回滚（期望 $PREV_N，实际 $NOW_N）"
[ -s "$ROOT/site/suvex-web/dist/index.html" ] \
  && pass "站点非空（不会出现 403）" || fail "★ 站点被清空 —— 事故重现"
# 半套产物决不能残留
grep -q 'SHOULD-NOT-LAND' "$ROOT/site/suvex-web/dist/index.html" 2>/dev/null \
  && fail "★ 新产物残留（半套状态）" || pass "新产物未残留"
mountpoint -q "$ROOT/site/suvex-web/dist" && pass "回滚后挂载点仍在" || fail "挂载点丢了"

# ── I2. 备份失败时必须在动现场之前停下 ────────────────────
echo
echo "=== I2. 备份失败 → 不得改动现场（而不是改完才发现没回滚点） ==="
FAKEBIN2="$ROOT/fakebin2"; mkdir -p "$FAKEBIN2"
# 同 I 段：必须 export，假脚本才能 exec 到真 rsync
export REAL_RSYNC
cat > "$FAKEBIN2/rsync" <<FAKE2
#!/bin/sh
# 假 rsync：凡是往 backup_web_* 备份的那次就失败，其余转发真 rsync
case " \$* " in
  *backup_web_*) echo "fake-rsync: 备份失败" >&2; exit 11 ;;
esac
exec "\$REAL_RSYNC" "\$@"
FAKE2
chmod +x "$FAKEBIN2/rsync"

# 正向对照：证明它确实能转发（否则"备份失败被捕获"可能只是脚本坏了）
CTRL2="$ROOT/ctrl2"; mkdir -p "$CTRL2/src" "$CTRL2/dst"
echo C2 > "$CTRL2/src/f.txt"
if "$FAKEBIN2/rsync" -a "$CTRL2/src/" "$CTRL2/dst/" 2>/dev/null && [ -f "$CTRL2/dst/f.txt" ]; then
  pass "正向对照（I2）：假 rsync 能正常转发"
else
  fail "★ 正向对照失败（I2）—— 假 rsync 坏了，本段结论无效"
fi

BEFORE_IDX=$(cat "$ROOT/site/suvex-web/dist/index.html")
PATH="$FAKEBIN2:$PATH" SUPEX_HOME="$ROOT/site" \
  SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web "$ROOT/dist2.tgz" --no-restart \
  > "$ROOT/inject2.txt" 2>&1
RC8=$?
[ "$RC8" -ne 0 ] && pass "备份失败被捕获（退出码 $RC8）" || fail "备份失败竟然返回 0"
AFTER_IDX=$(cat "$ROOT/site/suvex-web/dist/index.html")
[ "$AFTER_IDX" = "$BEFORE_IDX" ] \
  && pass "现场未被改动（'$BEFORE_IDX'）" || fail "★ 备份失败却改了现场"
grep -q '未做任何改动' "$ROOT/inject2.txt" && pass "明确说明未改动" || fail "没说清状态"

# ── J. CLI 参数终止性：--web 缺值必须报错退出 ─────────────
echo
echo "=== J. CLI：--web 后面不跟路径必须报错（不能静默不发布） ==="
CLI_OUT="$ROOT/cli.txt"
SUPEX_HOME="$ROOT/site" SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web --no-restart > "$CLI_OUT" 2>&1
RC8=$?
[ "$RC8" -eq 2 ] && pass "--web 缺值退出 2（参数错误）" || fail "--web 缺值退出 $RC8（期望 2）"
grep -q '需要一个 dist 包路径' "$CLI_OUT" && pass "错误信息明确" || fail "错误信息不明确"

echo
echo "=== K. CLI：--web= （等号传空）也必须报错 ==="
SUPEX_HOME="$ROOT/site" SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web= --no-restart > "$ROOT/cli2.txt" 2>&1
RC9=$?
[ "$RC9" -eq 2 ] && pass "--web= 空值退出 2" || fail "--web= 空值退出 $RC9（期望 2）"

echo
echo "=== L. CLI：--web 指向不存在的文件必须报错 ==="
SUPEX_HOME="$ROOT/site" SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web /nope/zz.tgz --no-restart > "$ROOT/cli3.txt" 2>&1
RC10=$?
[ "$RC10" -eq 6 ] && pass "不存在的包退出 6" || fail "不存在的包退出 $RC10（期望 6）"

# ── I3. ★ 静默失败：rsync 说成功但结果不对 ────────────────
#
# 「切换后再验一次」唯一能抓到的场景就是这一种：rsync 退出码 0，
# 但产物不对（磁盘满导致的截断、rsync 的 --delete 被跳过、或者
# 目标文件系统对某些文件的写入被静默丢弃）。
#
# 没有这个场景，「切换后验证」就是一段永远为真的死代码 ——
# 前面 rsync 失败时已经走回滚分支了，根本到不了这里。
echo
echo "=== I3. rsync 返回 0 但产物不全（静默失败）→ 必须靠切换后校验发现 ==="
FAKEBIN3="$ROOT/fakebin3"; mkdir -p "$FAKEBIN3"
export REAL_RSYNC
MARKER3="$ROOT/.injected3"
cat > "$FAKEBIN3/rsync" <<FAKE3
#!/bin/sh
# 假 rsync：第一次「发布」时**只拷 index.html，然后返回 0**（假成功）
LAST=""
for a in "\$@"; do LAST="\$a"; done
case " \$* " in
  *" --delete "*)
    if [ "\$LAST" = "$ROOT/site/suvex-web/dist/" ] && [ ! -f "$MARKER3" ]; then
      : > "$MARKER3"
      SRC=""
      for a in "\$@"; do
        case "\$a" in -*) continue ;; *) SRC="\$a"; break ;; esac
      done
      [ -n "\$SRC" ] && [ -f "\$SRC/index.html" ] \
        && cp "\$SRC/index.html" "$ROOT/site/suvex-web/dist/index.html"
      echo "fake-rsync: 假装成功，实际只拷了 index.html" >&2
      exit 0
    fi
    ;;
esac
exec "\$REAL_RSYNC" "\$@"
FAKE3
chmod +x "$FAKEBIN3/rsync"
rm -f "$MARKER3"

# 正向对照
CTRL3="$ROOT/ctrl3"; mkdir -p "$CTRL3/src" "$CTRL3/dst"
echo C3 > "$CTRL3/src/f.txt"
"$FAKEBIN3/rsync" -a "$CTRL3/src/" "$CTRL3/dst/" 2>/dev/null
[ -f "$CTRL3/dst/f.txt" ] && pass "正向对照（I3）：假 rsync 能转发" \
  || fail "★ 正向对照失败（I3）"

# 恢复稳定现场
rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'KEEP3' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'k' > "$ROOT/underlying/assets/index-KEEP3.js"
PREV3=$(cat "$ROOT/site/suvex-web/dist/index.html")
PREV3_N=$(find "$ROOT/site/suvex-web/dist/assets" -type f | wc -l)

NEWSRC3="$ROOT/newsrc3"; mkdir -p "$NEWSRC3/assets"
echo '<html>SILENT-FAIL</html>' > "$NEWSRC3/index.html"
echo 's' > "$NEWSRC3/assets/index-SILENT.js"
( cd "$NEWSRC3" && tar czf "$ROOT/dist3.tgz" . )

PATH="$FAKEBIN3:$PATH" SUPEX_HOME="$ROOT/site" \
  SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web "$ROOT/dist3.tgz" --no-restart \
  > "$ROOT/silent.txt" 2>&1
RC11=$?

# 关键：rsync 返回 0，脚本必须自己发现"assets 数量不对"
[ "$RC11" -ne 0 ] && pass "静默失败被发现（退出码 $RC11）" \
  || fail "★ rsync 假成功被当成真成功（退出码 0）—— 切换后校验没起作用"
grep -qE 'assets 数量不符|主 bundle' "$ROOT/silent.txt" \
  && pass "错误信息指出了产物不完整" || fail "错误信息不明确"
NOW3=$(cat "$ROOT/site/suvex-web/dist/index.html")
NOW3_N=$(find "$ROOT/site/suvex-web/dist/assets" -type f | wc -l)
[ "$NOW3" = "$PREV3" ] && pass "已回滚到旧 index.html（'$PREV3'）" \
  || fail "★ 未回滚（期望 '$PREV3'，实际 '$NOW3'）"
[ "$NOW3_N" -eq "$PREV3_N" ] && pass "已回滚到旧 assets（$PREV3_N）" \
  || fail "★ assets 未回滚（期望 $PREV3_N，实际 $NOW3_N）"

# ── I4. 静默失败：rsync 成功、index.html 在、主 bundle 在，但少了别的 asset ──
#
# 切换后三条校验各自守一种失败形态。I3 只触发「主 bundle 不在」这一条，
# 所以还必须单独造出「只差 assets 数量」和「只差 index.html」两种现场，
# 否则另外两条校验是没测过的死代码。
echo
echo "=== I4. rsync 成功但少了非主 asset → 必须靠「assets 数量」发现 ==="
FAKEBIN4="$ROOT/fakebin4"; mkdir -p "$FAKEBIN4"
MARKER4="$ROOT/.injected4"
cat > "$FAKEBIN4/rsync" <<FAKE4
#!/bin/sh
LAST=""
for a in "\$@"; do LAST="\$a"; done
case " \$* " in
  *" --delete "*)
    if [ "\$LAST" = "$ROOT/site/suvex-web/dist/" ] && [ ! -f "$MARKER4" ]; then
      : > "$MARKER4"
      "\$REAL_RSYNC" "\$@" || exit \$?
      # 真拷完之后，悄悄删掉一个非主 asset
      rm -f "$ROOT/site/suvex-web/dist/assets/extra.css"
      echo "fake-rsync: 成功，但少了一个 asset" >&2
      exit 0
    fi
    ;;
esac
exec "\$REAL_RSYNC" "\$@"
FAKE4
chmod +x "$FAKEBIN4/rsync"
rm -f "$MARKER4"

CTRL4="$ROOT/ctrl4"; mkdir -p "$CTRL4/src" "$CTRL4/dst"
echo C4 > "$CTRL4/src/f.txt"
"$FAKEBIN4/rsync" -a "$CTRL4/src/" "$CTRL4/dst/" 2>/dev/null
[ -f "$CTRL4/dst/f.txt" ] && pass "正向对照（I4）：假 rsync 能转发" \
  || fail "★ 正向对照失败（I4）"

rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'KEEP4' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'k' > "$ROOT/underlying/assets/index-KEEP4.js"
PREV4=$(cat "$ROOT/site/suvex-web/dist/index.html")

NEWSRC4="$ROOT/newsrc4"; mkdir -p "$NEWSRC4/assets"
echo '<html>FOUR</html>' > "$NEWSRC4/index.html"
echo 'm' > "$NEWSRC4/assets/index-AAA.js"
echo 'c' > "$NEWSRC4/assets/extra.css"
( cd "$NEWSRC4" && tar czf "$ROOT/dist4.tgz" . )

PATH="$FAKEBIN4:$PATH" REAL_RSYNC="$REAL_RSYNC" SUPEX_HOME="$ROOT/site" \
  SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web "$ROOT/dist4.tgz" --no-restart \
  > "$ROOT/four.txt" 2>&1
RC12=$?

[ "$RC12" -ne 0 ] && pass "assets 缺失被发现（退出码 $RC12）" \
  || fail "★ assets 数量不符却被放行（退出码 0）—— 数量校验没起作用"
grep -qE 'assets 数量不符' "$ROOT/four.txt" \
  && pass "错误信息指向 assets 数量" || fail "错误信息不明确"
NOW4=$(cat "$ROOT/site/suvex-web/dist/index.html")
[ "$NOW4" = "$PREV4" ] && pass "已回滚到旧 index.html（'$PREV4'）" \
  || fail "★ 未回滚（期望 '$PREV4'，实际 '$NOW4'）"
[ -f "$ROOT/site/suvex-web/dist/assets/index-KEEP4.js" ] \
  && pass "旧 asset 已恢复" || fail "★ 旧 asset 未恢复"

# ── I5. 静默失败：rsync 成功、assets 齐、但 index.html 不见了 ──
echo
echo "=== I5. rsync 成功但 index.html 丢失 → 必须靠「入口文件在」发现 ==="
FAKEBIN5="$ROOT/fakebin5"; mkdir -p "$FAKEBIN5"
MARKER5="$ROOT/.injected5"
cat > "$FAKEBIN5/rsync" <<FAKE5
#!/bin/sh
LAST=""
for a in "\$@"; do LAST="\$a"; done
case " \$* " in
  *" --delete "*)
    if [ "\$LAST" = "$ROOT/site/suvex-web/dist/" ] && [ ! -f "$MARKER5" ]; then
      : > "$MARKER5"
      "\$REAL_RSYNC" "\$@" || exit \$?
      rm -f "$ROOT/site/suvex-web/dist/index.html"
      echo "fake-rsync: 成功，但 index.html 不见了" >&2
      exit 0
    fi
    ;;
esac
exec "\$REAL_RSYNC" "\$@"
FAKE5
chmod +x "$FAKEBIN5/rsync"
rm -f "$MARKER5"

CTRL5="$ROOT/ctrl5"; mkdir -p "$CTRL5/src" "$CTRL5/dst"
echo C5 > "$CTRL5/src/f.txt"
"$FAKEBIN5/rsync" -a "$CTRL5/src/" "$CTRL5/dst/" 2>/dev/null
[ -f "$CTRL5/dst/f.txt" ] && pass "正向对照（I5）：假 rsync 能转发" \
  || fail "★ 正向对照失败（I5）"

rm -rf "$ROOT/underlying/"* 2>/dev/null || true
echo 'KEEP5' > "$ROOT/underlying/index.html"
mkdir -p "$ROOT/underlying/assets"
echo 'k' > "$ROOT/underlying/assets/index-KEEP5.js"
PREV5=$(cat "$ROOT/site/suvex-web/dist/index.html")

NEWSRC5="$ROOT/newsrc5"; mkdir -p "$NEWSRC5/assets"
echo '<html>FIVE</html>' > "$NEWSRC5/index.html"
echo 'm' > "$NEWSRC5/assets/index-BBB.js"
( cd "$NEWSRC5" && tar czf "$ROOT/dist5.tgz" . )

PATH="$FAKEBIN5:$PATH" REAL_RSYNC="$REAL_RSYNC" SUPEX_HOME="$ROOT/site" \
  SUPEX_DATABASE_URL="postgresql+psycopg://stub:stub@127.0.0.1:5432/stub" \
  bash "$UPDATE_SH" "$ROOT/backend.tgz" --web "$ROOT/dist5.tgz" --no-restart \
  > "$ROOT/five.txt" 2>&1
RC13=$?

[ "$RC13" -ne 0 ] && pass "index.html 丢失被发现（退出码 $RC13）" \
  || fail "★ index.html 缺失却被放行（退出码 0）—— 入口文件校验没起作用"
grep -qE 'index\.html 缺失' "$ROOT/five.txt" \
  && pass "错误信息指向 index.html 缺失" || fail "错误信息不明确"
NOW5=$(cat "$ROOT/site/suvex-web/dist/index.html")
[ "$NOW5" = "$PREV5" ] && pass "已回滚到旧 index.html（'$PREV5'）" \
  || fail "★ 未回滚（期望 '$PREV5'，实际 '$NOW5'）"

echo
echo "=============================================="
echo "  PASS: $PASS   FAIL: $FAIL"
if [ "$FAIL" -eq 0 ]; then
  echo "  VERDICT: PASS"; RC=0
else
  echo "  VERDICT: FAIL"; RC=1
fi
echo "=============================================="

cleanup
exit $RC
