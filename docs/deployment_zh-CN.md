# EPUB Commentor — 部署指南

一份完整的、可复制粘贴的演练,把一台全新的 Ubuntu 服务器从"空盒子"带到
"守护进程已经在 systemd 下运行、第一本书提交并完成"。所有步骤结束后,
你会看到一张最终的文件系统快照,一眼看清每样东西在哪。

示例面向 **Ubuntu 22.04 LTS 或 24.04 LTS**,用 **systemd(用户级)** 跑
守护进程——不需要 root、正常操作不需要 `sudo`、密钥只放在你家目录下一个
0600 的文件里。同样的形状在任何 Debian 系/systemd 发行版上都成立;变体
见文末 [附录:替代方案](#附录替代方案)。

> 想看守护进程本身的行为参考(每个配置项、每条 `epubctl` 子命令、状态机
> 等)?见 [`daemon_zh-CN.md`](./daemon_zh-CN.md)。

---

## 目录

- [前置条件](#前置条件)
- [0. 先选定一份目录布局(先读这一段)](#0-先选定一份目录布局先读这一段)
- [1. 安装 Python 3.13](#1-安装-python-313)
- [2. 安装 Poetry](#2-安装-poetry)
- [3. 克隆仓库并安装包](#3-克隆仓库并安装包)
- [4. 创建工作目录](#4-创建工作目录)
- [5. 放进两份配置文件](#5-放进两份配置文件)
- [6. 把 API key 存进私有 env 文件](#6-把-api-key-存进私有-env-文件)
- [7. 写 systemd(用户级)单元](#7-写-systemd用户级单元)
- [8. 启动守护进程](#8-启动守护进程)
- [9. 提交第一本书](#9-提交第一本书)
- [10. 在阅读器上读输出](#10-在阅读器上读输出)
- [第二天:更新、备份、清理](#第二天更新备份清理)
- [故障排查](#故障排查)
- [附录:最终文件系统快照](#附录最终文件系统快照)
- [附录:替代方案](#附录替代方案)

---

## 前置条件

- 一台 Ubuntu 22.04+ 的服务器(或 VM / 云主机),你可以通过 SSH 登录。
- 一个非 root 用户,有 `sudo` 权限用来装 Python。
- 一个 OpenAI 兼容的 API key(OpenAI、DeepSeek、Azure 等)。
- 一本现成的 `.epub` 文件(步骤 9 会拿它做冒烟测试)。

**不需要**:域名、反向代理、HTTP 端口、TLS、数据库服务器、Docker(尽管
见 [替代方案](#附录替代方案))。

---

## 0. 先选定一份目录布局(先读这一段)

本指南坚持一套统一的路径,这样命令就能直接复制粘贴。如果你更喜欢别的
位置,改前缀就行——但走到一半别再挪动部件。

| 东西 | 路径 | 归属 |
|---|---|---|
| 仓库 + Python venv | `~/epub-commentor/` | 你 |
| 守护进程 workspace(SQLite + 每任务目录) | `~/epub-daemon/` | 你 |
| 守护进程配置 | `~/epub-daemon/format.daemon.json` | 你(0600) |
| LLM 凭据 | `~/epub-daemon/format.json` | 你(0600,不含 key) |
| API key 环境文件 | `~/epub-daemon/.env` | 你(0600) |
| systemd 单元 | `~/.config/systemd/user/epub-commentor.service` | 你 |
| 待标注的书 | `~/books/` | 你 |

守护进程跑在 **你的用户账号** 下(不是 root),所以它创建的所有文件都归
你,不需要特殊权限。

---

## 1. 安装 Python 3.13

Ubuntu 22.04 自带 Python 3.10;24.04 自带 3.12。项目要求 3.13。

**方案 A — deadsnakes PPA(最简单):**

```bash
sudo apt update
sudo apt install -y software-properties-common
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.13 python3.13-venv python3.13-dev
python3.13 --version   # 应该输出 Python 3.13.x
```

**方案 B — uv(现代、快、一站式):**

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
# 重新登入(或 `source ~/.bashrc`)让 uv 出现在 PATH
uv python install 3.13
```

二选一——想要纯 `apt` 选 A,想要一个工具包办 venv 选 B。

---

## 2. 安装 Poetry

Poetry 管理项目 venv 和两个 console script(`epubctl`、`epub-commentor`)。

```bash
curl -sSL https://install.python-poetry.org | python3.13 -
# 重新登入(或 `source ~/.bashrc`)
poetry --version
```

确认 `~/.local/bin` 在你的 `PATH` 上:

```bash
echo "$PATH" | tr ':' '\n' | grep -E '\.local/bin' || echo '把 export PATH=$HOME/.local/bin:$PATH 加进 ~/.bashrc'
```

---

## 3. 克隆仓库并安装包

```bash
cd ~
git clone https://github.com/noau/epub-commentor.git
cd epub-commentor
poetry install
```

`poetry install` 会把 venv 建在 `~/epub-commentor/.venv/`,并把两个
console script **装进这个 venv**:

- `epub-commentor`(一次性 CLI)
- `epubctl`(守护进程客户端)

你可以确认:

```bash
poetry run which epubctl
# → /home/you/epub-commentor/.venv/bin/epubctl
```

后面本指南里调 `epubctl` 有两种写法:

```bash
# (1) 永远前缀 `poetry run` —— 任意目录都行
poetry --directory ~/epub-commentor run epubctl status

# (2) 每个 shell 里激活一次 venv
source ~/epub-commentor/.venv/bin/activate
epubctl status   # 不用前缀
```

挑一个你顺手的。下面示例用 `(1)`,因为跨 shell 直接复制粘贴就能跑。

---

## 4. 创建工作目录

```bash
mkdir -p ~/epub-daemon
chmod 700 ~/epub-daemon
```

这里会放 SQLite 队列(`daemon.sqlite`)、每任务 workspace
(`jobs/job_<id>/`)和两份配置文件。

---

## 5. 放进两份配置文件

守护进程读 **两份** 扁平 JSON:

| 文件 | 用途 |
|---|---|
| `format.daemon.json` | workspace 路径、磁盘阈值、日志级别、轮询节奏 |
| `format.json` | LLM 凭据 + 每运行的默认值(`url`、`model`、……) |

两份彼此独立——守护进程读 `format.daemon.json` 拿自己的设置,读
`format.json` 拿 LLM 凭据。

### `format.daemon.json`

```bash
cp ~/epub-commentor/format.daemon.template.json ~/epub-daemon/format.daemon.json
chmod 600 ~/epub-daemon/format.daemon.json
$EDITOR ~/epub-daemon/format.daemon.json
```

至少把 `workspace_dir` 改成 **绝对路径**:

```json
{
    "workspace_dir": "/home/you/epub-daemon",
    "sqlite_path": null,
    "log_level": "INFO",
    "log_format": "text",
    "max_retries": 3,
    "disk": {
        "min_free_gb": 2.0,
        "min_free_percent": 10.0
    },
    "shutdown_grace_seconds": 30,
    "poll_interval_idle_seconds": 5,
    "poll_interval_paused_seconds": 60,
    "notification_command": null
}
```

> **为什么用绝对路径?** 模板里的 `./daemon_workspace` 是相对于守护
> 进程的 `cwd`。在 systemd 下,那是你单元里的 `WorkingDirectory`,实际
> 跑起来往往不是你想的那个。绝对路径省得猜。

小机器(20–50 GB 磁盘)用默认值就好。空间大的话,把 `disk.min_free_gb`
调到 `5.0`,留点日志尖峰的余地。

### `format.json`

```bash
cp ~/epub-commentor/format.template.json ~/epub-daemon/format.json
chmod 600 ~/epub-daemon/format.json
$EDITOR ~/epub-daemon/format.json
```

按你的供应商改 `url`、`model`、`token_encoding`:

```json
{
    "url": "https://api.openai.com/v1",
    "model": "gpt-4o-mini",
    "token_encoding": "o200k_base",
    "timeout": 360.0,
    "retry_times": 5,
    "retry_interval_seconds": 6.0,
    "temperature": 0.4,
    "top_p": 0.9,
    "json_mode": false,
    "rpm_limit": 60,
    "tpm_limit": 200000,
    "request_concurrency": 4
}
```

注意 **没有 `key` 字段**——下一节我们用环境文件挂载 API key。这样
`format.json` 就可以放心提交到 git。

---

## 6. 把 API key 存进私有 env 文件

建 `~/epub-daemon/.env`:

```bash
cat > ~/epub-daemon/.env <<'EOF'
EPUB_COMMENTOR_API_KEY=sk-your-secret-api-key
EOF
chmod 600 ~/epub-daemon/.env
$EDITOR ~/epub-daemon/.env   # 把右侧换成形如 sk-... 的真 key
```

这个文件是 **磁盘上** 密钥唯一的落脚点。systemd 的 `EnvironmentFile=`
会在启动守护进程时把它加载进来。

---

## 7. 写 systemd(用户级)单元

用户级 systemd 让服务跑在你的账号下——不用 root,不会和系统实例撞端口,
也不会出现诡异的归属问题。单元文件就在你家里。

```bash
mkdir -p ~/.config/systemd/user
$EDITOR ~/.config/systemd/user/epub-commentor.service
```

```ini
[Unit]
Description=EPUB Commentor Daemon
After=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/you/epub-commentor
EnvironmentFile=/home/you/epub-daemon/.env
ExecStart=/home/you/epub-commentor/.venv/bin/python -m epub_commentor.daemon --workspace /home/you/epub-daemon
Restart=on-failure
RestartSec=30
TimeoutStopSec=60

[Install]
WantedBy=default.target
```

几点说明:

- **`WorkingDirectory`** 必须指向仓库,而不是 workspace——守护进程模块是
  从那里 import 的。
- **`ExecStart`** 直接调 venv 里的 Python(不绕 `poetry run`),因为
  systemd 不会 source 你的 shell profile。
- **`EnvironmentFile`** 从那个 0600 文件里加载 API key。
- **`Restart=on-failure`** 配 `RestartSec=30` 兜底崩溃;守护进程自己的
  `recover_crashed_jobs` 保证不会有任务卡在 `PROCESSING`。

告诉 systemd 用户实例:就算你没登录也要跑你的服务:

```bash
loginctl enable-linger "$USER"
```

加载单元、开机自启、现在启动:

```bash
systemctl --user daemon-reload
systemctl --user enable --now epub-commentor.service
```

---

## 8. 启动守护进程

```bash
systemctl --user status epub-commentor.service
```

应该看到 `Active: active (running)`。如果看不到,跳到
[故障排查](#故障排查)。

跟踪守护进程自己的日志(systemd journal 镜像了守护进程的 stderr):

```bash
journalctl --user -u epub-commentor.service -f
```

应该看到类似:

```
[INFO] epub_commentor.daemon.server: daemon started; workspace=/home/you/epub-daemon
[INFO] epub_commentor.daemon.server: recovered 0 crashed jobs
```

按 `Ctrl-C` 退出 tail(不会停掉守护进程)。

守护进程现在已经创建了 SQLite 队列,在等任务:

```bash
ls -la ~/epub-daemon/
# 期望: daemon.sqlite  daemon.sqlite-wal  daemon.sqlite-shm  format.daemon.json
#        format.json  .env  jobs/  ...
```

---

## 9. 提交第一本书

在另一个终端里(或者 `Ctrl-C` 退出 journal tail 之后):

```bash
# 如果还没在机器上放 epub,先建一个目录丢一本进去
mkdir -p ~/books
# (scp / rsync / wget 把书丢进 ~/books/ —— 略)

EPUBCTL="poetry --directory ~/epub-commentor run epubctl --db ~/epub-daemon/daemon.sqlite"

$EPUBCTL submit ~/books/your-book.epub \
    --synopsis "一句话简介你的书。" \
    --priority 1
```

预期输出:

```
job id 1 enqueued
```

盯队列:

```bash
$EPUBCTL status --watch
```

表格会每两秒刷新一次。按 `Ctrl-C` 退出。也可以过滤:

```bash
$EPUBCTL status                 # 全部任务,最新的在前
$EPUBCTL status --status PROCESSING
```

跟踪每任务的日志(任务进 `PROCESSING` 之后):

```bash
$EPUBCTL log 1 --follow
```

跑起来的时候,守护进程就在打 LLM 了。典型情况下,200 页的书 30–90 分钟;
30 页的短篇大概 5 分钟。`status` 表格会实时显示 token 消耗。

`status` 显示 `SUCCESS` 之后,EPUB 在:

```
~/epub-daemon/jobs/job_1/output.commented.epub
```

你可以确认一下:

```bash
ls -la ~/epub-daemon/jobs/job_1/
```

---

## 10. 在阅读器上读输出

把文件从服务器拉下来:

```bash
# 从你的笔记本
scp you@your-server:~/epub-daemon/jobs/job_1/output.commented.epub ~/Downloads/
```

把 `output.commented.epub` 拖进 Calibre / Send-to-Kindle / Kobo /
Apple Books——按设备的具体步骤见 [主 README](../README_zh-CN.md#在设备上阅读结果)。
原文原封不动,只在边上多了带边框的 `<aside>` 块。

---

## 第二天:更新、备份、清理

### 更新包

```bash
cd ~/epub-commentor
git pull
poetry install
systemctl --user restart epub-commentor.service
```

`recover_crashed_jobs` 不会触发(没崩溃),正在跑的 `PROCESSING` 任务也
不会被掐——单元里的 `TimeoutStopSec=60` 给 worker 一个把当前章节干净
收尾的窗口。

### 备份队列 + 任务

值得备份的就两样:

- `~/epub-daemon/daemon.sqlite`(队列)
- `~/epub-daemon/jobs/`(每任务的输入 + 输出 + meta)

挂在你名下的一个小日常 cron 就够用:

```bash
cat > ~/bin/backup-epub-daemon.sh <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
TS=$(date +%Y%m%d-%H%M)
tar -C ~ -czf ~/backups/epub-daemon-${TS}.tar.gz epub-daemon/daemon.sqlite epub-daemon/jobs
EOF
chmod +x ~/bin/backup-epub-daemon.sh
mkdir -p ~/backups
```

`jobs/` 会涨得很大(每保留一本 EPUB + meta)。磁盘紧张的话,先清旧的终态:

```bash
$EPUBCTL prune --cancelled --force
$EPUBCTL prune --failed --force
```

SUCCESS 的留着,除非你已经把它们的 EPUB 拷到别处了。

### 删掉守护进程

```bash
systemctl --user disable --now epub-commentor.service
rm ~/.config/systemd/user/epub-commentor.service
systemctl --user daemon-reload
rm -rf ~/epub-daemon
```

仓库和 Poetry venv 留着;`epub-commentor` 一次性 CLI 还能照常用。

---

## 故障排查

| 现象 | 修法 |
|---|---|
| `systemctl --user status` 显示 `inactive (dead)` | 单元文件加载失败。`journalctl --user -u epub-commentor.service -xe` 会打出确切错误。 |
| `ExecStart` 说 "No such file or directory" | venv Python 路径不对。`ls ~/epub-commentor/.venv/bin/python`,然后改正 `ExecStart`。 |
| `ModuleNotFoundError: No module named 'epub_commentor.daemon'` | `WorkingDirectory` 没指向仓库,或者你克隆完没跑 `poetry install`。 |
| `format.daemon.json is not valid JSON` | 多余逗号或漏了引号。用 `python3 -m json.tool ~/epub-daemon/format.daemon.json` 校验。 |
| 第一个任务落进 `FAILED`,`error_stage=api_key` | `.env` 没被加载,或者变量名写错。看 `journalctl --user -u epub-commentor.service`,再跑 `systemctl --user show epub-commentor.service -p Environment` 确认 systemd 看到了这个变量。 |
| `~/epub-daemon/daemon.lock` 上 `Permission denied` | 之前守护进程是另一个用户跑的。`rm ~/epub-daemon/daemon.lock`,然后重启。 |
| 任务卡在 `PENDING` | Worker 被暂停了(操作员 `pause-all` 或磁盘熔断器跳闸)。`epubctl health` 会显示原因;`epubctl resume-all` 解除。 |
| `journalctl --user` 报 "Failed to connect" | 你的用户实例没跑。`systemctl --user status` —— 如果说 "Failed to fully start",跑 `loginctl enable-linger $USER`。 |
| `epubctl: command not found`,即使 `poetry install` 过 | 忘了 `poetry run` / `--directory` 前缀,或者你的 shell 没把 `~/.local/bin` 加进 PATH(这只影响裸 `poetry` 调用,不影响 `epubctl`)。 |

---

## 附录:最终文件系统快照

走完步骤 10 后,你的家目录大致长这样(略去常见的 `~/Documents`、
`~/.config/...` 等等):

```
/home/you/
├── epub-commentor/                  # 克隆下来的仓库(代码 + venv)
│   ├── .venv/                       # Poetry 管理的 venv
│   │   └── bin/
│   │       ├── python -> /usr/bin/python3.13
│   │       ├── epubctl              # console script(守护进程客户端)
│   │       └── epub-commentor       # console script(一次性 CLI)
│   ├── pyproject.toml
│   ├── format.template.json         # 没动的模板
│   ├── format.daemon.template.json
│   ├── README.md
│   └── docs/
│       ├── daemon_zh-CN.md
│       └── deployment_zh-CN.md      # 本文件
│
├── epub-daemon/                     # 守护进程 workspace(chmod 700)
│   ├── .env                         # API key(chmod 600)
│   ├── format.json                  # LLM 凭据,没有 `key` 字段(chmod 600)
│   ├── format.daemon.json           # 守护进程设置(chmod 600)
│   ├── daemon.sqlite                # 队列 DB(WAL 模式)
│   ├── daemon.sqlite-wal
│   ├── daemon.sqlite-shm
│   ├── daemon.lock                  # 单实例锁(启动时创建)
│   └── jobs/
│       └── job_1/
│           ├── input.epub           # 你提交的那本书的副本
│           ├── output.commented.epub
│           ├── meta.json            # CommentorResult 快照
│           ├── commentor.log        # 这个任务守护进程 stderr 镜像
│           ├── cache/               # LLM 缓存(SUCCESS 时清掉)
│           └── logs/
│               └── archive.tar.gz   # 每请求 LLM 日志,已归档
│
├── books/
│   └── your-book.epub               # 你提交的那本书
│
├── bin/
│   └── backup-epub-daemon.sh        # 可选备份脚本
│
├── backups/
│   └── epub-daemon-20260703-0200.tar.gz
│
└── .config/
    └── systemd/
        └── user/
            └── epub-commentor.service
```

**归属**:`~` 下所有东西都属 `you:you`。守护进程以你身份跑,所以读写上面
所有内容都不需要 `sudo`。

**磁盘预算(粗略,每 200 页书):**

| 项 | 大约体积 |
|---|---|
| `input.epub` | 1–3 MB |
| `output.commented.epub` | 1–3 MB |
| `meta.json` | < 100 KB |
| `logs/archive.tar.gz` | 5–20 MB |
| `cache/`(SUCCESS 时删除) | 5–50 MB |

守护进程的 `disk.min_free_gb`(默认 2 GB)是相对单任务成本来说非常大的
缓冲,避免缓存增长先于熔断器触发。

---

## 附录:替代方案

### 发行版

这套步骤在任何 Debian 系 / systemd 主机上都行得通。RHEL/Fedora 上把
`apt` 换成 `dnf`/`yum`;Arch 上换成 `pacman`。Fedora 上的 deadsnakes 等价
物是 `dnf install python3.13`(近期版本官方仓库就带 3.13);Arch 是
`pacman -S python`。

### 改成系统级 systemd 而不是用户级

如果你更愿意把守护进程跑成系统服务(无论谁登录,主机上只跑一个):

- 单元文件放 `/etc/systemd/system/epub-commentor.service`(root)。
- `WantedBy=` 改成 `WantedBy=multi-user.target`。
- `EnvironmentFile=/home/you/...` 这一行保留就行——单元本身归 root,
  但 `EnvironmentFile` 是用 systemd 启动时的权限读的。
- `sudo systemctl daemon-reload && sudo systemctl enable --now
  epub-commentor.service`。

其余目录布局完全一样。

### 不要 systemd

冒烟测试也可以让守护进程跑在 `tmux` / `screen` / `nohup` 下,但会失去崩
溃自动重启和开机自启。只在笔记本或开发机上用这种模式。

### Docker

如果你团队已经标准化用容器,见 [`daemon_zh-CN.md` 的 Docker 一节](./daemon_zh-CN.md#docker--容器)
看一份 `Dockerfile`。上面这套步骤更轻——Docker 多了镜像构建、卷挂载、
`docker compose` 这些层,只有当你要编排很多服务时才必要。