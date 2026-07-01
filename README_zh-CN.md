<div align=center>
  <h1>EPUB Commentor</h1>
  <p>
    <a href="https://github.com/noau/epub-commentor/actions/workflows/merge-build.yml" target="_blank"><img src="https://img.shields.io/github/actions/workflow/status/noau/epub-commentor/merge-build.yml" alt="ci" /></a>
    <a href="https://github.com/noau/epub-commentor/blob/main/LICENSE" target="_blank"><img src="https://img.shields.io/github/license/noau/epub-commentor" alt="许可证" /></a>
  </p>
  <p><a href="./README.md">English</a> | 中文</p>
</div>

> 本项目含有使用 LLM 生成的代码或文档等内容。

**EPUB Commentor** 读入一本 EPUB，把*同一本书*还给你——原文一字不改——只在正文旁边加上 AI 撰写的阅读陪伴：段落之前的**导读**、段落之后的**总结**，以及零星的**夹注**（针对难懂的词句）。这些评注以安静、样式化的侧边块呈现，Kindle、Kobo 等电子墨水屏可原生显示。导入即读。

**实际效果** — 原文一字不改，模型只在四周加上带边框的评注块：

<p align="center">
  <img src="./docs/imgs/example.png" alt="示例"
       style="max-width: 560px; width: 100%; height: auto;" />
</p>

<p align="center"><sub>示例取自老舍《茶馆》。</sub></p>

## 你会得到什么

- **原书原封不动。** 没有任何段落被改写、翻译或重排。Commentor 只在正文旁边*新增*内容。
- **三类陪伴式评注**，全部由模型撰写：
  - **导读（intro）** — 1–3 句的开场白，放在段落*之前*，让你对接下来的内容先有预期。
  - **总结（summary）** — 1–3 句的收尾，放在段落*之后*，把这段内容串起来。
  - **夹注（note）** — 针对某个具体词句或概念的简短说明。
- **评注语言任你选** — 读英文书配中文注，或反过来都行。
- **专为墨水屏优化的样式** — 灰度、无彩色无阴影，评注块换页时不会被拆开。
- **一份可直接阅读的 `.epub`**，就写在源文件旁边。无需任何后处理——拖进阅读器即可。

---

## 目录

- [你会得到什么](#你会得到什么)
- [目录](#目录)
- [安装](#安装)
- [准备一个 API key](#准备一个-api-key)
- [配置 `format.json`](#配置-formatjson)
  - [服务商示例](#服务商示例)
  - [`format.json` 也能写流水线选项](#formatjson-也能写流水线选项)
- [运行](#运行)
  - [一次真实的首跑](#一次真实的首跑)
- [交互式挑选章节](#交互式挑选章节)
- [调整评注效果](#调整评注效果)
- [命令参数一览](#命令参数一览)
- [在你的设备上阅读](#在你的设备上阅读)
- [用缓存省钱](#用缓存省钱)
- [出问题时怎么办](#出问题时怎么办)
  - [常见问题](#常见问题)
- [用 Python 调用](#用-python-调用)
  - [观察进度](#观察进度)
  - [用代码挑选章节](#用代码挑选章节)
  - [`CommentConfig` 选项](#commentconfig-选项)
- [常见问题](#常见问题-1)
- [许可证](#许可证)
- [支持](#支持)

---

## 安装

你需要 **Python 3.13+** 和 [Poetry](https://python-poetry.org/)（Python 的依赖管理工具）。

```bash
git clone https://github.com/noau/epub-commentor.git
cd epub-commentor
poetry install
```

就这么简单——`poetry install` 会把所有依赖装进一个独立环境。之后所有命令都加上 `poetry run` 前缀，让它们用这个环境。

> **没装 Poetry？** 用 `pipx install poetry` 一次装好（或参考[官方指南](https://python-poetry.org/docs/#installation)）。

---

## 准备一个 API key

Commentor 可以对接任何 **OpenAI 兼容**的对话 API——包括 OpenAI 本身、Azure OpenAI，以及大多数自建或第三方网关（DeepSeek、Together、Groq、带 OpenAI 兼容层的本地 Ollama 等）。你需要从服务商那里拿到三样东西：

1. **API key**（一串密钥，通常以 `sk-...` 开头）。
2. API 的**基础地址（base URL）**（以 `/v1` 结尾的那部分）。
3. 你要用的**模型名称**。

下一步会用到，先备好。

---

## 配置 `format.json`

Commentor 从一个叫 `format.json` 的文件读取凭据。复制模板即可创建：

```bash
cp format.template.json format.json
```

然后打开 `format.json` 填写。下面是一份**每个字段都有说明**的完整示例——你实际*只需*改 `key`、`url`、`model`、`token_encoding` 四项：

```json
{
  "key": "sk-your-secret-api-key",
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "token_encoding": "o200k_base",
  "timeout": 360.0,
  "retry_times": 5,
  "retry_interval_seconds": 6.0,
  "temperature": 0.4,
  "top_p": 0.9,
  "cache_path": "./commentary_cache",
  "log_dir_path": null
}
```

| 字段 | 必填？ | 填什么 | 说明 |
|---|---|---|---|
| `key` | **是** | 你的 API key。 | 请保密——别把 `format.json` 提交到公开仓库。 |
| `url` | **是** | API 基础地址，以 `/v1` 结尾。 | 见下方服务商对照表。 |
| `model` | **是** | 模型名称。 | 如 `gpt-4o`、`deepseek-chat`，或你的 Azure 部署名。 |
| `token_encoding` | **是** | 你模型使用的分词器名称。 | 仅用于在进度条里统计 token。GPT-4o / GPT-4.1 / o 系列用 `o200k_base`，较老的 GPT-4 / GPT-3.5 用 `cl100k_base`。拿不准就用 `o200k_base`。 |
| `timeout` | 否 | 单次响应最多等多少秒。 | `360.0` 对长章节比较从容。填 `null` 表示不限时。 |
| `retry_times` | 否 | 网络失败时重试几次。 | 默认 `5`。 |
| `retry_interval_seconds` | 否 | 两次重试之间等待的秒数。 | 默认 `6.0`。 |
| `temperature` | 否 | 文字的发挥程度，`0.0`–`1.0`。 | `0.4` 让评注既有文采又不跑题。越高越多变，越低越贴字面。 |
| `top_p` | 否 | temperature 的替代项（核采样）。 | 保持 `0.9` 即可，或填 `null` 忽略它。 |
| `cache_path` | 否 | 存放响应的文件夹，让重跑免费。 | 见[用缓存省钱](#用缓存省钱)。留空或 `null` 表示不缓存。 |
| `log_dir_path` | 否 | 存放调试日志的文件夹。 | `null` = 关闭。见[出问题时怎么办](#出问题时怎么办)。 |

### 服务商示例

| 服务商 | `url` | `model`（示例） | `token_encoding` |
|---|---|---|---|
| OpenAI | `https://api.openai.com/v1` | `gpt-4o` | `o200k_base` |
| Azure OpenAI | `https://<resource>.openai.azure.com/openai/deployments/<deployment>` | *你的部署名* | 与模型匹配 |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` | `cl100k_base` |
| 任意 OpenAI 兼容服务 | `https://your-service.com/v1` | *因服务商而异* | 与模型分词器匹配 |

> **`format.json` 可以放在书旁边。** Commentor 按以下顺序查找它：`--format-json` 指定的路径、源 EPUB 同目录、当前工作目录。

### `format.json` 也能写流水线选项

`format.json` 不只放凭据。你可以把任意**流水线选项**写进同一个扁平文件，作为持久默认值——省得每次运行都重敲同样的旗标：

```json
{
  "key": "sk-your-secret-api-key",
  "url": "https://api.openai.com/v1",
  "model": "gpt-4o",
  "token_encoding": "o200k_base",

  "concurrency": 8,
  "block_size": 8,
  "target_language": "Chinese",
  "book_synopsis": "一本讲述迫降飞行员的哲学童话。"
}
```

[`CommentConfig` 选项表](#commentconfig-选项)里的任意字段都可以写在这里——`concurrency`、`block_size`、`target_language`、`book_synopsis`、`position`、`kinds`、`max_json_retries` 等等。两条规则：

- **用配置字段名，且命令行旗标优先。** `format.json` 里的值只是*默认值*；传入对应旗标（如 `--concurrency 4`）会覆盖它。注意文件里用的是配置字段名（`book_synopsis`、`cache_seed_user_id`），而非旗标写法（`--synopsis`、`--cache-user-id`）。
- **无法识别的键会被忽略**并在 stderr 打一条警告——拼错一个键不会让运行崩溃。

---

## 运行

最基本的命令接收一本 EPUB，外加（可选的）一句话简介来定调：

```bash
poetry run epub-commentor "path/to/book.epub" --synopsis "一本讲述迫降飞行员的哲学童话。"
```

跑完后会显示一个总结面板，并在原文件旁边生成一个名为 **`book.commented.epub`** 的新文件。那就是拿来读的文件。

想指定输出位置？用 `-o`：

```bash
poetry run epub-commentor "book.epub" -o "~/Kindle/book-annotated.epub" --synopsis "..."
```

**`--synopsis`** 可选但强烈建议填——关于这本书的一句话能帮模型把评注调到合适的层次。不填也能跑，Commentor 会改用书自带的元数据。

### 一次真实的首跑

```bash
poetry run epub-commentor "The little prince.epub" \
    --synopsis "一个飞行员在撒哈拉迫降后遇到小王子的诗意故事。" \
    --target-language "Chinese"
```

运行时会看到实时进度：上行跟踪章节（`Ch. 3/28: ...`），下行跟踪当前章节内部的小批次。长书要跑一阵，也会真实消耗 API token——结尾的总结会精确列出用了多少 token。

---

## 交互式挑选章节

大多数 EPUB 不只有正文章节：封面、目录、版权页都在里面。想精确挑选要评注的内容，加上 **`-i`**（interactive，交互式）：

```bash
poetry run epub-commentor "book.epub" --synopsis "..." -i
```

会弹出一个包含每一章的勾选列表。操作方式：

| 按键 | 动作 |
|---|---|
| `↑` / `↓` | 上 / 下移动 |
| `Space` 或 `Enter` | 切换当前高亮章节的勾选 |
| `A` | 全选 |
| `I` | 反选 |
| `C` | 清空 |
| 移到 `[ Confirm ]` + `Enter` | 开始评注已选章节 |
| `Esc` / `Q` | 取消退出 |

没有正文的章节（封面、导航、纯图页）会**预先取消勾选**——所以你常常直接在 `[ Confirm ]` 上按 `Enter` 就能一键跳过所有杂项。没勾选的部分会原样复制进输出文件。

> `-i` 需要真实终端。若通过管道输入或在脚本里运行，它会直接报错而不是瞎猜。

---

## 调整评注效果

评注效果由几个可选旗标控制，全部可省略。

- **`--target-language "Chinese"`** — 评注使用的语言。书本身永远不会被翻译，只有新增的评注用这个语言。默认中文。
- **`--synopsis "..."`** — 一句话书籍简介，用来定调。
- **`--block-size 6`** — 模型每批看多少个段落。越小越细致（注更多但 API 调用更多、更贵）；越大越概括、越省。默认 `6`。
- **`--concurrency 4`** — 一个章节内同时处理几个批次。越高跑得越快，但更容易撞上 API 的速率限制。默认 `4`。
- **`--no-css`** — 只注入评注块，不带内置样式（进阶用法；当你要自备样式表时用）。

---

## 命令参数一览

随时用 `poetry run epub-commentor --help` 查看权威列表。除 `source` 外，下列参数均可选。

| 参数 | 含义 |
|---|---|
| `source` | 要评注的 EPUB 路径。**必填。** 只读——原文件永不被改。 |
| `-o`, `--output PATH` | 输出位置。默认：源文件旁的 `<名字>.commented.epub`。 |
| `--format-json PATH` | 从哪里读凭据。默认：源文件旁的 `format.json`，其次当前目录。 |
| `--synopsis TEXT` | 一句话书籍简介，用于定调。 |
| `--target-language LANG` | 评注使用的语言。默认 `Chinese`。 |
| `--block-size N` | 每批段落数。默认 `6`。 |
| `--concurrency N` | 章节内同时处理的批次数。默认 `4`。 |
| `--max-json-retries N` | 某批评注格式错误时的重试次数。默认 `3`。 |
| `--max-scan-retries N` | 章节概览格式错误时的重试次数。默认 `3`。 |
| `--cache-path DIR` | 缓存响应的文件夹（让重跑免费）。 |
| `--css-path PATH` | 样式表在 EPUB 内的路径。默认 `Styles/commentary.css`。 |
| `--no-css` | 不注入样式表。 |
| `--fail-on-empty-chapter` | 遇到无段落的章节时报错，而不是跳过。 |
| `--log-dir DIR` | 把详细调试日志写到这个文件夹。 |
| `--debug` | 开启调试日志（日志文件夹默认 `./temp/logs/`）。 |
| `--cache-user-id ID` | 缓存命名空间。换个值可为新书/新用户强制重新生成。 |
| `-i`, `--interactive` | 运行前从勾选列表挑选章节。 |
| `-q`, `--quiet` | 关闭进度显示和结尾总结。 |

---

## 在你的设备上阅读

输出是标准 `.epub`。阅读方式：

- **Kindle** — 把文件发到你的 [Send to Kindle](https://www.amazon.com/sendtokindle) 邮箱，或拖进 Send to Kindle 桌面应用。（新版 Kindle 直接支持 EPUB。）
- **Kobo / PocketBook / 其他墨水屏** — 用 USB 把 `.epub` 拷进设备，或通过设备的图书库应用添加。
- **Calibre** — 直接把文件加入书库，评注样式会一并带上。
- **Apple Books / Google Play Books** — 直接导入文件。

评注会以带边框的侧边块出现在正文中，样式经过设计，在灰度屏上依然清晰易读。

---

## 用缓存省钱

调用 LLM 是要花钱的，一本长书就是很多次调用。如果你设置了**缓存文件夹**，Commentor 会记住每一次响应——于是当你重跑同一本书（比如改了某一章之后，或崩溃之后重来），已经做过的部分会瞬间、免费地返回。

```bash
poetry run epub-commentor "book.epub" --synopsis "..." --cache-path ./commentary_cache
```

也可以在 `format.json` 里设 `"cache_path": "./commentary_cache"`，让它始终开启。

缓存以书的内容加上你的设置为键，所以改了简介、语言或模型都会正确地重新生成评注。如果你想对同一本书刻意重头再来，删掉缓存文件夹，或传一个新的 `--cache-user-id`。

---

## 出问题时怎么办

如果某次运行失败，或评注看起来不对劲，开启**调试日志**就能看到模型到底被问了什么、又回了什么：

```bash
poetry run epub-commentor "book.epub" --synopsis "..." --debug
# 日志落在 ./temp/logs/ —— 每次请求一个文件
```

每份日志记录完整的请求、原始响应，以及——如果某批需要重试——错误信息和模型返回的确切错误内容。当某一章产出奇怪或缺失的评注时，这是第一个该看的地方。

### 常见问题

| 现象 | 可能原因 / 解决 |
|---|---|
| `format.json not found` | 你没复制模板。运行 `cp format.template.json format.json` 并填写。 |
| `format.json is not valid JSON` | 有拼写错误——通常是多了逗号或少了引号。用任意 JSON 校验器检查。 |
| 认证 / 401 错误 | `key` 或 `url` 填错了。对照服务商核对两者。 |
| 长章节超时 | 调大 `format.json` 里的 `timeout`（如 `600.0`），或调小 `--block-size`。 |
| 速率限制错误 | 调小 `--concurrency`（试试 `2` 或 `1`）。 |
| 某章没有评注 | 它可能没有真正的段落（封面或导航页）——这是正常的，会被跳过。用 `-i` 看清各章情况。 |
| `--interactive requires a TTY` | 你在管道或脚本里用了 `-i`。去掉 `-i`，或在正常终端里运行。 |

---

## 用 Python 调用

想用脚本跑？同样的功能就是一次函数调用。

```python
from epub_commentor import LLM, comment_epub, CommentConfig

llm = LLM(
    key="sk-your-api-key",
    url="https://api.openai.com/v1",
    model="gpt-4o",
    token_encoding="o200k_base",
)

config = CommentConfig(
    book_synopsis="一本讲述迫降飞行员的哲学童话。",
    target_language="Chinese",   # 评注语言；书本身不会被翻译
    block_size=6,                # 每批段落数
    concurrency=4,               # 章节内同时处理的批次数
)

result = comment_epub(
    source="book.epub",
    output="book-annotated.epub",  # 可选；默认 <名字>.commented.epub
    llm=llm,
    config=config,
)

print(f"已评注章节: {result.chapters_processed}")
print(f"生成评注数: {result.total_comments}")
print(f"消耗 token: {result.total_tokens}")
```

### 观察进度

传一个 `progress_callback` 即可获得实时更新。最简单的做法是用与 CLI 相同的渲染器：

```python
from epub_commentor import comment_epub, make_default_progress_callback

progress = make_default_progress_callback(quiet=False)  # quiet=True 可静默
comment_epub(source="book.epub", llm=llm, config=config, progress_callback=progress)
```

也可以自己写——回调会收到一个带 `stage`、`current`、`total`、`message` 的 `ProgressEvent`：

```python
def on_progress(event):
    print(f"[{event.stage}] {event.current}/{event.total}  {event.message or ''}")

comment_epub(source="book.epub", llm=llm, config=config, progress_callback=on_progress)
```

### 用代码挑选章节

提供一个 `chapter_filter`，为每章返回一个 `True`/`False`（按阅读顺序）：

```python
from epub_commentor import comment_epub, Chapter

def only_real_chapters(chapters: list[Chapter]) -> list[bool]:
    # 保留真正含有段落的章节
    return [any(True for _ in ch.body.iter("p")) for ch in chapters]

comment_epub(source="book.epub", llm=llm, config=config, chapter_filter=only_real_chapters)
```

### `CommentConfig` 选项

| 选项 | 默认 | 作用 |
|---|---|---|
| `book_synopsis` | `None` | 一句话简介，用于定调。 |
| `target_language` | `"Chinese"` | 评注的语言。 |
| `block_size` | `6` | 每批段落数。 |
| `concurrency` | `4` | 章节内同时处理的批次数。 |
| `kinds` | 三种全开 | 允许哪些评注类型（`INTRO`、`SUMMARY`、`NOTE`）。 |
| `position` | `BEFORE` | 模型未指定时的默认放置位置。 |
| `max_scan_retries` | `3` | 章节概览格式错误时的重试次数。 |
| `max_json_retries` | `3` | 批次评注格式错误时的重试次数。 |
| `inject_css` | `True` | 是否加入内置样式表。 |
| `css_path_in_epub` | `Styles/commentary.css` | 样式表在 EPUB 内的位置。 |
| `fail_on_empty_chapter` | `False` | 遇到无段落章节时报错（而非跳过）。 |
| `cache_seed_user_id` | `"default"` | 缓存命名空间；改它可强制重新生成。 |

若某章重试穷尽仍无法评注，`comment_epub` 会抛出 `CommentorError`——如果想优雅处理失败，捕获它即可：

```python
from epub_commentor import comment_epub, CommentorError

try:
    comment_epub("book.epub", llm=llm)
except CommentorError as exc:
    print(f"失败: {exc}")
```

---

## 常见问题

**它会改动或翻译我的书吗？**
不会。原文一字不差地保留，Commentor 只在旁边*新增*评注块。`--target-language` 控制的是这些新增评注的语言，不是书本身。

**结果能在 Kindle 上读吗？**
能——见[在你的设备上阅读](#在你的设备上阅读)。输出是普通 EPUB，新版 Kindle 及所有其他阅读器都能接受。

**评注一本书要花多少钱？**
取决于书的长度和你模型的定价。每次运行结尾的总结会报告确切的 token 数。用 `--cache-path` 让重跑不重复付费，用 `-i` 跳过不需要的章节。

**一定要用 OpenAI 账号吗？**
不用——任何 OpenAI 兼容 API 都行（OpenAI、Azure、DeepSeek、本地网关……）。把 `url` 和 `model` 指向你的服务商即可。

**为什么某一章被跳过了？**
它没有可读段落——通常是封面、目录或纯图页。这是有意为之。如果你更想被明确告知，传 `--fail-on-empty-chapter`。

## 许可证

MIT，详见 [LICENSE](LICENSE)。Fork 自 [oomol-lab/epub-translator](https://github.com/oomol-lab/epub-translator)，同样条款。

## 支持

有问题或 bug？欢迎提交 [GitHub Issue](https://github.com/noau/epub-commentor/issues)。
