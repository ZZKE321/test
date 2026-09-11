"""
mineru_pipeline.py — MinerU 云端批量解析组件（供其他脚本导入）

对外只暴露两个名字：
    MineruConfig     流水线全部可调参数（唯一参数入口）
    MineruPipeline   执行器：MineruPipeline(cfg).run() -> manifest 列表

其余一切（常量、辅助类、内部函数）均以 _ 开头，属模块私有实现，
外部脚本不应引用——保证将来内部重构不会破坏使用者。
输入/输出目录约定、参数详解见 README.md。
"""

import os
import re
import json
import time
import shutil
import threading
from pathlib import Path
from collections import namedtuple
from dataclasses import dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Optional, Set, Tuple, Dict

import pymupdf
from mineru import MinerU
from dotenv import load_dotenv

__all__ = [
    "MineruConfig",
    "MineruPARSE",
]   # 供外部脚本导入


# ========================= 模块私有实现（外部勿用） =========================
_SUPPORTED_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}  # 新增后缀改这里
_SLICE_RE = re.compile(r"_page(\d+-\d+)\.pdf$")  # 切片文件命名模式
_OUTPUT_DIR_NAMES = ("md", "md_split", "pdf_finish")  # 默认输出布局
_Dirs = namedtuple("Dirs", "md md_split finished")  # 输出目录组


def _under(root: Path, cat: Optional[str], rel: Path) -> Path:
    return root / cat / rel if cat else root / rel


# ---------------- 配置（唯一公开参数入口） ----------------
@dataclass
class MineruConfig:
    """MinerU 批量解析流水线的全部配置。

    除 input_path 外全部有默认值。非法配置（后缀不支持 / 路径不存在 /
    overlap >= max_pages）在构造时（__post_init__）立即抛异常。
    """

    # ========================= 输入 =========================

    # 输入入口（唯一必填项），模式由它决定：
    #   指向文件 → 单文件模式：只处理该文件，类型按其自身后缀识别，
    #              此时 file_suffixes 被忽略；
    #   指向目录 → 目录模式：按 file_suffixes 收集，结构约定：
    #     <root>/散文件.pdf              → 归入 ungrouped_name 类别
    #     <root>/类别1/a.pdf             → 一级子目录 = 一个类别（组）
    #     <root>/类别1/子目录/b.pdf      → 递归收集，输出保留相对层级（防同名冲突）
    #     子目录里的 *_page1-200.pdf     → 整目录算一个单元（断点续跑，不再重切）
    input_path: Path

    # 目录模式下要处理的后缀集合（单文件模式下忽略）。比较时统一转小写，
    # 因此 ".PDF" 与 ".pdf" 等价。只能是模块支持后缀的子集，否则构造时报错。
    # 例：{".pdf"}、{".doc", ".docx"}、{".pdf", ".pptx"}。
    # 注意：非 PDF 无法本地数页/切片 → 整文件直传，结果落 md/（不进 md_split/）。
    file_suffixes: Set[str] = field(default_factory=lambda: {".pdf"})

    # ========================= 输出布局 =========================

    # 输出根目录。None = input_path.parent，即 md/md_split/pdf_finish
    # 三个输出目录建在输入的同级。传入路径则全部建在该路径下。
    output_base: Optional[Path] = None

    # 运行清单的写出路径。None = 不写文件，只通过 run() 返回值获取 manifest。
    manifest_path: Optional[Path] = None

    # 目录模式下，根目录散文件归入的类别名。
    ungrouped_name: str = "未分组"

    # 递归收集时按【目录名】跳过的目录：任何一层目录名命中，该子树整体不收集。
    # 默认 = 三个输出目录名，防止输出目录被重扫。
    # （set 是可变默认值，必须用 field(default_factory=...) 防止跨实例共享）
    skip_dir_names: Set[str] = field(default_factory=lambda: set(_OUTPUT_DIR_NAMES))

    # ========================= 解析行为 =========================

    # 单次上传 MinerU 的页数上限（对应云端单文件 200 页限制）。
    # PDF 超过此页数 → 本地切片分段上传；非 PDF 不数页，不受影响。
    max_pages: int = 200

    # 相邻切片的重叠页数：使跨页表格/段落在前片末尾、后片开头各完整出现一次。
    # 0 = 不重叠；必须 < max_pages。代价：重叠页被云端解析两遍。
    overlap: int = 0

    # ========================= MinerU 服务 =========================

    # API token。None = 运行时从环境变量（.env）TOKEN_CLOUD_API_KEY 读取。
    token: Optional[str] = None

    # API 地址。None = 读环境变量 MINERU_BASE_URL。
    base_url: Optional[str] = None

    # 解析模型："vlm"（视觉大模型，效果好，慢）或 "pipeline"（快）。
    model: str = "vlm"  # "vlm" | "pipeline" | "html"

    # 是否 OCR 提取文字。扫描件/图片型 PDF 必须 True。
    ocr: bool = True

    # 是否识别公式（输出 LaTeX）。
    formula: bool = True

    # 是否识别表格（输出 HTML/Markdown）。
    table: bool = True

    # ========================= 并发与轮询 =========================

    # 同时打在 MinerU 上的批次数（= 线程池大小），唯一吞吐旋钮：
    # 按账号配额调——太大触发限流/429，太小浪费等待窗口。
    api_concurrency: int = 8

    # 轮询起始间隔（秒）。
    poll_min: int = 3

    # 轮询间隔上限：实际间隔从 poll_min 起、每轮 +2 递增、封顶于此。
    poll_max: int = 10

    def __post_init__(self):
        self.input_path = Path(self.input_path)
        self.file_suffixes = {s.lower() for s in self.file_suffixes}
        bad = self.file_suffixes - _SUPPORTED_SUFFIXES
        if bad:
            raise ValueError(
                f"不支持的后缀 {sorted(bad)}，" f"支持 {sorted(_SUPPORTED_SUFFIXES)}"
            )
        if not self.input_path.exists():
            raise FileNotFoundError(f"路径不存在: {self.input_path}")
        if self.overlap >= self.max_pages:
            raise ValueError(
                f"overlap({self.overlap}) 必须 < " f"max_pages({self.max_pages})"
            )

    @property
    def base(self) -> Path:
        return Path(self.output_base) if self.output_base else self.input_path.parent


# ---------------- MinerU 服务封装（线程安全：每线程独立 client） ----------------
class _MineruService:
    def __init__(self, token: str, base_url: str, cfg: MineruConfig):
        self._token, self._base_url = token, base_url
        self._model, self._ocr = cfg.model, cfg.ocr
        self._formula, self._table = cfg.formula, cfg.table
        self._poll_min, self._poll_max = cfg.poll_min, cfg.poll_max
        self._local = threading.local()

    @property
    def _client(self) -> MinerU:
        if getattr(self._local, "client", None) is None:
            self._local.client = MinerU(token=self._token, base_url=self._base_url)
        return self._local.client

    def parse_batch(self, files, save_root: Path, label: str, per_file_subdir=True):
        batch = self._submit(files, label)
        print(f"[{label}] 批次 {batch} 已提交（{len(files)} 个文件）")
        for r in self._wait(batch, label):
            out = save_root / Path(r.filename).stem if per_file_subdir else save_root
            out.mkdir(parents=True, exist_ok=True)
            print(f"[{label}] 保存 -> {out}")
            r.save_all(str(out))

    def _submit(self, files, label, tries=3):
        for i in range(tries):
            try:
                return self._client.submit_batch(
                    files,
                    model=self._model,
                    ocr=self._ocr,
                    formula=self._formula,
                    table=self._table,
                )
            except Exception as exc:
                if i == tries - 1:
                    raise
                wait = 2 ** (i + 1)
                print(f"[{label}] 提交失败（{exc}），{wait}s 后重试")
                time.sleep(wait)

    def _wait(self, batch, label):
        interval = self._poll_min
        while True:
            rs = self._client.get_batch(batch)
            bad = [r.filename for r in rs if r.state in ("failed", "error")]
            if bad:
                raise RuntimeError(f"服务端解析失败: {bad}")
            done = sum(r.state == "done" for r in rs)
            if done == len(rs):
                print(f"[{label}] ✅ 全部完成（{done}/{len(rs)}）")
                return rs
            print(f"[{label}] 进度 {done}/{len(rs)}")
            time.sleep(interval)
            interval = min(interval + 2, self._poll_max)


# ---------------- 解析单元 ----------------
@dataclass
class _Unit:
    label: str  # 日志/清单名（类别/相对路径）
    src: Path  # 归档对象：原件文件，或切片目录
    dest: Path  # 归档目标目录
    out: Path  # MinerU 结果目录（切片时每片一个子目录）
    files: List[str]  # 上传文件
    split: bool
    pages: List[str]


def _split_pdf(pdf: Path, out_dir: Path, max_pages: int, overlap: int):
    files, pages = [], []
    with pymupdf.open(pdf) as src:
        n, start = src.page_count, 0
        while start < n:
            end = min(start + max_pages, n)
            p = out_dir / f"{pdf.stem}_page{start + 1}-{end}.pdf"
            with pymupdf.open() as dst:
                dst.insert_pdf(src, from_page=start, to_page=end - 1)
                dst.save(p, garbage=3, deflate=True)
            files.append(str(p))
            pages.append(f"{start + 1}-{end}")
            if end >= n:
                break
            start = end - overlap
    return files, pages


def _make_unit(
    src: Path, cat: Optional[str], rel: Path, dirs: _Dirs, cfg: MineruConfig
) -> _Unit:
    label = f"{cat}/{rel}" if cat else str(rel)
    dest = _under(dirs.finished, cat, rel.parent)

    if src.suffix.lower() != ".pdf":  # word/ppt：整文件直传，不做切片
        return _Unit(
            label, src, dest, _under(dirs.md, cat, rel), [str(src)], False, [""]
        )

    with pymupdf.open(src) as doc:
        n = doc.page_count
    if n <= cfg.max_pages:
        return _Unit(
            label, src, dest, _under(dirs.md, cat, rel), [str(src)], False, [f"1-{n}"]
        )

    sdir = src.parent / src.stem  # 切片目录与 PDF 同级同名
    sdir.mkdir(exist_ok=True)
    files, pages = _split_pdf(src, sdir, cfg.max_pages, cfg.overlap)
    shutil.move(src, sdir / src.name)  # 原件收进切片目录，失败可续跑
    return _Unit(label, sdir, dest, _under(dirs.md_split, cat, rel), files, True, pages)


def _unit_from_presplit(d: Path, cat: Optional[str], rel: Path, dirs: _Dirs) -> _Unit:
    slices = sorted(p for p in d.iterdir() if p.is_file() and _SLICE_RE.search(p.name))
    pages = [_SLICE_RE.search(p.name).group(1) for p in slices]
    label = f"{cat}/{rel}" if cat else str(rel)
    return _Unit(
        label,
        d,
        _under(dirs.finished, cat, rel.parent),
        _under(dirs.md_split, cat, rel),
        [str(p) for p in slices],
        True,
        pages,
    )


# ---------------- 收集 ----------------
def _iter_files(d: Path, exts: Set[str], skip: Set[str]) -> List[Path]:
    """递归收集指定类型文件；跳过输出目录。"""
    out = []
    for p in sorted(d.rglob("*")):
        if (
            p.is_file()
            and p.suffix.lower() in exts
            and not any(part in skip for part in p.relative_to(d).parts)
        ):
            out.append(p)
    return out


def _collect(
    root: Path, exts: Set[str], cfg: MineruConfig
) -> List[Tuple[Path, Optional[str], Path, bool]]:
    """[(src, 类别, 相对路径, 是否已切片目录)]；根目录散文件归 ungrouped_name。"""
    skip = cfg.skip_dir_names
    items = []
    for f in sorted(root.iterdir()):
        if f.is_file() and f.suffix.lower() in exts:
            items.append(
                (f, cfg.ungrouped_name, f.relative_to(root).with_suffix(""), False)
            )
    for cat_dir in sorted(p for p in root.iterdir() if p.is_dir()):
        if cat_dir.name in skip:
            continue
        groups, singles = {}, []
        for p in _iter_files(cat_dir, exts, skip):
            # 子目录里的切片文件 → 整目录一个单元（断点续跑）；
            # 类别目录直接躺着的切片名文件仍按普通文件处理
            if _SLICE_RE.search(p.name) and p.parent != cat_dir:
                groups.setdefault(p.parent, []).append(p)
            else:
                singles.append(p)
        for d in sorted(groups):
            print(f"已切片目录: {d}（{len(groups[d])} 片）")
            items.append((d, cat_dir.name, d.relative_to(cat_dir), True))
        for p in singles:
            items.append(
                (p, cat_dir.name, p.relative_to(cat_dir).with_suffix(""), False)
            )
    return items


def _build_units(items, dirs: _Dirs, cfg: MineruConfig):
    units, errors = [], []
    for src, cat, rel, pre in items:
        try:
            u = (
                _unit_from_presplit(src, cat, rel, dirs)
                if pre
                else _make_unit(src, cat, rel, dirs, cfg)
            )
            units.append(u)
            print(
                f"{'切片' if u.split else '直传'}: {u.label}（{len(u.files)} 个上传文件）"
            )
        except Exception as exc:
            print(f"❌ 预处理失败: {src} -> {exc}")
            errors.append(
                {"source_name": str(src), "success": False, "error": str(exc)}
            )
    return units, errors


# ---------------- 流水线组件（公开） ----------------
class MineruPARSE:
    """使用方式：
    cfg = MineruConfig(input_path=Path(...), file_suffixes={".pdf"})
    manifest = MineruPARSE(cfg).run()
    """

    def __init__(self, config: MineruConfig):
        self.cfg = config

    def run(self) -> List[Dict]:
        """执行完整流水线，返回 manifest（含失败条目）。"""
        load_dotenv()  # 幂等，导入本模块无副作用
        cfg = self.cfg
        service = self._make_service()

        if cfg.input_path.is_file():  # 单文件模式：按文件自身后缀
            suffix = cfg.input_path.suffix.lower()
            if suffix not in _SUPPORTED_SUFFIXES:
                raise ValueError(
                    f"不支持的类型 {suffix}，" f"支持 {sorted(_SUPPORTED_SUFFIXES)}"
                )
            items = [(cfg.input_path, None, Path(cfg.input_path.stem), False)]
            print(f"单文件模式: {cfg.input_path}（{suffix}）")
        else:  # 目录模式：按 file_suffixes 收集
            items = _collect(cfg.input_path, cfg.file_suffixes, cfg)
            print(
                f"目录模式: {cfg.input_path}"
                f"（{' / '.join(sorted(cfg.file_suffixes))}），收集到 {len(items)} 个源"
            )

        dirs = _Dirs(*(cfg.base / n for n in _OUTPUT_DIR_NAMES))
        for d in dirs:
            d.mkdir(parents=True, exist_ok=True)
        units, errors = _build_units(items, dirs, cfg)
        print(f"共 {len(units)} 个解析单元")
        manifest = self._execute(units, service, dirs, errors)
        if cfg.manifest_path:
            cfg.manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"清单已写出: {cfg.manifest_path}")
        return manifest

    # ---------- 内部 ----------
    def _make_service(self) -> _MineruService:
        cfg = self.cfg
        token = cfg.token or os.environ.get("TOKEN_CLOUD_API_KEY")
        base_url = cfg.base_url or os.environ.get("MINERU_BASE_URL")
        if not token:
            raise RuntimeError(
                "缺少 token：请在环境/.env 配置 TOKEN_CLOUD_API_KEY，"
                "或传入 MineruConfig.token"
            )
        if not base_url:
            raise RuntimeError(
                "缺少 base_url：请在环境/.env 配置 MINERU_BASE_URL，"
                "或传入 MineruConfig.base_url"
            )
        return _MineruService(token, base_url, cfg)

    def _execute(
        self, units: List[_Unit], service: _MineruService, dirs: _Dirs, pre_errors
    ) -> List[Dict]:
        cfg = self.cfg
        manifest = list(pre_errors)
        with ThreadPoolExecutor(cfg.api_concurrency) as ex:
            futs = {
                ex.submit(service.parse_batch, u.files, u.out, u.label, u.split): u
                for u in units
            }
            for f in as_completed(futs):
                u = futs[f]
                entry = {
                    "source_name": u.label,
                    "is_split": u.split,
                    "output_dir": str(u.out),
                    "page_ranges": u.pages,
                }
                try:
                    f.result()
                    self._archive(u)
                    entry.update(success=True, error=None)
                    print(f"✅ 完成并归档: {u.label}")
                except Exception as exc:
                    entry.update(success=False, error=str(exc))
                    print(f"❌ 失败: {u.label} -> {exc}")
                manifest.append(entry)
        return manifest

    @staticmethod
    def _archive(u: _Unit) -> None:
        u.dest.mkdir(parents=True, exist_ok=True)
        target = u.dest / u.src.name
        if target.exists():  # 同名兜底
            target = (
                u.dest
                / f"{u.src.stem}_{int(time.time() * 1000) % 10_000}{u.src.suffix}"
            )
        shutil.move(str(u.src), str(target))


# ---------------- 最小示例（直接运行本文件时） ----------------
if __name__ == "__main__":
    config = MineruConfig(
        input_path=Path(
            r"C:\Users\Administrator\Downloads\AI大模型核心算法原理全面解析.pdf"
        ),
        file_suffixes={".pdf"},
        manifest_path=Path("log.json"),
    )
    results = MineruPARSE(config).run()
    failed = [e for e in results if not e.get("success")]
    print(f"成功 {len(results) - len(failed)} / 失败 {len(failed)}")
