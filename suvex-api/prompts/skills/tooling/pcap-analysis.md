---
name: pcap-analysis
description: 流量包分析（本镜像有 tshark / tcpdump / capinfos）。遇到 .pcap/.pcapng、需要提取 HTTP 对象、追踪 TCP 流、看 DNS/凭据时用；不要用它分析内存镜像（用 volatility3）或磁盘镜像（用 sleuthkit）。
---

# 流量包分析（tshark / tcpdump）

本镜像：TShark 4.6.9、tcpdump。

## 先看包概览，再决定拆哪一层

```bash
capinfos /work/chall.pcap            # 包数、时间范围、链路类型（如已装）
tshark -r /work/chall.pcap -q -z io,phs        # 协议分层统计
tshark -r /work/chall.pcap -Y 'http.request' -T fields -e http.host -e http.request.uri | head
```

`-Y` 是**显示过滤器**（wireshark 语法），`-f` 是抓包过滤器（BPF）——
两者不要混用：`-f` 只在抓包时生效，读文件时要用 `-Y`。

## 高频动作

```bash
# 提取 HTTP 对象（图片、下载的文件）到目录
mkdir -p /work/http && tshark -r /work/chall.pcap --export-objects http,/work/http

# 追踪一条 TCP 流（流的编号从 -T fields -e tcp.stream 拿）
tshark -r /work/chall.pcap -q -z follow,tcp,ascii,0

# 导出字段（必须同时给 -T fields）
tshark -r /work/chall.pcap -T fields -e ip.src -e ip.dst -e tcp.port -e http.host

# 找凭据 / 表单
tshark -r /work/chall.pcap -Y 'http.request.method == "POST"' -T fields -e http.file_data
tshark -r /work/chall.pcap -Y 'ftp.request.command || telnet' -T fields -e ftp.request.arg -e telnet.data
```

## tcpdump：抓包/存包，不负责细解析

```bash
tcpdump -r /work/chall.pcap -nn -c 20 'tcp port 80'
tcpdump -i eth0 -w /work/cap.pcap -c 500 'tcp port 80'
```

- 容器里能抓到哪些流量取决于网络与权限；看不到别的容器/宿主机流量是正常的。
- 抓包过滤器语法（BPF）与 `tshark -Y` 不同；写错不会报错，只会抓到 0 个包。

## 坑

- **TLS 内容是密文**：没有 keylog/私钥时，`-Y http` 看不到明文；先看
  `tls.handshake`、证书、SNI，再判断是否需要解题给的密钥。
- **重组**：分片/重传的 HTTP 用 `-Y 'http'` 可能只看到部分；用
  `follow,tcp` 或 `-o tcp.desegment_tcp_streams:TRUE`（默认已开）。
- **坏包**：题目给的 pcap 可能被截断；先 `capinfos` 看包数/文件尾，必要时
  用 `pcapfix`（镜像里已装）修。
- 大包先 `-c` 限制或 `-Y` 收窄；不要一次把几万行 `-V` 输出贴回上下文。

## 与其他工具的衔接

- 提取出的文件：`file` / `binwalk` / `exiftool` / `steghide`。
- 内存镜像：`volatility3`；磁盘镜像：`sleuthkit`（`fls` / `icat` / `mactime`）。
