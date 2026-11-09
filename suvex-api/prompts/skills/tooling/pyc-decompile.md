---
name: pyc-decompile
description: Python 字节码（.pyc）反编译 —— 本镜像有 pycdc / pycdas（支持到 3.13）与 uncompyle6（支持到约 3.8）。遇到 .pyc 题时先按版本选工具；不要把它当成能反编译任意版本。
---

# .pyc 反编译（pycdc / pycdas / uncompyle6）

本镜像三个入口：

| 工具 | 覆盖版本 | 用途 |
| --- | --- | --- |
| `pycdc` | 1.x – **3.13** | 反编译成 Python 源码 |
| `pycdas` | 1.x – 3.13 | 反汇编字节码（反编译失败时的兜底） |
| `uncompyle6` | 约到 **3.8** | 老题的源码级还原（比 pycdc 更贴近原写法） |

**先读 magic 判断版本**，再选工具：

```bash
python3 - <<'PY'
import struct
data = open('/work/chall.pyc','rb').read(4)
print('magic', data.hex())
PY
# 或用 file /work/chall.pyc（有时能直接报 Python 版本）
```

## pycdc（首选，新版也能打）

```bash
pycdc /work/chall.pyc > /work/chall.py
pycdc -o /work/chall.py /work/chall.pyc     # 等价
pycdas /work/chall.pyc > /work/chall.dis     # 反汇编兜底
```

- 3.14 的 pyc 会报 `Bad MAGIC!`：本镜像的 `python3` 是 3.14，**用它现编译
  的 pyc 喂给 pycdc 会失败**，但题目给的 3.6–3.11 字节码通常能还原。
- 反编译结果是近似源码：控制流、变量名、`try/except` 结构可能与原文件不同。
  关键常量（flag 拼接、密钥、比较串）通常能原样看到。

## uncompyle6（老版本更准）

```bash
uncompyle6 /work/chall.pyc > /work/chall.py
uncompyle6 -o /work/out /work/chall.pyc
```

- 3.9+ 会报 `Unsupported Python version` —— 此时换 `pycdc`，不要反复试。
- 它依赖 `xdis`；解析失败时报的是 xdis 的版本错误，不是"文件损坏"。

## 坑

- **不要**用本镜像的 `python3 -m dis` 去反编译题目 pyc：3.14 的 dis 读不了
  老版本 magic。可以用 `python3 -m dis` 只分析自己现编译的 3.14 字节码。
- 只拿到 `.py` 的编译产物时，先确认不是 **PyInstaller / Nuitka** 打包：
  `strings | grep -i pyinstaller`；那是另一个流程（先解包再找 pyc）。
- 反编译出的代码里有 `exec` / `marshal.loads` 时，跟着常量走，不要执行
  未知字节码 —— 直接在沙箱里跑题目给的代码前先看一眼它做什么。

## 与其他工具的衔接

- Ghidra 不负责 pyc；`ctf-reverse` 的通用流程可参考，但工具选择以本页为准。
- 打包样本：`binwalk` / `pyinstxtractor`（如镜像内有）/ `uncompyle6`。
