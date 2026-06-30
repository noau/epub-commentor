<div align=center>
  <h1>EPUB Commentor</h1>
  <p>
    <a href="https://github.com/your-org/epub-commentor/actions/workflows/merge-build.yml" target="_blank"><img src="https://img.shields.io/github/actions/workflow/status/your-org/epub-commentor/merge-build.yml" alt="ci" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/badge/pip_install-epub--commentor-blue" alt="pip install epub-commentor" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/pypi/v/epub-commentor.svg" alt="pypi epub-commentor" /></a>
    <a href="https://pypi.org/project/epub-commentor/" target="_blank"><img src="https://img.shields.io/pypi/pyversions/epub-commentor.svg" alt="python 版本" /></a>
    <a href="https://github.com/your-org/epub-commentor/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/github/license/your-org/epub-commentor" alt="许可证" /></a>
  </p>
  <p><a href="./README.md">English</a> | 中文</p>
</div>


想要 LLM 生成的阅读引导，又不想丢掉原文？**EPUB Commentor** 把 AI 写的导读、总结、夹注直接注入你的 EPUB——原文逐字保留，评注以样式化的 `<aside>` 块形式与正文并列，电子墨水屏原生支持。

[oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator) 的 fork，沿用了同一套 XML / EPUB 处理框架，把"翻译段落"改成"评注段落"。产出的 EPUB 同时具备**书 + 伴读**两种身份，可以直接导入 Kindle / Kobo / Calibre，无需后处理。

![评注效果](./docs/images/commentary.png)

## 为什么是"评注"而非"翻译"？

大多数 LLM 驱动的 epub 工具都改写原文。用于双语阅读没问题，但会让原著失去声音，对语言学习者也极不友好。Commentor 改走另一条路：**原文原封不动**，只在旁边注入三类新内容：

- **`intro`**（前置导读） — 1–3 句铺垫，放在目标段落的*第一段之前*，让读者对接下来的内容先有预期。
- **`summary`**（后置总结） — 1–3 句归纳，放在目标段落的*最后一段之后*，把段落组收个尾。
- **`note`**（夹注） — 针对某个术语或概念的短评，可前可后。

产出的 EPUB 保留原书的每一个 `<p>`、每一个 `<em>`、每一层标题结构——新增的 DOM 只有 `<aside class="commentary commentary-{kind}">` 兄弟节点，外加每个章节 `<head>` 引用的一份 CSS。

## 安装

```bash
pip install epub-commentor
```

（或用 `poetry add epub-commentor` 做项目级安装）

**系统要求**：Python 3.11 / 3.12 / 3.13。

## 快速开始

### 1. 准备凭据

复制模板并填入你的 OpenAI 兼容端点：

```bash
cp format.template.json format.json
# 编辑 format.json 填入 key / url / model / token_encoding 等
```

`format.json` 是扁平对象，详见下方 [配置说明](#配置说明)。

### 2. 用 CLI 跑

```bash
poetry run epub-commentor path/to/source.epub --synopsis "一本哲学童话。"
# 输出：<source-stem>.commented.epub，写到源文件同目录
```

或直接从 repo checkout 跑（不依赖安装的 entrypoint）：

```bash
poetry run python scripts/comment_epub.py path/to/source.epub --synopsis "..."
```

### 3. 或调 Python API

```python
from epub_commentor import LLM, comment_epub, CommentConfig, CommentKind, CommentPosition

llm = LLM(
    key="your-api-key",
    url="https://api.openai.com/v1",
    model="gpt-4",
    token_encoding="o200k_base",
)

config = CommentConfig(
    book_synopsis="一个飞行员在撒哈拉迫降后遇到外星来客的童话。",
    target_language="Chinese",            # 让 LLM 用中文写评注
    block_size=6,                         # 每个 Stage 2 批次的段落数
    concurrency=4,                        # 章内并发线程数
    kinds=(CommentKind.INTRO, CommentKind.SUMMARY, CommentKind.NOTE),
    position=CommentPosition.BEFORE,
)

result = comment_epub(
    source="path/to/source.epub",
    output="path/to/annotated.epub",      # 默认：<stem>.commented.epub 在源文件同目录
    llm=llm,
    config=config,
)

print(f"处理章节数: {result.chapters_processed}")
print(f"跳过章节数: {result.chapters_skipped}")
print(f"生成评注数: {result.total_comments}")
print(f"总 token 数: {result.total_tokens}")
```

### 带进度条

```python
from tqdm import tqdm

with tqdm(total=100, desc="评注中", unit="%") as pbar:
    last = {"v": 0.0}
    def on_progress(stage: str, current: int, total: int) -> None:
        # 阶段顺序：extract → process → inject
        ratio = {"extract": 0.1, "process": 0.85, "inject": 1.0}[stage] * 100
        pbar.update(ratio - last["v"])
        last["v"] = ratio

    comment_epub(source="book.epub", llm=llm, config=config, progress_callback=on_progress)
```

## API 参考

### `comment_epub(source, output=None, *, llm, config=None, progress_callback=None) -> CommentorResult`

唯一的顶层入口。在 `source` 上跑 extract → process → inject，把新 EPUB 写到 `output`（默认：源文件同目录的 `<stem>.commented.epub`）。

| 参数 | 类型 | 说明 |
|---|---|---|
| `source` | `Path \| str` | 源 EPUB 路径。只读不改。 |
| `output` | `Path \| str \| None` | 输出位置。`None` → 源文件旁的 `<stem>.commented.epub`。 |
| `llm` | `LLMProtocol` | 满足 protocol 的任何 LLM（生产用 `LLM`，测试用 `MockLLM`）。 |
| `config` | `CommentConfig \| None` | 流水线配置。`None` → 走默认值。 |
| `progress_callback` | `(stage, current, total) -> None \| None` | 进度回调。阶段依次：`extract`、`process`、`inject`。 |

返回 `CommentorResult`，含 `output_path`、`annotations`、处理 / 跳过的章节计数以及 LLM 的 token 用量（`total_tokens` / `input_tokens` / `input_cache_tokens` / `output_tokens`）。

### `CommentConfig`

所有运行时开关集中在一个 dataclass：

| 字段 | 默认 | 说明 |
|---|---|---|
| `position` | `CommentPosition.BEFORE` | LLM 没指定时评注的默认位置（`before` / `after`）。 |
| `kinds` | `(INTRO, SUMMARY, NOTE)` | Stage 2 提示词枚举的允许评注类型。 |
| `block_size` | `6` | 每个 Stage 2 批次的段落数（每批让 LLM 一次性标注）。 |
| `max_scan_retries` | `3` | Stage 1 解析 `ChapterMemo` 失败时的重试次数。 |
| `max_json_retries` | `3` | Stage 2 解析 `BlockAnnotation` 失败时的重试次数。 |
| `concurrency` | `4` | 章内 Stage 2 块的工作线程数。 |
| `cache_seed_user_id` | `"default"` | 缓存命名空间组件。换用户或换书时改这个值即可清缓存。 |
| `book_synopsis` | `None` | 一句话简介，会同时进 Stage 1 和 Stage 2 的提示词。 |
| `inject_css` | `True` | 设 `False` 则跳过 `commentary.css` / OPF patch / head link（仅注入 `<aside>` 标记）。 |
| `css_path_in_epub` | `Path("Styles/commentary.css")` | CSS 在 EPUB 内落地的相对路径。 |
| `target_language` | `"English"` | LLM 应使用哪种语言撰写评注。 |
| `fail_on_empty_chapter` | `False` | `True` 时，遇到零 `<p>` 章节直接抛 `CommentNoParagraphsError`，不静默跳过。 |

### `CommentKind` / `CommentPosition`

```python
from epub_commentor import CommentKind, CommentPosition

CommentKind.INTRO       # "intro"
CommentKind.SUMMARY     # "summary"
CommentKind.NOTE        # "note"

CommentPosition.BEFORE  # "before"
CommentPosition.AFTER   # "after"
```

### CLI

`pyproject.toml` 里注册了 `epub-commentor` 控制台脚本，参数如下：

```text
poetry run epub-commentor SOURCE [-o OUTPUT] [--format-json PATH] [--synopsis TEXT]
                              [--block-size N] [--concurrency N]
                              [--max-json-retries N] [--max-scan-retries N]
                              [--cache-path DIR] [--cache-user-id ID]
                              [--target-language LANG]
                              [--css-path PATH] [--no-css]
                              [--fail-on-empty-chapter] [-q]
```

所有旗标一一映射到 `CommentConfig` 字段（外加 `--cache-path` 给 LLM 缓存用）。`epub-commentor --help` 可见完整列表。

## 配置说明

`format.json` 是扁平对象，CLI / API 直接 `LLM(**cfg)` 散开传给 LLM：

```json
{
  "key": "sk-...",
  "url": "https://api.openai.com/v1",
  "model": "gpt-4",
  "token_encoding": "o200k_base",
  "timeout": null,
  "retry_times": 5,
  "retry_interval_seconds": 6.0,
  "temperature": 0.4,
  "top_p": null,
  "cache_path": "./commentary_cache",
  "log_dir_path": null
}
```

| 提供方 | 示例 `url` | 注意事项 |
|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` 用 `o200k_base`，旧模型用 `cl100k_base`。 |
| Azure OpenAI | `https://<res>.openai.azure.com/openai/deployments/<dep>` | `model` 字段填部署名。 |
| 任意 OpenAI 兼容服务 | `https://your-service.com/v1` | `token_encoding` 必须与你模型的分词器对应。 |

### 单 LLM 哲学

原 `epub-translator` 用过两个 LLM 实例（一个创意、一个结构）。Commentor 收敛成**单个** LLM：

- Stage 1（`scan_chapter` —— 全章节 memo）和 Stage 2（`annotate_block` —— 每块评注）共用同一个客户端。
- 一个温度（如 `0.4`）就能在"有文采"和"结构稳定 JSON"之间取好折中。
- Token 统计自然落在单实例上，无需聚合。

## 流水线原理

Commentor 对每章跑一个**两阶段 LLM 流水线**：

### Stage 1 — Scan（全章扫读）

把整章正文（纯文本）连同书的元数据 + 你的 synopsis 一起发给 LLM。返回 `ChapterMemo`：

```jsonc
{
  "core_thesis": "一句话概括本章主旨。",
  "outline": ["3..7 个子主题，按出现顺序"],
  "key_terms": [{"term": "...", "gloss": "..."}],
  "tone": "说教 | 沉思 | ...",
  "target_audience": "大众读者 | ...",
  "reading_anchors": ["0..3 个值得标记的句段"]
}
```

### Stage 2 — Annotate（块级标注）

把章节段落切成 `block_size` 大小的块，每块给段落加 `data-p-id="0..N"` 让 LLM 用局部索引引用。配合 memo + synopsis，LLM 返回该块的评注列表：

```jsonc
{
  "comments": [
    {
      "target_p_ids": [0, 1, 2],          // 块内的连续子集
      "position": "before" | "after",     // 相对 FIRST / LAST p_id
      "kind": "intro" | "summary" | "note",
      "content": "1-4 句（用 <target_language>）"
    }
  ]
}
```

校验规则：

1. 每个 `target_p_ids` 值落在 `[0, block_size)` 区间。
2. 区间必须连续（如 `[3,4,5]` OK；`[2,4]` 拒绝）。
3. 同一块内任意两个评注不得共享一个段落。

穷尽 `max_json_retries` 仍失败时抛 `CommentInvalidJSONError`。

### Inject（注入）

每条 `CommentItem` 变成一个 `<aside class="commentary commentary-{kind}" id="cmt-...">`，插在锚点段落的邻近位置。CSS 走三个幂等步骤接进书里：

1. 把 `commentary.css` 加进目标 ZIP（通过 `Zip.add()`）。
2. OPF `<manifest>` 加 `<item id="commentary-css" ...>`（已存在则跳过）。
3. 每个章节 `<head>` 加 `<link rel="stylesheet" type="text/css" href="..."/>`（已存在则跳过）。

CSS 专为电子墨水屏优化——灰度边框、无 box-shadow、无彩色、`break-inside: avoid` 确保每个 `<aside>` 在换页时不被拆开。

## 异常体系

流水线抛的所有异常都派生自 `CommentorError`（继承自 `ValueError`）：

| 异常 | 触发条件 |
|---|---|
| `CommentScanFailedError` | Stage 1 重试穷尽仍无法产出合法 `ChapterMemo`。 |
| `CommentInvalidJSONError` | Stage 2 重试穷尽仍无法产出合法 `BlockAnnotation`。 |
| `CommentOrphanPIdError` | 评注引用了块外的 p_id，或 p_id 不连续。 |
| `CommentOverlapError` | 同一块内两条评注共享段落。 |
| `CommentNoParagraphsError` | 章节零 `<p>` 且 `fail_on_empty_chapter=True`。 |

```python
from epub_commentor import comment_epub, CommentorError

try:
    result = comment_epub("book.epub", llm=llm)
except CommentorError as exc:
    # 一切可恢复 / 结构性失败都收敛到这里
    print(f"失败: {type(exc).__name__}: {exc}")
```

## 并发模型

- **章间**：串行。Stage 1 是单章节 LLM 调用大头，并行收益不大。
- **章内**：通过 `ThreadPoolExecutor(max_workers=concurrency)` 并发。同一章内的 Stage 2 块相互独立。
- **缓存**：`LLMContext` 在全局锁下提交缓存写入，避免多线程竞态。

## 测试

```bash
# 全部单元 / 集成 / 端到端（约 173 用例，包含真实 EPUB 资产）
poetry run pytest tests/ -v

# 仅评注相关测试
poetry run pytest tests/test_commentor_*.py -v

# 10 个手工挑出的 challenge 回归（走 MockLLM，零网络）
poetry run python scripts/comment_challenge.py
```

`tests/_mock_llm.py` 提供 `MockLLM`，按 cache seed 前缀（`:scan:` / `:annotate:`）分发预制 JSON 响应，因此整条流水线都能在不连 OpenAI 的情况下跑一遍。

## 相关项目

- [PDF Craft](https://github.com/oomol-lab/pdf-craft) — 先把扫描版 / 图片版 PDF 转成 EPUB，再走 Commentor 加评注。
- [SpineDigest](https://github.com/oomol-lab/spinedigest) — 不止要夹注？SpineDigest 能整书出结构化摘要、章节拓扑与知识图谱。

## 贡献

欢迎贡献！随时提交 Pull Request。架构设计意图见 `plans/this-is-a-forked-encapsulated-seal.md`。

## 许可证

本项目采用 MIT 许可证，详见 [LICENSE](LICENSE)。Fork 自 [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator)，同样条款。

## 支持

- **问题反馈**：[GitHub Issues](https://github.com/your-org/epub-commentor/issues)
