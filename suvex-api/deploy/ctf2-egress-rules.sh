#!/bin/bash
# CTF2 解题沙箱出站白名单。
#
# 目的：让 web / pwn 题的沙箱容器能访问**本题靶站**，同时挡住内网与宿主。
# 没有这个，web 题的容器只能 --network none，容器里的 gobuster / ffuf
# 一次都用不上（实测代价：一道目录探测题 76 轮、807 次工具调用仍未解出）。
#
# 幂等：先删本脚本自己的旧规则，再重新追加。只匹配精确的源/目的组合，
# 不会动 DOCKER-USER 链上别人的规则。
set -u

NET_SUBNET="172.18.0.0/16"      # docker network suvex-ctf2
TARGET_REAL="117.21.200.0/24"   # buuoj 靶机真实 IP（容器里 /etc/hosts 解析结果）
TARGET_PROXY="198.18.0.0/15"    # dasctf 代理网段（宿主 DNS 把公网域名都解析到这里）

log() { echo "[ctf2-egress] $*"; }

# DOCKER-USER 链由 docker.service 创建，本 unit 排在它之后，
# 但容器运行时也可能重建链 —— 等一下更稳。
for _ in $(seq 1 30); do
    if iptables -S DOCKER-USER >/dev/null 2>&1; then
        break
    fi
    sleep 2
done
if ! iptables -S DOCKER-USER >/dev/null 2>&1; then
    log "DOCKER-USER 链不存在，放弃（docker 没起来？）"
    exit 1
fi

drop_rule() {
    while iptables -C DOCKER-USER "$@" 2>/dev/null; do
        iptables -D DOCKER-USER "$@" || break
    done
}

log "清理旧规则"
drop_rule -s "$NET_SUBNET" -d "$TARGET_REAL" -j ACCEPT
drop_rule -s "$NET_SUBNET" -d "$TARGET_PROXY" -j ACCEPT
drop_rule -s "$NET_SUBNET" -j DROP

log "写入白名单"
iptables -A DOCKER-USER -s "$NET_SUBNET" -d "$TARGET_REAL" -j ACCEPT
iptables -A DOCKER-USER -s "$NET_SUBNET" -d "$TARGET_PROXY" -j ACCEPT
iptables -A DOCKER-USER -s "$NET_SUBNET" -j DROP

log "当前规则："
iptables -L DOCKER-USER -n --line-numbers
