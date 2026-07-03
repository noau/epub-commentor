# EPUB Commentor — 云端守护进程

`epub-commentor` 的常驻队列 + worker，专为云服务器与批处理流水线设计——在这些场景里，SSH 断连、状态不可见、磁盘爆满都是真问题。

守护进程**保留**了原有的单文件 CLI，只是在它之上**可选地**加了一层：一次性把多本 EPUB 排进队列，然后去忙别的事，回头用本地 CLI 客户端看一切。没有 HTTP、没有鉴权、没有额外进程——只有一个 SQLite 文件、一个 workspace 目录、一条 Python 线程。

> 想看单文件 CLI？请回到 [主 README](../README_zh-CN.md)。

---

## 目录

- [它解决了什么问题](#它解决了什么问题)
- [架构一览](#架构一览)
- [每任务的 workspace](#每任务的-workspace)
- [快速上手](#快速上手)
- [配置文件](#配置文件)
  - [`format.daemon.json`（守护进程设置）](#formatdaemonjson守护进程设置)
  - [`format.json`（LLM 凭据）](#formatjsonllm-凭据)
- [启动守护进程](#启动守护进程)
- [`epubctl` —— 本地 CLI 客户端](#epubctl--本地-cli-客户端)
  - [提交任务](#提交任务)
  - [查看队列](#查看队列)
  - [实时刷新](#实时刷新)
  - [查看日志](#查看日志)
  - [生命周期事件](#生命周期事件)
  - [优先级、暂停、取消](#优先级暂停取消)
  - [健康状态、恢复、清理](#健康状态恢复清理)
- [每任务旗标 (`--flags-json`)](#每任务旗标---flags-json)
- [任务生命周期与状态机](#任务生命周期与状态机)
- [崩溃恢复](#崩溃恢复)
- [磁盘熔断](#磁盘熔断)
- [鲁棒性 —— 出问题时会怎样](#鲁棒性--出问题时会怎样)
- [部署守护进程](#部署守护进程)
  - [systemd 单元（Linux）](#systemd-单元linux)
  - [Docker / 容器](#docker--容器)
  - [单实例保证](#单实例保证)
  - [优雅停止](#优雅停止)
- [故障排查](#故障排查)
- [常见问题](#常见问题)

---

## 它解决了什么问题

一本长 EPUB 的评注要跑**几个小时**，在云服务器上裸跑 `epub-commentor` CLI 会撞到四个真实痛点：

1. **SSH 断连 = 任务作废。** 关掉终端一切归零。
2. **状态不可见。** 你没法在另一个终端说清楚"哪本卡在哪章、消耗了多少 token、哪次 LLM 调用在飞"。
3. **没有多本排队。** 想连着评 5 本书？只能人工串行。
4. **磁盘撑爆服务器。** 每本 LLM 缓存+调试日志可达数百 MB；20–50 GB 的云盘跑 3–4 本就触发 `OSError: [Errno 28]`。

守护进程把这四条全治了：

- **抗断连** —— 作为后台进程跑，不挂在 shell 下。包一层 `systemd` 或 `docker` 让它自愈。
- **任意终端可见** —— `epubctl status` / `watch` / `show`。
- **内置队列，支持优先级 + 重试** —— 想排多少排多少，单 worker 按 `priority DESC, created_at` 拉取。
- **主动磁盘监控** —— 剩余空间越过阈值时自动暂停所有非终态任务，腾出空间后自动恢复。

它**不**加 HTTP API、不加鉴权、不加通知、不加每任务并行 worker —— 这些都被刻意排除（详见[常见问题](#常见问题)）。

---

## 架构一览

```
┌────────────────────┐
│  epubctl submit    │ ── INSERT ──┐
│  epubctl status    │ ◀─ SELECT ──┤
│  epubctl cancel    │ ── signal ──┤
│  epubctl log       │ ── read FS ─┤
└────────────────────┘             ▼
                        ┌──────────────────────┐
                        │  daemon.sqlite (WAL) │
                        │   - jobs             │
                        │   - events           │
                        │   - control_signals  │
                        │   - server_stats     │
                        └──────────────────────┘
                                   ▲
                                   │ SELECT/UPDATE
                                   │
       ┌───────────────────────────┴─────────────────────────────┐
       │  python -m epub_commentor.daemon                         │
       │   worker_loop (单线程，阻塞前台):                        │
       │     while not shutdown:                                  │
       │       if disk_low(): pause + sleep                       │
       │       if cancel-signal for current job: request_abort()  │
       │       job = fetch_next_pending()                         │
       │       if job: run_job(job)   # ← 复用 comment_epub()    │
       │       else: sleep 5                                       │
       └──────────────────────────────────────────────────────────┘
                                    │
                                    ▼
                            comment_epub(
                                source     = <ws>/input.epub,
                                output     = <ws>/output.commented.epub,
                                llm        = LLM(cache_path=<ws>/cache,
                                                  log_dir_path=<ws>/logs),
                                progress_cb= quiet,
                            )
```

Worker **进程内**调用 `comment_epub()`（不 shell out），原因有二：

1. CLI 内部错误时会 `sys.exit(2)`；subprocess 路径会把 worker 一起杀掉。
2. 限流器、缓存、abort 标志都是进程内全局——多进程需要 IPC 才能共享。

每任务隔离因此通过既有的 CLI 参数实现：`cache_path`、`log_dir_path`、`output`
都指向该任务自己的子目录。

---

## 每任务的 workspace

每个任务拥有一个自洽的目录：

```
<workspace_dir>/
├── daemon.sqlite              # 队列数据库（WAL 模式）
├── daemon.lock                # 单实例锁（fcntl/PID）
├── format.daemon.json         # 守护进程配置（可选）
├── format.json                # LLM 凭据（与 CLI 共享）
└── jobs/
    └── job_<id>/
        ├── input.epub              # 提交时的拷贝
        ├── output.commented.epub   # SUCCESS 时写入——最终产物
        ├── cache/                  # LLM 缓存（SUCCESS 时删除）
        ├── logs/                   # LLM 调试日志（SUCCESS 时打包）
        ├── commentor.log           # 守护进程自身的 stderr 镜像
        └── meta.json               # CommentorResult 快照（仅 SUCCESS）
```

关键不变量：

- **`input.epub` 是拷贝，不是软链** —— 原文件永远不被触碰。任务入队后你可以立刻删/改源文件。
- **`cache/` 和 `logs/` 任务独立** —— 两本书前后跑不会污染彼此的缓存。
- `SUCCESS` 时缓存被删除，日志被打包成 `logs/archive.tar.gz`，让 workspace 在跑过很多任务后仍保持可控。
- `FAILED` 时缓存也删除 —— 校验失败可能留下污染条目，下次重试必须从干净缓存开始。
- `output.commented.epub` 和 `meta.json` 永久保留，方便你导出 EPUB + 审计模型产出。

---

## 快速上手

前提：`epub-commentor` 已安装（`poetry install`）；守护进程就在同一个包内。

```bash
# 1. 选一个 workspace 目录。所有东西都放这下面。
mkdir -p ~/epub-daemon

# 2. （可选）放一份配置。默认值对多数情况都够用。
cat > ~/epub-daemon/format.daemon.json <<'EOF'
{
  "workspace_dir": "/home/you/epub-daemon",
  "disk": { "min_free_gb": 2.0, "min_free_percent": 10.0 },
  "max_retries": 3,
  "log_level": "INFO"
}
EOF

# 3. 提供 API key（守护进程沿用同样的 resolve_api_key）。
export EPUB_COMMENTOR_API_KEY=sk-...

# 4. 启动守护进程。阻塞前台；用 systemd / `nohup` / `tmux` 包起来。
poetry run python -m epub_commentor.daemon --workspace ~/epub-daemon

# 5. 另开一个终端：提交任务、观察队列。
poetry run epubctl submit ~/books/little-prince.epub \
    --flags '{"ai_select": true, "no_review": true}' \
    --synopsis "一个飞行员在撒哈拉迫降后遇到小王子的诗意故事。" \
    --priority 5

poetry run epubctl status --watch    # 实时刷新，Ctrl-C 退出
poetry run epubctl log 1 --follow    # 跟踪任务 1 的日志
```

当 `status` 显示 `SUCCESS`，EPUB 就在
`~/epub-daemon/jobs/job_1/output.commented.epub` —— 拖进阅读器即可。

---

## 配置文件

守护进程读**两份**扁平 JSON 文件，都跟 CLI 那边同名：

| 文件 | 用途 | 守护进程特有？ |
|---|---|---|
| `format.json` | LLM 凭据 + 每运行默认值（`url`、`model`、`key`、`concurrency` …） | 否 —— 与 CLI 完全相同 |
| `format.daemon.json` | workspace 路径、磁盘阈值、日志级别、轮询节奏 | 是 |

### `format.daemon.json`（守护进程设置）

把模板拷到 workspace 里改：

```bash
cp format.daemon.template.json ~/epub-daemon/format.daemon.json
```

```json
{
  "workspace_dir": "./daemon_workspace",
  "sqlite_path": null,
  "log_level": "INFO",
  "log_format": "text",
  "max_retries": 3,
  "disk": {
    "min_free_gb": 2.0,
    "min_free_percent": 10.0
  },
  "shutdown_grace_seconds": 30,
  "poll_interval_idle_seconds": 5.0,
  "poll_interval_paused_seconds": 60.0,
  "notification_command": null
}
```

| 字段 | 默认 | 作用 |
|---|---|---|
| `workspace_dir` | `./daemon_workspace` | 持有 `daemon.sqlite` 和 `jobs/` 的根目录。**CLI `--workspace` 覆盖它。** |
| `sqlite_path` | `<workspace_dir>/daemon.sqlite` | 一般不用改；想让 DB 落到别的盘才需要。 |
| `log_level` | `INFO` | Python 根 logger 级别。调试诡异的 retry 时用 `DEBUG`。 |
| `log_format` | `text` | `text` 或 `json`。`json` 跟 `journalctl -o json` 配合得很好。 |
| `max_retries` | `3` | `FAILED` 任务自动重排的最大次数；用尽后保持 FAILED。 |
| `disk.min_free_gb` | `2.0` | 剩余空间低于该 GB 数时熔断。 |
| `disk.min_free_percent` | `10.0` | 已用百分比超过 `100 - 此值`时熔断。 |
| `shutdown_grace_seconds` | `30` | 保留给将来用；当前循环只等当前任务完成。 |
| `poll_interval_idle_seconds` | `5.0` | 队列空闲时 worker 多久查一次。 |
| `poll_interval_paused_seconds` | `60.0` | 处于熔断态时多久重查一次磁盘。 |
| `notification_command` | `null` | 可选的 shell 钩子（见下文）。 |

**查找顺序**（高优先级优先）：

1. `--config <path>` CLI 旗标
2. `$EPUBCTL_DAEMON_CONFIG` 环境变量
3. `<cwd>/format.daemon.json`

**未知键在 WARNING 级别记录，但不会让启动失败** —— 拼错不会被静默吞掉。

#### 可选的 `notification_command`

`notification_command` 是一个逃生口：设置后，守护进程在这些事件后通过
`subprocess` 调用它，把事件种类和一句话摘要填进 argv。默认 `null`（不通知）。示例：

```json
{
  "notification_command": "/home/you/bin/notify.sh {kind} {summary}"
}
```

钩子是单次 shell 调用，写法自定；守护进程只填 `{kind}`（`started` /
`finished` / `failed` / `cancelled` / `disk_low` / `disk_recovered`）和
`{summary}`。

### `format.json`（LLM 凭据）

跟单文件 CLI 的 `format.json` 完全相同。守护进程启动时读一次，所有任务共享；每任务覆盖通过 `--flags-json`（见下文）。所有 CLI 字段都生效：`url`、`model`、`token_encoding`、`timeout`、`temperature`、`top_p`、`cache_path`、`log_dir_path`、`rpm_limit`、`tpm_limit`、`request_concurrency` 等。

API key 解析顺序也相同：`$EPUB_COMMENTOR_API_KEY` 环境变量优先于 `format.json` 的 `key` 字段。守护进程**不需要** key 即可启动——只有拉到第一个任务时才查；两个来源都没有则该任务落入 `FAILED`，`error_stage=api_key`。

---

## 启动守护进程

```bash
poetry run python -m epub_commentor.daemon --workspace ~/epub-daemon
```

| 旗标 | 默认 | 作用 |
|---|---|---|
| `--workspace PATH` | *必填* | 根目录，持有 `daemon.sqlite` 与 `jobs/`。不存在会自动创建。 |
| `--config PATH` | 自动发现 | 覆盖 `format.daemon.json` 查找位置。 |
| `--once` | 关 | 只跑一轮 poll cycle 就退出，调试用。 |
| `--max-seconds N` | `0`（永久） | N 秒后退出，烟雾测试用。 |

启动时守护进程会：

1. 解析配置和 SQLite 路径。
2. 打开数据库（WAL 模式），不存在则建表。
3. 跑 `recover_crashed_jobs`——见[崩溃恢复](#崩溃恢复)。
4. 抢占单实例锁（`<workspace>/daemon.lock`）。
5. 给 SIGINT / SIGTERM 接上优雅停机处理。
6. 一次性加载 `format.json`，作为基础 LLM kwargs。
7. 进入 worker 循环。

停止：发 SIGTERM（`kill <pid>`）或在守护进程所在终端按 Ctrl-C。当前任务协作式 abort，对应行变成 `CANCELLED`，然后守护进程退出。

---

## `epubctl` —— 本地 CLI 客户端

`epubctl` 是与运行中的守护进程打交道的本地工具。它直接读写 SQLite——全程不联网。

```
poetry run epubctl --db ~/epub-daemon/daemon.sqlite <subcommand> [args]
```

不传 `--db` 时按以下顺序解析：

1. `--db <path>` 参数
2. `$EPUBCTL_DAEMON_DB` 环境变量
3. `./daemon.sqlite`（当前工作目录）

### 提交任务

```bash
poetry run epubctl submit ~/books/little-prince.epub
```

`submit` 把文件拷贝到 `jobs/job_<N>/input.epub` 并插入一行 `PENDING`。参数：

| 旗标 | 作用 |
|---|---|
| `file`（位置参数） | 源 `.epub` 路径，必须存在。 |
| `--priority N` | 整数，越大越优先。默认 `0`。 |
| `--synopsis "..."` | 一句话书籍简介（转发给 Stage 1）。 |
| `--max-retries N` | 该任务的重试预算。默认 `3`（与 `format.daemon.json` 一致）。 |
| `--flags-json '{...}'` | 每任务 `CommentConfig` + `LLM` 覆盖，见[每任务旗标](#每任务旗标---flags-json)。 |
| `--ai-select` | 便捷开关：往 flags 里加 `"ai_select": true`。 |
| `--no-review` | 便捷开关：往 flags 里加 `"no_review": true`。 |
| `--ai-review` | 便捷开关：往 flags 里加 `"ai_review": true`。 |

示例：

```bash
# 紧急书，AI 预筛章节，跳过人工 review
poetry run epubctl submit ~/books/a.epub \
    --priority 10 --ai-select --no-review

# 同一本书，用 JSON 完全控制
poetry run epubctl submit ~/books/a.epub --flags '{
  "ai_select": true,
  "no_review": true,
  "concurrency": 2,
  "block_size": 4,
  "target_language": "English"
}'

# 多本书排队
for f in ~/books/*.epub; do
    poetry run epubctl submit "$f" --priority 1
done
```

### 查看队列

```bash
poetry run epubctl status          # 全部任务，最新在前
poetry run epubctl status --status PROCESSING
poetry run epubctl show 3          # 任务 3 的完整 JSON
poetry run epubctl show 3 --meta   # 同时打印 meta.json
```

`status` 输出定宽表：

```
id    status      pri  file                     tokens       comments  age
3     SUCCESS     0    catcher-in-the-rye.epub  12345/8765   42        2h
2     PROCESSING  5    little-prince.epub       4321/2100    12        18m
1     PAUSED      0    pride-and-prejudice.epub 0/0          0         4m
--------------------------------------------------------------------
depths: PENDING=0, PROCESSING=1, SUCCESS=1, PAUSED=1, FAILED=0, CANCELLED=0
```

`show <id>` 把整行打成 JSON（含 flags、tokens、错误信息、时间戳）。加
`--meta` 还会打印 `meta.json`（`CommentorResult` 快照），方便事后审计。

### 实时刷新

```bash
poetry run epubctl watch --interval 2
```

清屏后每隔 N 秒重渲 `status` 表。Ctrl-C 退出。

### 查看日志

```bash
poetry run epubctl log 3 --tail 200       # 最后 200 行
poetry run epubctl log 3 --follow         # 类似 `tail -f`
poetry run epubctl log 3 --follow --interval 1
```

日志来自 `<workspace>/jobs/job_<id>/logs/*.log`——就是 `comment_epub()` 流水线自己写的文件。任务首次运行后能看到每次 LLM 请求一个文件，每个都包含 prompt、原始响应、以及 `[[StageError]]` / `[[FinalError]]` 标记。

### 生命周期事件

```bash
poetry run epubctl events 3 --limit 50
```

打印单个任务的审计流水：

```
2026-07-03 10:00:01  enqueued
2026-07-03 10:00:03  started
2026-07-03 10:42:11  finished — /home/you/daemon/jobs/job_3/output.commented.epub
```

`failed`、`cancelled`、`paused`、`resumed`、`restarted`、`disk_low`、
`disk_recovered` 也会出现在这里。事后复盘某个奇怪任务时这是第一站。

### 优先级、暂停、取消

```bash
poetry run epubctl priority 3 10         # 把任务 3 优先级提到 10
poetry run epubctl cancel  3             # 协作式取消（下一章节生效）
poetry run epubctl retry   3             # FAILED → PENDING（retry_count++）
poetry run epubctl resume  3             # PAUSED → PENDING
poetry run epubctl pause-all --reason "operator away"
poetry run epubctl resume-all
```

`cancel` 不杀 worker——它在 SQLite 里写一个控制标志，worker 在章节之间读取。当前章节跑完，对应行变成 `CANCELLED`，下一个任务开始。避免 cache / log 处于不一致状态。

对 `FAILED` 任务 `retry` 会递增 `retry_count`；若预算已用尽则命令大声报错。

### 健康状态、恢复、清理

```bash
poetry run epubctl health     # 队列深度 + 最近一次 server_stats 行
poetry run epubctl recover    # 手动触发崩溃恢复
poetry run epubctl prune      # 删旧的 SUCCESS/FAILED/CANCELLED
poetry run epubctl prune --success --force   # 不再交互确认
poetry run epubctl prune --failed --cancelled
```

`prune` 既删数据库行（通过 FK cascade 顺带删事件），也删 `jobs/job_<id>/`
workspace。除非传 `--force`，否则每条都要确认。默认保留 SUCCESS（你大概率
还要那个 EPUB）；用状态旗标精确控制清哪一类。

---

## 每任务旗标 (`--flags-json`)

`format.json` 里的每个 key，加上每个 `CommentConfig` 字段，都可以按任务覆盖。传一个 JSON 对象：

```bash
poetry run epubctl submit ~/books/long.epub --flags '{
  "ai_select": true,
  "no_review": true,
  "concurrency": 2,
  "block_size": 8,
  "target_language": "English",
  "book_synopsis": "A philosophical fairy tale.",
  "cache_seed_user_id": "long-book-v1",
  "fail_on_empty_chapter": false
}'
```

**不被支持的旗标会大声失败**，而不是默默忽略：

- `--interactive`（`-i`）—— 需要 TTY，守护进程没有。
- `--review`（交互式）—— 同理。

这些在提交时就被拒绝，`error_stage=flag`，让你在浪费一次 LLM 调用之前就知道。

未知 key 被忽略并打警告，与 CLI 对 `format.json` 拼错时的行为一致。

---

## 任务生命周期与状态机

```
              ┌────── daemon_restart ────┐
              │                         │
              ▼                         │
         PENDING ──worker picks──► PROCESSING ──success──► SUCCESS ──► (cleanup cache/)
            ▲                          │
            │                          ├── error ─► FAILED ──retryable──► PENDING (retry_count++)
            │                          │            └─non-retry──► FAILED
            │                          ├── abort ──────► CANCELLED
            │                          └── disk_low ───► PAUSED
            │                                              ▲
            └── resume (手动或自动) ──────────────────────┘
```

六种状态：

| 状态 | 含义 |
|---|---|
| `PENDING` | 已入队，等 worker 拉取。 |
| `PROCESSING` | worker 正在跑 `comment_epub()`。 |
| `SUCCESS` | 输出 EPUB 已写入。缓存已删，日志已打包。 |
| `FAILED` | 抛了异常。最多重试 `max_retries` 次。 |
| `PAUSED` | 被磁盘熔断或操作员暂停。手动恢复或磁盘恢复时自动恢复。 |
| `CANCELLED` | 操作员主动取消。终态——不再自动重试。 |

三种是**终态**（`SUCCESS`、`FAILED`、`CANCELLED`）；其余三种循环。

---

## 崩溃恢复

守护进程在任务中途被杀（OOM kill、机器重启、systemd 重启），行就会留在 `PROCESSING`。下次启动时守护进程跑 `recover_crashed_jobs`，做两遍：

1. **陈旧行** —— `PROCESSING` 任务且 `started_at` 超过 1 小时的，升格为 `FAILED`，`error_stage=timeout`。真正孤儿任务不会无限循环。
2. **新鲜行** —— `PROCESSING` 任务且 `started_at` 不超过 1 小时的，重置为 `PENDING`，`error_stage=daemon_restart`，写入 `restarted` 事件供审计。下次 worker 直接拉起来。

每任务的 workspace 已经包含 `cache/` 和 `logs/`，所以重试从该 worker 自己文件的干净状态开始（校验失败已通过 `LLMContext.discard_last` 把污染条目驱逐）。

如果守护进程本身有问题但又不想重启，也可以手动触发恢复：

```bash
poetry run epubctl recover
```

---

## 磁盘熔断

Worker 每次迭代都用 `shutil.disk_usage(<workspace_dir>)` 检查一次（忙时几秒一次，闲置时按 `poll_interval_paused_seconds` 重查）。以下任一条件成立即熔断：

- `avail_gb < min_free_gb`（默认 2.0 GB）
- `used_percent > (100 - min_free_percent)`（默认 90%）

熔断后守护进程会：

1. 把所有非终态任务（`PENDING`、`PROCESSING`）批量暂停，写 `paused` 事件，detail 标 `bulk: disk_low`。
2. 停止拉取新任务。
3. 睡眠 + 每 `poll_interval_paused_seconds` 重查。

磁盘恢复时熔断器检测到边沿，批量恢复所有暂停任务，记
`disk_recovered — resumed N paused job(s)`。

这是守护进程抵御云盘 `Errno 28` 的主防线。阈值按磁盘大小调——30 GB 的 VM
留 2 GB 够了；200 GB 的盒子可以放宽到 5 GB，给日志和其他租户留余地。

---

## 鲁棒性 —— 出问题时会怎样

| 场景 | 守护进程的处理 |
|---|---|
| 进程被杀 | `systemd`/`docker` 重启 → `recover_crashed_jobs` 复位 `PROCESSING` 行。 |
| 同 workspace 启动两个守护进程 | 第二个抢不到 `<workspace>/daemon.lock` 直接退出。SQLite 永远不会被双重打开。 |
| `$EPUB_COMMENTOR_API_KEY` 缺失 | 第一个任务落入 `FAILED`，`error_stage=api_key`。守护进程继续运行——补上 env 后 `epubctl retry`。 |
| 任务途中磁盘写满 | `OSError: [Errno 28]` 冒泡为 `FAILED`，`error_stage=disk_full`。熔断器随后暂停其他任务。 |
| OOM（内核杀 worker） | 与守护进程崩溃同——重启时 `recover_crashed_jobs` 复位。 |
| 守护进程所在终端按 Ctrl-C | SIGINT → `request_abort()` → 在飞任务抛 `CommentAbortError` → 行变 `CANCELLED`。守护进程干净退出。 |
| 任务中途 `epubctl cancel <id>` | control_signals 表插一行 → worker 下一轮读到 → 与 Ctrl-C 同路径。 |
| 缓存里有污染条目 | 流水线的 `LLMContext.discard_last` 在校验失败时驱逐；守护进程也在 `FAILED` 时清空该任务的 `cache/`，下次重试从干净开始。 |
| 日志把磁盘塞满 | `SUCCESS` 把所有 `logs/*.log` 打成 `logs/archive.tar.gz`，删原文件。 |
| 崩溃后留下陈旧 `PROCESSING` | `recover_crashed_jobs` 的 1 小时规则升格为 `FAILED`（`error_stage=timeout`），不无限循环。 |

---

## 部署守护进程

### systemd 单元（Linux）

最小化单元，丢到 `/etc/systemd/system/`：

```ini
[Unit]
Description=EPUB Commentor Daemon
After=network-online.target

[Service]
Type=simple
User=you
WorkingDirectory=/home/you
Environment="EPUB_COMMENTOR_API_KEY=sk-..."
ExecStart=/home/you/.local/bin/python -m epub_commentor.daemon --workspace /home/you/daemon
Restart=on-failure
RestartSec=30

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now epub-commentor
journalctl -u epub-commentor -f      # 实时跟踪
```

`Restart=on-failure` 加 `RestartSec=30` 覆盖崩溃；守护进程自己的 `recover_crashed_jobs` 保证不会有任务卡在 `PROCESSING`。

### Docker / 容器

守护进程就是单个阻塞的 Python 进程——按 CLI 那样塞进容器即可：

```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir .

ENV EPUB_COMMENTOR_API_KEY=""
VOLUME ["/daemon"]
CMD ["python", "-m", "epub_commentor.daemon", "--workspace", "/daemon"]
```

```bash
docker build -t epub-daemon .
docker run -d \
    --name epub-daemon \
    -e EPUB_COMMENTOR_API_KEY=sk-... \
    -v /srv/daemon:/daemon \
    --restart unless-stopped \
    epub-daemon
```

把 `/daemon` 挂到宿主卷，确保 SQLite 和 `jobs/` 跨容器重启幸存。`epubctl`
可以在宿主机上跑同一个卷：

```bash
epubctl --db /srv/daemon/daemon.sqlite status
```

### 单实例保证

守护进程打开 `<workspace>/daemon.lock` 并尝试 `fcntl.flock(LOCK_EX |
LOCK_NB)`。若锁已被持有（另一个守护进程在跑），新进程立刻退出。Windows 上
`fcntl` 不可用，回退到 PID 检查——第二个守护进程仍然退出，但竞态窗口略宽。

含义：

- 任意终端随便跑 `epubctl`——它从不开锁。
- 一个 workspace 只能跑**一个**守护进程。
- 迁移到新机器前先停旧的。

### 优雅停止

守护进程为 `SIGINT`（Ctrl-C）和 `SIGTERM` 装信号处理器：

1. 置 shutdown 事件。
2. 调 `request_abort()`，让在飞的 LLM 调用抛 `CommentAbortError`。
3. 当前任务以 `CANCELLED` 收尾。
4. worker 退出循环。
5. 锁和 SQLite 连接被释放。
6. 退出码 `0`。

下次启动时 `recover_crashed_jobs` 仍跑——但若停机是优雅的，没什么需要恢复（取消任务已在 `CANCELLED`）。

---

## 故障排查

| 现象 | 可能原因 / 解决 |
|---|---|
| `format.daemon.json is not valid JSON` | 拼错（常是多了一个逗号）。用任意 JSON 校验器看一下。 |
| 守护进程退出并报 "could not open … daemon.lock" | 同 workspace 已有守护进程在跑。`epubctl status` 看一眼。 |
| 任务卡在 `PENDING` | worker 被暂停（`pause-all` 或磁盘熔断）或守护进程没在跑。看 `epubctl status` 与 `epubctl health`。 |
| 任务在 `PAUSED`，但没人主动暂停 | 磁盘熔断触发——腾出 workspace 卷的空间，守护进程确认安全后自动恢复。 |
| 首次任务 `error_stage=api_key` | `$EPUB_COMMENTOR_API_KEY` 没设，且 `format.json` 没 `key` 字段。补 env 后 `epubctl retry <id>`。 |
| `error_stage=flag: --review is not supported in the daemon` | 提交了带交互旗标的任务。守护进程无 TTY——改用 `--ai-review` 或 `--no-review`。 |
| 任务中途 `error_stage=disk_full` | 磁盘即便熔断还是满了。腾空间、调高 `min_free_gb`、`epubctl retry`。 |
| `epubctl` 报 "database not found at …" | 传 `--db`、设 `$EPUBCTL_DAEMON_DB`，或从 workspace 目录里跑。 |
| 守护进程自己重启了（任务从 `PROCESSING→PENDING`） | 这是正常的——`recover_crashed_jobs` 干的。`epubctl events <id>` 找 `restarted` 事件。 |
| 一个新任务 `error_stage=timeout` | 重启时 `PROCESSING > 1h` 触发——上次守护进程是被杀在任务中途的。看日志，需要的话 `epubctl retry`。 |
| worker 打 "ignoring unknown keys" | `format.json` 或 `--flags-json` 有拼错的键。改对再重启守护进程。 |

---

## 常见问题

**为什么没有 HTTP API？**
单用户 / 单主机场景不需要。`epubctl` 直接读 SQLite——更快、无鉴权面、无端口需要防护。如果你想跨机器驱动守护进程，SSH 跑 `epubctl` 即可，比部署 FastAPI + JWT 简单太多。

**为什么不像 Celery 那样每任务一个 subprocess？**
三条原因。(1) CLI 内部错误时调 `sys.exit(2)`，会把 worker 一起干掉。(2) LLM 限流器是进程内全局——多进程会各自计数、加起来打爆服务商配额。(3) 进程内崩溃通过 `recover_crashed_jobs` 恢复更直接。

**为什么没有邮件 / webhook 通知？**
初版不在范围内。`notification_command` 配置钩子提供了单次 shell 调用的能力——自己写 wrapper 分发到邮件 / Slack / webhook / 等等。

**为什么是单线程？**
长 EPUB 评注被上游 token/秒 限速，不是被 CPU 限速。再加 worker 只是抢同一个服务商配额。让守护进程保持简单；如果真要并行，跑两个守护进程用不同 workspace（不同 API key）。

**为什么没有 watchdog / 文件投放式提交？**
容器部署不需要目录监控器——宿主机跑 `epubctl submit` 就行。如果真的想要自动提交，包一层 `inotifywait` + `epubctl submit` 即可。

**守护进程把日志写到哪里？**
stderr（通过项目根 logger）+ 每任务写到 `<workspace>/jobs/job_<id>/logs/`。
守护进程级日志级别由 `format.daemon.json` 的 `log_level` 控制；每任务日志目录由 worker 的 `log_dir_path` 决定（默认在任务的 `logs/` 下）。

**守护进程停了还能 `epubctl submit` 吗？**
能——`epubctl` 只写 SQLite。守护进程下次 poll 时把新行拉起来。

**多任务顺序跑比并行能省钱吗？**
不能；但也**没亏**——瓶颈是上游，不是守护进程。顺序跑的好处是每本书的 token 消耗可预测、失败恢复更简单。真的需要并行，部署多个守护进程（不同 workspace、不同 API key），适合单 key 不够用的场景。

**能不能把一个任务的 workspace 搬到另一台机器？**
能——`epubctl prune` 删行和目录，`cp -r jobs/job_<id>/input.epub` 把输入带走即可。没有内建 "export to remote"，因为守护进程本来就没有 remote 概念。

**守护进程会不会跨任务共用缓存？**
不会——每任务有独立的 `cache/` 目录。刻意如此：用旧的 `format.json` 跑过的陈旧缓存条目会污染新任务的运行。如果想共用缓存，把同一个 `cache_path` 写在 `format.json` 里，并放弃每任务 workspace——但牺牲隔离性。