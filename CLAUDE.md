# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Unless specified, always reply user with Chinese.

## 项目概述

**EPUB Commentor** — Fork of [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator),把 "AI 翻译" 改造为 "AI 智能评注"。使用 LLM 为电子书添加 AI 生成的前置导读、后置总结、块级夹注,输出保留原文 + 注入评注的 EPUB 文件,可直接导入 Kindle 等墨水屏设备阅读。

- 用户向文档:[README.md](README.md) / [README_zh-CN.md](README_zh-CN.md)(安装、CLI flag、provider 表、FAQ)
- 原项目文档:<https://github.com/oomol-lab/epub-translator>
- Python `>=3.13,<3.14`,依赖管理使用 Poetry
- 进度:M1–M9 完成(包重命名 / 两阶段 LLM / 注入 / 校验 / 测试 / CLI / 进度条 + debug 日志 / 交互式章节选择 / AI 批处理闸口),M7b 真实 LLM 联调 ⏳

## 常用命令

```bash
# 安装依赖
poetry install

# 跑全部测试(项目根的 test.py 是 pytest 入口)
poetry run python test.py
# 等价于:pytest tests/ -v --tb=short

# 跑单个测试文件 / 测试函数
poetry run pytest tests/test_commentor_block.py -v
poetry run pytest tests/test_commentor_block.py::TestX::test_y -v

# 跑全部 13 个评注 challenge 回归(驱动 MockLLM,零网络)
poetry run python scripts/comment_challenge.py

# Lint / Format(ruff line-length=120, target py313)
poetry run ruff check epub_commentor tests
poetry run ruff format epub_commentor tests

# 类型检查
poetry run pyright epub_commentor
```

> **CI 不跑 ruff** — `.github/workflows/test.yml` 只跑 `pyright` + `pytest`,ruff 须本地手动执行。
> `poetry.toml` 配置腾讯 PyPI 镜像(`mirrors.cloud.tencent.com`),仅供国内加速。

> Never call git commit or do any git changes. Only give suggestions. Suggest git logs in form: `<kind>(<scope>): <summary>` without body.

## API key 解析

`epub_commentor/llm/_api_key.py:resolve_api_key` — `$EPUB_COMMENTOR_API_KEY` 环境变量优先于 `format.json.key`;两者都缺失或 `<PLACEHOLDER>` 时返回 `None`,CLI 层 `sys.exit(2)`。

## 架构 — pipeline 形状

整体流程:**EPUB 解压 → 按 spine 顺序逐章解析 XML → [Stage 1 全章扫读 → LLM → ChapterMemo] → [Stage 2 6 段切块 → LLM → BlockAnnotation × N 块] → DOM 注入 `<aside>` → 注入 commentary.css + 改 OPF + 改 chapter `<head>` → 写回 EPUB**。

- **三层流水线**:`extract → process → inject`,由 `comment_epub()` 编排,阶段汇报顺序固定。
- **两阶段 LLM**:Stage 1 章间串行(`scan_chapter()`),Stage 2 章内 `ThreadPoolExecutor` 并行(`annotate_block()`)。
- **三种 filter 闸口**: `ChapterFilter`(pre-select,`-i` / `--ai-select` / 不指定) + `AnnotationFilter`(post-review,`--review` / `--ai-review` / `--no-review` / 不指定)。两层都签名 `Callable[[list, dict[str, str]], list[bool]]`,第二个参数是已剥离 `__opf_path__` 的书级 metadata。

## 子包职责

不列举每个文件,只描述形状。具体模块从源码读。

- **`epub_commentor/` 根**: 公开 API(`__init__.py` 一站式 re-export)+ `CommentConfig`(`config.py`)+ `CommentorError` 异常层级(`errors.py`,8 个公共类,7 个 `CommentorError` 子类 + 1 个 `CommentAbortError(KeyboardInterrupt)`)+ 顶层 `comment_epub`(`commentor.py`)+ `progress.py`(3 个显示器)+ `logging_setup.py` 根 logger。
- **`epub_commentor/llm/`**: 单 `LLM` 客户端(`core.py`)+ 缓存/调试(`context.py` / `_debug_logger.py`)+ 限流/中止(`rate_limiter.py` / `_abort.py`)+ 两阶段编排(`memo.py` / `block.py`)+ AI 闸口(`select.py` / `review.py`)+ pydantic schema(`schema.py`)+ `_salvage.py` 部分成功恢复。
- **`epub_commentor/pipeline/`**: `extract.py`(spine + body 解析 + 三层 title 启发式)/ `process.py`(Stage 1+2 串并编排 + `process_chapters` 返回 `tuple[list[ChapterAnnotation], int]`)/ `inject.py`(aside 构造 + parent_map 找 body 直接子元素 + OPF/CSS 幂等注入 + 两阶段提交:先 CSS+OPF 后章节)。
- **`epub_commentor/epub/` + `epub_commentor/xml/`**: 底层 EPUB 操作(`zip.py` 同持源/目标两个 `ZipFile`,`add()` 注入新文件)+ 自研 XML 解析器(`xml_like.py` 记录 namespace 序列化还原 + `deduplication.py` 评注注入后调一次避免与原书 id 冲突)。

## 测试结构

`tests/_mock_llm.py` — 公共 `MockLLM` fixture,按 cache seed 前缀(`:scan:` / `:annotate:` / `:select:` / `:review:`)分发预制 JSON,带 `calls` 日志供断言;`log_dir_path` 参数让单元测试也能断言 `[[StageError]]` / `[[CacheCheck]]` 日志段。

26 个测试文件按 area 划分(不逐文件列举):

| Area | 覆盖 | 文件数 |
|---|---|---|
| 原 EPUB / XML / Math | 解析、namespace、自闭合、spine、toc、metadata、math | 6 |
| 流水线 + AI 闸口 + salvage / abort / cache / review | extract / schema / errors / block / inject / pipeline / select / ai_review / review / salvage / abort / cache | 12 |
| 日志 / 进度 / 限流 / API key | log / progress_noop / logging_setup / rate_limiter / api_key | 5 |
| CLI / CSS / E2E | CLI / CSS 字节校验 / 真实《小王子》E2E | 3 |

回归:`tests/commentary_challenge/case01..case13.json` — 13 个手工挑出的评注故障样例,由 `scripts/comment_challenge.py` 走 `MockLLM` 跑 `process_chapters` 回归。

## 评注哲学

AI 评注定位为**页边的陪伴者**,不是居高临下的解释者。三种 `kind` 各有语气:`intro` 像主持上场、`summary` 像后排朋友收束、`note` 像页边的铅笔印——一组并置的小声音,不是单一独白。CSS 把三种 kind 编码为"安静的视觉层级":`intro` 细虚线左缘(最轻)、`note` 中等实线+更深缩进(铅笔印)、`summary` 较粗实线(明确收束);e-ink 优化(用 border + 灰度,无 color / box-shadow)。`<aside class="commentary commentary-{kind}">` 对应古书"夹注 / 回末总评 / 行间小字"的传统。

## 两阶段 LLM

- **Stage 1** 全章扫读 → `ChapterMemo`(6 个读者面向字段 + 3 个私有 hint `motifs` / `foreshadowing` / `interpretive_warnings` 供 Stage 2 内部用)
- **Stage 2** 块级切组 + JSON 产出 → `BlockAnnotation` × N
- **cache key**:`f"commentor:{VERSION}:{stage}:{user_id}:{chapter_hash}[:{block_hash}]"`,换 prompt / 用户 / 章节 / 块各自独立
- **retry**:Stage 1 校验失败抛 `CommentScanFailedError`,Stage 2 抛 `CommentInvalidJSONError`;每次 retry 写 `[[StageError]]`,最终 `[[FinalError]]`
- **缓存失败回退**:`LLMContext.discard_last()` 在校验失败时丢弃被污染的 cache entry,写 `[[CacheEvict]]` 日志段

## AI 批处理闸口(M9)

- `--ai-select`(Stage 1 前预筛) / `--ai-review`(Stage 2 后复审) — 与 `-i` / `--review` / `--no-review` **互斥**
- 三层状态:手动(默认 TTY 弹 rich-selector) / AI(`--ai-*`) / 不指定
- 占位 memo(`(chapter skipped` 前缀)与 0 评论章节自动 `include=False` 且**免去 LLM 调用**
- 单次 LLM 调用覆盖全书(标题 + 段数 + 首段预览),失败抛 `CommentSelectFailedError` / `CommentReviewFailedError`
- `CommentorResult.ai_select_decisions` / `ai_review_decisions` 在摘要面板渲染 `✓ kept / ✗ dropped · <reason>`
- 4 个 `ai_*` `CommentConfig` 字段(`ai_select_min_body_chars` / `ai_review_min_comments_per_chapter` / `ai_select_max_retries` / `ai_review_max_retries`)通过 `_split_format_config` 自动从 `format.json` 路由,CLI 不额外暴露

## 单 LLM 架构

替换 fork 的 translation+fill 双 LLM。评注 JSON 结构化程度高,单一温度(0.4)兼顾"有文采"和"结构稳定"。`format.template.json` 顶层扁平字段(`key` / `url` / `model` / `token_encoding` / `timeout` / `retry_times` / `retry_interval_seconds` / `temperature` / `top_p` / `cache_path` / `log_dir_path` / `json_mode` / `rpm_limit` / `tpm_limit` / `request_concurrency` / `token_count_buffer`),详见 `README.md`。

## 修改指引

5 条 "when X, run Y" 模式:

- 改 `epub_commentor/data/*.jinja` → 跑 `scripts/comment_challenge.py`(13 case 全过)
- 改 `pipeline/inject.py` → 跑 `tests/test_commentor_inject.py` + `tests/test_xml_like.py`(DOM 操作 + namespace 还原)
- 改 `pipeline/process.py` 切组逻辑 → 跑 `tests/test_commentor_block.py` + `tests/test_commentor_pipeline.py`(边界 case:最后一批短、单章无 `<p>`、`<p>` 嵌套、retry)
- 改 `llm/schema.py` pydantic 模型 → 跑 `tests/test_commentor_schema.py`(连续性、重叠、范围检查)
- 改 `cli.py` flag / filter → 跑 `tests/test_commentor_cli.py::TestBuildChapterFilter` + `TestBuildAiChapterFilter` + `TestBuildAiAnnotationFilter`(互斥矩阵)
- 改 AI 闸口提示词 → 跑 `tests/test_commentor_select.py` + `tests/test_commentor_ai_review.py` HappyPath,再跑 `scripts/comment_challenge.py` 确认无回归

## 关键设计要点(摘要)

只列从源码不可恢复的几条:

- **段落临时 id**:Stage 2 切组时给块内每个 `<p>` 加 `data-p-id="0..N"`,`annotate_block` 在 `finally` 清一次,注入层再防御性 strip 一次
- **`<aside>` 注入**:`inject_comment()` 沿父链向上找 body 直接子元素(`<p>` 嵌套在 `<blockquote>` 等容器时)
- **CSS 自动集成幂等**:三步 ① `Zip.add(css_path_in_epub, bytes)` ② 改 OPF `<manifest>` 加 `<item id="commentary-css">`(已存在则跳过)③ 每章 `<head>` 加 `<link rel="stylesheet">`(已存在则跳过)
- **单章无 `<p>` 容错**:`len(body.iter("p")) == 0` → 默认 log warning + 返回占位 memo;`config.fail_on_empty_chapter=True` 抛 `CommentNoParagraphsError`
- **mimetype 顺序**:`Zip.__exit__` 确保 mimetype 是 ZIP 的第一个 entry