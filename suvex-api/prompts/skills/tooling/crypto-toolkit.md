---
name: crypto-toolkit
description: 密码学题的求解器与 RSA 攻击（本镜像有 z3 5.3、RsaCtfTool、gmpy2/sympy/fpylll）。遇到约束求解、RSA 弱参数、数论/格题时用；不要用它替代对协议本身的阅读，也不要在无网环境依赖 factordb。
---

# Crypto 工具（z3 / RsaCtfTool）

本镜像：`z3`（SMT 求解器 CLI）、`RsaCtfTool`、`fpylll`、`gmpy2`、`sympy`、
`pycryptodome`、`cryptography`。

## z3：先建模，再求解

命令行只吃 **SMT-LIB** 文件；Python API 更适合复杂建模。

```bash
cat > /work/q.smt2 <<'SMT'
(declare-const x (_ BitVec 32))
(assert (= (bvadd x #x00000001) #x0000002a))
(check-sat)
(get-model)
SMT
z3 /work/q.smt2 -T:30        # 注意是 -T:秒（冒号），不是 -T 30
```

```python
from z3 import *
x = BitVec('x', 32)
s = Solver(); s.add(x + 1 == 0x2a)
print(s.check(), s.model())
```

- 建模少一个约束 → `unsat`，看起来像"无解"；先用小规模/已知答案验证模型。
- 位向量与整数别混用（`BitVec` vs `Int`）；数组/字符串题先确认 z3 版本支持。
- 搜索空间大时设 `-T:` 或 `s.set(timeout=...)`，不要无限等。

## RsaCtfTool：先整理参数，再选攻击

```bash
RsaCtfTool --publickey /work/pub.pem --private
RsaCtfTool --publickey /work/pub.pem --attack wiener
RsaCtfTool -n <n> -e <e> --private          # 没有公钥文件时
```

- 它覆盖小 e、共模、Wiener、Fermat、部分泄露等；正常 2048 位密钥打不动，
  `no attack succeeded` **不代表题目无解**，可能缺参数（n/e/c）或需要换攻击。
- 部分攻击会查 factordb（联网）；解题沙箱无网时会失败 —— 先看错误是不是
  网络，不要把它读成"分解失败"。
- 输出私钥后还要自己解密：`openssl pkeyutl -decrypt` 或 Python
  `pow(c, d, n)` 转字节。

## 其它常用件

- `openssl`：看 PEM/DER、RSA 参数、证书；`openssl rsa -pubin -text -noout`。
- `python3` + `sympy`/`gmpy2`：小规模数论、连分数、模逆。
- `fpylll`：格密码（LLL/BKZ）；Coppersmith 类先确认格的构造，不要直接套模板。

## 坑

- 字节/整数转换：`int.from_bytes(..., 'big')` 与 `long_to_bytes` 的端序要一致。
- 题目给的 `c` 可能要先 base64/hex 解码；先 `file`/`xxd` 看格式。
- 不要暴力枚举密钥空间：提示词禁止消耗性爆破；用数学结构。

## 与其他工具的衔接

- 编码/古典密码：`python3` 手写 + `CyberChef` 思路；镜像有 `xxd`/`base64`。
- 逆向里的校验逻辑：先用 `ghidra-headless` / `radare2` 还原约束，再交给 z3。
