# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

> Unless specified, always reply user with Chinese.

## 项目概述

**EPUB Commentor** — Fork of [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator)，把"AI 翻译"改造为"AI 智能评注"。使用 LLM 为电子书添加 AI 生成的前置导读、后置总结、块级夹注，输出保留原文 + 注入评注的 EPUB 文件，可直接导入 Kindle 等墨水屏设备阅读。

- 完整 PRD：`plans/this-is-a-forked-encapsulated-seal.md`（亦在 `C:\Users\noau_\.claude\plans\this-is-a-forked-encapsulated-seal.md`）
- 原项目文档：https://github.com/oomol-lab/epub-translator
- Python ≥ 3.11，依赖管理使用 Poetry

> **⚠️ 当前进度：M1 完成（包重命名 + 删除翻译代码），M2-M7 待实施**。CLAUDE.md 中标注 `[已实现]`/`[待实施]` 的章节反映了这一状态。

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

# Lint / Format（ruff，line-length=120，py311）
poetry run ruff check epub_commentor tests
poetry run ruff format epub_commentor tests

# 类型检查
poetry run pyright epub_commentor

# [待实施] 命令行评注脚本（需配置 format.json，见下）
poetry run python scripts/comment_epub.py path/to/source.epub --synopsis "..."
# 或安装后直接用 entrypoint：
poetry run epub-commentor path/to/source.epub

# 检测日志中重复 ID
poetry run python scripts/check_duplicate_ids.py
```

`format.json` 由用户从 `format.template.json` 复制并填入 API key/URL 等。CLI 脚本通过 `scripts/utils.py` 中的 `load_comment_llm()` 读取该文件并构造**单个** LLM 实例。

## 测试结构

- `test.py` — 项目根的 pytest 入口脚本
- `tests/utils.py` — `create_temp_dir_fixture(subdir_name)` 临时目录 fixture
- `tests/assets/` — 真实 EPUB 文件（Cambridge、DeepSeek OCR、小王子、治疗精神病）；《小王子》28 章结构清晰，最适合端到端测试
- [待实施] `tests/commentary_challenge/` — 10 个手工挑出的评注故障样例，供 `scripts/comment_challenge.py` 回归测试
- [待实施] `tests/_mock_llm.py` — 测试替身，按预制 JSON 响应替换真实 LLM

**当前保留的测试**（M1 完成后）：
- `test_xml_like.py` — XMLLikeNode 编码保留、namespace 还原、self-closing
- `test_self_closing.py` — `<br>`/`<hr>`/`<img>` 等 void 元素的自闭合往返
- `test_metadata.py` — 从真实 EPUB 读 `<metadata>`、跳过 SKIP_FIELDS
- `test_spines.py` — 读 EPUB 2.0 (NCX) 和 3.0 (nav) 的 spine 顺序
- `test_toc.py` — read_toc/write_toc 处理 EPUB 2.0 和 3.0 目录
- `test_math.py` — MathML → LaTeX 转换
- [待实施] `test_zip.py` — 测 `Zip.add()` 新方法
- [待实施] `test_commentor_extract.py` / `block.py` / `inject.py` / `schema.py` / `pipeline.py` / `css.py` / `e2e.py`

## 架构

整体流程：**EPUB 解压 → 按 spine 顺序逐章节解析 XML → [Stage 1 全章扫读 → LLM → ChapterMemo] → [Stage 2 按 6 段切块 → LLM → BlockAnnotation(JSON) × N 块] → DOM 注入 `<aside>` → 注入 commentary.css + 改 OPF + 改 chapter `<head>` → 写回 EPUB**。

### 顶层入口（`epub_commentor/__init__.py`）
公开 API（M1 现状）：`LLM`、`Message`、`MessageRole`。
[待实施] 还会加：`comment_epub`、`CommentConfig`、`CommentPosition`、`CommentKind`、`AnnotationFailedEvent`、`ChapterMemo`、`CommentItem`。

### 子包职责

- **`epub_commentor/llm/`** — OpenAI 兼容 LLM 客户端（[已实现] 全部 8 文件）
  - `core.py:LLM` — 持有 `tiktoken` 编码、Jinja 模板环境、缓存/日志目录、token 统计
  - `context.py:LLMContext` — 上下文管理器：计算请求哈希 → 命中缓存则返回 → 否则走 executor → 临时文件 → 退出时 commit（多线程下用 `_CACHE_COMMIT_LOCK` 串行化）
  - `executor.py:LLMExecutor` — 流式调用 OpenAI SDK，按 `error.is_retry_error()` 重试 `retry_times` 次，间隔 `retry_interval_seconds`，把每 chunk 的 `usage` 累加进 `Statistics`
  - `statistics.py:Statistics` — 线程安全的 token 计数（`total_tokens`/`input_tokens`/`input_cache_tokens`/`output_tokens`）
  - `increasable.py:Increasable`/`Increaser` — 每次请求后用 `0.5*(end-begin)` 衰减，让 `temperature`/`top_p` 可作为 `(start, end)` 范围传入
  - `error.py` — 判定 OpenAI / httpx / requests 中的可重试错误（timeout、5xx、网络）
  - `types.py` — `Message` dataclass + `MessageRole` enum（SYSTEM/USER/ASSISTANT）
  - [待实施] `memo.py` — Stage 1 全章扫读封装（拼 system + user 消息、调 LLM、pydantic 校验）
  - [待实施] `block.py` — Stage 2 块级批注封装（切组 + 打 `data-p-id` + retry 循环）
  - [待实施] `schema.py` — pydantic 模型（`ChapterMemo`, `BlockAnnotation`, `CommentItem`）+ `validate_block_annotations()` 检查 p_id 连续性 / block 内重叠

- **`epub_commentor/epub/`** — EPUB 容器/目录/章节读取（[已实现] 全部 6 文件）
  - `zip.py:Zip` — 同时持有源/目标两个 `ZipFile`，`migrate()`/`replace()` 走目标，`add()` [已实施 M1.5] 注入新文件；`__exit__` 自动 migrate 未处理的源文件
  - `common.py` — `find_opf_path`（META-INF/container.xml → rootfile）
  - `spines.py` — 按 OPF manifest+spine 顺序产出 `(chapter_path, media_type)`
  - `toc.py` — 识别 EPUB 2.0（NCX）与 3.0（nav），统一成 `Toc` dataclass
  - `metadata.py` — 读取 `<metadata>` 子元素，跳过 `SKIP_FIELDS = {language, identifier, date, meta, contributor}`
  - `math.py` — `xml_to_latex`：把 `<math>` MathML 转 LaTeX（评注项目不需要中断翻译，但样式化数学书时可选用）

- **`epub_commentor/xml/`** — XML 处理基础（[已实现] 6 文件）
  - `xml_like.py:XMLLikeNode` — 解析时记录 namespace → 序列化时还原；HTML 风格（`text/html`）的 void 元素在写出时去掉自闭合；自动探测 encoding（UTF-8 BOM / UTF-16 / XML declaration / 兜底 latin-1）
  - `deduplication.py:deduplicate_ids_in_element` — 评注元素注入后调一次，避免与原书 id 冲突（id 重复会破坏文档内部链接）
  - `xml.py:find_first`, `iter_with_stack` — DOM 辅助函数
  - `utils.py` — `concat_attribute_values`, `iter_without_parents` 等
  - `self_closing.py` — `<br>`/`<hr>`/`<img>` 等 void 元素自闭合往返
  - `const.py` — `ID_KEY="id"`, `DISPLAY_ATTRIBUTE="display"`

- **`epub_commentor/template.py`** — `create_env()` 构造 Jinja `Environment`，自定义 `_DSLoader` 只从 `epub_commentor/data/` 目录读 `.jinja` 文件

- **`epub_commentor/utils.py`** — `normalize_whitespace`, `is_the_same` 等通用工具函数

- [待实施] **`epub_commentor/pipeline/`** — 评注核心编排
  - `extract.py` — 提取层：`Zip.open` → 读 spine → 逐章 `XMLLikeNode.parse` → 拿到 `body_element` + metadata + toc
  - `process.py` — 处理层：Stage 1（`scan_chapter()`）+ Stage 2（`annotate_block()`），章节内 `ThreadPoolExecutor` 并行
  - `inject.py` — 注入层：`inject_comment()`（沿父链向上找 body 直接子元素 + `parent.insert(idx ± 1, aside)`）+ 注入后清除 `data-p-id` + 调 `deduplicate_ids_in_element`

- [待实施] **`epub_commentor/commentor.py`** — 顶层编排入口 `comment_epub()`，串联 extract → process → inject → CSS 集成

- [待实施] **`epub_commentor/cli.py`** — `[project.scripts]` entrypoint

- [待实施] **`epub_commentor/errors.py`** — `CommentorError` 基类 + 5 个子类（`CommentInvalidJSONError`, `CommentOrphanPIdError`, `CommentOverlapError`, `CommentScanFailedError`, `CommentNoParagraphsError`）

- [待实施] **`epub_commentor/events.py`** — `AnnotationFailedEvent`

### 提示词模板（`epub_commentor/data/`）

- [待实施] `scan.jinja` — Stage 1 全章扫读：输出 JSON `{core_thesis, outline, key_terms, tone, target_audience, reading_anchors}`
- [待实施] `annotate.jinja` — Stage 2 块级批注：输出 JSON `{comments: [{target_p_ids, position, kind, content}, ...]}`，约束 `target_p_ids` 连续、`intro`/`summary`/`note` 三种 kind

### 脚本（`scripts/`）

- [待实施] `comment_epub.py` — CLI：`poetry run python scripts/comment_epub.py source.epub --synopsis "..."`，输出 tqdm 进度条 + token 用量
- [待实施] `comment_challenge.py` — 跑 `tests/commentary_challenge/*.json` 回归
- `check_duplicate_ids.py` — 扫描 `temp/logs/*.log`，定位重复 ID（[已实现]）
- `utils.py:load_comment_llm()` — 读 `format.json` 构造**单个** LLM（[已改造 M1]）

## 单 LLM 架构

**只用一个 LLM**（不再需要 translation/fill 双 LLM）。原因：评注任务的输出（JSON）结构化程度高，单一温度（如 0.4）足以兼顾"有文采"和"结构稳定"。

`format.template.json` 顶层字段（扁平）：`key`, `url`, `model`, `token_encoding`, `timeout`, `retry_times`, `retry_interval_seconds`, `temperature`, `top_p`（不再有 `translation` / `fill` 子字典）。

## 关键设计要点

- **两阶段 LLM** — Stage 1 输出 `ChapterMemo`（章级概述），Stage 2 把 memo + 6 段块 HTML 一起喂给 LLM 产出 JSON 评注。`cache_seed_content` 命名空间：`f"commentor:{VERSION}:{stage}:{user_id}:{chapter_hash}[:{block_hash}]"`，确保换 prompt / 换用户 / 换章节 / 换块各自独立缓存。

- **段落临时 id** — Stage 2 切组时给块内每个 `<p>` 加 `data-p-id="0..N"`，LLM 只能引用批内局部索引，注入完成后**必须**清除该属性。`cmt-...` 评注 id 命名空间避免与原书 id 冲突。

- **JSON 输出 + pydantic 校验** — 评注不要求原文结构保持，所以用 JSON（比 XML 块更易校验）。校验失败 → 把原始响应 + 错误信息拼成 user 第 2 条消息做多轮 retry，最多 3 次。

- **`<aside class="commentary commentary-{kind}">`** — 三种 kind 块级评注：intro（前置导读）、summary（后置总结）、note（块级夹注）。`<p>` 嵌套在 `<blockquote>` 等容器中时，`inject_comment()` 沿父链向上找最近的 body 直接子元素。

- **`Zip.add()`** — [已实施] 用于注入 `commentary.css`。`writestr(arcname, bytes, compress_type=ZIP_DEFLATED)`，不复制 source 中已有的同名文件。

- **命名空间往返** — `XMLLikeNode` 解析时记录 namespace URI，序列化时按映射还原。新插入的 `<aside>` 是无 namespace 的纯 HTML，不影响 OPF 的 `dc:` / `opf:` / `epub:` 前缀恢复。`_STANDARD_HTML_ATTRS` 修 `<link epub:type>` 和 `<link type>` 同名属性冲突的 bug。

- **CSS 自动集成** — 三步：① `Zip.add("Styles/commentary.css", bytes)` ② 改 OPF `<manifest>` 加 `<item id="commentary-css" href="Styles/commentary.css" media-type="text/css"/>` ③ 每个 chapter `<head>` 加 `<link rel="stylesheet" type="text/css" href="../Styles/commentary.css"/>`。CSS 针对 e-ink 优化（用 border + 灰度对比，不用彩色和阴影）。

- **单章无 `<p>` 容错** — poetry / list-only 章节：检测到 `len(body.findall(".//p")) == 0` → 跳过并 log warning，不抛错。

- **缓存语义** — `LLMContext` 把 `messages + cache_seed` 算 SHA512 写临时文件，退出时 commit；多线程下用全局 `_CACHE_COMMIT_LOCK` 避免竞争。

## 修改指引

- 改 `data/scan.jinja` / `annotate.jinja` 后，建议跑 `scripts/comment_challenge.py` 确认 `tests/commentary_challenge/` 全部通过
- 改 `pipeline/inject.py` 要同时看 `tests/test_commentor_inject.py` 和 `test_xml_like.py`（DOM 操作和 namespace 还原）
- 改 `pipeline/process.py` 的切组逻辑要跑 `tests/test_commentor_block.py`（边界 case：最后一批短、单章无 `<p>`、`<p>` 嵌套）
- 改 `llm/schema.py` 的 pydantic 模型要跑 `tests/test_commentor_schema.py`（连续性、重叠、范围检查）
- 改 namespace 处理（`xml/xml_like.py`）要跑 `tests/test_xml_like.py`、`test_self_closing.py`
- 新增 LLM provider 走 `llm/executor.py:LLMExecutor`（默认就是 OpenAI 兼容，无需改 SDK）；其他类型错误在 `llm/error.py` 加判定
- 改 `epub/zip.py` 要保证 mimetype 是 ZIP 的第一个 entry（在 `comment_epub.py` 主循环里显式 `migrate`）
- 改 `data/commentary.css` 时，本地跑 `pytest tests/test_commentor_css.py` 验证 CSS 字节被正确写入

## 实施里程碑

| 阶段 | 状态 | 产出 |
|---|---|---|
| M1 重构 | ✅ 完成 | 包改名 + 删除翻译代码 + `Zip.add()` + import 修复 |
| M2 数据流 | ⏳ 待实施 | `pipeline/extract.py` + `pipeline/process.py`（Stage 1+2 跑通） + `scan.jinja` + `annotate.jinja` + `llm/memo.py` + `llm/block.py` |
| M3 注入 | ⏳ 待实施 | `pipeline/inject.py` + `commentary.css` + OPF 更新 + chapter head link |
| M4 校验 | ⏳ 待实施 | `llm/schema.py` + `errors.py` |
| M5 测试 | ⏳ 待实施 | `tests/_mock_llm.py` + 7 个单元测试 + 10 个 challenge case |
| M6 CLI | ⏳ 待实施 | `scripts/comment_epub.py` + entrypoint |
| M7 真实 LLM 联调 | ⏳ 待实施 | 用 OpenAI 跑《The little prince》全本，验证样式、缓存、token 用量、Kindle 兼容性 |

详细 PRD 见 `plans/this-is-a-forked-encapsulated-seal.md`。