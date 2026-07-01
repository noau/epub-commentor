# EPUB Commentor — 实现 PRD

> 本文件基于 `epub-commentor-proposal.md` 的初始提案 + 对 `epub-translator` 源码的全面调研得出，并已根据用户拍板的关键决策做调整。

## Context — 为什么做这次改造

`oomol-lab/epub-translator` 是一个把 EPUB 翻译成双语对照版的 LLM 工具。本 fork (`epub-commentor`) 想把它改造成完全不同的产品：**AI 智能评注工具**——保留原文不变，注入 AI 生成的导读、总结、夹注，输出可直接进 Kindle 阅读的评注版 EPUB。

### 原提案 (`epub-commentor-proposal.md`) 的核心想法

1. 两阶段处理：全章扫读 → 局部批注
2. 5-8 段话题块切分与批注
3. 前置导读 / 后置总结
4. 管道模式 + BeautifulSoup 注入
5. 样式表适配

### 调研后对提案的修正

| 提案想法 | 评估 | 决策 |
|---|---|---|
| 用 BeautifulSoup 注入 | **错误建议** — `XMLLikeNode` 已现成且保留 namespace prefix、BOM、自闭合规则 | 复用 `XMLLikeNode`，不引入 bs4 |
| 话题块切分用 5-8 段 | 可行，但需新写切组函数（`XMLStreamMapper` 切的是 inline 不是 block） | 新写 `pipeline/process.py` 的切组逻辑 |
| 两阶段 LLM | 完全可行，`LLMContext` 已支持多轮对话和 cache_seed 命名 | 复用 `LLM/LLMContext` |
| 前置/后置挂载 | 可行，但 `SubmitKind` 三种都不支持"插入全新节点" | 新写 `inject_aside()`，绕过 `submitter.py` |
| 样式表适配 | 简单，但 `Zip` 没有 `add()` API | fork 改 `zip.py` 加 `add()` |
| 提案说"在原项目二次开发" | 正确方向 | 直接 fork 改包并改名 |
| 提案说"放弃重写底层" | 完全同意 | 复用全部 LLM/XML/Zip/spines/toc/metadata 代码 |

### 用户拍板的关键决策

1. **包结构**：直接修改 `epub_translator/` 并**改名**为 `epub_commentor/`，删除翻译相关代码
2. **评注类型**：v1 支持三种块级类型 — `intro`（前置导读）、`summary`（后置总结）、`note`（块级夹注）
3. **块大小**：默认 6 段/批
4. **CSS 集成**：完整自动注入（CSS 文件 + OPF manifest + 每章 `<head>` `<link>`）

---

## 项目结构

### 改造后的目录布局

```
epub-commentor/                              # 项目根（保留原仓库骨架）
├── pyproject.toml                            # 改名 + 改包名 + 删翻译相关 entrypoint
├── epub_commentor/                           # 改造后的主包（由 epub_translator/ 改名）
│   ├── __init__.py                           # 公开 API：comment_epub, CommentConfig, LLM, ...
│   ├── commentor.py                          # 入口编排
│   ├── pipeline/
│   │   ├── __init__.py
│   │   ├── extract.py                        # 提取层：spines + body 解析
│   │   ├── process.py                        # 处理层：Stage 1/2 调度
│   │   └── inject.py                         # 注入层：DOM 操作 + ID 命名空间
│   ├── llm/
│   │   ├── __init__.py
│   │   ├── core.py                           # 保留（原 core.py）
│   │   ├── context.py                        # 保留
│   │   ├── executor.py                       # 保留
│   │   ├── statistics.py                     # 保留
│   │   ├── increasable.py                    # 保留
│   │   ├── error.py                          # 保留
│   │   ├── memo.py                           # 新增：Stage 1 调用封装
│   │   ├── block.py                          # 新增：Stage 2 调用 + 切组
│   │   └── schema.py                         # 新增：pydantic schema + retry
│   ├── xml/
│   │   ├── __init__.py
│   │   ├── xml_like.py                       # 保留
│   │   ├── self_closing.py                   # 保留
│   │   ├── xml.py                            # 保留（find_first, iter_with_stack）
│   │   ├── deduplication.py                  # 保留
│   │   ├── inline.py                         # 保留
│   │   ├── friendly/                         # 保留（evaluate 但用得少）
│   │   └── utils.py                          # 保留
│   ├── epub/
│   │   ├── __init__.py
│   │   ├── zip.py                            # 改造：加 add() 方法
│   │   ├── common.py                         # 保留
│   │   ├── spines.py                         # 保留
│   │   ├── toc.py                            # 保留
│   │   ├── metadata.py                       # 保留
│   │   └── math.py                           # 保留
│   ├── data/
│   │   ├── scan.jinja                        # 新增：Stage 1 prompt
│   │   ├── annotate.jinja                    # 新增：Stage 2 prompt
│   │   └── commentary.css                    # 新增：e-ink 优化样式
│   ├── errors.py                             # 新增：异常类型
│   └── events.py                             # 新增：事件类型（AnnotationFailedEvent）
├── scripts/
│   ├── comment_epub.py                       # 新增：CLI
│   ├── check_duplicate_ids.py                # 保留（仍有用：评注 id 与原书去重）
│   └── utils.py                              # 改造：load_comment_llm 替代 load_llm
└── tests/
    ├── utils.py                              # 保留：create_temp_dir_fixture
    ├── assets/                               # 保留
    ├── test_xml_like.py                      # 保留
    ├── test_self_closing.py                  # 保留
    ├── test_xml_friendly.py                  # 保留
    ├── test_metadata.py                      # 保留
    ├── test_spines.py                        # 保留
    ├── test_toc.py                           # 保留
    ├── test_math.py                          # 保留
    ├── test_zip.py                           # 新增：测 add()
    ├── test_commentor_extract.py             # 新增
    ├── test_commentor_block.py               # 新增
    ├── test_commentor_inject.py              # 新增
    ├── test_commentor_schema.py              # 新增
    ├── test_commentor_pipeline.py            # 新增（mock LLM e2e）
    ├── test_commentor_css.py                 # 新增
    ├── test_commentor_e2e.py                 # 新增（真实 EPUB + mock LLM）
    ├── commentary_challenge/                 # 新增：10 个手挑故障样例
    │   ├── case1_normal_5p.json
    │   ├── case2_dense_argument.json
    │   ├── case3_poetry_no_p.json
    │   ├── case4_mixed_intro_after.json
    │   ├── case5_invalid_p_ids.json
    │   ├── case6_overlap.json
    │   ├── case7_malformed_json.json
    │   ├── case8_extra_text_before_json.json
    │   ├── case9_unicode_escape.json
    │   └── case10_empty_block.json
    └── _mock_llm.py                          # 新增：测试替身
```

### 删除的文件

- `epub_translator/segment/`（整个目录，翻译专用）
- `epub_translator/translation/`（整个目录，翻译专用）
- `epub_translator/xml_translator/`（整个目录，翻译专用）
- `epub_translator/data/translate.jinja`（翻译专用）
- `epub_translator/data/fill.jinja`（翻译专用）
- `scripts/translate_epub.py`（翻译 CLI）
- `scripts/translate_xml.py`（翻译单 XML CLI）
- `scripts/translate_challenge.py`（翻译爬山测试）
- `tests/test_inline_segment.py`、`test_block_segment.py`、`test_text_segment.py`、`test_submitter.py`、`test_score.py`、`test_serial.py`、`test_hill_climbing.py`、`test_epub_transcode.py`
- `tests/challenge/`（翻译故障样例）

> 备注：上述待删文件虽然包含对评注也有参考价值的代码（如 `XMLStreamMapper` 的切组思路），但**不直接复用**——评注按 `<p>` 数切组，逻辑差异较大，新写更清晰。

---

## 架构与数据流

### 端到端流程

```
source.epub
   │
   ▼
extract.py: Zip.open → spines → 逐章 XMLLikeNode → body_element + metadata + toc
   │
   ▼
process.py — Stage 1 (全章扫读, 按 spine 顺序)
   per chapter:
     system: scan.jinja + book_synopsis
     user:   chapter full text (plain, no data-p-id)
     ─► LLM ─► ChapterMemo (JSON, pydantic validated, cache by message hash)
   │
   ▼
process.py — Stage 2 (块级批注, 章节内 ThreadPool 并行)
   per chapter, per block of 6 <p>s:
     add data-p-id="0..5" to <p>s (local to block)
     system: annotate.jinja + memo + synopsis
     user:   block_html with data-p-id
     ─► LLM ─► BlockAnnotation (JSON, retry up to 3 times)
     strip data-p-id
   │
   ▼
inject.py — DOM 注入 (顺序: block_index 升序)
   per comment:
     find target <p data-p-id="N"> (in original DOM, not stripped)
     locate inject parent (walk up to body direct child if needed)
     construct <aside class="commentary commentary-{kind}" data-cmt-id="cmt-...">
     parent.insert(idx ± 1, aside)
   ─► deduplicate_ids_in_element(body) ─► save back to zip
   │
   ▼
style.py — CSS 集成
   zip.add("Styles/commentary.css", css_bytes)
   opf: parse with XMLLikeNode ─► append <item id="commentary-css" .../> ─► save
   per chapter <head>: append <link rel="stylesheet" href="../Styles/commentary.css"/>
   │
   ▼
commented.epub
```

### 两阶段时序与并行

```
[Stage 1 — chapter scan, sequential by spine]
  chapter_1 ─► scan ─► memo_1 (cached)
  chapter_2 ─► scan ─► memo_2 (cached)
  chapter_3 ─► scan ─► memo_3 (cached)
                    │
                    ▼
[Stage 2 — block annotation, per-chapter ThreadPool]
  chapter_1: [block_0..5] [block_6..11] [block_12..17] ─► 3 concurrent calls
  chapter_2: [block_0..5] [block_6..11] ...            ─► 3 concurrent calls
  ...
```

- **跨章节并发**：v1 不做（避免 Stage 1 / Stage 2 互相等待的复杂调度）。如果某本书 50+ 章且不在意进度条混乱，可后续优化。
- **章节内并发**：用 `ThreadPoolExecutor(max_workers=concurrency)`，默认 `concurrency=4`。

### 缓存策略

`cache_seed_content` 命名空间格式：

- Stage 1: `f"commentor:{VERSION}:scan:{chapter_hash}"`
- Stage 2: `f"commentor:{VERSION}:annotate:{chapter_hash}:{block_hash}"`

其中：
- `VERSION` = `importlib.metadata.version("epub-commentor")`（或 `"0.0.0-dev"`）
- `chapter_hash` = `sha1(spine_path.as_posix().encode()).hexdigest()[:8]`
- `block_hash` = `sha1(f"{block_start_idx}:{[p.text[:50] for p in block]}".encode()).hexdigest()[:8]`

效果：换 prompt 重跑自动全失效；同一本书重跑秒返；同章节不同 block 互不干扰。

---

## 公开 API

### `comment_epub()`

```python
# epub_commentor/commentor.py
from collections.abc import Callable
from enum import Enum
from os import PathLike
from pathlib import Path
from dataclasses import dataclass
from .llm.core import LLM

class CommentPosition(str, Enum):
    BEFORE = "before"
    AFTER = "after"

class CommentKind(str, Enum):
    INTRO = "intro"
    SUMMARY = "summary"
    NOTE = "note"

@dataclass
class CommentConfig:
    position: CommentPosition = CommentPosition.BEFORE
    kinds: tuple[CommentKind, ...] = (CommentKind.INTRO, CommentKind.SUMMARY, CommentKind.NOTE)
    block_size: int = 6
    max_json_retries: int = 3
    max_scan_retries: int = 3
    concurrency: int = 4
    cache_seed_user_id: str = "default"
    book_synopsis: str | None = None
    inject_css: bool = True
    css_path_in_epub: Path = Path("Styles/commentary.css")

class AnnotationFailedEvent:
    chapter_path: Path
    block_index: int
    raw_response: str
    error: Exception

def comment_epub(
    source_path: PathLike | str,
    target_path: PathLike | str | None = None,
    llm: LLM,
    config: CommentConfig | None = None,
    on_progress: Callable[[float], None] | None = None,
    on_annotation_failed: Callable[[AnnotationFailedEvent], None] | None = None,
    chapter_filter: Callable[[list[Chapter]], list[bool]] | None = None,  # M8
) -> CommentorResult:
    """
    给 EPUB 注入 AI 评注。

    - source_path: 源 EPUB
    - target_path: 输出 EPUB（None 时落到 ``<stem>.commented.epub``）
    - llm: OpenAI 兼容 LLM 客户端（复用 epub_commentor.llm.LLM）
    - config: 评注配置
    - on_progress: 进度回调 [0, 1]
    - on_annotation_failed: 单批失败回调（默认 silently skip 该 block）
    - chapter_filter: M8 新增。可选回调，接收 spine 顺序的 chapter 列表，
      返回等长的 ``list[bool]`` —— ``True`` 保留进 LLM 流水线、``False`` 跳过。
      跳过的章节原封不动经 ``Zip.__exit__`` 流回 EPUB（无需"恢复"逻辑）。
      默认 ``None`` = 不过滤。CLI 在 ``-i`` / ``--interactive`` 下用 rich-selector
      多选实现。校验失败（长度不匹配 / 非 bool 元素）抛 ``ValueError``。
    """
```

### 异常类型 (`epub_commentor/errors.py`)

```python
class CommentorError(Exception): ...

class CommentInvalidJSONError(CommentorError):
    """LLM 返回的 JSON 无法被 schema 解析（重试耗尽后抛出）。"""

class CommentOrphanPIdError(CommentorError):
    """comment.target_p_ids 引用了 block 范围外的 p_id，或非连续。"""

class CommentOverlapError(CommentorError):
    """同一个 block 内两条 comment 的 target_p_ids 互相重叠。"""

class CommentScanFailedError(CommentorError):
    """Stage 1 全章扫读在 max_scan_retries 次后仍失败。"""

class CommentNoParagraphsError(CommentorError):
    """章节没有 <p> 元素（poetry / list-only），跳过而非失败。"""
```

---

## 关键模块设计

### Stage 1 — `pipeline/process.py:scan_chapter()`

```python
def scan_chapter(
    body: ET.Element,
    chapter_path: Path,
    chapter_title: str,
    book_synopsis: str | None,
    book_metadata: dict[str, str],
    llm: LLM,
    config: CommentConfig,
) -> ChapterMemo:
    """Stage 1: 对单章做全章扫读，返回 ChapterMemo。"""
    chapter_hash = sha1(chapter_path.as_posix().encode()).hexdigest()[:8]
    seed = f"commentor:{VERSION}:scan:{config.cache_seed_user_id}:{chapter_hash}"
    
    system_text = llm.template("scan").render(
        target_language=...,
        book_synopsis=book_synopsis or "（用户未提供）",
    )
    user_text = format_scan_user(
        chapter_title=chapter_title,
        book_metadata=book_metadata,
        chapter_full_text=plain_text(body),
    )
    
    with llm.context(cache_seed_content=seed) as ctx:
        raw = ctx.request([
            Message(SYSTEM, system_text),
            Message(USER, user_text),
        ])
    
    return ChapterMemo.model_validate_json(retry_parse(raw, schema=ChapterMemo))
```

### Stage 2 — `pipeline/process.py:annotate_block()`

```python
def annotate_block(
    body: ET.Element,
    block_ps: list[ET.Element],
    block_index: int,
    chapter_path: Path,
    chapter_hash: str,
    memo: ChapterMemo,
    book_synopsis: str | None,
    llm: LLM,
    config: CommentConfig,
) -> list[CommentItem]:
    """Stage 2: 对一个 6-段 block 做批注，返回 CommentItem 列表。"""
    # 1. 给 block 内的 <p> 打 data-p-id
    for idx, p in enumerate(block_ps):
        p.set("data-p-id", str(idx))
    
    # 2. 拼 block HTML
    block_html = "\n".join(ET.tostring(p, encoding="unicode") for p in block_ps)
    
    # 3. 调用 LLM
    seed = f"commentor:{VERSION}:annotate:{config.cache_seed_user_id}:{chapter_hash}:{block_index}"
    system_text = llm.template("annotate").render(
        target_language=...,
        position=config.position.value,
        allowed_kinds_csv=",".join(k.value for k in config.kinds),
        block_size=len(block_ps),
    )
    user_text = format_annotate_user(
        book_synopsis=book_synopsis or "（用户未提供）",
        memo=memo,
        block_index=block_index,
        block_html=block_html,
    )
    
    annotations: list[CommentItem] = []
    with llm.context(cache_seed_content=seed) as ctx:
        messages = [
            Message(SYSTEM, system_text),
            Message(USER, user_text),
        ]
        for retry in range(config.max_json_retries):
            raw = ctx.request(messages)
            try:
                parsed = BlockAnnotation.model_validate_json(retry_parse(raw, schema=BlockAnnotation))
                annotations = validate_block_annotations(parsed, block_size=len(block_ps))
                break
            except (ValidationError, CommentOrphanPIdError, CommentOverlapError) as e:
                if retry == config.max_json_retries - 1:
                    raise CommentInvalidJSONError(...) from e
                messages.append(Message(ASSISTANT, raw))
                messages.append(Message(USER, format_validation_error(e, raw)))
    
    return annotations
```

### DOM 注入 — `pipeline/inject.py:inject_comment()`

```python
def inject_comment(
    body: ET.Element,
    target_p: ET.Element,
    position: CommentPosition,
    kind: CommentKind,
    content: str,
    cmt_id: str,
) -> None:
    """把 <aside> 注入到 target_p 的前/后。处理嵌套容器。"""
    # 沿 <p> 父链向上找最近的 body 直接子元素（避免把 <aside> 塞进 <p>）
    inject_parent = target_p
    while inject_parent.getparent() is not None and inject_parent.getparent() is not body:
        inject_parent = inject_parent.getparent()
    
    idx = list(inject_parent).index(target_p)
    aside = ET.Element("aside")
    aside.set("class", f"commentary commentary-{kind.value}")
    aside.set("data-cmt-id", cmt_id)
    aside.set("data-cmt-kind", kind.value)
    aside.text = content
    
    final_idx = idx if position == CommentPosition.BEFORE else idx + 1
    inject_parent.insert(final_idx, aside)
```

> **关键**：循环注入时，**在当前 block 全部处理完后才统一调 `deduplicate_ids_in_element()`**，并在 `body.save()` 之前**清除所有临时 `data-p-id` 属性**（`for p in body.findall(".//p"): p.attrib.pop("data-p-id", None)`）。

### CSS 注入 — `commentor.py:inject_commentary_style()`

```python
def inject_commentary_style(zip: Zip, opf_path: Path, chapter_paths: list[Path]) -> None:
    # 1. 写入 CSS 文件
    css_bytes = (Path(files("epub_commentor")) / "data" / "commentary.css").read_bytes()
    zip.add(Path("Styles/commentary.css"), css_bytes)
    
    # 2. 更新 OPF manifest
    opf_xml = XMLLikeNode(zip.replace(opf_path), is_html_like=False)
    manifest = find_first(opf_xml.element, "manifest")
    if manifest is not None and find_first(manifest, 'item[@id="commentary-css"]') is None:
        ET.SubElement(manifest, "item", {
            "id": "commentary-css",
            "href": "Styles/commentary.css",
            "media-type": "text/css",
        })
    opf_xml.save(zip.replace(opf_path))
    
    # 3. 每个 chapter <head> 加 <link>
    for chapter_path in chapter_paths:
        with zip.replace(chapter_path) as f:
            chapter_xml = XMLLikeNode(f, is_html_like=False)
            head = find_first(chapter_xml.element, "head")
            if head is not None and not _has_commentary_link(head):
                ET.SubElement(head, "link", {
                    "rel": "stylesheet",
                    "type": "text/css",
                    "href": "../Styles/commentary.css",
                })
            chapter_xml.save(zip.replace(chapter_path))
```

### `Zip.add()`（必须 fork 加）

在 `epub_commentor/epub/zip.py` 末尾追加：

```python
def add(self, path: Path, data: bytes | IO[bytes]) -> None:
    """添加一个新文件到 target ZIP（不复制 source 已有文件）。"""
    if isinstance(data, (bytes, bytearray)):
        self._target_zip.writestr(path.as_posix(), bytes(data), compress_type=zipfile.ZIP_DEFLATED)
    else:
        with data as f:
            self._target_zip.writestr(path.as_posix(), f.read(), compress_type=zipfile.ZIP_DEFLATED)
    self._processed_files.add(path)
```

### Pydantic Schema — `llm/schema.py`

```python
from pydantic import BaseModel, Field
from enum import Enum

class KeyTerm(BaseModel):
    term: str
    gloss: str

class ChapterMemo(BaseModel):
    core_thesis: str = Field(..., min_length=1, max_length=2000)
    outline: list[str] = Field(..., min_length=3, max_length=7)
    key_terms: list[KeyTerm] = Field(default_factory=list, max_length=15)
    tone: str
    target_audience: str
    reading_anchors: list[str] = Field(default_factory=list, max_length=3)

class CommentPosition(str, Enum):
    BEFORE = "before"
    AFTER = "after"

class CommentKind(str, Enum):
    INTRO = "intro"
    SUMMARY = "summary"
    NOTE = "note"

class CommentItem(BaseModel):
    target_p_ids: list[int] = Field(..., min_length=1)
    position: CommentPosition
    kind: CommentKind
    content: str = Field(..., min_length=1, max_length=2000)

class BlockAnnotation(BaseModel):
    comments: list[CommentItem] = Field(default_factory=list)
```

新增依赖：`pydantic>=2.5,<3.0`

### `validate_block_annotations()` — `llm/schema.py`

```python
def validate_block_annotations(ann: BlockAnnotation, block_size: int) -> list[CommentItem]:
    for c in ann.comments:
        # p_id 必须在 [0, block_size) 范围
        for pid in c.target_p_ids:
            if pid < 0 or pid >= block_size:
                raise CommentOrphanPIdError(...)
        # 必须连续
        sorted_pids = sorted(c.target_p_ids)
        if sorted_pids != list(range(sorted_pids[0], sorted_pids[-1] + 1)):
            raise CommentOrphanPIdError(...)
    
    # 检查 block 内重叠
    used: set[int] = set()
    for c in ann.comments:
        for pid in c.target_p_ids:
            if pid in used:
                raise CommentOverlapError(...)
            used.add(pid)
    
    return ann.comments
```

---

## 提示词模板

### `data/scan.jinja`（Stage 1）

```jinja
You are a meticulous literary analyst. Your job is to scan a full chapter
and produce a JSON "memo" that will guide downstream annotation.

Inputs you will receive:
- Book synopsis (may be empty)
- Book metadata
- Chapter title
- Chapter full text (paragraphs separated by newlines, plain text)

Output a JSON object with this schema:
{
  "core_thesis":     "1-3 sentences, what this chapter is about",
  "outline":         ["topic 1", "topic 2", ...],   // 3-7 items, in order
  "key_terms":       [{"term": "...", "gloss": "..."}],  // 5-15 items
  "tone":            "e.g. polemic / lyrical / didactic",
  "target_audience": "who the author is writing for",
  "reading_anchors": ["1-3 sentences that anchor the chapter's argument"]
}

Rules:
- Do NOT annotate individual paragraphs.
- Output ONLY the JSON object, no markdown fences, no commentary.
- Use {{ target_language }} for content fields.
```

### `data/annotate.jinja`（Stage 2）

```jinja
You are an AI annotator writing marginalia for a serious ebook reader.

You will receive:
- A book synopsis (may be empty)
- A chapter memo (the "what is this chapter about" summary from a prior pass)
- A small block of {{ block_size }} consecutive paragraphs (p_id 0..{{ block_size - 1 }})
- The user's preferred default position: "{{ default_position }}" — before | after
- Allowed kinds: {{ allowed_kinds_csv }}  (intro / summary / note)

Your job: emit a JSON object listing annotations to attach to this block.

Schema (return ONLY this object, no markdown, no prose):
{
  "comments": [
    {
      "target_p_ids": [int, ...],          // contiguous subset of 0..{{ block_size - 1 }}
      "position": "before" | "after",      // relative to LAST target_p
      "kind": "intro" | "summary" | "note",
      "content": "1-4 sentences in {{ target_language }}"
    }
  ]
}

Rules:
- target_p_ids MUST be contiguous integers (e.g. [3,4,5] OK; [2,4] NOT OK).
- position="before" means the comment appears BEFORE the FIRST p in target_p_ids.
- position="after"  means the comment appears AFTER  the LAST  p in target_p_ids.
- intro: 1-3 sentence scene-setting placed BEFORE the first target_p (kind=intro, position=before preferred).
- summary: 1-3 sentence synthesis placed AFTER the last target_p (kind=summary, position=after preferred).
- note: short gloss on a specific term/concept, can be before or after target_p.
- If no paragraph deserves annotation, return {"comments": []}.
- Do NOT quote the original text verbatim — paraphrase or summarize.
- Do NOT add meta-commentary ("this paragraph talks about..."); speak directly to the reader.
```

### `data/commentary.css`（e-ink 优化）

```css
/* e-ink 优化：避免彩色和阴影，用 border + 灰度对比 */

.commentary {
  display: block;
  margin: 0.6em 1em;
  padding: 0.5em 0.8em;
  border-left: 2px solid #555;
  font-size: 0.88em;
  line-height: 1.45;
  color: #222;
  page-break-inside: avoid;
  break-inside: avoid;
}

.commentary-intro {
  background: #f0f0f0;
  border-left: 3px solid #333;
}

.commentary-summary {
  font-weight: 500;
  border-left: 3px solid #000;
}

.commentary-note {
  font-size: 0.82em;
  color: #444;
  margin: 0.3em 1.5em;
  border-left: 1px dashed #888;
}

@media (prefers-color-scheme: dark) {
  .commentary { color: #ddd; border-left-color: #aaa; }
  .commentary-intro { background: #1a1a1a; }
  .commentary-summary { border-left-color: #fff; }
  .commentary-note { color: #bbb; border-left-color: #666; }
}
```

---

## CLI（`scripts/comment_epub.py`）

风格沿用 `translate_epub.py:1-97`：

```python
import argparse
import sys
from pathlib import Path
from tqdm import tqdm

# ... 复用 scripts/utils.py:load_comment_llm()

def main():
    parser = argparse.ArgumentParser(description="Add AI commentary to EPUB files")
    parser.add_argument("source_path", type=str)
    parser.add_argument("-o", "--output", type=str, default=None,
                        help="Target EPUB path (default: <source>.commented.epub)")
    parser.add_argument("--block-size", type=int, default=6)
    parser.add_argument("--position", choices=["before", "after"], default="before")
    parser.add_argument("--kinds", default="intro,summary,note",
                        help="Comma-separated kinds to allow")
    parser.add_argument("--synopsis", type=str, default=None,
                        help="Optional book synopsis text (or @file.txt)")
    parser.add_argument("--user-id", type=str, default="default")
    parser.add_argument("--no-css", action="store_true")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="M8: 解析完成后弹 rich-selector 多选让用户勾选要评注的章节"
                             "（空章节默认不勾选；非 TTY 直接 exit 2；Esc/Q/Ctrl-C → exit 130）")
    args = parser.parse_args()
    
    if args.synopsis and args.synopsis.startswith("@"):
        args.synopsis = Path(args.synopsis[1:]).read_text(encoding="utf-8")
    
    # ... 调 comment_epub() + tqdm 进度条 + 末尾 token 统计
```

`[project.scripts]` entrypoint：

```toml
[project.scripts]
epub-commentor = "epub_commentor.cli:main"
```

---

## 关键文件清单（实施时聚焦）

### 必须新增

- `epub_commentor/pipeline/__init__.py`
- `epub_commentor/pipeline/extract.py`
- `epub_commentor/pipeline/process.py`
- `epub_commentor/pipeline/inject.py`
- `epub_commentor/llm/memo.py`
- `epub_commentor/llm/block.py`
- `epub_commentor/llm/schema.py`
- `epub_commentor/data/scan.jinja`
- `epub_commentor/data/annotate.jinja`
- `epub_commentor/data/commentary.css`
- `epub_commentor/errors.py`
- `epub_commentor/events.py`
- `epub_commentor/commentor.py`
- `epub_commentor/cli.py`
- `scripts/comment_epub.py`
- `tests/_mock_llm.py`
- `tests/test_commentor_*.py`（7 个文件）
- `tests/commentary_challenge/case*.json`（10 个文件）

### 必须修改

- `pyproject.toml` — name、packages、加 pydantic、加 entrypoint
- `epub_commentor/__init__.py` — 公开 `comment_epub`, `CommentConfig`, `LLM`, ...
- `epub_commentor/epub/zip.py` — 加 `add()` 方法
- `epub_commentor/llm/__init__.py` — 移除 `LLMContext` 等不必要 export（仅保留 `LLM`, `Message`, `MessageRole`）
- `scripts/utils.py` — `load_comment_llm()` 替代 `load_llm()`

### 必须删除

- `epub_commentor/segment/`（整个目录）
- `epub_commentor/translation/`（整个目录）
- `epub_commentor/xml_translator/`（整个目录）
- `epub_commentor/data/translate.jinja`、`fill.jinja`
- `epub_commentor/xml/friendly/`（仅在 Stage 2 不用，评估后可删）
- `scripts/translate_epub.py`、`translate_xml.py`、`translate_challenge.py`
- `tests/test_inline_segment.py`、`test_block_segment.py`、`test_text_segment.py`、`test_submitter.py`、`test_score.py`、`test_serial.py`、`test_hill_climbing.py`、`test_epub_transcode.py`
- `tests/challenge/`

### 复用的关键文件（不修改）

- `epub_commentor/llm/core.py:LLM` — OpenAI 客户端（含 cache/log/statistics）
- `epub_commentor/llm/context.py:LLMContext` — 上下文（多轮 + cache_seed 命名空间）
- `epub_commentor/llm/executor.py:LLMExecutor` — 流式 + 重试
- `epub_commentor/llm/statistics.py:Statistics` — thread-safe token 累加
- `epub_commentor/llm/increasable.py:Increasable` — temperature 区间衰减
- `epub_commentor/llm/error.py` — 可重试错误判定
- `epub_commentor/xml/xml_like.py:XMLLikeNode` — DOM 解析/序列化（**关键复用**）
- `epub_commentor/xml/xml.py:find_first` / `iter_with_stack`
- `epub_commentor/xml/deduplication.py:deduplicate_ids_in_element`
- `epub_commentor/xml/self_closing.py`
- `epub_commentor/epub/common.py:find_opf_path`
- `epub_commentor/epub/spines.py:search_spine_paths`
- `epub_commentor/epub/toc.py:read_toc` / `write_toc`
- `epub_commentor/epub/metadata.py:read_metadata` / `write_metadata`
- `epub_commentor/epub/math.py:xml_to_latex`（可选，评注项目也用得上）
- `tests/utils.py:create_temp_dir_fixture`

---

## 实施里程碑

| 阶段 | 产出 | 估时 |
|---|---|---|
| **M1 重构** | 改名 + 删翻译代码 + 保留基础设施 + `Zip.add()` + 改 `__init__.py` + `pyproject.toml` | 1-2 天 |
| **M2 数据流** | `pipeline/extract.py` + `pipeline/process.py` 单章 stage 1+2 跑通 | 1-2 天 |
| **M3 注入** | `pipeline/inject.py` + `commentary.css` + `Zip.add()` + OPF 更新 + chapter head link | 1-2 天 |
| **M4 校验** | `llm/schema.py` pydantic + retry 逻辑 + `errors.py` | 1 天 |
| **M5 测试** | `tests/_mock_llm.py` + 7 个单元测试 + 10 个 challenge case | 2-3 天 |
| **M6 CLI** | `scripts/comment_epub.py` + entrypoint + `format.template.json` 评注版 | 0.5-1 天 |
| **M7 真实 LLM 联调** | 用 OpenAI 跑《The little prince》全本，验证样式、缓存、token 用量、Kindle 兼容性 | 1-2 天 |
| **M8 交互式章节选择** | `ChapterFilter = Callable[[list[Chapter]], list[bool]]` 类型别名 + `comment_epub(chapter_filter=...)` 可选 kwarg + `-i/--interactive` CLI 旗标（rich-selector 多选弹窗，空章节默认不勾选；非 TTY 直接 exit 2；Esc/Q/Ctrl-C → exit 130；rich Progress 在 `-i` 下自动静默让出终端） + `tests/test_commentor_pipeline.py::TestChapterFilter`（5 个用例）+ `tests/test_commentor_cli.py::TestBuildChapterFilter`（7 个用例，含 Esc/Q + Ctrl-C 两条新分支） | 0.5-1 天 |

**总计：8-13 天**

---

## 风险与边界情况

| 风险 | 缓解 |
|---|---|
| 长章节（500+ `<p>`）Stage 1 超上下文 | 截断到估算窗口 60%，附 `<truncation_notice>` |
| 无 `<p>` 章节（poetry / list） | 检测到 0 个 `<p>` → 跳过，log warning，不抛错 |
| LLM JSON 不稳定 | pydantic 校验 + 3 次 retry + 失败回调 |
| 评注重叠 | 同一 block 内重叠 → `CommentOverlapError` → retry；跨 block 不检测（天然不交） |
| `<p>` 嵌套在 `<blockquote>` | `inject_comment()` 沿父链向上找 body 直接子元素 |
| `Zip.add()` 与原仓库冲突 | 改动仅在 `epub_commentor/epub/zip.py`（fork 内部），不向上游 push |
| `data-p-id` 残留 | 注入完成后整章扫描清除 |
| 评注 id 与原书 id 冲突 | 命名空间 `cmt-...` 前缀 + `deduplicate_ids_in_element` 防御 |
| LLM 偏离 system prompt 输出 markdown 围栏 | 在 `retry_parse` 里用正则 `re.search(r"\{.*\}", raw, re.DOTALL)` 容忍围栏；pydantic 仍报错则 retry |
| 中文 unicode escape | `pydantic` 解析字符串字段时会自动 decode `\uXXXX` |

---

## 验证（Verification）

### 1. 单元测试

```bash
poetry run pytest tests/test_commentor_*.py -v
```

### 2. Challenge 集回归

```bash
poetry run python scripts/comment_challenge.py           # 跑全部 10 个 case
poetry run python scripts/comment_challenge.py case5     # 单 case
```

（`comment_challenge.py` 类似 `translate_challenge.py`，用 `MockLLM` 跑 challenge 样例。）

### 3. 端到端 mock 测试

```bash
poetry run pytest tests/test_commentor_e2e.py -v
# 跑通即说明：The little prince 全本用 mock LLM 处理后产生合法 EPUB
```

### 4. 真实 LLM 联调（小王子全本）

```bash
# 1. 准备 format.json（用真实 API key）
cp format.template.json format.json
# 编辑 format.json，填入 key/url/model/token_encoding

# 2. 跑
poetry run python scripts/comment_epub.py "tests/assets/The little prince.epub" \
    --synopsis "小王子是一个来自遥远星球的王子，他在旅途中遇见了各种各样的大人..." \
    --position before \
    --block-size 6 \
    --concurrency 4

# 3. 用 epubcheck 验证
epubcheck "tests/temp/comment_epub/The little prince.commented.epub"

# 4. 用 calibre 或 Kindle Previewer 打开，目检样式
```

### 5. 手工检查项（真实 LLM 跑完后）

- [ ] `mimetype` 是 ZIP 第一个 entry
- [ ] `Styles/commentary.css` 存在且非空
- [ ] OPF `<manifest>` 含 `commentary-css` item
- [ ] 每个 chapter `<head>` 含 `<link rel="stylesheet">`
- [ ] 至少 N 个 chapter 含 `<aside class="commentary commentary-{kind}">`（N 应 > 章节数 × 50%）
- [ ] Kindle/macOS Books/iBooks 打开样式正常
- [ ] 进度条走完、token 统计打印无异常

### 6. 缓存验证

```bash
# 跑两次相同命令，第二次应秒返（cache 命中）
poetry run python scripts/comment_epub.py source.epub ...
poetry run python scripts/comment_epub.py source.epub ...
# 看 token 统计：第二次的 input_cache_tokens 应远高于 input_tokens
```

---

## 实施前置条件

- Python 3.11+（沿用 `pyproject.toml`）
- 用户需在 `format.json` 填入 OpenAI 兼容 API key/URL/model/token_encoding
- 推荐先在 `tests/assets/The little prince.epub` 上跑通（28 章结构清晰，体积小）

---

## 后续迭代方向（v1 不做）

- 行内夹注（`<span class="note">` + 选词 anchor）
- 用户传入章节级 synopsis（`--chapter-synopsis FILE.json`）
- Stage 1 跨章节并行（v1 sequential 简单稳定）
- 评注质量评分（让 LLM 自评 + 二次修订）
- 在评注里加 `<a href="#ref-...">` 双向跳转锚点
- 组合模式："先评注后翻译" pipeline
