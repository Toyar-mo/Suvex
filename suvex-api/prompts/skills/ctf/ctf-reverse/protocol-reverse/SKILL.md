---
name: protocol-reverse
description: 自定义二进制协议、Protobuf/gRPC/WebSocket、PCAP 流量逆向：帧布局、序列化、状态机与可复现解码。
---

<!--
  Adapted from zhaoxuya520/reverse-skill (MIT License).
  Source: https://github.com/zhaoxuya520/reverse-skill/tree/main/skills/protocol-reverse
  Changes: removed upstream routing/self-bootstrap/field-journal; kept the
  tshark/Python workflow that matches the Suvex sandbox.
-->

# 协议逆向

## 适用

- 自定义 TCP/UDP 二进制协议；
- Protobuf / gRPC / FlatBuffers / MessagePack；
- WebSocket / MQTT / 私有 RPC；
- PCAP/PCAPNG 还原字段、状态机、校验与序列号。

不适用：纯 HTTP 参数签名/JS 加密（转 `js-reverse`）；纯 TLS 证书问题
（先用浏览器/代理确认，不要把证书错误当协议问题）。

## 工作流

### 1. 采集与分诊

拿到 PCAP、代理导出、客户端日志或二进制。先标记：

```text
方向：C→S / S→C
握手 / 心跳 / 重连
固定头？魔数？长度字段？TLV？定长？
压缩（gzip/zlib/lz4）或加密（AES/ChaCha）？
```

```bash
tshark -r /work/cap.pcap -T fields -e frame.number -e ip.src -e ip.dst \
  -e tcp.srcport -e tcp.dstport -e tcp.payload | head
```

### 2. 帧布局

- 对齐同类消息，找不变字节、自增序列号、长度字段（大端/小端、含头/不含头）；
- 找 CRC/checksum/HMAC 的位置与覆盖范围；
- 画出状态机：`Connect → Auth → Ready → Request/Response → Close`；
- 需要模板时用 Python 解包（`struct`）或 Wireshark dissector 草稿。

```python
import struct
# 例：magic(4) + version(1) + length(2, big-endian) + payload
magic, ver, n = struct.unpack(">4sBH", frame[:7])
```

### 3. 序列化与加密

- Protobuf：先 `protoc --decode_raw`（镜像没有该命令时用 Python protobuf
  或手工 varint 解析；不要假装命令存在）；
- gRPC：HTTP/2 headers + protobuf body，先看 status/trailer；
- 加密：找密钥派生（客户端 so/dll/JS）→ 联合 `ghidra-reverse`、
  `js-reverse`、`apk-reverse`；
- 重放只在授权 scope 内，先无害字段再敏感操作。

### 4. 产物

MUST 产出：

- 消息类型表：name / opcode / 字段 / 长度 / 校验；
- 至少一条可复现的解码命令或脚本；
- Evidence：原始 hex 摘录 + 解码结果（凭据脱敏）。

脚本、PCAP 片段、输出写到 `/work/persist/protocol/`。

## 工具

| 工具 | 用途 | 说明 |
|---|---|---|
| `tshark` / `tcpdump` | PCAP 读取、字段提取 | 镜像已装 |
| `python3` | `struct`、hex、脚本 | 镜像已装 |
| `ghidra-decompile` | 客户端序列化函数 | 见 `ghidra-reverse` |
| `browser` | WebSocket/HTTP 观察 | 需要真实页面时 |

不要依赖 `protoc`、ImHex、010 Editor 或 IDA MCP；需要时先确认命令存在，
不存在就写 Python 解析或在报告里明确缺失。
