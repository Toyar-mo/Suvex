# 生产部署

这份文档面向**运维**，不是开发者。目标：拿到一台干净的 Linux 机器，
按这里做完就能跑起来，并且知道出问题时看哪里。

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
sudo -u suvex .venv/bin/pip install -r requirements.txt
```

> **只装 `requirements.txt`，不装 `requirements-dev.txt`**。
> 后者带 pytest 等测试依赖，生产不需要。
>
> ⚠️ 这里曾经有过一个真实的坑：`httpx` 一度只在 dev 依赖里，
> 而运行时要 import 它 —— 结果任何"只装 requirements.txt"的部署
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
sudo -u suvex .venv/bin/pip install -r /opt/suvex/suvex-api/requirements.txt
sudo systemctl restart suvex-api     # 迁移在启动命令里，会自动跑

# 前端要重新构建（产物不是源码）
cd /opt/suvex/suvex-web && sudo -u suvex npm ci && sudo -u suvex npm run build
```

> ⚠️ **重启会终止正在跑的扫描。** `recover_orphans()` 会把它们标成
> `aborted`，而子进程树会被终止。升级前先确认没有扫描在跑
> （总览页或 `GET /api/v1/scans?status=running`）。
