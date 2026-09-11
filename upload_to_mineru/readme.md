# MinerU 批量解析组件(upload_file_to_mineru.py)

将本地 PDF / Word / PPT 文档批量送 **MinerU 云端 API** 解析为 Markdown（含图片、公式、表格）的可复用软件组件。导入即用，无命令行参数、无全局状态，所有配置通过 `MineruConfig` 传入。

# 注意：运行前务必检查.env 文件中是否配置了TOKEN_CLOUD_API_KEY并且且没有过期
# 一般key的有效期为3个月。[检查网址](https://mineru.net/apiManage/token)

---


## 功能特性

- **目录模式**：按类别子目录批量处理，根目录散文件自动归入「未分组」
- **单文件模式**：`input_path` 指向文件时只处理该文件，类型按扩展名自动识别
- **长 PDF 自动切片**：超过 `max_pages`（默认 200 页）的 PDF 本地切片分段上传，可选重叠页缓解跨页表格截断
- **断点续跑**：失败的文件不归档，重跑同一配置自动重试；已切好片的目录直接复用
- **Word/PPT 直传**：不做本地转换，整文件上传 MinerU 云端处理
- **并发控制**：`api_concurrency` 个线程同时持有云端批次
- **结果可追溯**：返回 manifest 列表（成功/失败/页区间/输出位置），可选写出 JSON 清单

---

## 环境依赖

| 依赖            | 说明                                                              |
|-----------------|-------------------------------------------------------------------|
| Python : 3.10-3.13       |                                                                   |
| `pymupdf`       | 本地 PDF 数页 / 切片                                              |
| `mineru-open-sdk` | MinerU 云端 Open API 的 SDK（即 mineru-open-sdk）                 |
| `python-dotenv` | 读取 `.env` 中的凭证                                              |
| 网络            | 需能访问 MinerU 云端 API                                          |
| MinerU 账号     | 需[开通](https://mineru.net/apiManage/docs) Open API 并获取 token |


---
## 输入输出目录结构
```text
<输入根目录>/ ← input_path 指向这里
├── 散置文件A.pdf ← 根目录散文件 → 归入「未分组」类别
├── 散置文件B.docx ← 后缀命中 file_suffixes 才会收集
│
├── 类别1/ ← 一级子目录 = 一个类别（组）
│ ├── a.pdf ← 类别目录下的普通文件
│ ├── 子目录x/ ← 更深层子目录：递归收集
│ │ └── b.pdf ← 输出保留相对层级（防同名冲突）
│ │
│ └── b/ ← 【已切片目录】上次运行中断留下的
│   ├── b.pdf ← 原件（切片时自动移入）
│   ├── b_page1-200.pdf ← 切片文件：整目录算一个单元，
│   └── b_page201-380.pdf ← 直接续传，不会重切
│
└── 类别2/
└── …



**收集规则**：

1. **一级子目录 = 类别**，更深层子目录只是路径的一部分，不算新类别；
2. 根目录散文件统一归入 `ungrouped_name`（默认「未分组」）类别；
3. 递归遍历时，目录名命中 `skip_dir_names`（默认为三个输出目录名）的整棵子树跳过——防止输出目录被重扫；
4. 位于**子目录中**、文件名匹配 `*_页起-页止.pdf` 的文件识别为已切片目录的成员，整目录合并为一个解析单元；
5. 非 PDF（word/ppt）不数页、不切片，整文件上传。

**单文件模式**：`input_path` 直接指向某个文件（如 `E:\docs\手册.docx`），只处理它，类型按该文件自身后缀识别（此时 `file_suffixes` 被忽略），输出在该文件所在目录旁边。
```

---


## 输出目录结构

输出默认建在 **输入目录的同级**（目录模式）；单文件模式建在**该文件所在目录**同级。可经 `output_base` 改到任意位置。

```text
<输出根目录>/
├── md/ ← 未切片单元的结果（小 PDF / word / ppt）
│ ├── 未分组/
│ │ └── 散置文件A/ ← 每个源文件一个结果子目录
│ │     ├── 散置文件A.md ← 解析出的 Markdown
│ │     └── images/ 等 ← 图片等附属文件
│ └── 类别1/
│   ├── a/
        ├── a.md
        └── images/
│   ├── 子目录x/
│   └── b/ ← 保留输入的相对层级
│
├── md_split/ ← 切片单元的结果（每片一个子目录）
│ └── 类别1/
│   └── b/
│     ├── b_page1-200/
        ├── b_page1-200.md 
        └── images/
│     └── b_page201-380/
│
└── pdf_finish/ ← 归档区：解析成功的原件移入这里
  │── 散置文件A.pdf
  └── 类别1/
  ├── 未分组/
    ├── a.pdf
  ├── 子目录x/
  │ └── b.pdf
  └── b/ ← 切片单元：整个目录（原件+全部切片）一起归档
    ├── b.pdf
    ├── b_page1-200.pdf
    └── b_page201-380.pdf

**归档即完成标记**：解析成功的源文件/源目录移入 `pdf_finish/`；**没有**进入 `pdf_finish/` 的输入，下次运行同一配置时会自动重新处理（这就是断点续跑机制）。
```

---

## 快速开始

```python
from pathlib import Path
from upload_file_to_mineru import MineruConfig, MineruPARSE

config = MineruConfig(
input_path=Path(r"E:\docs\pdf"), # 目录或单个文件
file_suffixes={".pdf"},
)
manifest = MineruPARSE(config).run() # 阻塞直至全部完成
```



---

## 配置参数（MineruConfig）

### 输入

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `input_path` | `Path` | **必填** | 指向文件→单文件模式；指向目录→目录模式 |
| `file_suffixes` | `Set[str]` | `{".pdf"}` | 目录模式下要处理的后缀集合，大小写不敏感。可选 `.pdf / .doc / .docx / .ppt / .pptx` |

### 输出布局

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `output_base` | `Optional[Path]` | `None` | 输出根目录；`None` = 输入目录同级。输入目录只读时用它改位置 |
| `manifest_path` | `Optional[Path]` | `None` | 清单 JSON 写出路径；`None` = 只通过返回值获取，不落盘 |
| `ungrouped_name` | `str` | `"未分组"` | 根目录散文件归入的类别名 |
| `skip_dir_names` | `Set[str]` | 输出目录名集合 | 递归收集时跳过的目录名（整棵子树不扫） |

### 解析行为

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `max_pages` | `int` | `200` | 单次上传页数上限（对应云端 200 页限制），超过则切片 |
| `overlap` | `int` | `0` | 相邻切片重叠页数，缓解跨页表格截断；必须 < `max_pages`。重叠页会被解析两遍 |

### MinerU 服务

| 参数 | 类型 | 默认值 | 说明                                                 |
|---|---|---|------------------------------------------------------|
| `token` | `Optional[str]` | `None` | API token；`None` = 读环境变量 `TOKEN_CLOUD_API_KEY` |
| `base_url` | `Optional[str]` | `None` | API 地址；`None` = 读环境变量 `MINERU_BASE_URL`      |
| `model` | `str` | `"vlm"` | 可选参数： "vlm "  "pipeline"  "html"                |
| `ocr` | `bool` | `True` | OCR 提取文字；扫描件/图片型 PDF 必须开               |
| `formula` | `bool` | `True` | 公式识别（输出 LaTeX）                               |
| `table` | `bool` | `True` | 表格识别（输出 HTML/Markdown）                       |

### 并发与轮询

| 参数 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `api_concurrency` | `int` | `8` | 同时打在 MinerU 上的批次数。**唯一吞吐旋钮**：按账号配额调，过大触发限流 |
| `poll_min` | `int` | `3` | 轮询起始间隔（秒） |
| `poll_max` | `int` | `10` | 轮询间隔上限（间隔从 poll_min 起每轮 +2 递增至封顶） |

> 非法配置（不支持的后缀 / 路径不存在 / `overlap >= max_pages`）在**构造 MineruConfig 时**立即抛异常，不会跑到一半才失败。

---

## 使用示例

### 1. 目录模式：批量处理 PDF

```python
from pathlib import Path
from upload_file_to_mineru import MineruConfig, MineruPARSE

config = MineruConfig(
input_path=Path(r"E:\zzk\Agent\RAG\Script\upload_to_mineruAPI\新建文件夹"),
file_suffixes={".pdf"},
manifest_path=Path("log.json"), # 可选：写清单文件
)
manifest = MineruPARSE(config).run()
```


### 2. 处理 Word / PPT（可混合）

```python
config = MineruConfig(
input_path=Path(r"E:\docs\word"),
file_suffixes={".doc", ".docx", ".pptx"}, # 整文件直传云端
)
manifest = MineruPARSE(config).run()
```

### 3. 单文件模式

```python
config = MineruConfig(input_path=Path(r"E:\docs\某手册.docx"))
manifest = MineruPARSE(config).run()
```
