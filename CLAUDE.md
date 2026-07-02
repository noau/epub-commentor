# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Unless specified, always reply user with Chinese.

## 项目概述

**EPUB Commentor** — Fork of [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator)，把"AI 翻译"改造为"AI 智能评注"。使用 LLM 为电子书添加 AI 生成的前置导读、后置总结、块级夹注，输出保留原文 + 注入评注的 EPUB 文件，可直接导入 Kindle 等墨水屏设备阅读。

- 完整 PRD：`plans/this-is-a-forked-encapsulated-seal.md`（亦在 `C:\Users\noau_\.claude\plans\this-is-a-forked-encapsulated-seal.md`）
- 原项目文档：https://github.com/oomol-lab/epub-translator
- Python ≥ 3.11，依赖管理使用 Poetry

> **进度：M1-M8 完成（包重命名 / 两阶段 LLM / 注入 / 校验 / 测试 / CLI / 进度条 + debug 日志 / 交互式章节选择），M7b 真实 LLM 联调待实施**。CLAUDE.md 中标注 `[已实现]` / `[待实施]` 的章节反映这一状态。

## 常用命令

```bash
# 安装依赖
poetry install

# 跑全部测试（项目根的 test.py 是 pytest 入口）
python test.py
# 等价于：pytest tests/ -v --tb=short

# 跑单个测试文件
poetry run pytest tests/test_xml_like.py -v

# 跑单个测试函数
poetry run pytest tests/test_xml_like.py::TestXMLLikeNode::test_method -v

# 跑全部 10 个评注 challenge 回归（驱动 MockLLM，零网络）
poetry run python scripts/comment_challenge.py

# CLI 评注主入口（API key 优先从 $EPUB_COMMENTOR_API_KEY 读，否则 cp format.template.json format.json）
poetry run python scripts/comment_epub.py path/to/source.epub --synopsis "..."
# 或安装后用 entrypoint：
poetry run epub-commentor path/to/source.epub

# 检测日志中重复 ID
poetry run python scripts/check_duplicate_ids.py

# 跑带进度条 + debug 日志的 CLI（--log-dir 落地 request *.log；--debug 自动默认 ./temp/logs/）
poetry run python scripts/comment_epub.py path/to/source.epub \
    --synopsis "..." --log-dir ./temp/logs --debug

# Lint / Format（ruff，line-length=120，py311）
poetry run ruff check epub_commentor tests
poetry run ruff format epub_commentor tests

# 类型检查
poetry run pyright epub_commentor
```

`format.json` 由用户从 `format.template.json` 复制并填入 URL / model / token_encoding 等；`key` 字段可省略，改用 env var（见下文）。`scripts/comment_epub.py` 与 `epub_commentor.cli` 都通过同一份 `format.json` 构造**单个** LLM（`LLM(**cfg)` 直接散开顶层字段）。

> **API key 解析顺序（十二要素风格）**：CLI / `scripts/utils.py:load_comment_llm` 在构造 LLM 前先调用 `epub_commentor.llm._api_key.resolve_api_key()` —— `$EPUB_COMMENTOR_API_KEY` 环境变量优先于 `format.json` 的 `key` 字段；两者都缺失或 `key` 是 `<PLACEHOLDER>` 时返回 `None`，CLI 层会 `sys.exit(2)` 给出清晰提示。`LLM.__init__` 的契约保持不变（`key` 显式必填），只是 CLI / scripts 这两条 loader 路径自动 resolve。

> Never call git commit or do any git changes. Only give suggestions. Suggest git logs in form: `<kind>(<scope>): <summary>` without body.

## 测试结构

- `test.py` — 项目根的 pytest 入口脚本
- `tests/utils.py` — `create_temp_dir_fixture(subdir_name)` 临时目录 fixture
- `tests/assets/` — 真实 EPUB 文件（Cambridge、DeepSeek OCR、《小王子》、《治疗精神病》）；《小王子》28 章结构清晰，最适合端到端测试
- `tests/commentary_challenge/case01..case10.json` — 10 个手工挑出的评注故障样例，由 `scripts/comment_challenge.py` 走 `MockLLM` 跑 `process_chapters` 回归
- `tests/_mock_llm.py` — `MockLLM` + `json_dumps`，按 cache seed 前缀（`:scan:` / `:annotate:`）分发预制 JSON；附带 `calls` 日志用于断言

**测试覆盖**（M1-M8 完成后，约 197 用例 / 60 subtests，约 194 passing — 历史遗留三处失败 `test_xml_like::test_header_with_whitespace_and_newlines` / `test_commentor_schema::test_minimum_outline_required` / `test_maximum_outline` 与本任务无关，pre-existing）：

- `test_xml_like.py` / `test_self_closing.py` / `test_metadata.py` / `test_spines.py` / `test_toc.py` / `test_math.py` — 原 EPUB / XML 基础
- `test_zip.py` — `Zip.add()` 注入新文件
- `test_commentor_extract.py` — `extract_chapters()` 读 spine + 解析 body
- `test_commentor_schema.py` — pydantic 模型 + 连续性 / 重叠校验
- `test_commentor_errors.py` — `CommentorError` 五子类语义
- `test_commentor_block.py` — Stage 2 切组、retry、空章节
- `test_commentor_inject.py` — aside 注入、parent_map、OPF manifest、head link
- `test_commentor_pipeline.py` — extract → process → inject 串行 + `ProgressEvent` 进度事件断言 + `TestChapterFilter`（mask 长度 / 内容校验 / 顺序）
- `test_commentor_log.py` — `[[StageError]]` / `[[FinalError]]` / `[[CacheCheck]]` 日志段断言（用 `MockLLM(log_dir_path=...)`）
- `test_commentor_css.py` — `commentary.css` 字节校验（color / shadow / `break-inside: avoid`）
- `test_commentor_e2e.py` — 拿真实《小王子》EPUB 端到端跑一遍
- `test_commentor_cli.py` — argparse / `format.json` 解析 / 进度回调 + `TestBuildChapterFilter`（`-i` 默认 / TTY / 非 TTY / 短旗标）

## 架构

整体流程：**EPUB 解压 → 按 spine 顺序逐章节解析 XML → [Stage 1 全章扫读 → LLM → ChapterMemo] → [Stage 2 按 6 段切块 → LLM → BlockAnnotation(JSON) × N 块] → DOM 注入 `<aside>` → 注入 commentary.css + 改 OPF + 改 chapter `<head>` → 写回 EPUB**。

### 顶层入口（`epub_commentor/__init__.py`）

公开 API（M1-M8 完成态）：

- LLM：`LLM`、`Message`、`MessageRole`
- 配置 / 数据类：`CommentConfig`、`CommentKind`、`CommentPosition`、`ChapterAnnotation`、`CommentorResult`
- 顶层编排：`comment_epub(source, output, llm, config, progress_callback, chapter_filter) -> CommentorResult`（阶段汇报顺序：`extract` → `process` → `inject`；`chapter_filter` 可选，详见下文）
- 进度回调：`ProgressCallback`（`Callable[[ProgressEvent], None]`）+ `ProgressEvent` dataclass + `make_default_progress_callback(quiet)` 工厂；CLI 默认装一个 `rich.progress.Progress` 渲染器，**单实例 + 两 TaskID** 垂直堆叠（chapter task 顶行 + block task 底行，每行独立 spinner + bar + M/N + ETA）
- 章节过滤回调：`ChapterFilter`（`Callable[[list[Chapter]], list[bool]]`）— 返回与 spine 顺序等长的 bool mask，`True` 保留、`False` 跳过；CLI 在 `-i`/`--interactive` 下弹 rich-selector 多选（`↑/↓` 移动、`Space/Enter` 切换、`A/I/C` 全选/反选/清空，移动到 `[ Confirm ]` 后回车提交，`Esc/Q` 取消）
- 提取 / 注入：`Chapter`、`extract_chapters`（pipeline 内）、`inject_annotations`（pipeline 内）
- 异常：`CommentorError(ValueError)` + 五个子类（`CommentInvalidJSONError`、`CommentOrphanPIdError`、`CommentOverlapError`、`CommentScanFailedError`、`CommentNoParagraphsError`）

### 子包职责

- **`epub_commentor/commentor.py`** + **`cli.py`**（[已实现]）— 顶层 `comment_epub()` 串联 extract → process → inject；CLI 走 argparse，所有旗标 1:1 映射 `CommentConfig` 字段

- **`epub_commentor/config.py`**（[已实现]）— `CommentConfig` 数据类（13 字段：`position` / `kinds` / `block_size` / `max_json_retries` / `max_scan_retries` / `concurrency` / `cache_seed_user_id` / `book_synopsis` / `inject_css` / `css_path_in_epub` / `target_language` / `fail_on_empty_chapter`）

- **`epub_commentor/errors.py`**（[已实现]）— `CommentorError(ValueError)` 基类 + 5 子类，全部继承自 `ValueError`

- **`epub_commentor/llm/`**（[已实现]）
  - `_debug_logger.py:make_request_logger` — per-request FileHandler 工厂（共享给生产 `LLM` 和 `MockLLM`），按 UTC 秒级时间戳命名,秒内冲突自动 `_2/_3/...` 后缀
  - `_api_key.py:resolve_api_key` — API key 解析：`$EPUB_COMMENTOR_API_KEY` env var 优先于 `format.json.key`，占位符 `<...>` 与空串视为缺失；CLI 与 `scripts/utils.py:load_comment_llm` 在构造 `LLM` 前调用
  - `core.py:LLM` — 持有 `tiktoken` 编码、Jinja 模板环境、缓存/日志目录、token 统计；`_create_logger` 委托给 `_debug_logger.make_request_logger`
  - `context.py:LLMContext` — 上下文管理器：计算请求哈希 → 命中缓存则返回 → 否则走 executor → 临时文件 → 退出时 commit（多线程下用 `_CACHE_COMMIT_LOCK` 串行化）；`__enter__` 创建 per-context logger（跨 retry 累积进同一文件），暴露 `ctx.logger` 给 Stage 1/2 写错误段；命中/未命中 cache 时打 `[[CacheCheck]]`
  - `executor.py:LLMExecutor` — 流式调用 OpenAI SDK，按 `error.is_retry_error()` 重试 `retry_times` 次，间隔 `retry_interval_seconds`，把每 chunk 的 `usage` 累加进 `Statistics`；`request(logger=...)` 接收外部 logger 并写 `[[Parameters]] / [[Request]] / [[Response]] / [[Error]]` 段
  - `statistics.py:Statistics` — 线程安全的 token 计数（`total_tokens` / `input_tokens` / `input_cache_tokens` / `output_tokens`）
  - `increasable.py:Increasable` / `Increaser` — 每次请求后用 `0.5*(end-begin)` 衰减，让 `temperature` / `top_p` 可作为 `(start, end)` 范围传入
  - `error.py` — 判定 OpenAI / httpx / requests 中的可重试错误（timeout、5xx、网络）
  - `types.py` — `Message` dataclass + `MessageRole` enum（SYSTEM / USER / ASSISTANT）
  - `protocol.py:LLMProtocol` / `ContextProtocol` — 结构化 typing，便于 mock
  - `schema.py`（[已实现]）— pydantic 模型（`KeyTerm` / `ChapterMemo` / `CommentPosition` / `CommentKind` / `CommentItem` / `BlockAnnotation`）+ `validate_block_annotations()` 检查 p_id 连续性 / 范围 / 块内重叠
  - `memo.py`（[已实现]）— Stage 1 全章扫读封装（拼 system + user 消息、走 `LLMContext`、pydantic 校验 → `ChapterMemo`；校验失败抛 `CommentScanFailedError` 并写 `[[StageError]]`）
  - `block.py`（[已实现]）— Stage 2 块级批注封装（切组 + 打 `data-p-id` + multi-turn retry 循环 + `finally` 块清除 `data-p-id`；穷尽 `max_json_retries` 抛 `CommentInvalidJSONError`，每次 retry 写 `[[StageError]]` / 最终 `[[FinalError]]`）

- **`epub_commentor/progress.py`**（[已实现]）— 进度渲染器（rich 后端）：`ProgressEvent` dataclass + `RichProgressDisplay`（单 `Progress` 实例 + `chapter_task` + `block_task` 两 `TaskID`，列：`SpinnerColumn · TextColumn · BarColumn · MofNCompleteColumn · TimeRemainingColumn`，`transient=False` 停在 100%）+ `_NoOpDisplay`（quiet 或非 TTY）+ `make_default_progress_callback(quiet)` 工厂；底层依赖 `rich.progress`

- **`epub_commentor/pipeline/`**（[已实现]）
  - `extract.py` — 提取层：`Zip.open` → 读 spine → 逐章 `XMLLikeNode.parse` → 拿到 `body_element` + 扁平化 `dict[str, str]` metadata（含保留键 `__opf_path__` 留给 inject 用）+ 每章 `Chapter`
  - `process.py` — 处理层：Stage 1（`scan_chapter()`，**章间串行**）+ Stage 2（`annotate_block()`，**章内 `ThreadPoolExecutor` 并行**），返回 `list[ChapterAnnotation]`（`comments` 已按 `target_p_ids[0]` 排序，并把 block-local p_id 平移到绝对索引）；可选 `progress_callback` 参数，emit `ProgressEvent(stage="process", substage="scan"|"annotate", current, total, message)` 替代原本的 6 处原始 `print()`
  - `inject.py` — 注入层：构造 `aside`（`commentary commentary-{kind}` 类、`cmt-...` id）→ 沿父链向上找 body 直接子元素 → 清除 `data-p-id` → `deduplicate_ids_in_element` → chapter `<head>` 注入 `<link rel="stylesheet">` → OPF `<manifest>` 加 `<item id="commentary-css">` → `Zip.add()` 注入 CSS → `zip.replace()` 写回每个章节

- **`epub_commentor/epub/`**（[已实现] 全部 6 文件）
  - `zip.py:Zip` — 同时持有源/目标两个 `ZipFile`，`migrate()` / `replace()` 走目标，`add()` 注入新文件；`__exit__` 自动 migrate 未处理的源文件
  - `common.py` — `find_opf_path`（META-INF/container.xml → rootfile）
  - `spines.py` — 按 OPF manifest+spine 顺序产出 `(chapter_path, media_type)`
  - `toc.py` — 识别 EPUB 2.0（NCX）与 3.0（nav），统一成 `Toc` dataclass
  - `metadata.py` — 读取 `<metadata>` 子元素，跳过 `SKIP_FIELDS = {language, identifier, date, meta, contributor}`
  - `math.py` — `xml_to_latex`：把 `<math>` MathML 转 LaTeX

- **`epub_commentor/xml/`**（[已实现] 6 文件）
  - `xml_like.py:XMLLikeNode` — 解析时记录 namespace → 序列化时还原；HTML 风格（`text/html`）的 void 元素在写出时去掉自闭合；自动探测 encoding（UTF-8 BOM / UTF-16 / XML declaration / 兜底 latin-1）
  - `deduplication.py:deduplicate_ids_in_element` — 评注元素注入后调一次，避免与原书 id 冲突
  - `xml.py:find_first`, `iter_with_stack`, `plain_text` — DOM 辅助函数
  - `utils.py` — `concat_attribute_values`, `iter_without_parents` 等
  - `self_closing.py` — `<br>` / `<hr>` / `<img>` 等 void 元素自闭合往返
  - `const.py` — `ID_KEY="id"`, `DISPLAY_ATTRIBUTE="display"`

- **`epub_commentor/template.py`** — `create_env()` 构造 Jinja `Environment`，自定义 `_DSLoader` 只从 `epub_commentor/data/` 目录读 `.jinja` 文件

- **`epub_commentor/utils.py`** — `normalize_whitespace`, `is_the_same` 等通用工具函数

### 提示词模板（`epub_commentor/data/`）

- `scan.jinja`（[已实现]）— Stage 1 全章扫读：输出 JSON `{core_thesis, outline(3..7), key_terms, tone, target_audience, reading_anchors}`
- `annotate.jinja`（[已实现]）— Stage 2 块级批注：输出 JSON `{comments: [{target_p_ids, position, kind, content}, ...]}`，约束 `target_p_ids` 连续、`intro` / `summary` / `note` 三种 kind、position `before` / `after`
- `commentary.css`（[已实现]）— e-ink 优化样式（border + 灰度，无 `color` / `box-shadow`），按 `commentary commentary-{kind}` 三类切换；详见 `tests/test_commentor_css.py`

### 脚本（`scripts/`）

- `comment_epub.py`（[已实现]）— CLI 入口：thin wrapper 调 `epub_commentor.cli.main`，纯 `! /usr/bin/env python3` + `sys.path` 注入，方便从 repo checkout 直接跑
- `comment_challenge.py`（[已实现]）— 评注 challenge 回归：遍历 `tests/commentary_challenge/case*.json`，按 case 构造 `Chapter` / `MockLLM` / `CommentConfig`，跑 `process_chapters`，对比 `expected_comments` 或期待抛 `expected_error`，exit code = 0/1
- `check_duplicate_ids.py`（[已实现]）— 扫描 `temp/logs/*.log`，定位重复 ID
- `utils.py:load_comment_llm()` — 读 `format.json` 构造**单个** LLM

### Annotation Philosophy

EPUB Commentor 把 AI 评注定位为**页边的陪伴者**，而不是居高临下的解释者。整套 prompt / schema / CSS 都是这一哲学的具体落地：

- **Chapter memo 是 Stage 2 的私有工作笔记**，**永不**出现在最终 EPUB 中。读者看不到 memo，模型也不应在 `content` 里引用或复述它。Stage 2 只是在 memo 的隐性指导下生成评注。
- **三种 `kind` 各有语气**：`intro` 像主持上场、`summary` 像后排朋友的口吻收束、`note` 像页边的铅笔印——一组并置的小声音，不是单一独白。
- **温和密度规则**：`annotate.jinja` 的 Rule #5.6 鼓励大多数 block 用满三种 kind，但**不**做硬约束。空 block 罕见但允许存在；只有 `note` 的 block 常常意味着缺 `intro` 或 `summary`。
- **CSS 把三种 kind 编码为"安静的视觉层级"**：`intro` 细虚线左缘（最轻）、`note` 中等实线+更深缩进（铅笔印）、`summary` 较粗实线（明确收束）。没有任何一边框，评注读起来不像"框"——更像读者身边的伴读印记。
- **`<aside class="commentary commentary-{kind}">`** 输出的三段式（intro / summary / note）对应古书"夹注 / 回末总评 / 行间小字"的传统。

### Schema 中的私有 hint 字段

`ChapterMemo`（`epub_commentor/llm/schema.py`）除了 6 个读者面向字段外，新增 3 个**可选 list[str]** 字段供 Stage 2 内部使用：

- `motifs` — 本章反复出现的图像 / 符号 / 概念，0-8 条
- `foreshadowing` — 本章早段埋伏、后段兑现的 hook，0-5 条
- `interpretive_warnings` — 粗心读者可能误读作者意图的地方，0-5 条

这三个字段在 `epub_commentor/llm/block.py:_format_private_memo_context` 中拼成 Stage 2 user 消息的一段"Internal context (private — never cite, never echo)"。它们**不会**出现在最终 EPUB 中，pydantic 用 `default_factory=list` 兼容旧 JSON。

## 单 LLM 架构

**只用一个 LLM**（不再需要 translation / fill 双 LLM）。原因：评注任务的输出（JSON）结构化程度高，单一温度（如 0.4）足以兼顾"有文采"和"结构稳定"。

`format.template.json` 顶层字段（扁平）：`key`, `url`, `model`, `token_encoding`, `timeout`, `retry_times`, `retry_interval_seconds`, `temperature`, `top_p`（不再有 `translation` / `fill` 子字典）。

## 关键设计要点

- **两阶段 LLM** — Stage 1 输出 `ChapterMemo`（章级概述），Stage 2 把 memo + 6 段块 HTML 一起喂给 LLM 产出 JSON 评注。`cache_seed_content` 命名空间：`f"commentor:{VERSION}:{stage}:{user_id}:{chapter_hash}[:{block_hash}]"`，确保换 prompt / 换用户 / 换章节 / 换块各自独立缓存。

- **段落临时 id** — Stage 2 切组时给块内每个 `<p>` 加 `data-p-id="0..N"`，LLM 只能引用批内局部索引；`annotate_block` 在 `finally` 里清一次，注入层再防御性 strip 一次。`cmt-...` 评注 id 命名空间避免与原书 id 冲突；`inject_chapter` 末尾调 `deduplicate_ids_in_element`。

- **进度回调契约 (`ProgressEvent`)** — `Callable[[ProgressEvent], None]`；`stage ∈ {"extract", "process", "inject"}`、`substage ∈ {"scan", "annotate"}`（仅 process 阶段）；`current`/`total` 表示该 stage 内的进度。`commentor.py` 在三个 stage 起止各打一次（6 次），`process.py` 在每个 chapter + 每个 block 完成时打。CLI 通过 `make_default_progress_callback(quiet=False)` 装一个 `AliveProgressDisplay` 单条 bar：主计数为 chapter（每次 `process/scan` 推进一次），`bar.text` 动态切换为 `Ch. X/N: title (scan)` 或 `Ch. X/N: title (block Y/M)`；`quiet=True` 或 stderr 非 TTY 装 `_NoOpDisplay`（避免在 pipe / redirect 时输出转义码）。CLI 在 `try/finally` 末尾调 `progress_callback.__self__.close()` 让 alive_bar 上下文优雅退出。回调抛异常被吞咽并 `_logger.warning`，不阻塞流水线。

- **Debug 日志 (`log_dir_path` + `[[Section]]` 段)** — `LLM(log_dir_path=...)` 或 CLI `--log-dir PATH` / `--debug`（默认 `./temp/logs/`）启用。每个 `LLMContext` 在 `__enter__` 创建一份 `request <UTC 时间戳>.log`，跨 retry 累积。每份文件包含的段：`[[Parameters]] / [[Request]] / [[Response]] / [[Error]]`（executor 写）、`[[CacheCheck]] cache_key=<前缀>; hit=<bool>`（context 写）、`[[StageError]] stage=<scan|annotate>; attempt=N/M; error=...; Raw excerpt: <前 400 字符>`（block/memo 写）、`[[FinalError]] stage=...; attempts_exhausted=true; exception=...`（block 写）。`tests/_mock_llm.MockLLM(log_dir_path=...)` 用同一份 `make_request_logger` 工厂，让单元测试也能断言日志段而无需真实 LLM。

- **JSON 输出 + pydantic 校验 + multi-turn retry** — 评注不要求原文结构保持，所以用 JSON（比 XML 块更易校验）。校验失败 → 把原始响应 + 错误信息拼成 user 第 2 条消息做多轮 retry，最多 `config.max_json_retries` 次（默认 3）。Stage 1 校验失败抛 `CommentScanFailedError`，Stage 2 抛 `CommentInvalidJSONError`；块内 p_id 范围 / 连续性 / 重叠由 `validate_block_annotations()` 在模型层之外再走一遍。

- **`<aside class="commentary commentary-{kind}">`** — 三种 kind 块级评注：intro（前置导读）、summary（后置总结）、note（块级夹注）。`<p>` 嵌套在 `<blockquote>` 等容器中时，`inject_comment()` 沿父链向上找最近的 body 直接子元素。

- **`Zip.add()`** — 用于注入 `Styles/commentary.css`。`writestr(arcname, bytes, compress_type=ZIP_DEFLATED)`，不复制 source 中已有的同名文件。

- **命名空间往返** — `XMLLikeNode` 解析时记录 namespace URI，序列化时按映射还原。新插入的 `<aside>` 是无 namespace 的纯 HTML，不影响 OPF 的 `dc:` / `opf:` / `epub:` 前缀恢复。`_STANDARD_HTML_ATTRS` 修 `<link epub:type>` 和 `<link type>` 同名属性冲突的 bug。

- **CSS 自动集成（幂等）** — 三步：① `Zip.add(css_path_in_epub, bytes)` ② 改 OPF `<manifest>` 加 `<item id="commentary-css" ...>`（已存在则跳过）③ 每个 chapter `<head>` 加 `<link rel="stylesheet" type="text/css" href="...">`（已存在则跳过）。CSS 针对 e-ink 优化（用 border + 灰度对比，不用彩色和阴影）。

- **单章无 `<p>` 容错** — 检测到 `len(body.iter("p")) == 0` → 默认 `log warning` + 返回带占位 memo 的空 `ChapterAnnotation`（被 `comment_epub` 计入 `chapters_skipped`）；设 `config.fail_on_empty_chapter=True` → 抛 `CommentNoParagraphsError`。

- **章内并行 / 章间串行** — Stage 1 整章一次 LLM 调用，本身没有并行收益；Stage 2 块独立，`ThreadPoolExecutor(max_workers=config.concurrency)` 在章内并发，跨章维持顺序避免 Stage 1 / Stage 2 race。

- **缓存语义** — `LLMContext` 把 `messages + cache_seed` 算 SHA512 写临时文件，退出时 commit；多线程下用全局 `_CACHE_COMMIT_LOCK` 避免竞争。

## 修改指引

- 改 `data/scan.jinja` / `annotate.jinja` 后，跑 `scripts/comment_challenge.py` 确认 `tests/commentary_challenge/case*.json` 全部通过
- 改 `pipeline/inject.py` 同步看 `tests/test_commentor_inject.py` 和 `test_xml_like.py`（DOM 操作和 namespace 还原）
- 改 `pipeline/process.py` 的切组逻辑要跑 `tests/test_commentor_block.py`、`tests/test_commentor_pipeline.py`（边界 case：最后一批短、单章无 `<p>`、`<p>` 嵌套、retry）
- 改 `llm/schema.py` 的 pydantic 模型要跑 `tests/test_commentor_schema.py`（连续性、重叠、范围检查）
- 改 namespace 处理（`xml/xml_like.py`）要跑 `tests/test_xml_like.py`、`test_self_closing.py`
- 新增 LLM provider 走 `llm/executor.py:LLMExecutor`（默认就是 OpenAI 兼容，无需改 SDK）；其他类型错误在 `llm/error.py` 加判定
- 改 `epub/zip.py` 要保证 mimetype 是 ZIP 的第一个 entry（在 `comment_epub()` 主循环里显式 `migrate`）
- 改 CLI 旗标时同步更新 `tests/test_commentor_cli.py`（`test_minimal_invocation`、`test_all_flags`）
- 改 `--interactive` / `ChapterFilter` 行为时同步更新 `tests/test_commentor_cli.py::TestBuildChapterFilter`（`-i` / TTY / 非 TTY 矩阵）与 `tests/test_commentor_pipeline.py::TestChapterFilter`（mask 长度 / 内容 / 顺序）；`mask` 校验失败抛 `ValueError`（不是 `CommentorError`），属程序员错误
- 改 `data/commentary.css` 时，本地跑 `pytest tests/test_commentor_css.py` 验证 CSS 字节被正确写入且符合 e-ink 约束（无 color / box-shadow，`break-inside: avoid` 必备）
- 接新 LLM 测试时，先用 `tests/_mock_llm.MockLLM` 走 `process_chapters` 验证流水线，再用真 LLM 跑 `comment_challenge.py`

## 实施里程碑

| 阶段 | 状态 | 产出 |
|---|---|---|
| M1 重构 | ✅ 完成 | 包改名 + 删除翻译代码 + `Zip.add()` + import 修复 |
| M2 数据流 | ✅ 完成 | `pipeline/extract.py` + `pipeline/process.py`（Stage 1+2 跑通） + `scan.jinja` + `annotate.jinja` + `llm/memo.py` + `llm/block.py` |
| M3 注入 | ✅ 完成 | `pipeline/inject.py` + `commentary.css` + OPF 更新 + chapter head link |
| M4 校验 | ✅ 完成 | `llm/schema.py` + `errors.py` |
| M5 测试 | ✅ 完成 | `tests/_mock_llm.py` + 7 个单元测试 + 10 个 challenge case + `scripts/comment_challenge.py` |
| M6 CLI | ✅ 完成 | `scripts/comment_epub.py` + `commentor.py` + `cli.py` + entrypoint |
| M7a 进度条 + Debug 日志 | ✅ 完成 | `epub_commentor/progress.py` (`ProgressEvent` / `RichProgressDisplay` 单 Progress + 两 TaskID 堆叠，依赖 `rich.progress`) + `epub_commentor/llm/_debug_logger.py` + `[[CacheCheck]] / [[StageError]] / [[FinalError]]` 段 + `--log-dir` / `--debug` CLI 旗标 + `MockLLM(log_dir_path=...)` + `tests/test_commentor_log.py` |
| M7b 真实 LLM 联调 | ⏳ 待实施 | 用 OpenAI 跑《The little prince》全本，验证样式、缓存、token 用量、Kindle 兼容性 |
| M8 交互式章节选择 | ✅ 完成 | `ChapterFilter` callback (`Callable[[list[Chapter]], list[bool]]`) + `comment_epub(chapter_filter=...)` kwarg + `--interactive` / `-i` CLI 旗标（rich-selector 多选，空章节默认不勾选，非 TTY 直接 `exit 2`，`Esc/Q/Ctrl-C` → `exit 130`）+ 进度条在 `-i` 下自动静默 |

详细 PRD 见 `plans/this-is-a-forked-encapsulated-seal.md`。
