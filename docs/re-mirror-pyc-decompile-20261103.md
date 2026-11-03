# RE 镜像补 pyc 反编译（v1.61 项目2）

> 背景：用户审计「解题速度和工具使用上还有没有可优化的，工具都备齐了吧，遇到难的也要有应对的办法」。
> 项目1 = CTF 墙钟预算（见 `docs/ctf-wall-clock-20261103.md`）；本文 = 项目2。

## 病根：逆向镜像没有任何 .pyc 反编译器

Python 字节码题是逆向的常见题型（给一个 `.pyc`，还原源码找 flag）。审计两个沙箱镜像
（`suvex-sandbox` / `suvex-sandbox-re`）后确认：**没有 pyc 反编译工具**。

面对这种题，模型只有两条路：
- `strings` 硬捞明文字符串（能捞出常量，但控制流/逻辑全丢）；
- `python3 -m dis` 看字节码（能读，但要人工还原，极慢且易错）。

与当初 .NET 题「没有 mono/ILSpy」是同一类**环境缺口**：不是模型不聪明，是手里没工具。

## 为什么选 pycdc（Decompyle++），不选 decompyle3 / uncompyle6

| 方案 | 覆盖版本 | 运行依赖 | 结论 |
|---|---|---|---|
| `uncompyle6` / `decompyle3`（pip） | ≤ Python 3.8 字节码 | 需**目标版本的解释器** | ✗ 本镜像 python 是 3.14，装不上、也解析不了新 pyc |
| **pycdc**（Decompyle++，C++ 源码） | Python 1.x ~ 3.13 | **无**（独立二进制，自解析 marshal） | ✓ 覆盖真实 CTF 题（多为 3.6–3.11） |

`pycdc` 仓库同带 **`pycdas`**（字节码反汇编器）—— 反编译失败时至少能看反汇编，
这正是「遇到难的要有应对的办法」所需要的兜底。

## 改动

`suvex-api/docker/suvex-sandbox-re/Dockerfile`，在 angr pip 层之后新增：

```dockerfile
ARG PYCDC_REPO=https://github.com/zrax/pycdc.git
RUN git clone --depth 1 "$PYCDC_REPO" /tmp/pycdc \
    && cmake -S /tmp/pycdc -B /tmp/pycdc/build -DCMAKE_BUILD_TYPE=Release -G Ninja \
    && cmake --build /tmp/pycdc/build \
    && install -m 755 /tmp/pycdc/build/pycdc /usr/local/bin/pycdc \
    && install -m 755 /tmp/pycdc/build/pycdas /usr/local/bin/pycdas \
    && pycdas /tmp/pycdc/tests/compiled/binary_ops.3.11.pyc >/dev/null \
    && pycdc /tmp/pycdc/tests/compiled/binary_ops.3.11.pyc | grep -q 'print(.Addition' \
    && echo "pycdc/pycdas OK (3.11 pyc decompiled)" \
    && rm -rf /tmp/pycdc
```

构建工具（git / cmake / build-essential / ninja）base 镜像本就齐备，无需新增 apt 包。

### 验证脚本同步（`suvex-api/scripts/verify_sandbox_tools.py`）

1. `CATEGORY_REQUIREMENTS["reverse"]` 加 `pycdc`、`pycdas` —— 让「re 镜像必须能反编译 pyc」成为被守护的能力契约。
2. 该脚本原本**只认 apt / pip 装的包**，从源码构建的二进制它看不见 →
   把 pycdc 写进契约后会误报「Dockerfile 里没声明」。修的是脚本的世界观（不是契约）：
   新增 `_LOCAL_BIN_INSTALL_RE`，把 `install/cp ... /usr/local/bin/<name>` 落地的二进制也算作已声明，
   `ImagePackages` 加 `binaries` 字段，`declared()`/`commands()` 纳入、`packages()`/`owners()` 不含。
   （理由：漏报是脚本的盲区，不能为了方便脚本而否认镜像真的缺东西。）

## 构建踩坑（2 次）

1. 自检一开始写 `pycdc 2>&1 | head -1`。**pycdc 无参时打印 "No input file specified" 且 exit 1**
   （那是用法错误，不是坏掉）—— `set -o pipefail` 下整条 `&&` 链判失败。改为基于**仓库自带真 3.11 pyc**
   的反编译 + grep 自检（更硬：顺带证明真的能反编译，而不只是二进制能启动）。
2. base 镜像实际 tag 是 `suvex-sandbox:p0p1`（不是 `:latest`），rebuild 要 `--build-arg BASE_IMAGE=suvex-sandbox:p0p1`。

## 部署与验证

- 构建：`docker build --build-arg BASE_IMAGE=suvex-sandbox:p0p1 -t suvex-sandbox-re:v161-test2 .`
  → 日志 `pycdc/pycdas OK (3.11 pyc decompiled)`。
- 实测真 pyc：`pycdc binary_ops.3.11.pyc` 完整还原源码（`# Source Generated with Decompyle++` + prints/算术）、
  `pycdc f-string.3.7.pyc` 也可。
- 上线：`docker tag suvex-sandbox-re:latest suvex-sandbox-re:pre-v161-backup`（备份旧的 e839a2240ec8）→
  `docker tag v161-test2 → :latest`（新 11227db53124）。`docker run --rm suvex-sandbox-re:latest` 确认
  `/usr/local/bin/pycdc`、`/usr/local/bin/pycdas` 都在。runner 用无 tag 名 `suvex-sandbox-re` → `:latest`，
  **新求解容器自动用新镜像**（不用重启服务）。
- `verify_sandbox_tools.py --probe`（在 prod 真镜像上跑 `command -v`）：8 个分类全 ✅，reverse 12/12，
  pycdc/pycdas 实测在 PATH 里。

## 已知边界（诚实）

- **pycdc 读不了 Python 3.14 的 pyc**（它支持到 3.13）。本镜像 `python3` 恰是 3.14，
  所以「用本镜像的 python 现编译一个 pyc 再喂 pycdc」会 `Bad MAGIC!`。这不是故障 ——
  真实 CTF 题给的 pyc 绝大多数是 3.6–3.11（更老的题更常见），pycdc 都能反编译。
  真要解 3.14 的 pyc，退 `python3 -m dis` 看字节码。
- 部署只换镜像 tag，不改任何后端代码；回滚一条 `docker tag suvex-sandbox-re:pre-v161-backup suvex-sandbox-re:latest` 即可。
