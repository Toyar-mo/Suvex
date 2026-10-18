# 生产部署

这份文档面向**运维**，不是开发者。目标：拿到一台干净的 Linux 机器，
按这里做完就能跑起来，并且知道出问题时看哪里。

> **想省事就用脚本**：`deploy/install.sh` 把下面一～六节写成了幂等脚本，
> 先 `sudo bash install.sh --dry-run` 看清单，确认后再去掉 `--dry-run` 真装。
> 第七节验收它替不了，装完照样要手工跑一次真扫描。
>
> ```bash
> sudo bash deploy/install.sh \
>   --repo https://github.com/Toyar-mo/Suvex \
>   --domain your.domain
> ```
>
> 脚本会**自己生成**加密密钥、写 `api.env`、建库、装 systemd 单元、构建前端、
> 配 nginx。它只做"先查后做"，重复跑结果一致。
> ⚠️ 两处它不替你决定：`api.env` 里的数据库口令占位符（`<强口令>`）与
> 数据库用户的 `CHANGE_ME` 口令，装完必须手工改掉且两边保持一致。

> **前提**：Docker 必须装好并运行。扫描要起沙箱容器，CTF2 解题还要
> 一个 `suvex-ctf2` 网络（服务启动时自动创建）。没有 Docker 的话
> 服务能启动，但**每一次扫描都会失败** —— 而那种失败看起来像
> "目标不可达"，排查方向会跑偏。

---

## 一、准备目录与用户

```bash
# 专用用户。**不要用 root** —— 理由见 suvex-api.service 里的说明。
sudo useradd --system --create-home --shell /usr/sbin/nologin suvex

# 代码放这里（与 unit 文件里的 WorkingDirectory 一致）
sudo mkdir -p /opt/suvex
sudo chown suvex:suvex /opt/suvex

# ★ 加进 docker 组 —— 沙箱要访问 docker socket。
#   ⚠️ 这个组的权限**等价于 root**（能挂载宿主根目录进容器）。
#      知情后再决定；要真正隔离得给 strix 配远程 docker 端点。
sudo usermod -aG docker suvex
```

> ⚠️ `usermod -aG` 之后**要重新登录**（或重启服务）才生效。
> 症状是"权限明明加了但还是连不上 docker"。

---

## 二、放代码与装依赖

```bash
cd /opt/suvex
sudo -u suvex git clone <仓库地址> src
sudo -u suvex cp -r src/suvex-api /opt/suvex/suvex-api
sudo -u suvex cp -r src/suvex-web /opt/suvex/suvex-web

cd /opt/suvex/suvex-api
sudo -u suvex python3.12 -m venv .venv
# ★ 装**锁文件**，不是 requirements.txt（2026-10-11 改）
sudo -u suvex .venv/bin/pip install --require-hashes -r requirements.lock
```

> **为什么装 `requirements.lock` 而不是 `requirements.txt`**：
> 后者是**范围约束**（`fastapi>=0.115,<1.0`），同一个 commit 在不同
> 时间装会得到不同版本 —— "三个月前部署能跑，今天重装就炸"是真实
> 存在的失败模式。锁文件里是 37 个包的精确版本 + sha256 哈希，
> `--require-hashes` 会逐个校验。
>
> ⚠️ 锁文件是**在 Linux 上生成的**。如果你在 Windows 上重新生成过它，
> 它记的是 `win_amd64` 的 wheel，在这里会装不上 —— 而报错是
> "找不到满足条件的版本"，看起来像依赖写错了。重新生成的命令见
> `requirements.txt` 头部。

> **只装运行时依赖，不装 `requirements-dev.txt`**。
> 后者带 pytest 等测试依赖，生产不需要。
>
> ⚠️ 这里曾经有过一个真实的坑：`httpx` 一度只在 dev 依赖里，
> 而运行时要 import 它 —— 结果任何"只装运行时依赖"的部署
> 都会在**启动时**直接 `ModuleNotFoundError`，报错位置还在
> `providers/llm.py` 的 import 行，看起来像"某个 provider 出问题"。
> 已修，但值得知道这类错误长什么样。

---

## 三、配置

```bash
sudo mkdir -p /etc/suvex

# 生成加密密钥（provider 凭据用它加密）
sudo -u suvex /opt/suvex/suvex-api/.venv/bin/python -m app.core.crypto

# 写配置文件
sudo tee /etc/suvex/api.env > /dev/null <<'EOF'
SUPEX_ENV=prod
SUPEX_LOG_LEVEL=info

SUPEX_DATABASE_URL=postgresql+psycopg://suvex:<强口令>@127.0.0.1:5432/suvex

# ★ 上一步生成的那条
SUPEX_SECRET_KEY=<粘贴>

# ★ 生产必须 true —— 否则启动自检会**拒绝启动**
SUPEX_COOKIE_SECURE=true

# 前端与 API 同源（nginx 一个 server 块托管两者），所以这里
# 只列前端自己的地址。带凭据的跨源请求不能用通配符。
SUPEX_CORS_ORIGINS=https://suvex.example.com

SUPEX_SCAN_AUTHORIZATION_STATEMENT=我有权对此目标进行安全测试
EOF

# ★ 权限收紧：这个文件里有加密密钥
sudo chown root:suvex /etc/suvex/api.env
sudo chmod 0640 /etc/suvex/api.env
```

> ⚠️ **`api.env` 里不要写引号。** systemd 的 `EnvironmentFile`
> 语法与 shell 不同：`KEY="value"` 会把引号当成值的一部分，
> 于是 `SUPEX_COOKIE_SECURE` 变成字符串 `"true"` —— 而
> `pydantic-settings` 解析布尔值时会接受 `true`/`false`/`1`/`0`，
> 带引号的 `"true"` 会解析失败并**在启动时报一个看起来无关的错**。

> ⚠️ **`SUPEX_SECRET_KEY` 必须与数据库一起备份。**
> 丢了它，库里所有 provider 的 API key 都解不开 ——
> 而它们只能重新去各厂商申请。

---

## 四、数据库

用外部的 PostgreSQL 16（不是容器）：

```bash
sudo -u postgres psql <<'EOF'
CREATE USER suvex WITH PASSWORD '<强口令>';
CREATE DATABASE suvex OWNER suvex ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C' TEMPLATE template0;
EOF
```

> **为什么 `LC_COLLATE 'C'`**：C 的排序是字节序，跨机器完全一致。
> 跟随镜像 locale 的话，`ORDER BY` 的结果会随 glibc 版本变化 ——
> 症状是"同一份数据在两台机器上分页顺序不同"，而它极难怀疑到 locale。

迁移在服务启动时自动跑（见 unit 文件的 `ExecStart`），不用手工执行。
要单独跑：

```bash
sudo -u suvex /opt/suvex/suvex-api/.venv/bin/alembic upgrade head
```

---

## 五、装服务

```bash
sudo cp /opt/suvex/suvex-api/deploy/suvex-api.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now suvex-api

# 看它起没起来
sudo systemctl status suvex-api
sudo journalctl -u suvex-api -n 50 --no-pager
```

起来之后应当看到这几行（顺序固定）：

```
[suvex.bootstrap] 数据库连接正常，结构检查通过（N 张表）
[suvex.scheduler] 扫描调度器已启动（上限 N）
[suvex.ctf2] CTF2 调度器已启动（上限 N）
[suvex] Suvex API 0.1.0 已就绪（env=prod，…）
```

---

## 六、前端产物与反代

```bash
cd /opt/suvex/suvex-web
sudo -u suvex npm ci
sudo -u suvex npm run build      # 产物在 dist/

sudo cp /opt/suvex/suvex-api/deploy/nginx.conf /etc/nginx/conf.d/suvex.conf
# 按需改 server_name 与证书路径，然后：
sudo nginx -t && sudo systemctl reload nginx
```

> ⚠️ `nginx.conf` 里有**三处**改错会出问题的地方，都写在那个文件的
> 头部注释里：SSE 的 `proxy_buffering off`、`proxy_read_timeout`
> 要大于心跳间隔、以及 `X-Forwarded-For` 的信任边界。
> **改之前读那一段。**

---

## 七、验收

```bash
# 1. 健康检查（public）
curl -s https://suvex.example.com/api/v1/health
# 期望：{"status":"ok","version":"0.1.0"}

# 2. 指标（若开了 nginx 的网段限制，从本机打）
curl -s http://127.0.0.1:8000/api/v1/metrics | head -20

# 3. 登录页打得开
curl -sI https://suvex.example.com/ | head -1

# 4. ★ 一次真实扫描（这一步不能省）
#    浏览器登录 → 新建扫描 → 目标填一个你自己的测试地址 → 观察事件流
```

**第 4 步必须做。** 前面三步只能证明"进程活着、端口通"，
而扫描链路的失败（docker 权限、网络、镜像拉取）在那三步里
一个都不会暴露。

---

## 八、出问题看哪里

| 症状 | 先看 |
| --- | --- |
| 服务起不来 | `journalctl -u suvex-api -n 100`，找 `bootstrap` 那几行 |
| 登录后下一个请求还是未登录 | `SUPEX_COOKIE_SECURE` 与 nginx 的 TLS 是否一致 |
| 扫描全部失败 | 沙箱要 docker：`sudo -u suvex docker ps` 通不通 |
| 活动页一直空白 | nginx 的 `proxy_buffering off` 加了没 |
| 活动页反复重连 | `proxy_read_timeout` 是不是小于 SSE 心跳间隔 |
| 大附件上传 413 | nginx 的 `client_max_body_size` |
| 时间线图标不显示 | 前端产物是不是旧的（重新 `npm run build`）|
| 数据库连接报错 | `api.env` 里有没有多余引号 |

---

## 九、升级

```bash
cd /opt/suvex/src && sudo -u suvex git pull
sudo -u suvex cp -r suvex-api/app /opt/suvex/suvex-api/
# 依赖有变动时（锁文件也一起更新了）：
sudo -u suvex .venv/bin/pip install --require-hashes -r /opt/suvex/suvex-api/requirements.lock
sudo systemctl restart suvex-api     # 迁移在启动命令里，会自动跑

# 前端要重新构建（产物不是源码）
cd /opt/suvex/suvex-web && sudo -u suvex npm ci && sudo -u suvex npm run build
```

> ⚠️ **`git pull` 之后要确认锁文件是否也变了。** 只 `git pull` 而没重装
> 依赖，会遇到"代码是新的、依赖是旧的"—— 症状是新代码 import 一个
> 新包时 `ModuleNotFoundError`，或者更隐蔽地行为不一致。
> 判断方式：`git diff HEAD@{1} --stat | grep requirements`。

> ⚠️ **重启会终止正在跑的扫描。** `recover_orphans()` 会把它们标成
> `aborted`，而子进程树会被终止。升级前先确认没有扫描在跑
> （总览页或 `GET /api/v1/scans?status=running`）。

---

## 十、备份与恢复

**要备份的是两样东西，少一样都会出事。**

| 备份对象 | 丢了会怎样 |
| --- | --- |
| PostgreSQL 数据库 | 全部扫描、漏洞、报告、审计记录消失 |
| `SUPEX_SECRET_KEY` | **provider 的 API key 全部解不开**（密文还在，但没有密钥就是一堆乱码） |

> ⚠️ 第二样最容易被漏掉，而且漏了之后**症状具有欺骗性**：
> 数据库完好、界面能开、列表也在，只是每个 provider 都报
> "凭据无法解密"。如果这时以为"重新填一次 API key 就行"，
> 那原来的 key 就真的没了 —— 得重新去各厂商申请。

### 备份

```bash
# 1. 数据库（自定义格式，便于选择性恢复）
sudo -u postgres pg_dump -Fc suvex > /var/backups/suvex-$(date +%F).dump

# 2. 加密密钥（它就在 api.env 里，但单独存一份更稳妥）
sudo cp /etc/suvex/api.env /var/backups/suvex-api.env-$(date +%F)
sudo chmod 0600 /var/backups/suvex-api.env-$(date +%F)
```

> **`pg_dump -Fc` 而不是纯 SQL**：自定义格式支持 `pg_restore -t`
> 只恢复某张表，而纯 SQL 只能整库灌。出事故时"只救回
> `findings` 表"是真实需求。

> ⚠️ **备份里含密钥文件，所以要按密钥的规格保护它**
> （`chmod 0600`、不要进版本库、不要走聊天工具传输）。

### 恢复

```bash
# 1. 建一个干净库（不要往现有的库上灌）
sudo -u postgres createdb -O suvex suvex_restore

# 2. 灌回去
sudo -u postgres pg_restore -d suvex_restore /var/backups/suvex-2026-10-11.dump

# 3. 恢复密钥：把备份的 api.env 放回去
sudo cp /var/backups/suvex-api.env-2026-10-11 /etc/suvex/api.env
sudo chown root:suvex /etc/suvex/api.env && sudo chmod 0640 /etc/suvex/api.env

# 4. 让服务指向恢复出来的库（改 SUPEX_DATABASE_URL），重启
sudo systemctl restart suvex-api
```

**恢复后必须验的一件事**：打开设置页，确认 provider 显示的是
**掩码**（如 `sk-****abcd`）而不是"凭据无法解密"。
前者说明密钥配对了，后者说明密钥不是当初那把。

---

## 十一、磁盘容量与清理

> ★ **这一节是必需的，不是可选的。** 扫描产物目前**没有任何自动
> 清理**，只增不减（代码里也这么写着：`app/scans/runner.py` 的
> `scan_upload_dir` docstring）。真实部署下不清理会一直涨到磁盘满，
> 而那时数据库也会跟着写不进去。

### 会涨在哪里

默认都落在**项目根**下（可用 `SUPEX_SCAN_ROOT` 改到别的盘）：

| 路径 | 是什么 | 实测体积 |
| --- | --- | --- |
| `_scan_runs/scan_<id>/` | **扫描产物**（strix 的运行记录、日志、漏洞 JSON） | 5–7 MB / 次 |
| `_scan_runs/_ctf2_attachments/<题目id>/` | CTF2 题目附件原件 | 单机实测 64 MB |
| `_scan_runs/_uploads/<扫描id>/` | 用户上传的题目附件原件 | 视上传量 |
| `_uploads/` | 漏洞附件（漏洞详情里贴的图/文件） | 视上传量 |

> **量级感**：按每次扫描 6 MB、每天 20 次算，`scan_*` 一年约
> **43 GB**。CTF2 附件是另一条线，取决于下多少题。

### 可以删什么、绝不能删什么

| 路径 | 能删吗 |
| --- | --- |
| `_scan_runs/scan_<id>/` | ✅ **这就是要清的**。报告正文已存进数据库，删了不影响历史报告 |
| `_scan_runs/_uploads/` | ❌ 题目附件**原件**，同一道题重跑还要用 |
| `_scan_runs/_ctf2_attachments/` | ❌ 跟题目走，重跑要复用 |
| `_uploads/` | ❌ 漏洞附件，漏洞详情页还在引用 |
| `_scan_runs/_uploads/_staging/` | ✅ 暂存区，服务启动时会自动清（`sweep_staging`） |

> ⚠️ **为什么附件刻意放在 `_scan_runs/` 里面的子目录**：这样
> "手工清一次扫描"（`rm -rf _scan_runs/scan_xxx`）不会顺手带走
> 附件。**但反过来也意味着：如果你按"删 `_scan_runs` 下所有东西"
> 的思路清，就会把附件一起删掉。** 所以下面的命令只匹配 `scan_*`。

### 清理命令

```bash
cd /opt/suvex/suvex-api

# 1. 先看现状（不要跳过这一步）
du -sh _scan_runs
ls -1 _scan_runs | wc -l

# 2. 试运行：列出 30 天前、且已结束的扫描产物（只列不删）
find _scan_runs -maxdepth 1 -type d -name 'scan_*' -mtime +30 -print | head -20
find _scan_runs -maxdepth 1 -type d -name 'scan_*' -mtime +30 -print | wc -l

# 3. 确认清单没问题后再真删
find _scan_runs -maxdepth 1 -type d -name 'scan_*' -mtime +30 -exec rm -rf {} +
```

> ⚠️ **三处必须注意**：
> 1. `-maxdepth 1` —— 少了它会把子目录也匹配上；
> 2. `-name 'scan_*'` —— **绝不能写成 `-name '*'` 或省略**，否则
>    `_uploads/`、`_ctf2_attachments/` 会被一起删掉，而那是不可恢复的
>    （用户得重新上传/重新下题）；
> 3. `-mtime +30` 是**按目录修改时间**算的。正在跑的扫描目录时间很新，
>    不会被选中 —— 但为稳妥，清理前确认没有扫描在跑
>    （`GET /api/v1/scans?status=running`）。

### 建议挂个定时任务

```bash
# /etc/cron.weekly/suvex-clean（或 systemd timer）
#!/bin/bash
set -e
cd /opt/suvex/suvex-api
find _scan_runs -maxdepth 1 -type d -name 'scan_*' -mtime +30 -exec rm -rf {} +
logger -t suvex-clean "已清理 30 天前的扫描产物，当前 $(du -sh _scan_runs | cut -f1)"
```

> **保留期取多少**：报告正文在数据库里，磁盘上留的是**证据与原始记录**。
> 30 天是"还来得及回溯"与"不撑爆磁盘"之间的折中。要长期留档就把
> 它归档到对象存储再删本地，而不是无限留着。

### 数据库也会涨

`activity_events` 是最大的表（单次扫描约 936 条事件，实测 10 MB）。
它**有**保留策略：启动时按审计保留期清理（见 `app/main.py`）。
若发现它异常大，先查：

```bash
sudo -u postgres psql suvex -c \
  "SELECT relname, pg_size_pretty(pg_total_relation_size(relid)) AS size
   FROM pg_statio_user_tables ORDER BY pg_total_relation_size(relid) DESC LIMIT 10;"
```

---

## 十二、容量与并发参数

`/etc/suvex/api.env` 里与容量相关的项（README 的变量表里也有）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `SUPEX_DB_POOL_SIZE` | 5 | 每个进程的常驻连接数 |
| `SUPEX_DB_MAX_OVERFLOW` | 10 | 峰值时可临时多开的连接 |
| `SUPEX_MAX_CONCURRENT_SCANS` | 3 | 同时跑的扫描数（每个会起一个沙箱容器） |
| `SUPEX_SCAN_ROOT` | 项目根 | 产物与附件落盘根，**建议单独挂一块盘** |

> ⚠️ **连接数要算乘法**：如果 uvicorn 开了多个 worker
> （`--workers 4`），那么 `4 × (5 + 10) = 60` 个连接。
> PostgreSQL 默认 `max_connections = 100`，**再叠加别的客户端就会打满**。
> 规则：`worker 数 × (POOL_SIZE + MAX_OVERFLOW) < max_connections`。

> ⚠️ **并发扫描数与内存**：每个沙箱容器有 CPU / 内存上限
> （设置页可改，默认 4 核 / 4096 MB）。`MAX_CONCURRENT_SCANS=3`
> 意味着峰值约 12 GB 内存 —— 部署前按机器实际内存调这个数。

