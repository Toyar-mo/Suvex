---
name: impacket
description: Impacket 的 Windows/AD 协议脚本（本镜像有 secretsdump.py、GetNPUsers.py、GetUserSPNs.py、psexec.py、wmiexec.py、smbclient.py 等）。已有凭据或哈希、需要枚举/导出/远程执行时用；不做口令爆破，也不要在未授权目标上执行。
---

# Impacket（Windows / AD 协议）

本镜像的脚本都在 `/opt/venv/bin/*.py`（`command -v secretsdump.py` 可确认），
版本 0.13.1。它们**都需要目标可达**；解题沙箱 `--network none` 时连不出去。

## 常见脚本与用途

| 脚本 | 用途 |
| --- | --- |
| `secretsdump.py` | 导出 SAM/LSA/NTDS（本地或 DCSync），得到哈希 |
| `GetNPUsers.py` | AS-REP roasting（不需要预认证的账号） |
| `GetUserSPNs.py` | Kerberoasting（有域账号时） |
| `smbclient.py` | SMB 交互（列共享/读写） |
| `psexec.py` / `wmiexec.py` / `atexec.py` | 用凭据或哈希远程执行 |
| `GetADUsers.py` / `GetADComputers.py` | 域内对象枚举 |

## 语法要点

Impacket 的目标参数是**一个字符串**：`[domain/]user[:password]@target`。

```bash
secretsdump.py 'CORP/alice:Passw0rd@10.0.0.5'
secretsdump.py -hashes ':aad3b435b51404eeaad3b435b51404ee:31d6cfe0d16ae931b73c59d7e0c089c0' \
  'CORP/alice@10.0.0.5'
secretsdump.py -just-dc 'CORP/Administrator:Passw0rd@dc01.corp.local'
GetUserSPNs.py 'CORP/alice:Passw0rd@dc01.corp.local' -request
smbclient.py 'CORP/alice:Passw0rd@10.0.0.5'
```

- `-hashes LM:NTLM`：Pass-the-Hash；LM 部分可用空串占位（`':NTLM'`）。
- `-just-dc` 需要**域管/复制权限**；普通账号会报权限错误，不是脚本坏了。
- 输出里的哈希是**敏感数据**：只用于解题结论，不要写进报告正文或对话摘要。

## 坑

- 域名/用户名大小写、`DOMAIN/user` 与 `user@domain` 两种写法都可能被接受；
  报 `STATUS_LOGON_FAILURE` 先核对格式，不要先怀疑网络。
- SMB 端口 445 不通时全部脚本都会超时；先 `nc -zv target 445` 确认。
- `psexec.py` 会在目标写服务/文件，属于**改动目标**的动作；只在明确授权的
  范围里用，CTF 题里优先 `smbclient.py` 只读查看。
- 不做口令爆破：没有凭据就用 `GetNPUsers.py` 等**无需预认证**的枚举，
  或按题目给的线索走。

## 与其他工具的衔接

- 拿到哈希后：`hashcat` / `john`（`-m 1000` NTLM）。
- SMB 共享内容：`smbclient.py` 下载后 `file` / `strings` / `binwalk`。
- 域内路径：`active_directory` 技能有更完整的攻击面清单。
