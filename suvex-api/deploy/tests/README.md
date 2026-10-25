# deploy/tests —— 部署脚本的真机测试

这里的东西**不进 CI**：它们需要 root、需要能 `mount --bind`、需要一台真机
（当前是 Kali）。CI 里跑不了，但扔掉更糟 —— 2026-10-25 那两次事故
（前端 dist 被清空、迁移文件从没部署）都是**只在真机上才显形**的形态，
单测守不住。

所以要手动跑。跑法和理由都写在这里，免得下次有人看见一堆 `.sh` 不知道该干嘛。

## 为什么不能放进 `tests/`（pytest）

- `test_update_web.sh` 要用 `mount --bind` 造出「dist 是挂载点」这个真实拓扑。
  `rm -rf` 清空挂载点内容却删不掉目录 —— 这个事故形态**只在挂载点上发生**，
  普通目录下 `rm -rf` 直接把目录删掉，问题会以另一种方式暴露，测不到真形态。
- 两个脚本都要 `sudo`，而且会往 `/tmp` 里造完整安装骨架。
- pytest 的 `tests/` 是给应用代码的，跑在 CI 的普通用户下。

## 三个文件

| 文件 | 干什么 | 怎么跑 |
|---|---|---|
| `test_update_web.sh` | `update.sh` 的前端发布：56 条断言，含三种「rsync 假成功」的静默失败形态 | `sudo bash test_update_web.sh <update.sh 路径>` |
| `test_detect_home.sh` | `update.sh` 的安装根探测：按可信度依次取线索，探不到就明确失败 | `sudo bash test_detect_home.sh <update.sh 路径>` |
| `reverse_verify.py` | 反向验证：逐条改坏 `update.sh`，上面两个脚本**必须变红**。9 条用例 | `python reverse_verify.py`（自己 scp 到 kali，不需要手动传） |

`reverse_verify.py` 需要能 `ssh kali`（免密）且 `sudo` 口令是 `kali`。
这两条硬编码在脚本里，换机器要改 —— 之所以没做配置化，是因为它只服务于
这一个部署目标，参数化反而是多余的间接层。

## 什么情况下必须重跑

改了 `deploy/update.sh` 的任何一行。特别是：

- 前端发布那一段（备份 / 切换 / 三条切换后校验 / 回滚）
- `detect_home()` 的探测顺序或失败分支
- 退出码语义（`0/2/3/4/5/6`）

## 反向验证的意义

正向测试只能证明「现在是对的」。它证明不了「测试真的在守这条约束」——
2026-10-25 实测：首轮 9 条反向用例里 2 条是假绿，改坏了测试还全过。
真正原因是那两条用例的改坏点**没被任何断言覆盖**（不是产品代码有问题）。
后面又发现「切换后校验」原本只有一种场景覆盖，另外两条校验是没测过的
死代码 —— 补了 I4/I5 两个场景才算守住。

所以改完 `update.sh`，**跑 `reverse_verify.py` 而不是只跑正向测试**。

## 输出解读

```
  PASS: 56   FAIL: 0
  VERDICT: PASS
```

`reverse_verify.py` 的三种非红结果：

- `FALSE-GREEN` —— 改坏了测试还全绿。测试没在守这条约束，要补断言。
- `STALE-ANCHOR` —— 找不到改坏点。`update.sh` 改了措辞，用例要同步更新。
- `RED` —— 期望值。

## 与 `suvex-api/tests/test_deploy_unit_migrations.py` 的分工

那个是 pytest，进 CI，守的是 **systemd unit 的静态契约**（`ExecStart` 里
必须有 `alembic upgrade head`）。它不需要真机。

这里是**运行时的真机行为**。两者都要有：unit 写对了但脚本探测错目录，
服务照样起不来。
