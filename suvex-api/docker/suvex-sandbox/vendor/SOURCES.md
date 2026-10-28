# vendor/：构建期随上下文带进来的第三方资产

为什么存在这个目录：Kali 构建期只连得上国内镜像（tuna 的 apt / pip 通），
`github.com`、`rubygems.org`、`nodejs.org` 直连都报
`SSL: unexpected eof while reading`（2026-10-27 实测）。
所以凡是必须从 GitHub 取的资产，都在开发机预下载、校验后放在这里，
由 Dockerfile 的 `COPY` 带进镜像 —— 构建期零外网依赖、且版本可复现。

| 目录 | 来源 | 版本 | 许可 | 放进来的是什么 |
|---|---|---|---|---|
| `repak/repak` | https://github.com/trumank/repak | v0.2.3（tag `v0.2.3`） | MIT OR Apache-2.0 | `repak_cli-x86_64-unknown-linux-gnu.tar.xz` 解出的预编译二进制；sha256 = `933bdb8e26f34e8fd70ea50201efca39df041de58aa83b1cd6eb83da124a2046`（与官方 `.sha256` 一致，已验）。同目录保留 LICENSE-MIT / LICENSE-APACHE。 |
| `bkcrack/` | https://github.com/kimci86/bkcrack | 2026-10-27 克隆的 main | zlib/libpng | 完整源码，构建期 `cmake` 编译。 |
| `pcapfix/` | https://github.com/Rup0rt/pcapfix | 2026-10-27 克隆的 main | GPL-2.0 | 完整源码，构建期 `make` 编译。 |
| `jwt_tool/` | https://github.com/ticarpi/jwt_tool | 2026-10-27 克隆的 main | GPL-3.0 | 完整源码（脚本 + requirements）。 |
| `sstimap/` | https://github.com/vladko312/SSTImap | 2026-10-27 克隆的 main | GPL-3.0 | 完整源码（脚本 + requirements），装进独立 venv。 |
| `nuclei-templates/` | https://github.com/projectdiscovery/nuclei-templates | 2026-10-27 克隆的 main | MIT | **精简子集**：http / dns / network / ssl / headless / dast / javascript / workflows / file / code。砍掉 cloud、tencent、helpers 等对 CTF 无用且占体积的部分。 |
| `gef/gef.py` | https://github.com/hugsy/gef | 2026-10-27 克隆的 main（单文件 451,120 字节） | MIT | GDB 增强（P0 · Pwn）。**只有 gef.py 一个文件**，由 `/etc/gdb/gdbinit` 用 try/except 包住加载：gef 挂了 gdb 仍可用，不会把 Pwn 全废掉。 |
| `RsaCtfTool/` | https://github.com/RsaCtfTool/RsaCtfTool | 2026-10-27 克隆的 main（4.7 MB / 124 文件，去掉 .git/.venv/tests/docs） | MIT | RSA 全攻击面（P0 · Crypto）。**PyPI 上只有 1.0/1.1 且 Require-Python >=3.8,<3.9**，装不到镜像的 3.14 上，所以 vendor 源码本地 `pip install --no-deps`；依赖（cryptography/gmpy2/pycryptodome/z3-solver/psutil/tqdm/factordb-pycli）单独 pip 装。 |

## 许可合规说明
- `bkcrack`（zlib）、`jwt_tool`（GPL-3.0）、`sstimap`（GPL-3.0）、
  `pcapfix`（GPL-2.0）的 LICENSE 文件随各自目录一并分发，未作修改。
- `repak` 的 MIT/Apache 双许可文件已保留；Apache-2.0 要求保留 NOTICE/LICENSE，
  已满足。
- `nuclei-templates` 为 MIT，`LICENSE.md` 已随目录带上。
- 这些源码/二进制仅用于本地沙箱镜像内部的解题运行，不对外分发二进制产物。

## 升级步骤（任一资产换版本时）
1. 在能出网的机器上下载新版；
2. 更新上表的版本号与 sha256；
3. 若是 `repak`，确认官网 `.sha256` 与本地 `Get-FileHash` 一致；
4. 重新构建镜像并跑 Dockerfile 末尾那段自检。
