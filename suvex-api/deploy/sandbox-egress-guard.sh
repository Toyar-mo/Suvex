#!/bin/bash
# 沙箱 → 宿主机自身服务 的出站封堵。
#
# ## 为什么需要它（实测，不是推测）
#
# 2026-10-26 在 Kali 上用生产参数起容器实测：
#
#     docker exec … curl http://192.168.44.128:8080/  →  http_code=200
#     172.19.0.1:8080 = OPEN      ← suvex-pentest 的网关（就是宿主）
#     192.168.44.128:8080 = OPEN  ← 宿主 LAN IP
#     172.17.0.1:8080 = OPEN      ← docker0
#
# 也就是说：一个被放开能力的沙箱（甚至只是它里面跑的、来自不可信目标的
# 一份文件）可以直接打开**平台自己的前端**，而那一侧就是登录后的管理界面。
#
# ## 为什么 DOCKER-USER 那条链挡不住它（关键，别搞错方向）
#
# `DOCKER-USER` 只在 FORWARD 上生效 —— 它管的是「容器 → 容器」「容器 → 外网」
# 这类**转发**流量。
#
# 而「容器 → 宿主自己的 IP」根本不是转发：包进了宿主的 INPUT 链。
# 所以 `ctf2-egress-rules.sh` 里那三条规则对 8080 这个目标**完全没有作用**，
# 这不是它的疏漏，是链的位置决定的。
#
# 正确的落点是 INPUT：按**源子网**丢弃到本机端口的包。
#
# ## 为什么按端口而不用「源子网 → 整机 DROP」
#
# 容器需要能连外网（pip/apt/git），那走 FORWARD，不受这里影响。
# 但容器也可能需要连宿主提供的**正当**服务（以后若加内网 DNS/代理，
# 那一项要单独放行）。所以这里只封「平台自身的服务端口」这一组，
# 而不是一刀切 —— 一刀切将来想加正当服务时会出现「明明加进白名单
# 却连不上」的假象，而那时的第一反应会是去查 docker 网络。
#
# 幂等：先删自己的旧规则再追加，且只动带 `suvex-sandbox-guard` 注释的规则。
set -u

# 沙箱所在的子网（实测：suvex-ctf2 = 172.18/16，suvex-pentest = 172.19/16）。
SANDBOX_SUBNETS="172.18.0.0/16 172.19.0.0/16"

# 平台自身的服务端口：
#   8080  nginx（前端，实测 0.0.0.0 监听 → 这是唯一从容器真能连上的）
#   8000  API（127.0.0.1 监听，实测容器连不上；写进来是为了将来改绑时仍然封住）
#   5432  postgres 容器端口
#   55432 宿主上 postgres 的发布端口（docker ps 实测 127.0.0.1:55432->5432）
#   22    ssh
PLATFORM_PORTS="8080 8000 5432 55432 22"

TAG="suvex-sandbox-guard"

log() { echo "[sandbox-egress-guard] $*"; }

if ! iptables -S INPUT >/dev/null 2>&1; then
  log "INPUT 链不可用，放弃"
  exit 1
fi

# ── 清理旧规则（按注释精确匹配，不碰别人的） ──
flush_mine() {
  iptables -S INPUT | grep -- "--comment $TAG" | sed 's/^-A //' | while read -r rule; do
    # 把 "-A INPUT ..." 变成删除
    iptables -D $rule 2>/dev/null || true
  done
}
flush_mine

# ── 写入 ──
count=0
for net in $SANDBOX_SUBNETS; do
  for port in $PLATFORM_PORTS; do
    # 只封 tcp：沙箱不需要用 udp 打这些端口，留着 udp 反而可能被拿去探测。
    iptables -A INPUT -s "$net" -p tcp --dport "$port" \
      -m comment --comment "$TAG" -j DROP
    count=$((count + 1))
  done
done

log "已写入 $count 条规则（$SANDBOX_SUBNETS → 端口 $PLATFORM_PORTS）"
log "当前规则："
iptables -S INPUT | grep -- "--comment $TAG" || true
