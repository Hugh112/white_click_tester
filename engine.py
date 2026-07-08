from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import string
import sys
import threading
import time
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse, parse_qsl, urlencode, urlunparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# 打包后的 exe 优先使用软件目录内置 Chromium；没有内置时可使用本机 Edge/Chrome。
if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).resolve().parent
    bundled_browser_dir = exe_dir / "ms-playwright"
    if bundled_browser_dir.exists():
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(bundled_browser_dir)

from playwright.sync_api import Locator, TimeoutError as PlaywrightTimeoutError, sync_playwright

from proxy_provider import ProxyConfig, ProxyProvider


@dataclass
class RunConfig:
    start_url: str
    search_input_selector: str
    search_button_selector: str
    result_item_selector: str
    result_link_selector: str
    result_title_selector: str
    keywords_file: str
    whitelist_file: str
    blacklist_file: str = "blacklist.csv"
    output_log_file: str = "output/run_log.csv"
    progress_file: str = "output/task_progress.json"
    searches_per_keyword: int = 1
    search_buffer_seconds: float = 3.0
    stay_seconds_min: int = 2
    stay_seconds_max: int = 5
    max_clicks_per_keyword: int = 10
    page_timeout_ms: int = 30000
    result_wait_ms: int = 20000
    # 搜索必须先被提交，不能还没搜索就进入目标/白名单判断。
    force_search_submit: bool = True
    reset_mode: str = "new_browser_per_keyword"  # new_browser_per_keyword / new_context_per_keyword
    # 浏览器来源：auto_system_first / bundled_only / system_only / manual_path
    # auto_system_first：免下载推荐。优先使用软件内置 Chromium；没有内置时使用本机 Edge/Chrome。
    # bundled_only：只使用 Playwright 下载/打包进来的 Chromium。
    # manual_path：使用用户手动填写的浏览器 exe 路径。
    browser_source: str = "auto_system_first"
    browser_executable_path: str = ""
    headless: bool = True
    worker_count: int = 1
    viewport_width: int = 390
    viewport_height: int = 844
    user_agent: str = ""
    mobile_mode: bool = True
    # 搜索环境模式：全新干净环境 / 同关键词复用环境 / 本地增强模拟 / 持久环境
    environment_mode: str = "固定搜索环境：保留CK+地区定位（推荐）"
    fixed_search_environment: bool = True
    profile_name: str = "default"
    # 地区模拟：用于本地生活/商家板块稳定展示。留空则不设置定位。
    region_city: str = ""
    region_latitude: str = ""
    region_longitude: str = ""
    region_accuracy: str = "100"
    region_timezone: str = "Asia/Shanghai"
    region_locale: str = "zh-CN"
    # v1.0：地区修复。地区变化时隔离 CK；进入 site.baidu.com 模块页时将 city 参数同步为模拟城市。
    isolate_profile_by_region: bool = True
    force_region_city_in_target_url: bool = True
    enhanced_target_detection: bool = True
    debug_enabled: bool = True
    debug_dir: str = "output/debug"
    save_debug_html: bool = True
    save_debug_screenshot: bool = True
    clear_debug_on_start: bool = True
    traffic_saving_mode: str = "标准省流量：拦截图片/视频/音频/字体（推荐）"
    block_ads_and_trackers: bool = True
    block_maps: bool = True
    only_debug_on_failure: bool = True
    stop_after_all_whitelist_clicked: bool = True
    click_all_whitelist_in_target: bool = True
    fuzzy_threshold: float = 0.66
    min_fuzzy_chars: int = 3
    scan_scroll_rounds: int = 5
    scan_scroll_pixels: int = 900
    scroll_wait_ms: int = 900
    max_items_to_scan: int = 80
    verify_remaining_rounds: int = 3
    click_open_timeout_ms: int = 8000
    # 搜索后先下滑到指定板块/位置，再进入目标页。留空则直接在当前结果页扫描白名单。
    target_section_selector: str = ""
    target_section_text: str = ""
    # v1.0：目标模块来源标签和模块内商家DIV识别规则改为前端可配置。
    target_source_selector: str = "span.cosc-source-text.cos-line-clamp-1, .cosc-source-text"
    target_source_text: str = "百度本地生活"
    merchant_div_selector: str = "div"
    enter_target_page_selector: str = ""
    enter_target_page_text: str = ""
    target_page_wait_selector: str = ""
    target_page_wait_seconds: float = 3.0
    target_required: bool = True
    # 严格目标板块优先。配置了目标板块后，没找到/没进入目标板块就不扫描白名单。
    target_first_strict: bool = True
    target_scroll_rounds: int = 8
    target_scroll_pixels: int = 850
    proxy_mode: str = "none"
    proxy_list_file: str = "proxies.txt"
    proxy_api_url: str = ""
    fixed_proxy: str = ""
    change_ip_url: str = ""
    proxy_username: str = ""
    proxy_password: str = ""
    proxy_change_wait_seconds: int = 3


class StopFlag:
    """线程间共享的停止/暂停状态。

    暂停不会强行关闭浏览器，而是在每个安全检查点停住；停止会尽快让循环退出。
    """
    def __init__(self) -> None:
        self.stopped = False
        self.paused = False
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)

    def stop(self) -> None:
        with self._cond:
            self.stopped = True
            self.paused = False
            self._cond.notify_all()

    def pause(self) -> None:
        with self._cond:
            if not self.stopped:
                self.paused = True
                self._cond.notify_all()

    def resume(self) -> None:
        with self._cond:
            self.paused = False
            self._cond.notify_all()

    def is_stopped(self) -> bool:
        with self._lock:
            return self.stopped

    def is_paused(self) -> bool:
        with self._lock:
            return self.paused

    def checkpoint(self) -> None:
        with self._cond:
            while self.paused and not self.stopped:
                self._cond.wait(timeout=0.25)
            if self.stopped:
                raise RuntimeError("用户已停止任务")

    def sleep(self, seconds: float) -> bool:
        """可暂停/可停止的等待。返回 False 表示已停止。"""
        end = time.time() + max(float(seconds), 0.0)
        while time.time() < end:
            with self._cond:
                while self.paused and not self.stopped:
                    self._cond.wait(timeout=0.25)
                if self.stopped:
                    return False
            time.sleep(min(0.25, max(0.0, end - time.time())))
        return not self.is_stopped()


_LOG_LOCK = threading.Lock()
_STATS_LOCK = threading.Lock()


# 文件解码和表格读取兼容：CSV/TXT/XLSX/XLS、Excel/WPS 多余列、空列和编码差异。
def safe_decode_bytes(raw: bytes) -> str:
    """安全解码文件内容。

    v1.0 修复点：旧版只要 utf-8 能解码就直接返回，后续 repair_mojibake_text 又可能
    把正常中文误修复成“鐭冲...”这类乱码。这里改成多编码候选打分，选择最像正常中文的结果。
    """
    if raw is None:
        return ""
    encodings = []
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        encodings.extend(["utf-16", "utf-16-le", "utf-16-be"])
    encodings.extend(["utf-8-sig", "utf-8", "gb18030", "gbk", "utf-16", "utf-16-le", "utf-16-be", "latin1"])
    candidates = []
    seen = set()
    for enc in encodings:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            decoded = raw.decode(enc)
        except Exception:
            continue
        if decoded and decoded.count("\x00") > max(2, len(decoded) // 10):
            continue
        candidates.append((decoded, enc, _text_quality_score(decoded)))
    if candidates:
        # 如果 UTF-8 和 GB18030 都能解码，选择中文质量更高的；质量接近时优先 UTF-8。
        candidates.sort(key=lambda x: (x[2], 1 if x[1] in ("utf-8-sig", "utf-8") else 0), reverse=True)
        return candidates[0][0]
    return raw.decode("utf-8", errors="replace")


def safe_read_text(path: str) -> str:
    return safe_decode_bytes(Path(path).read_bytes())


def safe_load_json(path: str, default=None):
    try:
        text = safe_read_text(path).strip()
        if not text:
            return default
        return json.loads(text)
    except Exception:
        return default


def load_config(path: str) -> RunConfig:
    data = safe_load_json(path, default={}) or {}
    # 兼容界面配置里的展示项，例如 search_submit_mode/result_extract_mode。
    allowed = set(RunConfig.__dataclass_fields__.keys())
    data = {k: v for k, v in data.items() if k in allowed}
    return RunConfig(**data)


def _sniff_csv_dialect(text: str):
    sample = text[:4096]
    # 兼容 Excel 另存为“Unicode 文本”时的 Tab 分隔。
    try:
        return csv.Sniffer().sniff(sample, delimiters=",\t;|")
    except Exception:
        class D(csv.excel):
            delimiter = "\t" if "\t" in sample and sample.count("\t") >= sample.count(",") else ","
        return D


def _text_quality_score(text: str) -> int:
    """用于自动挑选更像正常中文的文本。分数越高越可信。"""
    text = str(text or "")
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    ascii_ok = sum(1 for ch in text if ch.isascii() and (ch.isalnum() or ch in " _-.,;:/\\|()[]{}@#&+=?%\r\n\t"))
    replacement = text.count("�") + text.count("\ufffd")
    nul = text.count("\x00")
    private_use = sum(1 for ch in text if "\ue000" <= ch <= "\uf8ff")
    hangul = sum(1 for ch in text if "\uac00" <= ch <= "\ud7af")
    controls = sum(1 for ch in text if ord(ch) < 32 and ch not in "\r\n\t")
    # 这些字符/组合高频出现在中文 UTF-8 被错按 GBK/GB18030 解码后的文本里。
    mojibake_markers = "锛鏂囨湰鎴戜綘浠栧湪涓嶄竴鐭冲搴勪翰瀛愰壌瀹氫腑蹇犳椂妗冩牸鍏ㄩ儴"
    marker_count = sum(text.count(ch) for ch in mojibake_markers)
    # 正常简体高频字适当加分，避免“鐭冲...”这类虽然也是 CJK 但不自然的文本胜出。
    common_simplified = "的一是在不了有和人这中大为上个国我以要他时来用们生到作地于出就分对成会可主发年动同工也能下过子说产种面而方后多定行学法所民得经十三之进着等部度家电力里如水化高自二理起小物现实加量都两体制机当使点从业本去把性好应开它合还因由其些然前外天政四日那社义事平形相全表间样与关各重新线内数正心反你明看原又么利比或但质气第向道命此变条只没结解问意建月公无系军很情者最立代想已通并提直题党程展五果料象员革位入常文总次品式活设及管特件长求老头基资边流路级少图山统接知较将组见计别她手角期根论运农指几九区强放决西被干做必战先回则任取据处理世台南给色光门即保治北造百规热领七海口东导器压志世金增争济阶油思术极交受联识步类集"
    common_count = sum(text.count(ch) for ch in common_simplified)
    too_long_penalty = max(0, len(text) - 2000) // 20
    return cjk * 4 + ascii_ok + common_count * 2 - replacement * 40 - nul * 40 - private_use * 35 - hangul * 20 - controls * 20 - marker_count * 6 - too_long_penalty


def _has_mojibake_symptoms(text: str) -> bool:
    text = str(text or "")
    if not text:
        return False
    if "�" in text or "\ufffd" in text or "\x00" in text:
        return True
    if any("\ue000" <= ch <= "\uf8ff" for ch in text):
        return True
    if any("\uac00" <= ch <= "\ud7af" for ch in text):
        return True
    markers = "锛鏂囨湰鎴戜綘浠栧湪涓嶄竴鐭冲搴勪翰瀛愰壌瀹氫腑蹇犳椂妗冩牸鍏ㄩ儴"
    return sum(text.count(ch) for ch in markers) >= 2


def repair_mojibake_text(value) -> str:
    """尽量修复白名单/页面文本中的中文乱码。

    v1.0 关键修复：不再对正常中文强行“修复”。旧版会把
    正常中文被错误二次转码会导致关键词、白名单和页面文本匹配失败。
    现在只有发现明显乱码症状时才尝试修复。
    """
    text = str(value or "").replace("\ufeff", "").strip()
    if not text:
        return ""
    if not _has_mojibake_symptoms(text):
        return text
    candidates = [text]
    for enc in ("gb18030", "gbk", "latin1"):
        try:
            candidates.append(text.encode(enc, errors="ignore").decode("utf-8", errors="ignore"))
        except Exception:
            pass
    for enc in ("utf-8", "latin1"):
        try:
            candidates.append(text.encode(enc, errors="ignore").decode("gb18030", errors="ignore"))
        except Exception:
            pass
    best = max(candidates, key=_text_quality_score)
    if _text_quality_score(best) > _text_quality_score(text):
        return best.strip()
    return text.strip()


def _cell_to_text(value) -> str:
    """把 CSV/Excel 单元格安全转成文本，并自动修复常见中文乱码。"""
    if value is None:
        return ""
    if isinstance(value, list):
        parts = []
        for x in value:
            t = _cell_to_text(x)
            if t:
                parts.append(t)
        return repair_mojibake_text(" ".join(parts))
    # Excel 里纯数字可能读成 1.0，这里转成更自然的 1。
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return repair_mojibake_text(value)


_HEADER_ALIASES = {
    "keyword": "keyword", "keywords": "keyword", "kw": "keyword", "关键词": "keyword", "搜索词": "keyword", "词": "keyword",
    "title": "title", "name": "title", "names": "title", "标题": "title", "名称": "title", "店名": "title", "店铺": "title", "白名单": "title", "黑名单": "title", "白名单词": "title", "黑名单词": "title", "白名单名称": "title", "黑名单名称": "title",
    "url": "url", "link": "url", "href": "url", "链接": "url", "网址": "url", "地址": "url",
    "match_type": "match_type", "matchtype": "match_type", "type": "match_type", "匹配方式": "match_type", "匹配类型": "match_type",
}


def _normalize_header_name(value) -> str:
    text = _cell_to_text(value).strip().lower()
    text = text.replace(" ", "").replace("-", "_").replace("/", "_")
    return _HEADER_ALIASES.get(text, text)


def _matrix_to_dict_rows(matrix: List[List[object]]) -> Iterable[Dict[str, str]]:
    """把 Excel/CSV 的二维表转成 Dict 行。

    v1.0：支持 .xlsx/.xls/.csv/.txt，以及 WPS/Excel 多余空列、无表头单列名单。
    如果第一行包含 title/url/match_type/keyword 等表头，就按表头读取；否则把第一列当成名单/关键词。
    """
    rows = []
    for raw_row in matrix:
        row = [_cell_to_text(x) for x in (raw_row or [])]
        # 去掉行尾空列，但保留中间空列，避免 url 空列影响 match_type 列。
        while row and not row[-1]:
            row.pop()
        if any(row):
            rows.append(row)
    if not rows:
        return

    first = rows[0]
    normalized = [_normalize_header_name(c) for c in first]
    known = {"keyword", "title", "url", "match_type"}
    has_header = any(c in known for c in normalized)

    if has_header:
        headers = []
        used = {}
        for i, h in enumerate(normalized):
            h = h or f"col{i+1}"
            if h in used:
                used[h] += 1
                h = f"{h}_{used[h]}"
            else:
                used[h] = 1
            headers.append(h)
        data_rows = rows[1:]
    else:
        # 无表头：第一列直接作为关键词/名单 title；其他列作为附加文本。
        headers = ["title"] + [f"col{i}" for i in range(2, max(len(r) for r in rows) + 1)]
        data_rows = rows

    for row in data_rows:
        clean: Dict[str, str] = {}
        extras: List[str] = []
        for i, value in enumerate(row):
            key = headers[i] if i < len(headers) else "__extra__"
            value = _cell_to_text(value)
            if not value:
                continue
            if key == "__extra__" or key.startswith("col"):
                extras.append(value)
            else:
                # 若重复列有值，拼接，避免后列覆盖前列。
                clean[key] = (clean.get(key, "") + " " + value).strip() if clean.get(key) else value
        if extras:
            clean["__extra__"] = " ".join(extras).strip()
        if clean:
            yield clean


def _detect_table_kind(path: str) -> str:
    p = Path(path)
    ext = p.suffix.lower()
    try:
        head = p.read_bytes()[:8]
    except Exception:
        head = b""
    # 即使扩展名写错，也按文件头识别。
    if head.startswith(b"PK\x03\x04") or ext in (".xlsx", ".xlsm"):
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0") or ext == ".xls":
        return "xls"
    return "text"


def _read_xlsx_rows(path: str) -> Iterable[Dict[str, str]]:
    try:
        import openpyxl
    except Exception as e:
        raise RuntimeError("读取 .xlsx/.xlsm 需要 openpyxl；请重新运行打包脚本安装 requirements.txt。") from e
    wb = openpyxl.load_workbook(io.BytesIO(Path(path).read_bytes()), read_only=True, data_only=True)
    try:
        ws = wb.active
        matrix = [list(row) for row in ws.iter_rows(values_only=True)]
        yield from _matrix_to_dict_rows(matrix)
    finally:
        try:
            wb.close()
        except Exception:
            pass


def _read_xls_rows(path: str) -> Iterable[Dict[str, str]]:
    try:
        import xlrd
    except Exception as e:
        raise RuntimeError("读取 .xls 需要 xlrd；请重新运行打包脚本安装 requirements.txt。") from e
    book = xlrd.open_workbook(path)
    sheet = book.sheet_by_index(0)
    matrix = []
    for r in range(sheet.nrows):
        matrix.append([sheet.cell_value(r, c) for c in range(sheet.ncols)])
    yield from _matrix_to_dict_rows(matrix)


def read_text_csv(path: str) -> Iterable[Dict[str, str]]:
    """读取常见表格：CSV/TXT/TSV/XLSX/XLS。

    v1.0 修复：白名单/黑名单/关键词文件可直接选择 Excel 表格；即使 WPS 把 Excel 文件显示为
    “XLS 工作表”或扩展名不规范，也会先按文件头自动判断。
    """
    kind = _detect_table_kind(path)
    if kind == "xlsx":
        yield from _read_xlsx_rows(path)
        return
    if kind == "xls":
        yield from _read_xls_rows(path)
        return

    text = safe_read_text(path)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not text.strip():
        return
    dialect = _sniff_csv_dialect(text)
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    yield from _matrix_to_dict_rows([row for row in reader])


def load_keywords(path: str) -> List[str]:
    rows: List[str] = []
    for row in read_text_csv(path):
        # 兼容 keyword / 关键词 / 搜索词；如果表头被 WPS/Excel 改坏，则取第一列非空文本兜底。
        kw = (row.get("keyword") or row.get("关键词") or row.get("搜索词") or row.get("__extra__") or "").strip()
        if not kw:
            for key, value in row.items():
                if key == "__extra__":
                    continue
                if value:
                    kw = str(value).strip()
                    break
        if kw and kw.lower() not in ("keyword", "关键词", "搜索词"):
            rows.append(kw)
    if not rows:
        raise ValueError("关键词文件为空，或未找到 keyword / 关键词 / 搜索词 列")
    return rows


def split_terms(text: str) -> List[str]:
    text = (text or "").strip()
    if not text:
        return []
    parts = re.split(r"[|,，、/;；\n\r\t]+", text)
    return [p.strip() for p in parts if p.strip()]


def load_whitelist(path: str) -> List[Dict[str, str]]:
    rows: List[Dict[str, str]] = []
    for row in read_text_csv(path):
        title = (row.get("title") or row.get("name") or row.get("名称") or row.get("店名") or row.get("店铺") or row.get("白名单") or row.get("白名单词") or row.get("关键词") or row.get("__extra__") or "").strip()
        url = (row.get("url") or row.get("链接") or "").strip()
        match_type = (row.get("match_type") or row.get("匹配方式") or "fuzzy").strip().lower()
        if not title and not url:
            for key, value in row.items():
                if key in ("__extra__", "match_type", "匹配方式"):
                    continue
                if value:
                    title = str(value).strip()
                    break
        if title or url:
            rows.append({"title": title, "url": url, "match_type": match_type})
    if not rows:
        raise ValueError("白名单为空。为避免误点，程序不会在白名单为空时运行")
    return rows




def load_blacklist(path: str) -> List[Dict[str, str]]:
    """读取黑名单。黑名单为空或文件不存在时返回空列表。

    格式和白名单一致，支持列名：title/name/名称/黑名单/url/match_type。
    命中黑名单的卡片绝不点击，即使同时命中白名单。
    """
    if not path or not os.path.exists(path):
        return []
    rows: List[Dict[str, str]] = []
    for row in read_text_csv(path):
        title = (row.get("title") or row.get("name") or row.get("名称") or row.get("店名") or row.get("店铺") or row.get("黑名单") or row.get("黑名单词") or row.get("关键词") or row.get("__extra__") or "").strip()
        url = (row.get("url") or row.get("链接") or "").strip()
        match_type = (row.get("match_type") or row.get("匹配方式") or "fuzzy").strip().lower()
        if not title and not url:
            for key, value in row.items():
                if key in ("__extra__", "match_type", "匹配方式"):
                    continue
                if value:
                    title = str(value).strip()
                    break
        if title or url:
            rows.append({"title": title, "url": url, "match_type": match_type})
    return rows


def ensure_parent(path: str) -> None:
    parent = Path(path).parent
    if str(parent) and not parent.exists():
        parent.mkdir(parents=True, exist_ok=True)


def append_log(path: str, row: Dict[str, str]) -> None:
    ensure_parent(path)
    fieldnames = [
        "time", "worker", "keyword", "action", "matched_rule", "score", "title", "url",
        "status", "stay_seconds", "message"
    ]
    with _LOG_LOCK:
        file_exists = os.path.exists(path)
        with open(path, "a", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            if not file_exists:
                writer.writeheader()
            writer.writerow({k: row.get(k, "") for k in fieldnames})


# 常见中英文标点、空白会干扰模糊匹配，这里统一去掉。
_PUNCT_RE = re.compile(r"[\s\u200b\u200c\u200d\ufeff" + re.escape(string.punctuation) + r"，。！？、；：‘’“”（）【】《》〈〉〔〕［］｛｝—…·￥]+")


def normalize_cn(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "")
    text = text.lower()
    text = _PUNCT_RE.sub("", text)
    return text.strip()


def partial_ratio(shorter: str, longer: str) -> float:
    """对短白名单词和长结果文本做局部相似度，适合中文店名/品牌名模糊匹配。"""
    shorter = normalize_cn(shorter)
    longer = normalize_cn(longer)
    if not shorter or not longer:
        return 0.0
    if shorter in longer:
        return 1.0
    if len(shorter) > len(longer):
        shorter, longer = longer, shorter
    if len(shorter) <= 2:
        return 1.0 if shorter in longer else SequenceMatcher(None, shorter, longer).ratio()

    window = max(len(shorter), 3)
    best = SequenceMatcher(None, shorter, longer).ratio()
    # 限制滑动窗口数量，避免超长页面文本造成性能问题。
    max_start = min(len(longer) - window + 1, 500)
    for start in range(0, max_start):
        chunk = longer[start:start + window + 3]
        score = SequenceMatcher(None, shorter, chunk).ratio()
        if score > best:
            best = score
            if best >= 0.98:
                break
    return best


def token_overlap_score(term: str, value: str) -> float:
    t = normalize_cn(term)
    v = normalize_cn(value)
    if not t or not v:
        return 0.0
    if t in v:
        return 1.0
    chars = [c for c in t if "\u4e00" <= c <= "\u9fff" or c.isalnum()]
    if not chars:
        return 0.0
    hit = sum(1 for c in chars if c in v)
    return hit / max(len(chars), 1)


@dataclass
class MatchResult:
    rule: Dict[str, str]
    matched_term: str
    score: float
    reason: str


def match_one_term(term: str, value: str, match_type: str, threshold: float, min_fuzzy_chars: int) -> Tuple[bool, float, str]:
    raw_term = (term or "").strip()
    if not raw_term:
        return False, 0.0, "empty"

    norm_term = normalize_cn(raw_term)
    norm_value = normalize_cn(value)
    if not norm_term or not norm_value:
        return False, 0.0, "empty_norm"

    if match_type == "exact":
        ok = norm_term == norm_value
        return ok, 1.0 if ok else 0.0, "exact"

    if norm_term in norm_value:
        return True, 1.0, "contains"

    if match_type in ("contains", "include", "包含"):
        return False, 0.0, "contains_miss"

    # fuzzy 模式：短词必须更严格，避免“国权”“中心”等泛词误点。
    required = float(threshold)
    if len(norm_term) < max(min_fuzzy_chars, 1):
        required = max(required, 0.88)

    pr = partial_ratio(norm_term, norm_value)
    ov = token_overlap_score(norm_term, norm_value)
    score = max(pr, ov * 0.92)
    return score >= required, score, "fuzzy"


def match_whitelist(candidate_text: str, url: str, whitelist: List[Dict[str, str]], cfg: RunConfig) -> Optional[MatchResult]:
    candidate_text = repair_mojibake_text(candidate_text)
    url = repair_mojibake_text(url)
    combined = f"{candidate_text}\n{url}"
    for rule in whitelist:
        mt = (rule.get("match_type") or "fuzzy").lower()
        # title 支持一行多个模糊词：品牌词A|品牌词B|地址词
        for term in split_terms(rule.get("title") or ""):
            ok, score, reason = match_one_term(term, combined, mt, cfg.fuzzy_threshold, cfg.min_fuzzy_chars)
            if ok:
                return MatchResult(rule=rule, matched_term=term, score=score, reason=reason)
        for term in split_terms(rule.get("url") or ""):
            ok, score, reason = match_one_term(term, url, "contains" if mt == "fuzzy" else mt, cfg.fuzzy_threshold, cfg.min_fuzzy_chars)
            if ok:
                return MatchResult(rule=rule, matched_term=term, score=score, reason="url_" + reason)
    return None




def match_blacklist(candidate_text: str, url: str, blacklist: List[Dict[str, str]], cfg: RunConfig) -> Optional[MatchResult]:
    if not blacklist:
        return None
    return match_whitelist(candidate_text, url, blacklist, cfg)


def _blacklist_terms(blacklist: List[Dict[str, str]]) -> List[str]:
    terms: List[str] = []
    for rule in blacklist or []:
        terms.extend(split_terms(rule.get("title") or ""))
        terms.extend(split_terms(rule.get("url") or ""))
    return [t for t in terms if normalize_cn(t)]


def safe_inner_text(locator: Locator, timeout: int = 1200) -> str:
    try:
        return locator.inner_text(timeout=timeout).strip()
    except Exception:
        return ""


def safe_attr(locator: Locator, name: str, timeout: int = 1200) -> str:
    try:
        return (locator.get_attribute(name, timeout=timeout) or "").strip()
    except Exception:
        return ""


def _float_opt(value: str) -> Optional[float]:
    try:
        if value is None or str(value).strip() == "":
            return None
        return float(str(value).strip())
    except Exception:
        return None


def _profile_safe_name(name: str) -> str:
    name = normalize_cn(name or "default") or "default"
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name)[:64]


def is_fixed_search_env(cfg: RunConfig) -> bool:
    """固定搜索环境：稳定 UA/窗口/语言/地区定位，并保存/读取 CK。

    这不是自动过验证码；遇到安全验证仍会按异常页处理。
    """
    mode = (cfg.environment_mode or "").lower()
    return bool(getattr(cfg, "fixed_search_environment", False)) or "固定" in mode or "写死" in mode or "ck" in mode


def is_persistent_env(cfg: RunConfig) -> bool:
    mode = (cfg.environment_mode or "").lower()
    return is_fixed_search_env(cfg) or "持久" in mode or "cookie" in mode or "profile" in mode


def is_region_enhanced_env(cfg: RunConfig) -> bool:
    mode = (cfg.environment_mode or "").lower()
    return "本地" in mode or "地区" in mode or "增强" in mode or is_persistent_env(cfg)


def safe_text(value) -> str:
    if value is None:
        return ""
    try:
        return str(value)
    except Exception:
        return ""


def _region_profile_part(cfg: RunConfig) -> str:
    """地区搜索环境指纹。

    v1.0 修复：旧版本固定使用 profiles/fixed_search_default，
    用户修改地区后，如果继续读取旧 CK/LocalStorage，
    本地生活链接可能继续带旧城市。这里把地区加入 profile key，
    地区变化时自动使用新的 CK 容器。
    """
    city = safe_text(getattr(cfg, "region_city", "") or "").strip()
    lat = safe_text(getattr(cfg, "region_latitude", "") or "").strip()
    lon = safe_text(getattr(cfg, "region_longitude", "") or "").strip()
    tz = safe_text(getattr(cfg, "region_timezone", "") or "").strip()
    loc = safe_text(getattr(cfg, "region_locale", "") or "").strip()
    raw = "_".join([x for x in [city, lat, lon, tz, loc] if x])
    return _profile_safe_name(raw) if raw else ""


def profile_storage_path(cfg: RunConfig) -> str:
    prefix = "fixed_search" if is_fixed_search_env(cfg) else "profile"
    profile = safe_text(getattr(cfg, "profile_name", "") or "default").strip() or "default"
    if bool(getattr(cfg, "isolate_profile_by_region", True)):
        part = _region_profile_part(cfg)
        if part:
            profile = f"{profile}_{part}"
    base = Path("profiles") / _profile_safe_name(f"{prefix}_{profile}")
    base.mkdir(parents=True, exist_ok=True)
    return str(base / "storage_state.json")


def build_context_kwargs(cfg: RunConfig) -> Dict:
    locale = (cfg.region_locale or "zh-CN").strip() or "zh-CN"
    timezone_id = (cfg.region_timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    fixed = is_fixed_search_env(cfg)

    width = int(cfg.viewport_width or 390)
    height = int(cfg.viewport_height or 844)
    # 固定搜索环境下强制使用稳定的移动端窗口，避免同一任务不同线程窗口尺寸不一致。
    if fixed:
        width = 390
        height = 844

    default_mobile_ua = (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
        "Version/16.0 Mobile/15E148 Safari/604.1"
    )

    kwargs: Dict = {
        "viewport": {"width": width, "height": height},
        "screen": {"width": width, "height": height},
        "ignore_https_errors": True,
        "locale": locale,
        "timezone_id": timezone_id,
        "java_script_enabled": True,
        "color_scheme": "light",
        "extra_http_headers": {
            "Accept-Language": f"{locale},zh-CN;q=0.9,zh;q=0.8,en;q=0.5",
        },
    }
    if cfg.mobile_mode or fixed:
        kwargs.update({
            "is_mobile": True,
            "has_touch": True,
            "device_scale_factor": 3 if fixed else 2,
        })
    ua = (cfg.user_agent or "").strip() or (default_mobile_ua if fixed else "")
    if ua:
        kwargs["user_agent"] = ua

    lat = _float_opt(cfg.region_latitude)
    lon = _float_opt(cfg.region_longitude)
    acc = _float_opt(cfg.region_accuracy) or 100.0
    if lat is not None and lon is not None:
        kwargs["geolocation"] = {"latitude": lat, "longitude": lon, "accuracy": acc}
        kwargs["permissions"] = ["geolocation"]

    if is_persistent_env(cfg):
        state_path = profile_storage_path(cfg)
        if os.path.exists(state_path):
            kwargs["storage_state"] = state_path
    return kwargs



def install_region_environment_scripts(context, cfg: RunConfig, log: Optional[Callable[[str], None]] = None, worker_name: str = "") -> None:
    """在任何页面脚本运行前写入地区/定位环境。

    Playwright 的 geolocation 只有页面主动调用定位 API 时才生效；
    部分百度页面会先读取 navigator.language、LocalStorage 或缓存城市。
    v1.0 在 context 创建后、new_page 前注入脚本，避免仍沿用旧城市。
    """
    if not is_region_enhanced_env(cfg):
        return
    city = safe_text(getattr(cfg, "region_city", "") or "").strip()
    locale = safe_text(getattr(cfg, "region_locale", "") or "zh-CN").strip() or "zh-CN"
    lat = _float_opt(getattr(cfg, "region_latitude", ""))
    lon = _float_opt(getattr(cfg, "region_longitude", ""))
    acc = _float_opt(getattr(cfg, "region_accuracy", "")) or 100.0
    if not city and (lat is None or lon is None):
        return
    payload = json.dumps({"city": city, "lat": lat, "lon": lon, "accuracy": acc, "locale": locale}, ensure_ascii=False)
    js = f"""
    (() => {{
      const cfg = {payload};
      try {{
        if (cfg.locale) {{
          try {{ Object.defineProperty(navigator, 'language', {{get: () => cfg.locale, configurable: true}}); }} catch(e) {{}}
          try {{ Object.defineProperty(navigator, 'languages', {{get: () => [cfg.locale, 'zh-CN', 'zh'], configurable: true}}); }} catch(e) {{}}
        }}
        const applyStorage = () => {{
          try {{
            const city = cfg.city || '';
            if (city) {{
              const keys = ['city','cur_city','current_city','selected_city','lbs_city','baidu_city','BAIDU_CITY','BAIDU_LBS_CITY','wct_region_city'];
              for (const k of keys) {{ try {{ localStorage.setItem(k, city); sessionStorage.setItem(k, city); }} catch(e) {{}} }}
            }}
            if (cfg.lat !== null && cfg.lon !== null) {{
              const geo = JSON.stringify({{city: city, latitude: cfg.lat, longitude: cfg.lon, accuracy: cfg.accuracy}});
              for (const k of ['wct_geolocation','geolocation','BAIDU_GEO','lbs_geo']) {{ try {{ localStorage.setItem(k, geo); sessionStorage.setItem(k, geo); }} catch(e) {{}} }}
            }}
          }} catch(e) {{}}
        }};
        applyStorage();
        try {{ window.addEventListener('DOMContentLoaded', applyStorage, {{once:false}}); }} catch(e) {{}}
        if (cfg.lat !== null && cfg.lon !== null && navigator.geolocation) {{
          const pos = () => ({{
            coords: {{
              latitude: cfg.lat, longitude: cfg.lon, accuracy: cfg.accuracy || 100,
              altitude: null, altitudeAccuracy: null, heading: null, speed: null
            }},
            timestamp: Date.now()
          }});
          try {{
            navigator.geolocation.getCurrentPosition = function(success, error, options) {{
              setTimeout(() => {{ try {{ success && success(pos()); }} catch(e) {{}} }}, 0);
            }};
            navigator.geolocation.watchPosition = function(success, error, options) {{
              const id = Math.floor(Math.random() * 1000000);
              setTimeout(() => {{ try {{ success && success(pos()); }} catch(e) {{}} }}, 0);
              return id;
            }};
            navigator.geolocation.clearWatch = function(id) {{}};
          }} catch(e) {{}}
        }}
      }} catch(e) {{}}
    }})();
    """
    try:
        context.add_init_script(js)
        if log:
            log(f"[{worker_name}] 已注入地区模拟脚本：城市={city or '未设置'}；经纬度={getattr(cfg,'region_latitude','')},{getattr(cfg,'region_longitude','')}")
    except Exception as e:
        if log:
            log(f"[{worker_name}] 地区模拟脚本注入失败：{e}")


def apply_region_storage_to_page(page, cfg: RunConfig) -> None:
    """页面已经打开后，再补写一次地区 LocalStorage/SessionStorage。"""
    if not is_region_enhanced_env(cfg):
        return
    city = safe_text(getattr(cfg, "region_city", "") or "").strip()
    lat = _float_opt(getattr(cfg, "region_latitude", ""))
    lon = _float_opt(getattr(cfg, "region_longitude", ""))
    acc = _float_opt(getattr(cfg, "region_accuracy", "")) or 100.0
    try:
        page.evaluate(
            """
            ({city, lat, lon, acc}) => {
              try {
                if (city) {
                  const keys = ['city','cur_city','current_city','selected_city','lbs_city','baidu_city','BAIDU_CITY','BAIDU_LBS_CITY','wct_region_city'];
                  for (const k of keys) { try { localStorage.setItem(k, city); sessionStorage.setItem(k, city); } catch(e) {} }
                }
                if (lat !== null && lon !== null) {
                  const geo = JSON.stringify({city: city || '', latitude: lat, longitude: lon, accuracy: acc || 100});
                  for (const k of ['wct_geolocation','geolocation','BAIDU_GEO','lbs_geo']) { try { localStorage.setItem(k, geo); sessionStorage.setItem(k, geo); } catch(e) {} }
                }
              } catch(e) {}
            }
            """,
            {"city": city, "lat": lat, "lon": lon, "acc": acc},
        )
    except Exception:
        pass


def rewrite_baidu_site_city_url(page, cfg: RunConfig, log: Optional[Callable[[str], None]] = None, keyword: str = "") -> None:
    """同步百度商家模块页的地区和搜索词参数。

    v1.0 修复：百度搜索结果里的“查看更多”链接可能来自旧 CK/LocalStorage，
    即使当前搜索框是新城市，site.baidu.com 的 URL 里仍可能保留旧 city/query/title，
    导致前面搜索看似到新城市，进入模块后又跳回旧城市。

    处理规则：
    1. 只处理 site.baidu.com 页面；
    2. city 参数强制改为前端“模拟城市”；没有 city 时补上；
    3. query/title/word/wd/q/keyword 等搜索词参数优先同步为当前关键词；
    4. 如果 URL 参数里直接包含旧城市文本，也替换为模拟城市；
    5. 修改后重新加载模块页。
    """
    if not bool(getattr(cfg, "force_region_city_in_target_url", True)):
        return
    city = safe_text(getattr(cfg, "region_city", "") or "").strip()
    kw = safe_text(keyword or "").strip()
    if not city and not kw:
        return
    try:
        url = page.url or ""
        if "site.baidu.com" not in url:
            return
        parsed = urlparse(url)
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        changed = False
        old_city = ""
        query_keys = {"query", "title", "word", "wd", "q", "keyword", "search_key", "searchKey", "key", "kw"}
        city_keys = {"city", "loc", "location", "current_city", "cur_city"}
        new_pairs = []
        seen_city_key = False
        for k, v in pairs:
            key_l = str(k)
            new_v = v
            if key_l in city_keys and city:
                seen_city_key = True
                old_city = v
                if v != city:
                    new_v = city
                    changed = True
            elif key_l in query_keys and kw:
                # 这些字段是百度商家模块的搜索语义，必须跟当前关键词一致，避免进入旧城市结果。
                if v != kw:
                    new_v = kw
                    changed = True
            elif city and isinstance(v, str):
                # 常见旧城市兜底替换。避免 query/title 残留旧城市。
                # 不做全国城市库推断，只替换已知旧值和明显不同于当前城市的旧城市关键词。
                for old in ["石家庄", "北京", "上海", "广州", "深圳", "中山", "天津", "重庆", "南京", "杭州", "苏州", "成都", "武汉", "西安", "郑州", "长沙", "合肥", "济南", "青岛", "沈阳", "大连", "佛山", "东莞"]:
                    if old != city and old in new_v:
                        new_v = new_v.replace(old, city)
                        changed = True
            new_pairs.append((k, new_v))
        if city and not seen_city_key:
            new_pairs.append(("city", city))
            changed = True
        if not changed:
            return
        new_query = urlencode(new_pairs, doseq=True)
        new_url = urlunparse((parsed.scheme, parsed.netloc, parsed.path, parsed.params, new_query, parsed.fragment))
        if new_url == url:
            return
        if log:
            parts = []
            if city:
                parts.append(f"城市={old_city or '补充'} -> {city}")
            if kw:
                parts.append(f"搜索词={kw}")
            log("已同步目标模块URL地区/关键词参数：" + "；".join(parts))
        page.goto(new_url, wait_until="domcontentloaded", timeout=getattr(cfg, "page_timeout_ms", 30000))
        try:
            page.wait_for_timeout(1200)
        except Exception:
            pass
        apply_region_storage_to_page(page, cfg)
    except Exception as e:
        if log:
            log(f"目标模块地区/关键词URL同步失败：{e}")

def save_search_environment_state(context, cfg: RunConfig, log: Optional[Callable[[str], None]] = None, worker_name: str = "") -> None:
    """保存当前搜索环境 CK/LocalStorage。

    固定搜索环境与持久环境都会保存；全新环境不会保存。
    """
    if not is_persistent_env(cfg):
        return
    try:
        path = profile_storage_path(cfg)
        try:
            # Playwright 新版本支持 indexed_db=True；旧版本会走 TypeError 兜底。
            context.storage_state(path=path, indexed_db=True)
        except TypeError:
            context.storage_state(path=path)
        if log:
            log(f"[{worker_name}] 已更新固定搜索环境 CK/LocalStorage：{path}")
    except Exception as e:
        if log:
            log(f"[{worker_name}] 固定搜索环境保存失败：{e}")


def apply_fixed_search_environment(context, page, cfg: RunConfig, log: Callable[[str], None], worker_name: str = "") -> None:
    """应用固定搜索环境：地区定位权限、稳定语言/时区、CK 持久化。

    注意：这只用于保持测试环境稳定；遇到安全验证不会自动绕过。
    """
    if not is_fixed_search_env(cfg):
        return
    try:
        lat = _float_opt(cfg.region_latitude)
        lon = _float_opt(cfg.region_longitude)
        acc = _float_opt(cfg.region_accuracy) or 100.0
        if lat is not None and lon is not None:
            try:
                context.set_geolocation({"latitude": lat, "longitude": lon, "accuracy": acc})
            except Exception:
                pass
            # 百度常用域名都提前授权定位，避免每次搜索时权限状态不同。
            for origin in ["https://www.baidu.com", "https://m.baidu.com", "https://site.baidu.com"]:
                try:
                    context.grant_permissions(["geolocation"], origin=origin)
                except Exception:
                    pass
        apply_region_storage_to_page(page, cfg)
        log(f"[{worker_name}] 固定搜索环境已启用：CK持久化=True；地区={cfg.region_city or '未设置'}；经纬度={cfg.region_latitude},{cfg.region_longitude}；时区={cfg.region_timezone or 'Asia/Shanghai'}；语言={cfg.region_locale or 'zh-CN'}")
    except Exception as e:
        log(f"[{worker_name}] 固定搜索环境应用失败：{e}")


def _existing_file(path: str) -> str:
    try:
        p = Path(os.path.expandvars(os.path.expanduser(path))).resolve()
        return str(p) if p.exists() and p.is_file() else ""
    except Exception:
        return ""


def find_system_chromium_browser() -> str:
    """查找本机已安装的 Edge/Chrome。

    这是为了解决 Chromium 首次下载过慢的问题。Playwright 会启动一个全新的隐藏浏览器进程，
    不会直接打开用户平时使用的窗口，也不会复用用户的 Cookie/历史记录。
    """
    env_path = os.environ.get("WCT_BROWSER_PATH", "").strip()
    if env_path:
        found = _existing_file(env_path)
        if found:
            return found

    candidates = [
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Chromium\Application\chrome.exe",
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ]
    for c in candidates:
        found = _existing_file(c)
        if found:
            return found
    return ""


def _has_bundled_playwright_browser() -> bool:
    """粗略判断软件目录或环境变量里是否已经有 Playwright 浏览器。"""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env and Path(env).exists():
        for name in ("chromium-*", "chromium_headless_shell-*", "chrome-*"):
            if list(Path(env).glob(name)):
                return True
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "ms-playwright").exists():
            return True
    return False


def _browser_mode_value(value: str) -> str:
    v = (value or "").strip().lower()
    if "手动" in v or "manual" in v:
        return "manual_path"
    if "只用内置" in v or "内置" in v or "bundled" in v:
        return "bundled_only"
    if "只用本机" in v or "本机" in v or "system" in v:
        return "system_only"
    return "auto_system_first"



_TRAFFIC_AD_KEYWORDS = [
    "doubleclick", "googlesyndication", "google-analytics", "googletagmanager", "adservice", "adsystem",
    "hm.baidu.com", "bdstatic.com/uba", "sp0.baidu.com", "ulog", "log", "track", "tracker", "stat", "analytics",
    "beacon", "collect", "push", "alimama", "tanx", "cnzz", "sentry", "monitor", "metrics",
]
_TRAFFIC_MAP_KEYWORDS = [
    "map", "maps", "amap", "baidumap", "api.map.baidu", "map.baidu", "maps.googleapis", "tile", "tiles",
    "maponline", "mapclick", "geocoder", "geolocation",
]


def _traffic_mode_value(value: str) -> str:
    v = (value or "").strip().lower()
    if not v or "关闭" in v or "off" in v or "normal" in v:
        return "off"
    if "极限" in v or "extreme" in v:
        return "extreme"
    if "强力" in v or "aggressive" in v or "strong" in v:
        return "strong"
    return "standard"


def install_traffic_saving_routes(context, cfg: RunConfig, log: Callable[[str], None], worker_name: str = "") -> Dict[str, int]:
    """给整个浏览器上下文安装请求拦截，减少动态 IP 代理流量。

    标准模式只拦截图片/视频/音频/字体，通常不影响文本识别和点击。
    强力模式额外拦截广告/统计/地图资源；极限模式可能影响页面布局，只建议临时测试。
    """
    mode = _traffic_mode_value(cfg.traffic_saving_mode)
    stats = {"blocked": 0, "continued": 0, "image": 0, "media": 0, "font": 0, "ad": 0, "map": 0, "other": 0}
    if mode == "off":
        log(f"[{worker_name}] 省流量模式：关闭，页面资源正常加载")
        return stats

    def should_block(resource_type: str, url: str) -> Tuple[bool, str]:
        u = (url or "").lower()
        rt = (resource_type or "").lower()

        # 识别修复：无论标准/强力省流量，都不拦截 document/script/xhr/fetch/stylesheet。
        # 百度商家列表里的店铺名称经常由接口异步返回；如果把 XHR/fetch 或脚本误拦截，页面看起来存在，
        # 但软件拿不到完整 DOM，容易出现“人工打开有白名单，软件没识别到”。
        critical_types = {"document", "script", "xhr", "fetch", "stylesheet"}
        if mode in {"standard", "strong"} and rt in critical_types:
            return False, ""

        if rt in {"image", "media", "font"}:
            if rt == "image":
                return True, "image"
            if rt == "media":
                return True, "media"
            return True, "font"

        # 广告/统计/地图拦截只针对非关键资源；避免把承载商家数据的接口误杀。
        if cfg.block_ads_and_trackers and rt not in critical_types and any(k in u for k in _TRAFFIC_AD_KEYWORDS):
            return True, "ad"
        if cfg.block_maps and rt not in critical_types and any(k in u for k in _TRAFFIC_MAP_KEYWORDS):
            return True, "map"
        if mode == "extreme":
            # 极限模式只保留文档、脚本、XHR/fetch、样式，其他全部拦截。
            if rt not in critical_types:
                return True, "other"
        elif mode == "strong":
            # 强力模式保留样式和脚本，但拦截常见的 ping / manifest / websocket 等非必要资源。
            if rt in {"websocket", "manifest", "eventsource", "ping"}:
                return True, "other"
        return False, ""

    def handler(route):
        try:
            req = route.request
            block, reason = should_block(req.resource_type, req.url)
            if block:
                stats["blocked"] += 1
                stats[reason] = stats.get(reason, 0) + 1
                try:
                    route.abort("blockedbyclient")
                except TypeError:
                    route.abort()
            else:
                stats["continued"] += 1
                route.continue_()
        except Exception:
            try:
                route.continue_()
            except Exception:
                pass

    context.route("**/*", handler)
    log(f"[{worker_name}] 已开启省流量模式：{cfg.traffic_saving_mode}；图片/视频/音频/字体将被拦截；广告统计={cfg.block_ads_and_trackers}；地图资源={cfg.block_maps}")
    return stats


def summarize_traffic_stats(stats: Optional[Dict[str, int]]) -> str:
    if not stats:
        return ""
    return f"省流量拦截 {stats.get('blocked',0)} 个请求（图片 {stats.get('image',0)}、媒体 {stats.get('media',0)}、字体 {stats.get('font',0)}、广告统计 {stats.get('ad',0)}、地图 {stats.get('map',0)}、其他 {stats.get('other',0)}）"

def launch_browser(pw, cfg: RunConfig, proxy: Optional[Dict[str, str]]):
    # 强制把“隐藏浏览器”落实到浏览器启动参数。
    # 之前在部分本机 Edge/Chrome 环境中，仅传 headless=True 仍可能出现一个空白窗口。
    # 这里额外加入 Chromium 原生命令行参数，并把窗口位置放到屏幕外作为兜底。
    effective_headless = bool(cfg.headless)
    launch_args = [
        "--disable-blink-features=AutomationControlled",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-extensions",
        "--disable-popup-blocking",
        "--mute-audio",
    ]
    if effective_headless:
        launch_args.extend([
            "--headless=new",
            "--hide-scrollbars",
            "--window-position=-32000,-32000",
            f"--window-size={max(int(cfg.viewport_width or 390), 320)},{max(int(cfg.viewport_height or 844), 480)}",
        ])

    launch_kwargs: Dict = {
        "headless": effective_headless,
        "args": launch_args,
    }
    if proxy:
        launch_kwargs["proxy"] = proxy

    mode = _browser_mode_value(cfg.browser_source)

    manual_path = (cfg.browser_executable_path or "").strip()
    if mode == "manual_path":
        manual_found = _existing_file(manual_path)
        if not manual_found:
            raise RuntimeError("手动浏览器路径不存在，请选择 msedge.exe / chrome.exe / brave.exe 的完整路径")
        launch_kwargs["executable_path"] = manual_found
    elif mode == "system_only":
        system_found = find_system_chromium_browser()
        if not system_found:
            raise RuntimeError("未找到本机 Edge/Chrome。请安装 Edge/Chrome，或改为‘软件内置 Chromium’模式。")
        launch_kwargs["executable_path"] = system_found
    elif mode == "auto_system_first":
        # 免下载推荐：有内置就用内置；没有内置就用本机 Edge/Chrome，避免 180MB+ 的 Chromium 下载。
        if not _has_bundled_playwright_browser():
            system_found = find_system_chromium_browser()
            if system_found:
                launch_kwargs["executable_path"] = system_found
    # bundled_only 不设置 executable_path，交给 Playwright 使用自带 Chromium。

    try:
        return pw.chromium.launch(**launch_kwargs)
    except Exception as e:
        msg = str(e)
        if "Executable doesn't exist" in msg or "Please run the following command" in msg:
            raise RuntimeError(
                "Chromium 浏览器文件缺失。可直接改用免下载模式：在‘识别与点击’里把浏览器来源设为 "
                "‘自动免下载：优先本机 Edge/Chrome’，或填写本机 msedge.exe/chrome.exe 路径。"
                "如果必须做完整绿色版，再运行 04_fix_missing_chromium_in_dist.bat 下载内置 Chromium。"
            ) from e
        raise





def _set_search_keyword_without_mouse(page, selector: str, keyword: str, log: Callable[[str], None]) -> bool:
    """不依赖鼠标点击输入框，直接给真实 input/textarea 写入关键词。

    百度移动页经常在输入框上盖一层 fake-placeholder label，Playwright click 会被 label 拦截，
    旧版因此还没输入关键词就报 Locator.click Timeout。这里改为 JS 直接 focus/value/input/change，
    避免被遮挡层影响。
    """
    try:
        ok = page.evaluate(
            """
            ([selector, keyword]) => {
              const norm = (s) => (s || '').toString().trim();
              const isRealInput = (el) => {
                if (!el) return false;
                const tag = (el.tagName || '').toLowerCase();
                if (tag !== 'input' && tag !== 'textarea') return false;
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (['hidden','submit','button','checkbox','radio','file'].includes(type)) return false;
                if (el.disabled || el.readOnly) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden') return false;
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 10) return false;
                return true;
              };
              let nodes = [];
              try { nodes = Array.from(document.querySelectorAll(selector || 'input,textarea')); } catch(e) { nodes = []; }
              const extraSelectors = [
                '#index-kw', '#kw', 'input[name="word"]', 'textarea[name="word"]',
                'input[name="wd"]', 'textarea[name="wd"]', 'input[type="search"]',
                'input[placeholder*="关键词"]', 'textarea[placeholder*="关键词"]',
                'input[placeholder*="搜索"]', 'textarea[placeholder*="搜索"]'
              ];
              for (const sel of extraSelectors) {
                try { nodes.push(...Array.from(document.querySelectorAll(sel))); } catch(e) {}
              }
              const seen = new Set();
              const candidates = [];
              for (const el of nodes) {
                if (seen.has(el)) continue; seen.add(el);
                if (!isRealInput(el)) continue;
                let score = 0;
                const id = (el.id || '').toLowerCase();
                const name = (el.getAttribute('name') || '').toLowerCase();
                const cls = (el.className || '').toString().toLowerCase();
                const ph = (el.getAttribute('placeholder') || '').toLowerCase();
                const type = (el.getAttribute('type') || '').toLowerCase();
                if (id === 'index-kw' || id === 'kw') score += 120;
                if (name === 'word' || name === 'wd') score += 120;
                if (type === 'search') score += 50;
                if (ph.includes('关键词') || ph.includes('搜索')) score += 60;
                if (cls.includes('search') || cls.includes('se-input')) score += 40;
                const r = el.getBoundingClientRect();
                if (r.top >= -20 && r.top <= window.innerHeight + 120) score += 30;
                candidates.push({el, score});
              }
              candidates.sort((a,b) => b.score - a.score);
              const el = candidates.length ? candidates[0].el : null;
              if (!el) return {ok:false, reason:'未找到真实搜索输入框'};
              try { el.removeAttribute('readonly'); } catch(e) {}
              try { el.focus({preventScroll:false}); } catch(e) { try { el.focus(); } catch(_) {} }
              const proto = el.tagName.toLowerCase() === 'textarea' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
              const desc = Object.getOwnPropertyDescriptor(proto, 'value');
              try {
                if (desc && desc.set) desc.set.call(el, ''); else el.value = '';
                if (desc && desc.set) desc.set.call(el, keyword); else el.value = keyword;
              } catch(e) { el.value = keyword; }
              const fire = (type) => {
                let ev;
                if (type === 'input') ev = new InputEvent('input', {bubbles:true, cancelable:true, inputType:'insertText', data:keyword});
                else ev = new Event(type, {bubbles:true, cancelable:true});
                el.dispatchEvent(ev);
              };
              try { fire('input'); fire('change'); fire('keyup'); fire('keydown'); } catch(e) {}
              try {
                const lab = document.querySelector('label[for="' + el.id + '"]');
                if (lab) lab.style.pointerEvents = 'none';
              } catch(e) {}
              return {ok: norm(el.value).includes(norm(keyword)), value: el.value || '', id: el.id || '', name: el.getAttribute('name') || ''};
            }
            """,
            [selector or "", keyword],
        )
        if isinstance(ok, dict) and ok.get('ok'):
            log(f"已通过无鼠标写入方式输入关键词：{keyword}（input id={ok.get('id') or '-'} name={ok.get('name') or '-'}）")
            return True
        if isinstance(ok, dict):
            log(f"无鼠标写入搜索框未确认：{ok.get('reason') or ok.get('value') or ok}")
    except Exception as e:
        log(f"无鼠标写入搜索框失败：{e}")
    return False

def _find_visible_search_button_handle(page, custom_selector: str = ""):
    """查找真正可见的搜索按钮，避免点到 hidden-submit 或还没搜索就进入后续判断。"""
    try:
        if custom_selector.strip():
            loc = page.locator(custom_selector).locator("visible=true").first
            try:
                if loc.count() > 0 and loc.is_visible(timeout=800):
                    return loc.element_handle(timeout=1000), "自定义搜索按钮"
            except Exception:
                pass
    except Exception:
        pass
    try:
        handle = page.evaluate_handle(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                if (r.width < 8 || r.height < 8) return false;
                if (el.disabled) return false;
                if ((el.getAttribute('tabindex') || '') === '-1') return false;
                const cls = (el.className || '').toString().toLowerCase();
                if (cls.includes('hidden')) return false;
                return true;
              };
              const textOf = (el) => ((el.innerText || el.textContent || '') + ' ' + (el.value || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
              const candidates = Array.from(document.querySelectorAll('button,input[type="submit"],input[type="button"],[role="button"],a'));
              let best = null;
              let bestScore = -1;
              for (const el of candidates) {
                if (!visible(el)) continue;
                const id = (el.id || '').toLowerCase();
                const name = (el.getAttribute('name') || '').toLowerCase();
                const raw = textOf(el);
                let score = 0;
                if (id === 'su' || id === 'index-bn' || id.includes('search') || name === 'btnk') score += 100;
                if (/百度一下|搜索|搜一下|search/i.test(raw)) score += 80;
                const tag = (el.tagName || '').toLowerCase();
                if (tag === 'button') score += 30;
                if (tag === 'input') score += 20;
                const r = el.getBoundingClientRect();
                if (r.top >= -100 && r.top <= window.innerHeight + 200) score += 30;
                if (score > bestScore) { best = el; bestScore = score; }
              }
              return bestScore >= 50 ? best : null;
            }
            """
        )
        el = handle.as_element() if handle else None
        if el:
            return el, "页面可见搜索按钮"
    except Exception:
        pass
    return None, ""


def _submit_search_form_fallback(page, search_locator) -> bool:
    """没有可见搜索按钮时，尽量触发表单提交。"""
    try:
        return bool(search_locator.evaluate(
            """
            el => {
              const fire = (target, type) => target.dispatchEvent(new Event(type, {bubbles:true, cancelable:true}));
              try { fire(el, 'input'); fire(el, 'change'); } catch(e) {}
              const form = el.closest && el.closest('form');
              if (form) {
                try {
                  if (form.requestSubmit) { form.requestSubmit(); return true; }
                  form.submit(); return true;
                } catch(e) {}
              }
              return false;
            }
            """
        ))
    except Exception:
        return False


def _search_input_value(search_locator) -> str:
    try:
        return (search_locator.input_value(timeout=1000) or "").strip()
    except Exception:
        try:
            return str(search_locator.evaluate("el => (el.value || el.innerText || el.textContent || '').trim()") or "").strip()
        except Exception:
            return ""


def _ensure_search_input_keyword(page, search_locator, keyword: str, log: Callable[[str], None]) -> None:
    """确认关键词确实写进搜索框；禁止先 click 再 fill。

    移动百度首页的输入框上面有 fake-placeholder 标签，鼠标点击经常被拦截。
    这里顺序改成：fill 直写 → JS 直写真实输入框 → 最后才尝试点击，不再让点击失败阻断输入。
    """
    normalized_keyword = normalize_cn(keyword)
    value = _search_input_value(search_locator)
    if normalized_keyword and normalized_keyword in normalize_cn(value):
        log(f"搜索框关键词已确认：{keyword}")
        return

    # 第一优先：Playwright fill 不需要鼠标点击，通常能绕过遮挡 label。
    try:
        search_locator.fill("", timeout=2500)
        search_locator.fill(keyword, timeout=3500)
        value = _search_input_value(search_locator)
        if normalized_keyword and normalized_keyword in normalize_cn(value):
            log(f"搜索框关键词已确认：{keyword}")
            return
    except Exception as e:
        log(f"直接填入搜索框失败，改用无鼠标写入：{e}")

    # 第二优先：JS 对真实 input/textarea 设置 value 并触发 input/change。
    try:
        selector = "input[name='word'], input[name='wd'], textarea[name='word'], textarea[name='wd'], #index-kw, #kw, input[type='search']"
        if _set_search_keyword_without_mouse(page, selector, keyword, log):
            value = _search_input_value(search_locator)
            # 有些页面 locator 仍指向同一个 input，可直接确认；否则用页面 JS 确认。
            if normalized_keyword and normalized_keyword in normalize_cn(value):
                log(f"搜索框关键词已确认：{keyword}")
                return
            try:
                page_value = page.evaluate("""
                (keyword) => {
                  const ns = (s) => (s || '').toString().replace(/\s+/g,'');
                  const els = Array.from(document.querySelectorAll('input,textarea'));
                  return els.some(el => ns(el.value || el.textContent || '').includes(ns(keyword)));
                }
                """, keyword)
                if page_value:
                    log(f"搜索框关键词已确认：{keyword}")
                    return
            except Exception:
                pass
    except Exception as e:
        log(f"JS 写入搜索框兜底异常：{e}")

    # 最后才尝试点击：仅作为激活输入框兜底，不允许点击失败直接阻断后续 fill。
    try:
        search_locator.click(timeout=1200, force=True)
    except Exception:
        pass
    try:
        search_locator.fill("", timeout=1500)
        search_locator.fill(keyword, timeout=2500)
    except Exception:
        pass
    value = _search_input_value(search_locator)
    if normalized_keyword and normalized_keyword in normalize_cn(value):
        log(f"搜索框关键词已确认：{keyword}")
        return

    # 最终页面级确认。
    try:
        page_value = page.evaluate("""
        (keyword) => {
          const ns = (s) => (s || '').toString().replace(/\s+/g,'');
          return Array.from(document.querySelectorAll('input,textarea')).map(el => el.value || '').join(' ').includes(keyword)
              || ns(Array.from(document.querySelectorAll('input,textarea')).map(el => el.value || '').join(' ')).includes(ns(keyword));
        }
        """, keyword)
        if page_value:
            log(f"搜索框关键词已确认：{keyword}")
            return
    except Exception:
        pass
    raise RuntimeError(f"搜索框没有成功输入关键词：{keyword}；当前输入框内容：{value or '<空>'}")

def _page_contains_keyword(page, keyword: str, timeout_ms: int = 1800) -> bool:
    norm_kw = normalize_cn(keyword)
    if not norm_kw:
        return False
    try:
        body = page.locator("body").inner_text(timeout=timeout_ms)
        if norm_kw in normalize_cn(body):
            return True
    except Exception:
        pass
    try:
        title = page.title(timeout=timeout_ms)
        if norm_kw in normalize_cn(title):
            return True
    except Exception:
        pass
    return False


def _url_looks_like_search_result(url: str, keyword: str) -> bool:
    if not url:
        return False
    low = url.lower()
    if any(mark in low for mark in ["/s?", "?wd=", "&wd=", "?word=", "&word=", "query=", "keyword=", "sa=search", "search"]):
        return True
    # 不强制 URL 一定包含中文关键词：有些移动站会把关键词存入页面状态。
    return False


def _search_result_confirmed(page, search_locator, keyword: str, before_url: str, cfg: RunConfig) -> Tuple[bool, str]:
    """判断是否已经真正完成搜索。只有确认后才允许下滑找目标板块。"""
    reasons: List[str] = []
    try:
        current_url = page.url
    except Exception:
        current_url = ""
    try:
        current_value = _search_input_value(search_locator)
    except Exception:
        current_value = ""
    has_kw_in_input = bool(normalize_cn(keyword) and normalize_cn(keyword) in normalize_cn(current_value))
    has_kw_in_page = _page_contains_keyword(page, keyword, timeout_ms=1200)
    url_changed = bool(current_url and before_url and current_url != before_url)
    url_search_like = _url_looks_like_search_result(current_url, keyword)
    if has_kw_in_input:
        reasons.append("搜索框含关键词")
    if has_kw_in_page:
        reasons.append("页面文本含关键词")
    if url_changed:
        reasons.append("URL已变化")
    if url_search_like:
        reasons.append("URL像搜索结果页")
    # 可靠确认：页面或搜索框能看到关键词，并且 URL 已变化/像搜索页/结果项出现。
    result_item_seen = False
    try:
        selector = (cfg.result_item_selector or "").strip()
        if selector:
            result_item_seen = page.locator(selector).first.is_visible(timeout=1000)
    except Exception:
        result_item_seen = False
    if result_item_seen:
        reasons.append("结果项已出现")
    if (has_kw_in_input or has_kw_in_page) and (url_changed or url_search_like or result_item_seen):
        return True, "、".join(reasons) or "已确认"
    # 有些站点 URL 不变，但搜索结果异步加载；页面文本含关键词且出现结果结构也算确认。
    if has_kw_in_page and result_item_seen:
        return True, "页面文本含关键词、结果项已出现"
    return False, "、".join(reasons) or "未看到搜索结果特征"


def _wait_for_search_result_confirmed(page, search_locator, keyword: str, before_url: str, cfg: RunConfig, log: Callable[[str], None], stop_flag: Optional[StopFlag] = None) -> None:
    """搜索闸门：不确认搜索完成，就禁止进入下滑/目标板块/白名单判断。"""
    timeout_s = max(8.0, min(float(cfg.result_wait_ms or 20000) / 1000.0, 30.0))
    end = time.time() + timeout_s
    last_reason = ""
    while time.time() < end:
        if stop_flag:
            stop_flag.checkpoint()
        try:
            ok, reason = _search_result_confirmed(page, search_locator, keyword, before_url, cfg)
            last_reason = reason
            if ok:
                log(f"搜索结果页已确认：{keyword}（{reason}）")
                return
        except Exception as e:
            last_reason = repr(e)
        if stop_flag:
            if not stop_flag.sleep(0.5):
                raise RuntimeError("用户已停止任务")
        else:
            page.wait_for_timeout(500)
    raise RuntimeError(f"搜索未确认完成，已阻止后续下滑/白名单判断：{keyword}；原因：{last_reason}")


def _force_submit_search(page, search_locator, cfg: RunConfig, keyword: str, log: Callable[[str], None]) -> None:
    """搜索动作必须明确发生：优先点击可见搜索按钮；没有按钮再回车/表单提交兜底。"""
    before_url = page.url
    custom_selector = (cfg.search_button_selector or "").strip()
    button, button_desc = _find_visible_search_button_handle(page, custom_selector)
    if button:
        try:
            button.click(timeout=min(cfg.page_timeout_ms, 8000))
            log(f"已点击搜索按钮提交关键词：{keyword}（{button_desc}）")
            page.wait_for_timeout(500)
            return
        except Exception as e:
            log(f"搜索按钮点击失败，改用回车/表单提交：{e}")
    try:
        search_locator.press("Enter")
        log(f"未找到可见搜索按钮，已按 Enter 提交关键词：{keyword}")
        page.wait_for_timeout(500)
    except Exception as e:
        log(f"输入框 Enter 提交失败，尝试页面键盘/表单提交：{e}")
        try:
            page.keyboard.press("Enter")
            log(f"已通过页面键盘 Enter 提交关键词：{keyword}")
            page.wait_for_timeout(500)
        except Exception as e2:
            log(f"页面键盘 Enter 也失败，尝试表单提交：{e2}")
            if _submit_search_form_fallback(page, search_locator):
                log(f"已通过表单提交关键词：{keyword}")
            else:
                raise RuntimeError("搜索提交失败：没有可见搜索按钮，Enter 和表单提交均未成功")
    try:
        # 如果 URL 没变，也允许后续通过页面内容判断，但日志里明确已经提交过搜索。
        if page.url == before_url:
            page.wait_for_timeout(800)
    except Exception:
        pass

def perform_search(page, cfg: RunConfig, keyword: str, log: Callable[[str], None], stop_flag: Optional[StopFlag] = None) -> None:
    """搜索动作加“闸门”。

    以前的版本只要执行了 click/Enter 就进入下滑流程，遇到搜索按钮没点中、输入框没写入、页面还停留在首页时，
    就会出现“还没输入关键词搜索就开始滚动”。现在必须依次确认：打开搜索页 → 写入关键词 → 提交搜索 → 结果页确认。
    任一环节失败都会抛错并跳过本轮，不允许后续目标板块/白名单判断。
    """
    page.set_default_timeout(cfg.page_timeout_ms)
    if stop_flag:
        stop_flag.checkpoint()
    log(f"打开搜索页：{cfg.start_url}")
    page.goto(cfg.start_url, wait_until="domcontentloaded", timeout=cfg.page_timeout_ms)
    apply_region_storage_to_page(page, cfg)
    before_url = page.url
    # 固定搜索环境下，先让首页 CK/本地存储完成一次更新，再输入关键词。
    if is_fixed_search_env(cfg):
        try:
            page.wait_for_timeout(800)
            save_search_environment_state(page.context, cfg)
        except Exception:
            pass
    if stop_flag:
        stop_flag.checkpoint()
    search = page.locator(cfg.search_input_selector).first
    search.wait_for(timeout=cfg.page_timeout_ms)
    # 不要先 click 搜索框。移动百度的 fake-placeholder 会拦截 click，导致关键词还没写入就失败。
    # 直接进入输入确认函数；该函数会先 fill/JS 写入，最后才用 force click 兜底。
    _ensure_search_input_keyword(page, search, keyword, log)
    if stop_flag:
        stop_flag.checkpoint()
    # 必须先完成搜索提交，再确认搜索结果页，最后才允许后续目标板块/白名单判断。
    if bool(getattr(cfg, "force_search_submit", True)):
        _force_submit_search(page, search, cfg, keyword, log)
    else:
        search.press("Enter")
        log(f"已按 Enter 搜索关键词：{keyword}")
    log(f"已提交搜索，正在确认结果页：{keyword}")
    if cfg.search_buffer_seconds > 0:
        log(f"搜索后缓冲等待 {cfg.search_buffer_seconds:g} 秒，等待页面数据加载")
        if stop_flag:
            if not stop_flag.sleep(cfg.search_buffer_seconds):
                raise RuntimeError("用户已停止任务")
        else:
            page.wait_for_timeout(int(cfg.search_buffer_seconds * 1000))
    if stop_flag:
        stop_flag.checkpoint()
    try:
        page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
    except Exception:
        pass
    # 第二层确认：结果页必须能被确认，否则不进入滚动。
    try:
        _wait_for_search_result_confirmed(page, search, keyword, before_url, cfg, log, stop_flag)
    except Exception as first_error:
        # 兜底重试一次：某些移动页点击按钮不会触发，需要再次聚焦输入框并按 Enter。
        log(f"首次搜索结果页确认失败，重试一次 Enter 提交：{first_error}")
        try:
            search = page.locator(cfg.search_input_selector).first
            search.click(timeout=3000)
            _ensure_search_input_keyword(page, search, keyword, log)
            search.press("Enter")
            if stop_flag:
                if not stop_flag.sleep(max(2.0, float(cfg.search_buffer_seconds or 0))):
                    raise RuntimeError("用户已停止任务")
            else:
                page.wait_for_timeout(int(max(2.0, float(cfg.search_buffer_seconds or 0)) * 1000))
            _wait_for_search_result_confirmed(page, search, keyword, before_url, cfg, log, stop_flag)
        except Exception as second_error:
            raise RuntimeError(f"搜索没有真正完成，禁止开始下滑。第一次：{first_error}；第二次：{second_error}") from second_error
    try:
        page.locator(cfg.result_item_selector).first.wait_for(timeout=cfg.result_wait_ms)
    except Exception as e:
        # 不要因为默认结果项选择器没命中就判定搜索失败。
        # 百度本地生活/服务列表页面经常使用非固定 class，后续会继续通过目标板块文字、商家结构和全文白名单扫描识别。
        log(f"默认结果项未命中，继续使用目标板块/全文增强识别：{e}")
    log(f"搜索流程确认完成，允许开始目标板块识别：{keyword}")



def _safe_slug(text: str, max_len: int = 60) -> str:
    n = normalize_cn(text or "") or "blank"
    return re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", n)[:max_len]




def clear_debug_artifacts_on_start(cfg: RunConfig, log: Callable[[str], None]) -> None:
    """每次开始运行时清空上一轮调试截图/HTML，避免 output/debug 无限堆积。"""
    if not getattr(cfg, "clear_debug_on_start", True):
        return
    base = Path(getattr(cfg, "debug_dir", "output/debug") or "output/debug")
    try:
        if base.exists():
            for child in base.iterdir():
                try:
                    if child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)
                    else:
                        child.unlink(missing_ok=True)
                except Exception:
                    pass
        base.mkdir(parents=True, exist_ok=True)
        log(f"已清空上次调试截图/HTML目录：{base}")
    except Exception as e:
        log(f"清空调试目录失败，已忽略：{e}")


def save_debug_artifacts(page, cfg: RunConfig, keyword: str, phase: str, diagnosis: str, log: Callable[[str], None]) -> None:
    if not cfg.debug_enabled:
        return
    try:
        base = Path(cfg.debug_dir or "output/debug")
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name = f"{stamp}_{_safe_slug(keyword)}_{_safe_slug(phase)}"
        meta_path = base / f"{name}_判断.txt"
        meta_path.write_text(f"关键词：{keyword}\n阶段：{phase}\nURL：{getattr(page, 'url', '')}\n判断：{diagnosis}\n", encoding="utf-8")
        if cfg.save_debug_html:
            try:
                (base / f"{name}.html").write_text(page.content(), encoding="utf-8")
            except Exception as e:
                log(f"调试 HTML 保存失败：{e}")
        if cfg.save_debug_screenshot:
            try:
                page.screenshot(path=str(base / f"{name}.png"), full_page=True, timeout=8000)
            except Exception as e:
                log(f"调试截图保存失败：{e}")
        log(f"调试结果已保存：{base / name}.*")
    except Exception as e:
        log(f"调试结果保存失败：{e}")


def analyze_page_state(page, cfg: RunConfig, target_terms: Optional[List[str]] = None, whitelist: Optional[List[Dict[str, str]]] = None) -> Dict[str, object]:
    """严格事实判断，不再把未命中目标板块解释成“可能验证/可能环境问题”。

    用户要求：搜不到目标板块就按“当前页面没有目标板块”处理。
    因此这里仍会收集页面观察证据（是否出现目标词、白名单词、商家结构词、验证词），
    但对外诊断只给确定结论，不再输出“可能/疑似/建议”等推测。
    """
    target_terms = target_terms or _terms_from_setting(cfg.target_section_text)
    whitelist_terms: List[str] = []
    if whitelist:
        for rule in whitelist:
            whitelist_terms.extend(split_terms(rule.get("title") or ""))
            whitelist_terms.extend(split_terms(rule.get("url") or ""))
    try:
        body_text = page.evaluate("() => (document.body && (document.body.innerText || document.body.textContent) || '').slice(0, 180000)") or ""
    except Exception:
        body_text = ""
    norm_body = normalize_cn(body_text)
    target_hits = [t for t in target_terms if normalize_cn(t) and normalize_cn(t) in norm_body]
    whitelist_hits = [t for t in whitelist_terms if normalize_cn(t) and normalize_cn(t) in norm_body]
    signal_words = ["商家", "店", "门店", "地址", "电话", "评分", "评价", "距离", "km", "公里", "米", "营业", "咨询", "路线", "导航", "附近", "服务", "全部"]
    signal_hits = [w for w in signal_words if normalize_cn(w) in norm_body]
    abnormal_words = ["安全验证", "验证码", "登录", "访问异常", "网络不给力", "请稍后", "百度安全验证"]
    abnormal_hits = [w for w in abnormal_words if normalize_cn(w) in norm_body]

    if target_hits:
        diagnosis = f"已发现目标板块文字：{','.join(target_hits[:5])}"
    else:
        configured = "|".join([t for t in target_terms if t]) or "未配置目标板块文字"
        diagnosis = f"当前搜索结果未展示目标板块（{configured}），按未找到处理，不扫描白名单。"

    return {
        "body_len": len(body_text),
        "target_hits": target_hits,
        "whitelist_hits": whitelist_hits,
        "signal_hits": signal_hits,
        "abnormal_hits": abnormal_hits,
        "diagnosis": diagnosis,
    }


def _find_business_card_section(page, cfg: RunConfig) -> Tuple[bool, str]:
    """增强识别：不依赖固定文字，通过商家卡片结构判断目标板块。"""
    if not cfg.enhanced_target_detection:
        return False, ""
    try:
        result = page.evaluate(
            """
            () => {
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width > 80 && r.height > 40;
              };
              const nodes = Array.from(document.querySelectorAll('section,article,div,li'));
              let best = null;
              let bestScore = 0;
              for (const el of nodes) {
                if (!visible(el)) continue;
                const txt = (el.innerText || el.textContent || '').trim();
                if (!txt || txt.length < 20 || txt.length > 2500) continue;
                let score = 0;
                const patterns = [
                  /商家|门店|店铺|附近|本地生活|精选服务/,
                  /电话|地址|路线|导航|咨询|预约/,
                  /评分|评价|口碑|人浏览|浏览/,
                  /\d+(\.\d+)?\s*(km|公里|米|m)\b/i,
                  /查看更多|查看全部|全部|更多|展开/
                ];
                for (const p of patterns) if (p.test(txt)) score += 1;
                const links = el.querySelectorAll('a,button,[role="button"],[onclick]').length;
                if (links >= 1) score += 1;
                if (links >= 3) score += 1;
                if (score > bestScore) { bestScore = score; best = el; }
              }
              if (best && bestScore >= 3) {
                try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
                return {score: bestScore, text: (best.innerText || best.textContent || '').trim().slice(0, 160)};
              }
              return null;
            }
            """
        )
        if result:
            return True, f"商家结构分 {result.get('score')}：{result.get('text') or ''}"
    except Exception:
        pass
    return False, ""

def _terms_from_setting(text: str) -> List[str]:
    return split_terms(text or "")


def _find_and_scroll_by_selector(page, selector: str, timeout_ms: int = 1000) -> bool:
    selector = (selector or "").strip()
    if not selector:
        return False
    try:
        loc = page.locator(selector).first
        if loc.count() <= 0:
            return False
        loc.scroll_into_view_if_needed(timeout=timeout_ms)
        try:
            return bool(loc.is_visible(timeout=timeout_ms))
        except Exception:
            return True
    except Exception:
        return False


def _find_and_scroll_by_text(page, terms: List[str]) -> Tuple[bool, str]:
    """在当前已加载 DOM 中找包含目标中文文本的可见元素，并滚动到它的位置。"""
    terms = [normalize_cn(t) for t in terms if normalize_cn(t)]
    if not terms:
        return False, ""
    try:
        result = page.evaluate(
            """
            (terms) => {
              const norm = (s) => (s || '').toString().normalize('NFKC')
                .toLowerCase()
                .replace(/[\s\u200b\u200c\u200d\ufeff,.;:!?，。！？、；：'"‘’“”（）【】《》<>\[\]{}\-—…·￥]/g, '');
              const nodes = Array.from(document.querySelectorAll('section, article, div, li, a, button, span'));
              let best = null;
              for (const el of nodes) {
                const rect = el.getBoundingClientRect();
                if (rect.width < 20 || rect.height < 10) continue;
                const txt = (el.innerText || el.textContent || '').trim();
                if (!txt || txt.length > 1200) continue;
                const n = norm(txt);
                for (const term of terms) {
                  if (n.includes(term)) {
                    best = {text: txt.slice(0, 120), term};
                    el.scrollIntoView({block: 'center', inline: 'nearest'});
                    return best;
                  }
                }
              }
              return null;
            }
            """,
            terms,
        )
        if result:
            return True, str(result.get("text") or result.get("term") or "")
    except Exception:
        pass
    return False, ""



def _find_baidu_local_life_module_handle(page, cfg: RunConfig):
    """v1.0：按前端配置的“目标来源标签规则/文字”定位目标模块。

    默认规则等价于：span.cosc-source-text.cos-line-clamp-1 文本为“百度本地生活”。
    后续如要点击别的模块，只需要在前端改 target_source_selector / target_source_text，
    不再写死百度本地生活。
    """
    text_terms = _terms_from_setting(getattr(cfg, "target_source_text", "") or cfg.target_section_text) or _terms_from_setting(cfg.target_section_text) or ["百度本地生活"]
    norm_terms = [normalize_cn(t) for t in text_terms if normalize_cn(t)] or [normalize_cn("百度本地生活")]
    source_selector = (getattr(cfg, "target_source_selector", "") or "span.cosc-source-text.cos-line-clamp-1, .cosc-source-text").strip()
    try:
        handle = page.evaluate_handle(
            """
            ({terms, sourceSelector}) => {
              const norm = (s) => String(s || '').normalize('NFKC').toLowerCase()
                .replace(/[\s\u3000\u200b\u200c\u200d\ufeff,.;:!?，。！？、；：'"‘’“”（）【】《》<>\[\]{}\-—…·￥]/g, '');
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 2 && r.height >= 2 && r.bottom >= -300 && r.top <= window.innerHeight + 2600;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) || '').replace(/\u00a0/g, ' ').trim();
              const termHit = (txt) => {
                const n = norm(txt);
                return terms.some(t => t && n.includes(t));
              };
              let candidates = [];
              try {
                if (sourceSelector) candidates = Array.from(document.querySelectorAll(sourceSelector));
              } catch(e) { candidates = []; }
              if (!candidates.length) candidates = Array.from(document.querySelectorAll('span.cosc-source-text.cos-line-clamp-1, .cosc-source-text, span, div'));
              candidates = candidates.filter(el => visible(el) && textOf(el) && termHit(textOf(el)));
              // 优先 class/source 标签，其次短标题文字。
              candidates.sort((a,b) => {
                const ca = String(a.className || ''), cb = String(b.className || '');
                const sa = (a.matches && a.matches('span.cosc-source-text.cos-line-clamp-1')) || ca.includes('cosc-source-text') ? 0 : 1;
                const sb = (b.matches && b.matches('span.cosc-source-text.cos-line-clamp-1')) || cb.includes('cosc-source-text') ? 0 : 1;
                if (sa !== sb) return sa - sb;
                return textOf(a).length - textOf(b).length;
              });
              if (!candidates.length) return null;
              const source = candidates[0];
              let module = source;
              let best = source;
              let bestScore = -9999;
              for (let i = 0; module && i < 12; i++, module = module.parentElement) {
                if (!visible(module)) continue;
                const txt = textOf(module);
                const r = module.getBoundingClientRect();
                let score = 0;
                if (txt.includes('查看更多') || txt.includes('更多') || txt.includes('查看全部')) score += 80;
                if (/评分|评价|营业中|询价|咨询|商家|本地商家|平台精选|精选商家/.test(txt)) score += 45;
                if (r.width >= 250 && r.height >= 100) score += 30;
                if (r.height > 1200 || txt.length > 4500) score -= 90;
                if (score > bestScore) { bestScore = score; best = module; }
                if (score >= 90 && r.height <= 1100) break;
              }
              try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
              return {el: best, sourceText: textOf(source).slice(0,80), moduleText: textOf(best).slice(0,300)};
            }
            """,
            {"terms": norm_terms, "sourceSelector": source_selector},
        )
        if not handle:
            return None, "", ""
        props = handle.get_properties()
        el_prop = props.get('el')
        if not el_prop:
            return None, "", ""
        el = el_prop.as_element()
        source_text = ""
        module_text = ""
        try:
            source_text = str(props.get('sourceText').json_value() or '') if props.get('sourceText') else ''
            module_text = str(props.get('moduleText').json_value() or '') if props.get('moduleText') else ''
        except Exception:
            pass
        return el, source_text, module_text
    except Exception:
        return None, "", ""

def _click_more_in_baidu_local_life_module(page, cfg: RunConfig):
    """v1.0：只在已定位到的百度本地生活模块内点击“查看更多/更多/查看全部”。"""
    module, source_text, module_text = _find_baidu_local_life_module_handle(page, cfg)
    if not module:
        return None, False, ""
    text_setting = (cfg.enter_target_page_text or "查看更多|更多商家|查看全部|全部商家|更多|展开").strip()
    terms = [normalize_cn(t) for t in _terms_from_setting(text_setting) if normalize_cn(t)] or [normalize_cn("查看更多")]
    try:
        btn = module.evaluate_handle(
            """
            (root, terms) => {
              const norm = (s) => String(s || '').normalize('NFKC').toLowerCase()
                .replace(/[\s\u3000\u200b\u200c\u200d\ufeff,.;:!?，。！？、；：'"‘’“”（）【】《》<>\[\]{}\-—…·￥]/g, '');
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 4 && r.height >= 4;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) + ' ' +
                ((el && el.getAttribute && el.getAttribute('aria-label')) || '') + ' ' +
                ((el && el.getAttribute && el.getAttribute('title')) || '')).replace(/\u00a0/g, ' ').trim();
              const clickable = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
                  if (!visible(cur)) continue;
                  const tag = String(cur.tagName || '').toLowerCase();
                  const role = String(cur.getAttribute('role') || '').toLowerCase();
                  if (tag === 'a' || tag === 'button' || role === 'button' || role === 'link' || cur.onclick || cur.getAttribute('data-url') || cur.getAttribute('href')) return cur;
                }
                return visible(el) ? el : null;
              };
              let best = null, bestScore = -9999, bestText = '';
              const nodes = Array.from(root.querySelectorAll('a,button,[role="button"],[role="link"],[onclick],span,div'));
              for (const el of nodes) {
                if (!visible(el)) continue;
                const raw = textOf(el);
                if (!raw || raw.length > 200) continue;
                const n = norm(raw);
                if (!terms.some(t => t && n.includes(t))) continue;
                const clickEl = clickable(el);
                if (!clickEl) continue;
                const r = clickEl.getBoundingClientRect();
                let score = 0;
                const tag = String(clickEl.tagName || '').toLowerCase();
                if (tag === 'a') score += 80;
                if (tag === 'button') score += 70;
                if (clickEl.onclick) score += 40;
                if (r.top >= -100 && r.top <= window.innerHeight + 500) score += 50;
                if (/查看|更多|全部|商家|展开/.test(raw)) score += 40;
                score -= Math.max(0, raw.length - 40);
                if (score > bestScore) { bestScore = score; best = clickEl; bestText = raw.slice(0,80); }
              }
              if (best) { try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {} return {el: best, text: bestText}; }
              return null;
            }
            """,
            terms,
        )
        props = btn.get_properties() if btn else {}
        el_prop = props.get('el') if props else None
        if not el_prop:
            return None, False, ""
        btn_el = el_prop.as_element()
        btn_text = ""
        try:
            btn_text = str(props.get('text').json_value() or '') if props.get('text') else ''
        except Exception:
            pass
        target_page, opened_popup = _click_element_handle(page, btn_el, cfg)
        return target_page, opened_popup, btn_text or source_text or text_setting
    except Exception:
        return None, False, ""


def scroll_to_target_section(page, cfg: RunConfig, log: Callable[[str], None], stop_flag: Optional[StopFlag] = None, keyword: str = "", whitelist: Optional[List[Dict[str, str]]] = None) -> bool:
    """搜索后下滑到指定板块。支持 CSS 选择器或中文文本关键词。

    关键修复：如果用户明确填写了“目标板块文字/选择器”并勾选严格目标板块优先，
    那么只有 selector 或目标文字命中才算真正找到目标板块。
    “增强识别商家结构”和“页面出现白名单词”只能用于调试诊断，不能作为目标板块命中，
    避免没找到“百度本地生活”时就提前扫描/点击白名单。
    """
    has_selector_target = bool((cfg.target_section_selector or "").strip())
    has_text_target = bool((cfg.target_section_text or "").strip())
    has_target = has_selector_target or has_text_target
    if not has_target:
        return True

    strict_explicit_target = bool(getattr(cfg, "target_first_strict", True)) and has_target
    terms = _terms_from_setting(cfg.target_section_text)
    rounds = max(int(cfg.target_scroll_rounds or 1), 1)
    source_label_required = any("百度本地生活" in t for t in terms) or any("本地生活" == t for t in terms)
    # v1.0：优先按百度来源标签定位目标模块，避免把普通文本/商家结构误判为目标板块。
    module, source_text, module_text = _find_baidu_local_life_module_handle(page, cfg)
    if module:
        log(f"已通过来源标签定位目标模块：{source_text or (terms[0] if terms else '百度本地生活')}")
        if stop_flag:
            if not stop_flag.sleep(cfg.scroll_wait_ms / 1000):
                return False
        else:
            page.wait_for_timeout(cfg.scroll_wait_ms)
        return True
    for idx in range(rounds):
        if stop_flag:
            try:
                stop_flag.checkpoint()
            except RuntimeError:
                return False
        if _find_and_scroll_by_selector(page, cfg.target_section_selector):
            log(f"已下滑到目标板块 selector：{cfg.target_section_selector}")
            if stop_flag:
                if not stop_flag.sleep(cfg.scroll_wait_ms / 1000):
                    return False
            else:
                page.wait_for_timeout(cfg.scroll_wait_ms)
            return True
        # 如果目标是“百度本地生活”，必须命中 cosc-source-text 来源标签，不能只靠普通正文文本。
        if not source_label_required:
            ok, text = _find_and_scroll_by_text(page, terms)
            if ok:
                log(f"已下滑到目标板块文本：{text[:80]}")
                if stop_flag:
                    if not stop_flag.sleep(cfg.scroll_wait_ms / 1000):
                        return False
                else:
                    page.wait_for_timeout(cfg.scroll_wait_ms)
                return True
        # 目标板块阶段只按用户配置的目标文字/选择器判断。
        # 商家结构增强识别只允许在进入目标模块后用于白名单扫描，避免搜索结果页刚加载完就误判目标模块。
        page.mouse.wheel(0, int(cfg.target_scroll_pixels or cfg.scan_scroll_pixels or 850))
        if stop_flag:
            if not stop_flag.sleep(cfg.scroll_wait_ms / 1000):
                return False
        else:
            page.wait_for_timeout(cfg.scroll_wait_ms)
        module, source_text, module_text = _find_baidu_local_life_module_handle(page, cfg)
        if module:
            log(f"已通过来源标签定位目标模块：{source_text or (terms[0] if terms else '百度本地生活')}")
            return True
        log(f"正在下滑寻找目标板块：第 {idx + 1}/{rounds} 轮")
    state = analyze_page_state(page, cfg, terms, whitelist)
    diagnosis = str(state.get("diagnosis") or "未找到目标板块/指定位置")
    log(f"未找到目标板块：{diagnosis}")
    save_debug_artifacts(page, cfg, keyword or "unknown", "target_not_found", diagnosis, log)
    return False


def _click_element_handle(page, element, cfg: RunConfig):
    """点击 ElementHandle，并兼容新窗口/当前页跳转。"""
    try:
        element.evaluate("""
            el => {
              try {
                const a = el.closest && el.closest('a');
                if (a) a.removeAttribute('target');
                if (el.tagName === 'A') el.removeAttribute('target');
              } catch(e) {}
            }
        """)
    except Exception:
        pass

    try:
        element.scroll_into_view_if_needed(timeout=2500)
    except Exception:
        pass

    try:
        with page.expect_popup(timeout=1200) as popup_info:
            element.click(timeout=cfg.click_open_timeout_ms)
        popup_page = popup_info.value
        popup_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
        return popup_page, True
    except PlaywrightTimeoutError:
        try:
            page.wait_for_load_state("domcontentloaded", timeout=cfg.click_open_timeout_ms)
        except Exception:
            pass
        return page, False
    except Exception:
        # 有些移动端卡片是非标准可点击元素，Playwright 认为不可见/被遮挡时，用 JS click 兜底。
        before_url = page.url
        try:
            element.evaluate("el => { try { el.click(); } catch(e) {} }")
            page.wait_for_timeout(800)
            if page.url != before_url:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass
                return page, False
        except Exception:
            pass
        raise


def _find_clickable_by_text_handle(page, text_setting: str):
    """根据中文入口文字查找真正可点击元素，优先当前视口附近的 a/button/role/link。"""
    terms = [normalize_cn(t) for t in _terms_from_setting(text_setting) if normalize_cn(t)]
    if not terms:
        return None, ""
    try:
        handle = page.evaluate_handle(
            """
            (terms) => {
              const norm = (s) => (s || '').toString().normalize('NFKC')
                .toLowerCase()
                .replace(/[\s\u200b\u200c\u200d\ufeff,.;:!?，。！？、；：'"‘’“”（）【】《》<>\[\]{}\-—…·￥]/g, '');
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 8 && r.height >= 8;
              };
              const clickableAncestor = (el) => {
                let cur = el;
                for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
                  if (!visible(cur)) continue;
                  const tag = (cur.tagName || '').toLowerCase();
                  const role = (cur.getAttribute('role') || '').toLowerCase();
                  if (tag === 'a' || tag === 'button' || role === 'button' || role === 'link' || cur.onclick || cur.getAttribute('data-url') || cur.getAttribute('href')) return cur;
                }
                return visible(el) ? el : null;
              };
              const nodes = Array.from(document.querySelectorAll('a,button,[role="button"],[role="link"],[onclick],span,div,li,section'));
              let best = null;
              let bestScore = -1;
              let bestTerm = '';
              for (const el of nodes) {
                if (!visible(el)) continue;
                const raw = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
                if (!raw || raw.length > 800) continue;
                const n = norm(raw);
                for (const term of terms) {
                  if (!n.includes(term)) continue;
                  const clickEl = clickableAncestor(el);
                  if (!clickEl) continue;
                  const r = clickEl.getBoundingClientRect();
                  const tag = (clickEl.tagName || '').toLowerCase();
                  const role = (clickEl.getAttribute('role') || '').toLowerCase();
                  let score = 0;
                  if (tag === 'a') score += 100;
                  if (tag === 'button') score += 90;
                  if (role === 'button' || role === 'link') score += 70;
                  if (clickEl.onclick) score += 40;
                  if (raw.length <= 80) score += 40;
                  if (r.top >= -80 && r.top <= window.innerHeight + 500) score += 70;
                  if (/查看|更多|全部|商家|展开/.test(raw)) score += 30;
                  score -= Math.max(0, raw.length - 120) * 0.02;
                  if (score > bestScore) {
                    bestScore = score;
                    best = clickEl;
                    bestTerm = term;
                  }
                }
              }
              if (best) {
                try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
                return {el: best, term: bestTerm};
              }
              return null;
            }
            """,
            terms,
        )
        if not handle:
            return None, ""
        obj = handle.get_properties()
        el_handle = obj.get("el")
        term_handle = obj.get("term")
        if not el_handle:
            return None, ""
        element = el_handle.as_element()
        clicked_term = ""
        try:
            clicked_term = str(term_handle.json_value() or "") if term_handle else ""
        except Exception:
            clicked_term = ""
        # 还原成用户配置中的原词，日志更容易看。
        for t in _terms_from_setting(text_setting):
            if normalize_cn(t) == clicked_term:
                clicked_term = t
                break
        return element, clicked_term
    except Exception:
        return None, ""


def _click_by_text(page, text_setting: str, cfg: RunConfig):
    element, clicked_text = _find_clickable_by_text_handle(page, text_setting)
    if not element:
        return None, False, ""
    target_page, opened_popup = _click_element_handle(page, element, cfg)
    return target_page, opened_popup, clicked_text


def enter_target_page_if_configured(page, cfg: RunConfig, log: Callable[[str], None]):
    """进入指定页面，比如目标板块里的“查看更多/更多商家”。未配置时返回当前页。"""
    selector = (cfg.enter_target_page_selector or "").strip()
    text = (cfg.enter_target_page_text or "").strip()
    if not selector and not text:
        return page, False

    # v1.0：如果页面里存在 <span class="cosc-source-text cos-line-clamp-1">百度本地生活</span>，
    # 只在该模块容器内找“查看更多”，避免点到其他板块入口。
    if text:
        target_page, opened_popup, clicked_text = _click_more_in_baidu_local_life_module(page, cfg)
        if target_page:
            log(f"已通过百度来源标签模块入口进入指定页面：{clicked_text or text} / {target_page.url}")
            return target_page, opened_popup

    if selector:
        try:
            loc = page.locator(selector).first
            loc.scroll_into_view_if_needed(timeout=3000)
            try:
                loc.evaluate("el => { try { if (el.tagName === 'A') el.removeAttribute('target'); const a = el.closest && el.closest('a'); if (a) a.removeAttribute('target'); } catch(e) {} }")
            except Exception:
                pass
            try:
                with page.expect_popup(timeout=1200) as popup_info:
                    loc.click(timeout=cfg.click_open_timeout_ms)
                target_page = popup_info.value
                target_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                log(f"已通过选择器进入指定页面：{target_page.url}")
                return target_page, True
            except PlaywrightTimeoutError:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=cfg.click_open_timeout_ms)
                except Exception:
                    pass
                log(f"已通过选择器进入指定页面：{page.url}")
                return page, False
        except Exception as e:
            log(f"指定页面入口选择器点击失败：{e}")

    if text:
        target_page, opened_popup, clicked_text = _click_by_text(page, text, cfg)
        if target_page:
            log(f"已通过文本入口进入指定页面：{clicked_text or text} / {target_page.url}")
            return target_page, opened_popup
        log("未找到指定页面入口文本；将停留在当前页继续扫描白名单结果")

    return page, False


def prepare_target_page_after_search(page, cfg: RunConfig, keyword: str, log: Callable[[str], None], stop_flag: Optional[StopFlag] = None, whitelist: Optional[List[Dict[str, str]]] = None):
    """搜索后：必须先下滑到目标板块 -> 点击入口进入指定页面 -> 再扫描白名单。

    规则：配置了目标板块时，白名单判断只能发生在目标板块/目标页之后。
    这样避免普通搜索结果页或异常页里偶然出现白名单词时被提前扫描。
    """
    has_target_cfg = bool((cfg.target_section_selector or "").strip() or (cfg.target_section_text or "").strip())
    found = scroll_to_target_section(page, cfg, log, stop_flag, keyword, whitelist)
    if has_target_cfg and not found and bool(getattr(cfg, "target_first_strict", True)):
        msg = "未找到目标板块，严格目标板块优先：不扫描白名单，已跳过该轮搜索"
        log(msg)
        raise RuntimeError(msg)
    if not found and cfg.target_required:
        raise RuntimeError("未找到目标板块，已按配置跳过该关键词")

    has_enter_cfg = bool((cfg.enter_target_page_selector or "").strip() or (cfg.enter_target_page_text or "").strip())
    before_enter_url = page.url
    active_page, opened_popup = enter_target_page_if_configured(page, cfg, log)
    rewrite_baidu_site_city_url(active_page, cfg, log, keyword)
    apply_region_storage_to_page(active_page, cfg)
    # 如果用户配置了“进入页面按钮文字/规则”，严格模式下必须真正点击进入/跳转后才扫描白名单。
    # 没找到“查看更多/查看更多”时，不允许留在普通搜索结果页直接扫白名单。
    if has_enter_cfg and bool(getattr(cfg, "target_first_strict", True)):
        try:
            after_enter_url = active_page.url
        except Exception:
            after_enter_url = before_enter_url
        entered = bool(opened_popup or (active_page is not page) or (after_enter_url and after_enter_url != before_enter_url))
        if not entered:
            msg = "已找到目标板块，但未成功点击进入指定页面入口；严格模式下不扫描白名单，已跳过该轮搜索"
            log(msg)
            raise RuntimeError(msg)
    enter_wait = float(getattr(cfg, "target_page_wait_seconds", 0) or 0)
    if enter_wait > 0:
        log(f"进入目标模块后等待 {enter_wait:g} 秒，等待列表/统计数据加载")
        if stop_flag is not None:
            stop_flag.sleep(enter_wait)
        else:
            try:
                active_page.wait_for_timeout(int(enter_wait * 1000))
            except Exception:
                pass

    wait_selector = (cfg.target_page_wait_selector or cfg.result_item_selector or "").strip()
    if wait_selector:
        try:
            active_page.locator(wait_selector).first.wait_for(timeout=cfg.result_wait_ms)
        except Exception:
            # 目标页结构未必立刻出现结果项，继续让后续扫描给出更明确日志。
            pass
    return active_page, opened_popup

def _find_clickable_in_item_handle(page, item: Locator, cfg: RunConfig):
    """在一个结果项内找到真正可见、可点击的元素，避免点到隐藏 a 标签。"""
    try:
        if cfg.result_link_selector.strip():
            candidates = item.locator(cfg.result_link_selector)
            total = min(candidates.count(), 8)
            for idx in range(total):
                cand = candidates.nth(idx)
                try:
                    if cand.is_visible(timeout=500):
                        return cand.element_handle(timeout=800)
                except Exception:
                    continue
        handle = item.evaluate_handle(
            """
            (root) => {
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 8 && r.height >= 8;
              };
              const scoreOf = (el) => {
                if (!visible(el)) return -9999;
                const r = el.getBoundingClientRect();
                const tag = (el.tagName || '').toLowerCase();
                const role = (el.getAttribute('role') || '').toLowerCase();
                const txt = ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
                let score = 0;
                if (tag === 'a') score += 120;
                if (tag === 'button') score += 100;
                if (role === 'button' || role === 'link') score += 70;
                if (el.onclick || el.getAttribute('data-url')) score += 50;
                if (r.top >= -100 && r.top <= window.innerHeight + 700) score += 30;
                if (txt && txt.length <= 300) score += 20;
                score += Math.min(r.width * r.height / 10000, 20);
                return score;
              };
              const direct = [];
              if (root.matches && root.matches('a,button,[role="button"],[role="link"],[onclick]')) direct.push(root);
              direct.push(...Array.from(root.querySelectorAll('a,button,[role="button"],[role="link"],[onclick],[data-url]')));
              let best = null;
              let bestScore = -9999;
              for (const el of direct) {
                const sc = scoreOf(el);
                if (sc > bestScore) { best = el; bestScore = sc; }
              }
              if (best) {
                try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
                return best;
              }
              // 有些移动端整张卡片可点，内部无 a 标签，兜底点击卡片自身或最近可点父级。
              let cur = root;
              for (let i = 0; cur && i < 5; i++, cur = cur.parentElement) {
                if (scoreOf(cur) > -9999) {
                  try { cur.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
                  return cur;
                }
              }
              return null;
            }
            """
        )
        return handle.as_element() if handle else None
    except Exception:
        return None


def collect_item_info(page, item: Locator, cfg: RunConfig) -> Dict[str, str]:
    # 标题优先，其次读整个结果项文本，确保中文内容能被纳入匹配。
    title = ""
    if cfg.result_title_selector.strip():
        title = safe_inner_text(item.locator(cfg.result_title_selector).first)
    full_text = safe_inner_text(item)
    if not title:
        title = full_text

    href = ""
    aria = ""
    link_title = ""
    try:
        handle = _find_clickable_in_item_handle(page, item, cfg)
        if handle:
            href = (handle.get_attribute("href") or "").strip()
            aria = (handle.get_attribute("aria-label") or "").strip()
            link_title = (handle.get_attribute("title") or "").strip()
            if not href:
                # 如果可点元素不是 a，则尝试找最近 a 或 data-url。
                href = handle.evaluate("el => { const a = el.closest && el.closest('a'); return (el.getAttribute('data-url') || el.getAttribute('href') || (a && a.getAttribute('href')) || ''); }") or ""
    except Exception:
        pass

    abs_url = urljoin(page.url, href) if href else ""
    candidate_text = "\n".join([title, full_text, aria, link_title, abs_url])
    return {"title": title, "text": candidate_text, "url": abs_url}


def _find_precise_clickable_in_item_handle(page, item: Locator, cfg: RunConfig, matched_term: str = "", blacklist_terms: Optional[List[str]] = None):
    """在已命中白名单的卡片内精准选择可点击元素。

    优先点击包含白名单词的店名/链接，避免点到同卡片里的“咨询/电话/路线/询价”等按钮，
    并且卡片或点击元素包含黑名单词时直接返回 None。
    """
    term = (matched_term or "").strip()
    blacklist_terms = blacklist_terms or []
    try:
        handle = item.evaluate_handle(
            """
            (root, args) => {
              const [rawTerm, rawBlacklist] = args || [];
              const norm = (s) => String(s || '').normalize('NFKC').toLowerCase()
                .replace(/[\s\u3000\-—_.,，。:：;；|/\\()（）\[\]【】<>《》·'"“”‘’]+/g, '');
              const term = norm(rawTerm);
              const blacklist = (rawBlacklist || []).map(norm).filter(Boolean);
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 10 && r.height >= 10;
              };
              const textOf = (el) => ((el.innerText || el.textContent || '') + ' ' + (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')).trim();
              const hasBlack = (el) => {
                const t = norm(textOf(el));
                return blacklist.some(b => b && t.includes(b));
              };
              if (hasBlack(root)) return null;
              const badExact = ['询价','咨询','电话','路线','导航','客服','联系','复制','立即咨询','在线咨询','拨打电话','查看路线'];
              const isBadCta = (el) => {
                const t = norm(textOf(el));
                return badExact.some(x => t === norm(x) || t === norm(x + '按钮'));
              };
              const candidates = [];
              for (const el of Array.from(root.querySelectorAll('a,[role="link"],[onclick],[data-url],button,[role="button"]'))) {
                if (!visible(el) || hasBlack(el)) continue;
                const tag = String(el.tagName || '').toLowerCase();
                const role = String(el.getAttribute('role') || '').toLowerCase();
                const txt = norm(textOf(el));
                let score = 0;
                if (txt.includes(term) && term) score += 450;
                if (tag === 'a' || role === 'link' || el.getAttribute('href') || el.getAttribute('data-url')) score += 180;
                if (tag === 'button' || role === 'button') score -= 90;
                if (isBadCta(el) && !(term && txt.includes(term))) score -= 350;
                const r = el.getBoundingClientRect();
                score += Math.min((r.width * r.height) / 25000, 30);
                candidates.push([el, score]);
              }
              // 如果没有子链接，但根卡片自身可点且包含白名单词，才点卡片。
              const rootText = norm(textOf(root));
              if ((root.onclick || root.getAttribute('data-url') || root.getAttribute('href')) && (!term || rootText.includes(term)) && !hasBlack(root)) {
                candidates.push([root, 120]);
              }
              candidates.sort((a,b) => b[1] - a[1]);
              const best = candidates.length ? candidates[0][0] : null;
              if (best) { try { best.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {} }
              return best;
            }
            """,
            [term, blacklist_terms],
        )
        el = handle.as_element() if handle else None
        if el:
            return el
    except Exception:
        pass
    return _find_clickable_in_item_handle(page, item, cfg)


def click_link_in_item(page, item: Locator, cfg: RunConfig, matched_term: str = "", blacklist_terms: Optional[List[str]] = None):
    element = _find_precise_clickable_in_item_handle(page, item, cfg, matched_term, blacklist_terms)
    if not element:
        raise RuntimeError("未在该结果项中找到精准可点击元素，或该项命中黑名单")
    return _click_element_handle(page, element, cfg)


def back_to_results(active_page, main_page, opened_popup: bool, search_result_url: str, cfg: RunConfig) -> None:
    """点击详情/咨询页停留后，必须快速回到目标列表继续执行。

    旧版在返回后会等待 result_item_selector 最长 result_wait_ms，
    如果百度服务列表页面处于骨架屏/异步加载慢，就会看起来“卡住”。
    新版不再长时间等待统计页/骨架屏：优先直接回到已保存的目标列表 URL，
    最多短等几秒，失败也返回给上层继续下一轮扫描/结束当前关键词。
    """
    quick_timeout = max(3000, min(int(getattr(cfg, "click_open_timeout_ms", 8000) or 8000), 8000))

    # 新窗口/弹窗打开的详情页：关闭详情页，回到主列表页。
    if opened_popup or active_page is not main_page:
        try:
            active_page.close()
        except Exception:
            pass
        try:
            main_page.bring_to_front()
        except Exception:
            pass
        page = main_page
    else:
        page = active_page

    # 比 go_back 更稳定：直接回到点击前保存的目标列表 URL。
    # 这样即使详情页历史栈异常、骨架屏不结束，也不会卡住。
    try:
        current_url = page.url
    except Exception:
        current_url = ""

    try:
        if search_result_url and current_url != search_result_url:
            page.goto(search_result_url, wait_until="domcontentloaded", timeout=quick_timeout)
        else:
            # 当前已在列表页，轻量等待即可，不长时间阻塞。
            page.wait_for_timeout(500)
    except Exception:
        try:
            page.goto(search_result_url, wait_until="commit", timeout=quick_timeout)
        except Exception:
            pass

    # 短等结果项，不再用 50 秒长等。找不到也不抛异常，交给上层继续判断。
    try:
        page.locator(cfg.result_item_selector).first.wait_for(timeout=quick_timeout)
    except Exception:
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass



def _page_body_text(page, timeout: int = 2500) -> str:
    try:
        return page.locator("body").inner_text(timeout=timeout)
    except Exception:
        try:
            return page.evaluate("() => document.body ? (document.body.innerText || document.body.textContent || '') : ''") or ""
        except Exception:
            return ""


def _find_clickable_by_whitelist_term_handles(page, term: str, blacklist_terms: Optional[List[str]] = None, limit: int = 8):
    """兜底：在整页 DOM 里按白名单词找可点击店铺卡片。返回多个候选，避免漏点同模块内剩余白名单。"""
    term = (term or "").strip()
    if not term:
        return []
    try:
        handle = page.evaluate_handle(
            """
            ([rawTerm, rawBlacklist, rawLimit]) => {
              const norm = (s) => String(s || '')
                .normalize('NFKC')
                .toLowerCase()
                .replace(/[\s\u3000\-—_.,，。:：;；|/\\()（）\[\]【】<>《》·'"“”‘’]+/g, '');
              const term = norm(rawTerm);
              const blacklist = (rawBlacklist || []).map(norm).filter(Boolean);
              const limit = Math.max(1, Number(rawLimit || 8));
              if (!term) return [];
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 12 && r.height >= 12 && r.bottom >= -300 && r.top <= window.innerHeight + 1800;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) + ' ' +
                ((el && el.getAttribute && el.getAttribute('aria-label')) || '') + ' ' +
                ((el && el.getAttribute && el.getAttribute('title')) || '') + ' ' +
                ((el && el.getAttribute && el.getAttribute('alt')) || '')).trim();
              const hasBlacklist = (el) => {
                const n = norm(textOf(el));
                return blacklist.some(b => b && n.includes(b));
              };
              const isBadCta = (el) => {
                const t = norm(textOf(el));
                const bad = ['询价','咨询','电话','路线','复制','导航','客服','联系','立即咨询','在线咨询','拨打电话','查看路线'];
                return bad.some(x => t === norm(x) || t === norm(x + '按钮'));
              };
              const cardLike = (el) => {
                if (!el || !visible(el)) return false;
                const cls = String(el.className || '').toLowerCase();
                const role = String(el.getAttribute('role') || '').toLowerCase();
                const tag = String(el.tagName || '').toLowerCase();
                const t = textOf(el);
                if (tag === 'li' || tag === 'article' || tag === 'section') return true;
                if (role === 'listitem') return true;
                if (/card|item|poi|shop|store|list|result|waimai|merchant|business/.test(cls)) return true;
                const n = norm(t);
                let feature = 0;
                for (const k of ['评分','人感兴趣','营业中','服务','地址','电话','距离','km','咨询','产品','保障','推荐','门店','商家']) {
                  if (n.includes(norm(k))) feature++;
                }
                return feature >= 2 && t.length >= 8 && t.length <= 1500;
              };
              const nearestCard = (el) => {
                let card = el;
                for (let i = 0; card && i < 10; i++, card = card.parentElement) {
                  if (cardLike(card)) return card;
                }
                return el;
              };
              const pickClickable = (card, source) => {
                const candidates = [];
                const push = (el, base) => { if (el && visible(el) && !hasBlacklist(el) && !hasBlacklist(card)) candidates.push([el, base]); };
                let cur = source;
                for (let i = 0; cur && i < 6; i++, cur = cur.parentElement) {
                  const tag = String(cur.tagName || '').toLowerCase();
                  const role = String(cur.getAttribute('role') || '').toLowerCase();
                  if (tag === 'a' || role === 'link' || cur.getAttribute('href') || cur.getAttribute('data-url')) push(cur, 320 - i * 20);
                  else if ((role === 'button' || cur.onclick) && norm(textOf(cur)).includes(term)) push(cur, 230 - i * 20);
                  if (cur === card) break;
                }
                for (const el of Array.from(card.querySelectorAll('a,[role="link"],[onclick],[data-url],button,[role="button"]'))) {
                  const nt = norm(textOf(el));
                  let base = 80;
                  const tag = String(el.tagName || '').toLowerCase();
                  const role = String(el.getAttribute('role') || '').toLowerCase();
                  if (nt.includes(term)) base += 260;
                  if (tag === 'a' || role === 'link' || el.getAttribute('href') || el.getAttribute('data-url')) base += 120;
                  if (tag === 'button' || role === 'button') base -= 80;
                  if (isBadCta(el) && !nt.includes(term)) base -= 260;
                  push(el, base);
                }
                // 只有卡片本身可点击或文本明确包含店名时才把卡片作为兜底，避免误点相邻区域。
                if ((card.onclick || card.getAttribute('data-url') || card.getAttribute('href')) && norm(textOf(card)).includes(term)) push(card, 150);
                let best = null, bestScore = -9999;
                for (const [el, base] of candidates) {
                  const r = el.getBoundingClientRect();
                  const nt = norm(textOf(el));
                  let score = base + Math.min((r.width * r.height) / 25000, 20);
                  if (nt.includes(term)) score += 150;
                  if (r.top >= -100 && r.top <= window.innerHeight + 500) score += 20;
                  if (score > bestScore) { best = el; bestScore = score; }
                }
                return best;
              };

              const seenCards = new Set();
              const results = [];
              const all = Array.from(document.querySelectorAll('body *'));
              for (const el of all) {
                if (results.length >= limit) break;
                if (!visible(el)) continue;
                const txt = textOf(el);
                if (!txt || !norm(txt).includes(term)) continue;
                const card = nearestCard(el);
                if (!card || hasBlacklist(card)) continue;
                const cardText = norm(textOf(card));
                if (!cardText.includes(term)) continue;
                const key = cardText.slice(0, 180);
                if (seenCards.has(key)) continue;
                const clickEl = pickClickable(card, el);
                if (!clickEl) continue;
                seenCards.add(key);
                try { clickEl.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
                results.push(clickEl);
              }
              return results;
            }
            """,
            [term, blacklist_terms or [], int(limit or 8)],
        )
        out = []
        if not handle:
            return out
        props = handle.get_properties()
        for _, h in props.items():
            try:
                el = h.as_element()
                if el:
                    out.append(el)
            except Exception:
                pass
        return out
    except Exception:
        return []


def _find_clickable_by_whitelist_term_handle(page, term: str):
    handles = _find_clickable_by_whitelist_term_handles(page, term, [], 1)
    return handles[0] if handles else None

def _collect_element_card_info(page, element, term: str = "") -> Dict[str, str]:
    """读取商家卡片信息，优先提取店名行，避免把整页/分类栏当成店铺。"""
    try:
        data = element.evaluate(
            """
            (el) => {
              const visible = (x) => {
                if (!x) return false;
                const st = window.getComputedStyle(x);
                const r = x.getBoundingClientRect();
                return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0' && r.width >= 8 && r.height >= 8;
              };
              const textOf = (x) => ((x && (x.innerText || x.textContent || '')) + ' ' +
                ((x && x.getAttribute && x.getAttribute('aria-label')) || '') + ' ' +
                ((x && x.getAttribute && x.getAttribute('title')) || '') + ' ' +
                Array.from((x && x.querySelectorAll && x.querySelectorAll('img[alt]')) || []).map(i => i.getAttribute('alt') || '').join(' ')
              ).replace(/\u00a0/g, ' ').trim();
              const cardLike = (x) => {
                if (!x || !visible(x)) return false;
                const cls = String(x.className || '').toLowerCase();
                const tag = String(x.tagName || '').toLowerCase();
                const role = String(x.getAttribute('role') || '').toLowerCase();
                const txt = textOf(x);
                const r = x.getBoundingClientRect();
                if (r.width < 120 || r.height < 45 || r.height > 520) return false;
                if (tag === 'li' || tag === 'article' || tag === 'section' || role === 'listitem') return true;
                if (/card|item|poi|shop|store|list|result|merchant|business|service/.test(cls)) return true;
                let feature = 0;
                for (const k of ['评分','评价','人感兴趣','营业中','休息中','服务','地址','电话','距离','咨询','询价','产品','案例','精选好店','平台精选','本地商家']) {
                  if (txt.includes(k)) feature++;
                }
                return feature >= 2 && txt.length >= 8 && txt.length <= 1600;
              };
              let card = el;
              for (let i = 0; card && i < 8; i++, card = card.parentElement) {
                if (cardLike(card)) break;
              }
              if (!card || !cardLike(card)) card = el;
              const lines = textOf(card).split(/\n+/).map(x => x.trim()).filter(Boolean);
              const badLine = (x) => /^(平台精选|本地商家|全部分类|附近|智能排序|报价优惠|暂无更多内容|精选商家|服务保障)$/.test(x) || /^(评分|评价|营业中|休息中|询价|咨询)$/.test(x);
              let title = '';
              for (const line of lines) {
                const clean = line.replace(/\s+/g, ' ').trim();
                if (!clean || badLine(clean)) continue;
                if (clean.length >= 2 && clean.length <= 36 && /[\u4e00-\u9fff]/.test(clean) && !/(评分|人感兴趣|营业中|休息中|距离|km|公里|产品|案例|服务|精选好店|平台精选|本地商家)/.test(clean)) { title = clean; break; }
              }
              if (!title) {
                for (const sel of ['h1','h2','h3','h4','[class*=title]','[class*=name]','[class*=shop]','[class*=poi]','a']) {
                  const n = card.querySelector && card.querySelector(sel);
                  const t = n ? textOf(n).split(/\n+/).map(x=>x.trim()).find(Boolean) : '';
                  if (t && t.length >= 2 && t.length <= 40 && /[\u4e00-\u9fff]/.test(t) && !badLine(t)) { title = t; break; }
                }
              }
              if (!title) title = lines.find(x => /[\u4e00-\u9fff]/.test(x)) || '';
              const a = (card.querySelector && card.querySelector('a[href],a[data-url]')) || (el.closest && el.closest('a')) || null;
              const href = (a && (a.getAttribute('href') || a.getAttribute('data-url'))) || (el.getAttribute && (el.getAttribute('href') || el.getAttribute('data-url'))) || '';
              return {
                title: title || '',
                text: textOf(card) || textOf(el),
                href: href || '',
                aria: (el.getAttribute && el.getAttribute('aria-label')) || '',
                titleAttr: (el.getAttribute && el.getAttribute('title')) || ''
              };
            }
            """
        ) or {}
    except Exception:
        data = {}
    href = repair_mojibake_text(data.get("href") or "")
    abs_url = urljoin(page.url, href) if href else ""
    title = repair_mojibake_text(data.get("title") or term or "")
    text = repair_mojibake_text("\n".join([title, data.get("text") or "", data.get("aria") or "", data.get("titleAttr") or "", abs_url]))
    return {"title": title, "text": text, "url": abs_url}

def _find_candidate_shop_card_handles(page, cfg: Optional[RunConfig] = None, limit: int = 120):
    """v1.0：按前端配置的“模块商家DIV规则”识别商家卡片。

    默认规则是 div，但真正数量不是所有 div，而是经过商家特征过滤后的最小商家块。
    如页面结构变化，可在前端把“模块商家DIV规则”改成更准确的选择器。
    """
    selector = "div"
    if cfg is not None:
        selector = (getattr(cfg, "merchant_div_selector", "") or "div").strip() or "div"
    try:
        handle = page.evaluate_handle(
            """
            ({rawLimit, selector}) => {
              const limit = Math.max(1, Number(rawLimit || 120));
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 120 && r.height >= 40 && r.bottom >= -900 && r.top <= window.innerHeight + 4200;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) + ' ' +
                Array.from((el && el.querySelectorAll && el.querySelectorAll('img[alt]')) || []).map(i => i.getAttribute('alt') || '').join(' ')
              ).replace(/\u00a0/g, ' ').trim();
              const norm = (s) => String(s || '').normalize('NFKC').replace(/[\s\u3000]+/g, '').toLowerCase();
              const featureScore = (txt) => {
                let score = 0;
                if (/\d+(\.\d+)?分/.test(txt)) score += 4;
                if (/\d+人感兴趣|\d+条评价|评价|感兴趣/.test(txt)) score += 3;
                if (/营业中|休息中/.test(txt)) score += 2;
                if (/\d+(\.\d+)?\s*(km|公里|米|m)\b/i.test(txt)) score += 2;
                if (/询价|咨询/.test(txt)) score += 1;
                if (/案例|产品|环境优雅|服务热情|精选好店|精选商家|服务保障/.test(txt)) score += 1;
                if (/亲子鉴定|基因|检测|鉴定|生物|科技|中心|公司|门店|商家/.test(txt)) score += 1;
                return score;
              };
              const badContainer = (txt) => {
                if (!txt) return true;
                if (/全部分类|附近|智能排序|报价优惠/.test(txt) && txt.length < 80) return true;
                if (/暂无更多内容/.test(txt) && txt.length < 80) return true;
                // 允许页面里有“平台精选/本地商家”，但如果文本太长说明是外层大容器。
                if (/平台精选|本地商家/.test(txt) && txt.length > 1400) return true;
                return false;
              };
              const isBusinessDiv = (el) => {
                if (!visible(el)) return false;
                const txt = textOf(el);
                const r = el.getBoundingClientRect();
                if (!txt || txt.length < 8 || txt.length > 1200) return false;
                if (r.height < 45 || r.height > 520 || r.width < 160) return false;
                if (badContainer(txt)) return false;
                return featureScore(txt) >= 3 && /[\u4e00-\u9fff]/.test(txt);
              };
              let nodes = [];
              try { nodes = Array.from(document.querySelectorAll(selector || 'div')); } catch(e) { nodes = Array.from(document.querySelectorAll('div')); }
              // 如果用户给的 selector 太窄但没有命中，回退 div，避免数量为 0。
              if (!nodes.length && selector !== 'div') nodes = Array.from(document.querySelectorAll('div'));
              let all = nodes.filter(isBusinessDiv);
              // 选择最小商家卡片：如果当前节点包含另一个已选更小卡片，则当前节点视为外层容器。
              all.sort((a,b) => {
                const ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
                return (ra.width*ra.height)-(rb.width*rb.height);
              });
              const chosen = [];
              const seen = new Set();
              for (const el of all) {
                if (chosen.length >= limit) break;
                if (chosen.some(c => el.contains(c))) continue;
                const txt = textOf(el);
                const r = el.getBoundingClientRect();
                const key = Math.round(r.top)+':'+Math.round(r.left)+':'+norm(txt).slice(0,120);
                if (seen.has(key)) continue;
                seen.add(key);
                chosen.push(el);
              }
              chosen.sort((a,b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);
              return chosen;
            }
            """,
            {"rawLimit": int(limit or 120), "selector": selector},
        )
        out = []
        if not handle:
            return out
        for _, h in handle.get_properties().items():
            try:
                el = h.as_element()
                if el:
                    out.append(el)
            except Exception:
                pass
        return out
    except Exception:
        return []

def _click_card_by_title_point(page, card, matched_term: str, cfg: RunConfig):
    """v1.0：对已命中的商家卡片使用坐标点击兜底。

    优先点击包含店名/白名单词的标题区域；找不到标题时点击卡片左侧主体，避开右侧询价/咨询按钮。
    """
    before_url = page.url
    try:
        point = card.evaluate(
            """
            (root, rawTerm) => {
              const norm = (s) => String(s || '')
                .normalize('NFKC')
                .toLowerCase()
                .replace(/[\s\u3000\-—_.,，。:：;；|/\\()（）\[\]【】<>《》·'"“”‘’]+/g, '');
              const term = norm(rawTerm || '');
              const termHead = term ? term.slice(0, Math.min(6, Math.max(2, term.length))) : '';
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                if (st.display === 'none' || st.visibility === 'hidden' || st.opacity === '0') return false;
                const r = el.getBoundingClientRect();
                return r.width >= 8 && r.height >= 8;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) + ' ' +
                ((el && el.getAttribute && el.getAttribute('aria-label')) || '') + ' ' +
                ((el && el.getAttribute && el.getAttribute('title')) || '')).trim();
              const bad = (txt) => /询价|咨询|电话|路线|导航|客服|联系|报价优惠|立即咨询|在线咨询|拨打/.test(txt || '');
              const rootRect = root.getBoundingClientRect();
              let best = null;
              let bestScore = -9999;
              const push = (el, base) => {
                if (!visible(el)) return;
                const txt = textOf(el);
                if (!txt || bad(txt)) return;
                const r = el.getBoundingClientRect();
                // 避开右侧 CTA 区域。
                if (r.left > rootRect.left + rootRect.width * 0.66) return;
                let score = base;
                const n = norm(txt);
                if (term && n.includes(term)) score += 300;
                else if (termHead && n.includes(termHead)) score += 180;
                if (/^[\u4e00-\u9fffA-Za-z0-9（）()·\-—]{2,40}(\.\.\.)?$/.test(txt.replace(/\s+/g,''))) score += 60;
                if (r.top <= rootRect.top + rootRect.height * 0.55) score += 50;
                if ((el.tagName || '').toLowerCase() === 'a') score += 50;
                score -= Math.max(0, txt.length - 60) * 1.5;
                if (score > bestScore) { bestScore = score; best = el; }
              };
              for (const el of Array.from(root.querySelectorAll('a,[role="link"],h1,h2,h3,h4,[class*=title],[class*=name],[class*=shop],span,div'))) {
                push(el, 80);
              }
              const target = best || root;
              try { target.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
              const r = target.getBoundingClientRect();
              const rr = root.getBoundingClientRect();
              let x = r.left + Math.min(Math.max(r.width * 0.45, 18), Math.max(r.width - 8, 18));
              let y = r.top + Math.min(Math.max(r.height * 0.50, 10), Math.max(r.height - 6, 10));
              if (!isFinite(x) || !isFinite(y) || x < 0 || y < 0) {
                x = rr.left + rr.width * 0.42;
                y = rr.top + Math.min(Math.max(rr.height * 0.28, 28), rr.height - 16);
              }
              // 最终保证不点右侧按钮区。
              x = Math.min(x, rr.left + rr.width * 0.62);
              x = Math.max(x, rr.left + 20);
              y = Math.max(y, rr.top + 12);
              return {x, y, text: textOf(target).slice(0, 120)};
            }
            """,
            matched_term or "",
        ) or {}
    except Exception:
        point = {}
    x = point.get("x")
    y = point.get("y")
    if x is None or y is None:
        # 最后兜底：取卡片左上主体区域坐标。
        try:
            box = card.bounding_box()
            if not box:
                raise RuntimeError("无法取得卡片坐标")
            x = box["x"] + min(max(box["width"] * 0.42, 28), box["width"] * 0.62)
            y = box["y"] + min(max(box["height"] * 0.30, 26), box["height"] - 12)
        except Exception:
            # 再不行退回普通元素点击。
            return _click_element_handle(page, card, cfg)

    try:
        with page.expect_popup(timeout=1200) as popup_info:
            page.mouse.click(float(x), float(y))
        popup_page = popup_info.value
        popup_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
        return popup_page, True
    except PlaywrightTimeoutError:
        try:
            page.wait_for_timeout(900)
            if page.url != before_url:
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass
                return page, False
        except Exception:
            pass
        # 如果坐标点击没有跳转，尝试卡片 JS click / 普通点击。
        try:
            card.evaluate("el => { try { el.click(); } catch(e) {} }")
            page.wait_for_timeout(800)
            if page.url != before_url:
                return page, False
        except Exception:
            pass
        return _click_element_handle(page, card, cfg)



def _click_entire_business_div(page, card, cfg: RunConfig):
    """v1.0：命中白名单后点击整个商家 div 块进行跳转。

    不再优先点击卡片内部 a/button，避免点到“询价/咨询”；坐标取 div 主体中左区域。
    """
    before_url = page.url
    try:
        point = card.evaluate(
            """
            (root) => {
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0' && r.width >= 10 && r.height >= 10;
              };
              try { root.scrollIntoView({block:'center', inline:'nearest'}); } catch(e) {}
              const r = root.getBoundingClientRect();
              // 点击整张商家 div 的主体区域：略偏左/中上，避开右侧询价按钮。
              return {x: r.left + Math.min(Math.max(r.width * 0.42, 40), r.width * 0.62), y: r.top + Math.min(Math.max(r.height * 0.36, 28), r.height - 18)};
            }
            """
        ) or {}
    except Exception:
        point = {}
    x = point.get('x')
    y = point.get('y')
    try:
        if x is None or y is None:
            box = card.bounding_box()
            if not box:
                raise RuntimeError('无法取得商家div坐标')
            x = box['x'] + min(max(box['width'] * 0.42, 40), box['width'] * 0.62)
            y = box['y'] + min(max(box['height'] * 0.36, 28), box['height'] - 18)
        with page.expect_popup(timeout=1200) as popup_info:
            page.mouse.click(float(x), float(y))
        popup_page = popup_info.value
        popup_page.wait_for_load_state('domcontentloaded', timeout=cfg.page_timeout_ms)
        return popup_page, True
    except PlaywrightTimeoutError:
        try:
            page.wait_for_timeout(900)
            if page.url != before_url:
                try:
                    page.wait_for_load_state('domcontentloaded', timeout=cfg.page_timeout_ms)
                except Exception:
                    pass
                return page, False
        except Exception:
            pass
        # 如果坐标没有触发跳转，再尝试整块 div 的 JS click。
        try:
            card.evaluate("el => { try { el.click(); } catch(e) {} }")
            page.wait_for_timeout(900)
            if page.url != before_url:
                return page, False
        except Exception:
            pass
        raise RuntimeError('点击商家div后未发生跳转')


def scan_and_click_whitelist_by_candidate_cards(page, cfg: RunConfig, keyword: str, whitelist: List[Dict[str, str]], stop_flag: StopFlag, log: Callable[[str], None], worker_name: str, clicked_shop_keys: Optional[set] = None, stats: Optional[Dict[str, int]] = None, max_extra_clicks: Optional[int] = None, blacklist: Optional[List[Dict[str, str]]] = None) -> int:
    """v1.0：进入模块后按商家 div 逐卡识别、逐卡点击。

    修复点：
    1. 点击一家返回后重新读取当前页面 DOM，避免继续使用旧 ElementHandle 导致剩余店铺不点；
    2. 去重只按实际商家标题/URL/卡片文本，不按白名单词去重；
    3. 同一模块中多个不同标题的白名单店铺会逐个点击，直到连续复查没有新店铺。
    """
    if clicked_shop_keys is None:
        clicked_shop_keys = set()
    blacklist = blacklist or []
    max_clicks = max_extra_clicks if max_extra_clicks is not None else (10**9 if cfg.click_all_whitelist_in_target else max(int(cfg.max_clicks_per_keyword or 1), 1))
    if max_clicks <= 0:
        return 0

    search_result_url = page.url
    clicks = 0
    no_new_rounds = 0
    verify_rounds = max(int(getattr(cfg, "verify_remaining_rounds", 3) or 3), 1)
    scan_round = 0

    while clicks < max_clicks and not stop_flag.is_stopped():
        scan_round += 1
        try:
            stop_flag.checkpoint()
        except RuntimeError:
            break

        cards = _find_candidate_shop_card_handles(page, cfg, limit=max(int(getattr(cfg, 'max_items_to_scan', 80) or 80), 80))
        if not cards:
            if clicks == 0:
                log("模块内商家div识别数量：0 个，继续使用全文兜底")
            else:
                log(f"模块内商家div复查：未识别到新的商家div，复查 {no_new_rounds + 1}/{verify_rounds}")
            break

        log(f"模块内商家div识别数量：{len(cards)} 个，开始第 {scan_round} 轮按白名单/黑名单逐项校验")
        try:
            preview = []
            for c in cards[:10]:
                ii = _collect_element_card_info(page, c, "")
                name = repair_mojibake_text(ii.get("title") or _extract_shop_name_for_dedupe(ii.get("text") or "") or "")
                if name:
                    preview.append(name[:28])
            if preview:
                log("候选店铺预览：" + " / ".join(preview))
        except Exception:
            pass

        seen_card_fps = set()
        clicked_this_round = False
        matched_not_clicked = 0

        for idx, card in enumerate(cards, start=1):
            if stop_flag.is_stopped() or clicks >= max_clicks:
                break
            try:
                stop_flag.checkpoint()
            except RuntimeError:
                break

            info = _collect_element_card_info(page, card, "")
            text = info.get("text") or info.get("title") or ""
            if not normalize_cn(text):
                continue

            card_fp = _make_card_text_fingerprint(info)
            if card_fp and card_fp in seen_card_fps:
                # 同一张卡片可能被多个嵌套 div 识别到，只跳过完全相同文本的重复候选。
                continue
            if card_fp:
                seen_card_fps.add(card_fp)

            black = match_blacklist(text, info.get("url") or "", blacklist, cfg)
            if black:
                log(f"黑名单拦截：{black.matched_term} / 跳过候选卡片：{(info.get('title') or _extract_shop_name_for_dedupe(text) or text)[:80]}")
                continue

            matched = match_whitelist(text, info.get("url") or "", whitelist, cfg)
            if not matched:
                continue

            matched_not_clicked += 1
            shop_key = make_shop_dedupe_key(info, matched)
            matched_rule = matched.matched_term or matched.rule.get("title") or matched.rule.get("url") or ""
            display_title = info.get("title") or _extract_shop_name_for_dedupe(text) or text[:80]

            if shop_key and shop_key in clicked_shop_keys:
                log(f"模块候选卡片：仅跳过已点击过的同一商家：{display_title[:60]} / key={shop_key[:90]}")
                continue

            try:
                log(f"商家div命中白名单：{matched_rule} / 实际标题：{display_title[:80]} / 相似度 {matched.score:.2f} / 准备点击整个div")
                active_page, opened_popup = _click_entire_business_div(page, card, cfg)
                try:
                    active_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass

                stay = random.randint(cfg.stay_seconds_min, cfg.stay_seconds_max)
                log(f"已进入白名单页面，停留 {stay} 秒：{active_page.url}")
                if not stop_flag.sleep(stay):
                    log("停留期间收到停止指令，正在退出当前关键词")

                clicked_shop_keys.add(shop_key)
                clicks += 1
                clicked_this_round = True

                if stats is not None:
                    with _STATS_LOCK:
                        stats["clicked"] = int(stats.get("clicked", 0)) + 1
                        total_clicked = stats["clicked"]
                    log(f"[STATS] clicked={total_clicked} 实时统计：点击白名单数量 {total_clicked} 次")

                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "module_card_click",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": display_title,
                    "url": active_page.url,
                    "status": "success",
                    "stay_seconds": str(stay),
                    "message": "v1.0 模块商家div逐卡点击完成；去重按实际店铺标题/卡片文本，不按白名单词去重",
                })

                # 点击后页面可能跳转/刷新，必须回到列表页并重新提取 DOM，避免旧句柄失效和漏点。
                back_to_results(active_page, page, opened_popup, search_result_url, cfg)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass
                try:
                    page.wait_for_timeout(max(int(getattr(cfg, "click_open_timeout_ms", 8000) or 8000) // 4, 800))
                except Exception:
                    pass
                break
            except Exception as e:
                log(f"模块候选卡片点击失败，跳过该项：{e}")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "module_card_click",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": display_title,
                    "url": info.get("url") or page.url,
                    "status": "failed",
                    "message": repr(e),
                })
                try:
                    page.goto(search_result_url, wait_until="domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass

        if clicked_this_round:
            no_new_rounds = 0
            continue

        no_new_rounds += 1
        if matched_not_clicked > 0:
            log(f"本轮发现 {matched_not_clicked} 个白名单候选，但都已点击过或被黑名单/点击失败跳过；复查 {no_new_rounds}/{verify_rounds}")
        else:
            log(f"本轮没有发现新的白名单候选；复查 {no_new_rounds}/{verify_rounds}")
        if no_new_rounds >= verify_rounds:
            break

        # 继续下滑加载模块内更多商家，再重新扫描。
        try:
            page.mouse.wheel(0, max(int(getattr(cfg, "scan_scroll_pixels", 900) or 900), 300))
            if not stop_flag.sleep(max(float(getattr(cfg, "scroll_wait_ms", 900) or 900) / 1000, 0.3)):
                break
        except Exception:
            break

    if clicks:
        log(f"模块商家div识别已点击白名单 {clicks} 次")
    return clicks


def _find_force_card_handles_by_term(page, term: str, blacklist_terms: Optional[List[str]] = None, limit: int = 8):
    """v1.0：全文兜底用。按白名单词在页面里找包含该词的商家div。"""
    blacklist_terms = blacklist_terms or []
    try:
        handle = page.evaluate_handle(
            """
            ({term, blacklistTerms, limit}) => {
              const norm = (s) => String(s || '').normalize('NFKC').toLowerCase()
                .replace(/[\s\u3000\u200b\u200c\u200d\ufeff,.;:!?，。！？、；：'"‘’“”（）【】《》<>\[\]{}\-—…·￥]/g, '');
              const rawTerm = norm(term || '');
              const head = rawTerm ? rawTerm.slice(0, Math.min(6, Math.max(2, rawTerm.length))) : '';
              const badTerms = (blacklistTerms || []).map(norm).filter(Boolean);
              const visible = (el) => {
                if (!el) return false;
                const st = window.getComputedStyle(el);
                const r = el.getBoundingClientRect();
                return st.display !== 'none' && st.visibility !== 'hidden' && st.opacity !== '0' && r.width >= 120 && r.height >= 40;
              };
              const textOf = (el) => ((el && (el.innerText || el.textContent || '')) || '').replace(/\u00a0/g, ' ').trim();
              const feature = (txt) => /评分|评价|感兴趣|营业中|询价|咨询|案例|产品|精选好店|亲子鉴定|基因|检测|鉴定|公司|中心/.test(txt);
              const out = [];
              const nodes = Array.from(document.querySelectorAll('div'));
              for (const el of nodes) {
                if (out.length >= limit) break;
                if (!visible(el)) continue;
                const txt = textOf(el);
                if (!txt || txt.length < 8 || txt.length > 1200 || !feature(txt)) continue;
                const n = norm(txt);
                if (badTerms.some(b => b && n.includes(b))) continue;
                if ((rawTerm && n.includes(rawTerm)) || (head && n.includes(head))) out.push(el);
              }
              return out;
            }
            """,
            {"term": term or "", "blacklistTerms": blacklist_terms, "limit": int(limit or 8)},
        )
        out=[]
        if handle:
            for _, h in handle.get_properties().items():
                try:
                    el=h.as_element()
                    if el: out.append(el)
                except Exception:
                    pass
        return out
    except Exception:
        return []

def scan_and_click_whitelist_by_full_page(page, cfg: RunConfig, keyword: str, whitelist: List[Dict[str, str]], stop_flag: StopFlag, log: Callable[[str], None], worker_name: str, clicked_shop_keys: Optional[set] = None, stats: Optional[Dict[str, int]] = None, max_extra_clicks: Optional[int] = None, blacklist: Optional[List[Dict[str, str]]] = None) -> int:
    """增强白名单识别：进入目标模块后，按白名单词从全文/店铺卡片中寻找可点击店铺。

    同一个白名单词可以命中多个可点击卡片；逐个校验黑名单、白名单、去重后点击，
    避免页面里明明还有白名单店铺却只点了第一家。
    """
    if clicked_shop_keys is None:
        clicked_shop_keys = set()
    clicks = 0
    search_result_url = page.url
    max_clicks = max_extra_clicks if max_extra_clicks is not None else (10**9 if cfg.click_all_whitelist_in_target else max(int(cfg.max_clicks_per_keyword or 1), 1))
    tried_terms = set()
    blacklist = blacklist or []
    blacklist_terms = _blacklist_terms(blacklist)

    # v1.0：先按模块候选商家卡片做模糊匹配，解决店名被省略号截断导致完整白名单词无法 in 页面文本的问题。
    try:
        card_clicks = scan_and_click_whitelist_by_candidate_cards(
            page, cfg, keyword, whitelist, stop_flag, log, worker_name,
            clicked_shop_keys=clicked_shop_keys, stats=stats, max_extra_clicks=max_clicks - clicks, blacklist=blacklist
        )
        clicks += card_clicks
        if clicks >= max_clicks:
            return clicks
    except Exception as e:
        log(f"模块商家div识别异常，继续全文兜底：{e}")

    body_text = _page_body_text(page)
    if not body_text.strip():
        return clicks

    for rule in whitelist:
        if stop_flag.is_stopped() or clicks >= max_clicks:
            break
        terms = split_terms(rule.get("title") or "") + split_terms(rule.get("url") or "")
        for term in terms:
            if stop_flag.is_stopped() or clicks >= max_clicks:
                break
            term_key = normalize_cn(term)
            if not term_key or term_key in tried_terms:
                continue
            tried_terms.add(term_key)
            # v1.0：不要要求完整白名单词必须直接出现在页面文本里。
            # 百度列表会把店名截断成“xxx...”，完整词不出现但仍应通过卡片模糊匹配识别。
            try:
                stop_flag.checkpoint()
            except RuntimeError:
                break

            elements = _find_clickable_by_whitelist_term_handles(page, term, blacklist_terms, limit=8)
            force_card_click_mode = False
            if not elements:
                elements = _find_force_card_handles_by_term(page, term, blacklist_terms, limit=8)
                force_card_click_mode = True
                if elements:
                    log(f"全文发现白名单词，已启用 v1.0 卡片坐标兜底：{term} / 候选 {len(elements)} 个")
                else:
                    log(f"全文发现白名单词但未找到可点击卡片：{term}")
                    continue

            for element in elements:
                if stop_flag.is_stopped() or clicks >= max_clicks:
                    break
                try:
                    stop_flag.checkpoint()
                except RuntimeError:
                    break

                info = _collect_element_card_info(page, element, term)
                black = match_blacklist(info["text"], info["url"], blacklist, cfg)
                if black:
                    log(f"黑名单拦截：{black.matched_term} / 跳过白名单候选：{term}")
                    append_log(cfg.output_log_file, {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "worker": worker_name,
                        "keyword": keyword,
                        "action": "skip_blacklist",
                        "matched_rule": black.matched_term,
                        "score": f"{black.score:.3f}",
                        "title": info["title"],
                        "url": info["url"],
                        "status": "skipped",
                        "message": "全文兜底候选命中黑名单，不点击",
                    })
                    continue

                matched = match_whitelist(info["text"], info["url"], [rule], cfg)
                if not matched:
                    continue
                shop_key = make_shop_dedupe_key(info, matched)
                matched_rule = matched.matched_term or rule.get("title") or rule.get("url") or term
                if shop_key and shop_key in clicked_shop_keys:
                    log(f"全文兜底识别：已跳过重复店铺/白名单：{matched_rule}")
                    continue

                log(f"全文兜底识别命中白名单：{matched_rule} / 相似度 {matched.score:.2f} / 准备点击：{info['title'][:80]}")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "fallback_full_page_match",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": info["title"],
                    "url": info["url"],
                    "status": "matched",
                    "message": "通过页面全文/店铺卡片结构兜底识别；已通过黑名单校验",
                })

                try:
                    if force_card_click_mode:
                        active_page, opened_popup = _click_card_by_title_point(page, element, matched_rule, cfg)
                    else:
                        try:
                            active_page, opened_popup = _click_element_handle(page, element, cfg)
                        except Exception as click_err:
                            log(f"全文兜底普通点击失败，改用 v1.0 标题区域坐标点击：{click_err}")
                            active_page, opened_popup = _click_card_by_title_point(page, element, matched_rule, cfg)
                    try:
                        active_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                    except Exception:
                        pass
                    stay = random.randint(cfg.stay_seconds_min, cfg.stay_seconds_max)
                    log(f"已进入白名单页面，停留 {stay} 秒：{active_page.url}")
                    if not stop_flag.sleep(stay):
                        log("停留期间收到停止指令，正在退出当前关键词")
                    clicked_shop_keys.add(shop_key)
                    clicks += 1
                    if stats is not None:
                        with _STATS_LOCK:
                            stats["clicked"] = int(stats.get("clicked", 0)) + 1
                            total_clicked = stats["clicked"]
                        log(f"[STATS] clicked={total_clicked} 实时统计：点击白名单数量 {total_clicked} 次")
                    append_log(cfg.output_log_file, {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "worker": worker_name,
                        "keyword": keyword,
                        "action": "fallback_full_page_click",
                        "matched_rule": matched_rule,
                        "score": f"{matched.score:.3f}",
                        "title": info["title"],
                        "url": active_page.url,
                        "status": "success",
                        "stay_seconds": str(stay),
                        "message": "全文兜底识别点击并停留完成；已通过黑名单校验",
                    })
                    back_to_results(active_page, page, opened_popup, search_result_url, cfg)
                    body_text = _page_body_text(page)
                except Exception as e:
                    log(f"全文兜底点击失败，跳过该项：{e}")
                    append_log(cfg.output_log_file, {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "worker": worker_name,
                        "keyword": keyword,
                        "action": "fallback_full_page_click",
                        "matched_rule": matched_rule,
                        "score": f"{matched.score:.3f}",
                        "title": info["title"],
                        "url": info["url"],
                        "status": "failed",
                        "message": repr(e),
                    })
                    try:
                        page.goto(search_result_url, wait_until="domcontentloaded", timeout=cfg.page_timeout_ms)
                    except Exception:
                        pass
    return clicks


def _extract_shop_name_for_dedupe(text: str) -> str:
    """从商家 div 文本中提取实际店名。

    v1.0 修复：旧逻辑有时会把同一条白名单词或通用服务词当成去重 key，
    导致模块里 7 个不同店铺只点击 1-2 个。现在只优先使用卡片内真实店名行；
    若无法可靠提取，则交给 make_shop_dedupe_key 使用整张卡片文本指纹区分。
    """
    raw_text = repair_mojibake_text(text or "").replace("\r", "\n")
    bad_words = [
        "评分", "评价", "人感兴趣", "浏览", "营业", "咨询", "询价", "路线", "导航", "电话", "地址",
        "服务", "保障", "产品", "￥", "¥", "km", "公里", "米", "全部", "查看更多", "更多",
        "推荐", "精选", "平台", "广告", "HOT", "人浏览", "营业中", "休息中", "报价优惠", "暂无更多内容",
        "全部分类", "附近", "智能排序", "本地商家", "平台精选", "案例", "环境优雅", "服务热情",
    ]
    shop_suffix = ["中心", "公司", "门店", "店", "机构", "医院", "检测", "基因", "鉴定", "生物", "科技"]
    lines = []
    for raw in raw_text.split("\n"):
        line = re.sub(r"\s+", " ", (raw or "").strip())
        if not line:
            continue
        # 去掉常见标签后再判断，但保留店名里的括号分店信息。
        line = line.strip(" -｜|，,。:：;；")
        norm = normalize_cn(line)
        if len(norm) < 2 or len(line) > 64:
            continue
        if not any("\u4e00" <= ch <= "\u9fff" for ch in line):
            continue
        # 明显不是店名的行不要作为去重 key。
        if any(normalize_cn(w) == norm for w in bad_words):
            continue
        if re.fullmatch(r"\d+(\.\d+)?分|\d+条评价|\d+人感兴趣|\d+(\.\d+)?(km|公里|米|m)", norm, re.I):
            continue
        # 含业务/机构后缀的短行优先，是最稳定的店名 key。
        suffix_hit = any(k in line for k in shop_suffix)
        if suffix_hit:
            lines.insert(0, line)
        else:
            # 不含后缀但像品牌名的也保留，但优先级低。
            if not any(normalize_cn(w) in norm for w in bad_words):
                lines.append(line)
    if not lines:
        return ""
    # 优先短店名，避免把整张卡片多行内容拼成同一个 key。
    lines = sorted(dict.fromkeys(lines), key=lambda x: (0 if any(k in x for k in shop_suffix) else 1, len(x)))
    return normalize_cn(lines[0])[:160]


def _make_card_text_fingerprint(info: Dict[str, str]) -> str:
    """按整张卡片文本生成指纹，用来区分不同商家 div。

    不使用白名单词作为指纹，避免同一白名单命中多个商家时被误去重。
    """
    text = repair_mojibake_text("\n".join([info.get("title") or "", info.get("text") or "", info.get("url") or ""]))
    lines = []
    for raw in text.replace("\r", "\n").split("\n"):
        line = re.sub(r"\s+", " ", (raw or "").strip())
        if not line:
            continue
        n = normalize_cn(line)
        if not n:
            continue
        if len(n) <= 1:
            continue
        # 保留店名、评分、区域、距离等组合，足以区分同品牌不同分店。
        if any("\u4e00" <= ch <= "\u9fff" for ch in line) or re.search(r"\d", line):
            lines.append(n[:80])
        if len(lines) >= 8:
            break
    base = "|".join(lines) or normalize_cn(text)[:220]
    return base[:220]


def make_shop_dedupe_key(info: Dict[str, str], matched: MatchResult) -> str:
    """生成店铺去重 key。

    v1.0 规则：
    1. 只用实际商家标题/URL/卡片文本做去重；
    2. 不再用白名单词或匹配规则本身做去重；
    3. 同一白名单词命中多个不同商家 div 时，必须逐个点击。
    """
    title_key = _extract_shop_name_for_dedupe(info.get("title") or "")
    text_key = _extract_shop_name_for_dedupe(info.get("text") or "")
    url_key = normalize_cn(info.get("url") or "")
    if url_key and len(url_key) >= 12:
        # URL 不为空时最稳定，但只取前段避免跟踪参数过长。
        return "url:" + url_key[:220]
    if title_key and len(title_key) >= 2:
        return "shop-title:" + title_key
    if text_key and len(text_key) >= 2:
        return "shop-text:" + text_key
    fp = _make_card_text_fingerprint(info)
    if fp:
        return "card:" + fp
    # 最后兜底也不能只用 matched_term，否则会把同白名单的多家店铺误判重复。
    return "card-empty:" + normalize_cn((info.get("text") or info.get("title") or "")[:200])

def scan_and_click_whitelist_results(page, cfg: RunConfig, keyword: str, whitelist: List[Dict[str, str]], stop_flag: StopFlag, log: Callable[[str], None], worker_name: str, clicked_shop_keys: Optional[set] = None, stats: Optional[Dict[str, int]] = None, blacklist: Optional[List[Dict[str, str]]] = None) -> int:
    """扫描当前页并只点击白名单项。

    
    1. 同一实际店铺只点击一次，避免结果卡片、子链接、按钮被重复识别后连续点击。
    2. 不再用整条白名单规则去重，因此同一模块里的多家白名单店铺会继续全部点击。
    """
    if clicked_shop_keys is None:
        clicked_shop_keys = set()
    clicked_item_keys = set()
    blacklist = blacklist or []
    blacklist_terms = _blacklist_terms(blacklist)
    clicks = 0
    search_result_url = page.url
    max_clicks = 10**9 if cfg.click_all_whitelist_in_target else max(int(cfg.max_clicks_per_keyword or 1), 1)
    no_new_click_rounds = 0

    for round_idx in range(max(cfg.scan_scroll_rounds, 1)):
        round_clicks_at_start = clicks
        try:
            stop_flag.checkpoint()
        except RuntimeError:
            break
        if stop_flag.is_stopped() or clicks >= max_clicks:
            break

        items = page.locator(cfg.result_item_selector)
        count = min(items.count(), cfg.max_items_to_scan)
        if count == 0:
            state = analyze_page_state(page, cfg, whitelist=whitelist)
            diagnosis = str(state.get("diagnosis") or "未识别到结果项")
            log(f"未识别到结果项。判断：{diagnosis}")
            log("未识别到固定结果项，启动 v1.0 模块候选卡片/坐标兜底识别。")
            extra = scan_and_click_whitelist_by_full_page(page, cfg, keyword, whitelist, stop_flag, log, worker_name, clicked_shop_keys=clicked_shop_keys, stats=stats, max_extra_clicks=max_clicks - clicks, blacklist=blacklist)
            clicks += extra
            if extra > 0:
                break
            save_debug_artifacts(page, cfg, keyword, "no_result_items", diagnosis, log)
            break

        # 每一轮都从头扫描，但用 item_key 和 shop_key 双重去重，避免返回后 DOM 刷新导致重复点同一家。
        for i in range(count):
            try:
                stop_flag.checkpoint()
            except RuntimeError:
                break
            if stop_flag.is_stopped() or clicks >= max_clicks:
                break
            items = page.locator(cfg.result_item_selector)
            if i >= items.count():
                continue
            item = items.nth(i)
            info = collect_item_info(page, item, cfg)
            unique_key = normalize_cn(info.get("url") or info.get("title") or info.get("text")[:100])
            if not unique_key or unique_key in clicked_item_keys:
                continue

            black = match_blacklist(info["text"], info["url"], blacklist, cfg)
            if black:
                log(f"黑名单拦截：{black.matched_term} / 跳过候选：{(info.get('title') or '')[:80]}")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "skip_blacklist",
                    "matched_rule": black.matched_term,
                    "score": f"{black.score:.3f}",
                    "title": info["title"],
                    "url": info["url"],
                    "status": "skipped",
                    "message": "命中黑名单，不点击",
                })
                continue
            matched = match_whitelist(info["text"], info["url"], whitelist, cfg)
            if not matched:
                continue

            clicked_item_keys.add(unique_key)
            shop_key = make_shop_dedupe_key(info, matched)
            matched_rule = matched.matched_term or matched.rule.get("title") or matched.rule.get("url") or ""
            if shop_key and shop_key in clicked_shop_keys:
                log(f"已跳过重复店铺/白名单：{matched_rule} / 当前关键词已点击过，不再连续点击")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "skip_duplicate_shop",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": info["title"],
                    "url": info["url"],
                    "status": "skipped",
                    "message": "同一实际店铺已点击过，默认不重复点击",
                })
                continue

            log(f"匹配白名单：{matched_rule} / 相似度 {matched.score:.2f} / 准备点击：{info['title'][:80]}")
            append_log(cfg.output_log_file, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "worker": worker_name,
                "keyword": keyword,
                "action": "match",
                "matched_rule": matched_rule,
                "score": f"{matched.score:.3f}",
                "title": info["title"],
                "url": info["url"],
                "status": "matched",
                "message": f"仅白名单匹配项进入点击流程，匹配方式：{matched.reason}",
            })

            try:
                active_page, opened_popup = click_link_in_item(page, item, cfg, matched.matched_term, blacklist_terms)
                try:
                    active_page.wait_for_load_state("domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass
                stay = random.randint(cfg.stay_seconds_min, cfg.stay_seconds_max)
                log(f"已进入白名单页面，停留 {stay} 秒：{active_page.url}")
                # 停留期间支持暂停/继续/停止，避免长时间无响应。
                if not stop_flag.sleep(stay):
                    log("停留期间收到停止指令，正在退出当前关键词")

                clicked_shop_keys.add(shop_key)
                clicks += 1
                if stats is not None:
                    with _STATS_LOCK:
                        stats["clicked"] = int(stats.get("clicked", 0)) + 1
                        total_clicked = stats["clicked"]
                    log(f"[STATS] clicked={total_clicked} 实时统计：点击白名单数量 {total_clicked} 次")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "click",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": info["title"],
                    "url": active_page.url,
                    "status": "success",
                    "stay_seconds": str(stay),
                    "message": "点击并停留完成；同店铺已加入去重列表",
                })
                back_to_results(active_page, page, opened_popup, search_result_url, cfg)
            except Exception as e:
                log(f"点击失败，跳过该项：{e}")
                append_log(cfg.output_log_file, {
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "worker": worker_name,
                    "keyword": keyword,
                    "action": "click",
                    "matched_rule": matched_rule,
                    "score": f"{matched.score:.3f}",
                    "title": info["title"],
                    "url": info["url"],
                    "status": "failed",
                    "message": repr(e),
                })
                try:
                    page.goto(search_result_url, wait_until="domcontentloaded", timeout=cfg.page_timeout_ms)
                except Exception:
                    pass

        # 进入目标模块后执行增强白名单识别。
        # 即使普通结果项已点击到一部分，也继续用全文/店铺卡片兜底把模块里的其他白名单店铺点完。
        if getattr(cfg, "enhanced_target_detection", True) and cfg.click_all_whitelist_in_target and clicks < max_clicks:
            try:
                extra = scan_and_click_whitelist_by_full_page(
                    page, cfg, keyword, whitelist, stop_flag, log, worker_name,
                    clicked_shop_keys=clicked_shop_keys, stats=stats, max_extra_clicks=max_clicks - clicks, blacklist=blacklist
                )
                if extra > 0:
                    clicks += extra
                    log(f"增强白名单识别已补充点击 {extra} 家，继续检查模块内是否还有未点击白名单。")
            except Exception as e:
                log(f"增强白名单识别失败，继续普通扫描：{e}")

        if stop_flag.is_stopped() or clicks >= max_clicks:
            break

        # 点完后不立刻结束，继续复查几轮，避免模块里还有剩余白名单没点到。
        if cfg.stop_after_all_whitelist_clicked and cfg.click_all_whitelist_in_target and clicks > 0 and clicks == round_clicks_at_start:
            no_new_click_rounds += 1
            needed = max(int(getattr(cfg, "verify_remaining_rounds", 3) or 3), 1)
            if no_new_click_rounds >= needed:
                log(f"已连续复查 {no_new_click_rounds}/{needed} 轮没有发现新的白名单店铺，结束当前模块扫描")
                break
            log(f"本轮没有新增白名单点击，继续复查剩余白名单 {no_new_click_rounds}/{needed}")
        else:
            no_new_click_rounds = 0

        # 下滑加载更多结果，再继续匹配白名单。
        try:
            page.mouse.wheel(0, cfg.scan_scroll_pixels)
            if not stop_flag.sleep(cfg.scroll_wait_ms / 1000):
                break
        except Exception:
            break

    if clicks == 0:
        state = analyze_page_state(page, cfg, whitelist=whitelist)
        diagnosis = str(state.get("diagnosis") or "本轮未点击白名单")
        diagnosis += "；已启动 v1.0 模块候选卡片/坐标兜底识别。"
        extra = scan_and_click_whitelist_by_full_page(page, cfg, keyword, whitelist, stop_flag, log, worker_name, clicked_shop_keys=clicked_shop_keys, stats=stats, blacklist=blacklist)
        clicks += extra
        if extra > 0:
            log(f"v1.0 兜底识别已完成：额外点击白名单 {extra} 次")
        else:
            log(f"本轮未点击白名单。判断：{diagnosis}")
            save_debug_artifacts(page, cfg, keyword, "no_whitelist_clicked", diagnosis, log)
    log(f"本轮搜索完成：{keyword}，白名单点击 {clicks} 次")
    append_log(cfg.output_log_file, {
        "time": datetime.now().isoformat(timespec="seconds"),
        "worker": worker_name,
        "keyword": keyword,
        "action": "search_round_done",
        "status": "success",
        "message": f"本轮白名单点击 {clicks} 次；累计去重店铺 {len(clicked_shop_keys)} 个",
    })
    return clicks


def proxy_config_from_run_config(cfg: RunConfig) -> ProxyConfig:
    return ProxyConfig(
        mode=cfg.proxy_mode,
        proxy_list_file=cfg.proxy_list_file,
        proxy_api_url=cfg.proxy_api_url,
        fixed_proxy=cfg.fixed_proxy,
        change_ip_url=cfg.change_ip_url,
        proxy_username=cfg.proxy_username,
        proxy_password=cfg.proxy_password,
        change_wait_seconds=cfg.proxy_change_wait_seconds,
    )



def _progress_task_id(cfg: RunConfig, keywords: List[str]) -> str:
    """生成任务指纹。关键词文件、白名单文件、关键词内容或每词搜索次数变化时自动启用新进度。"""
    import hashlib
    raw = "\n".join([
        os.path.abspath(cfg.keywords_file),
        os.path.abspath(cfg.whitelist_file),
        os.path.abspath(getattr(cfg, "blacklist_file", "blacklist.csv") or "blacklist.csv"),
        str(cfg.searches_per_keyword),
        *keywords,
    ])
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16]


def _load_task_progress(cfg: RunConfig, keywords: List[str]) -> Dict[str, object]:
    task_id = _progress_task_id(cfg, keywords)
    data: Dict[str, object] = {
        "version": "v1.0",
        "task_id": task_id,
        "completed_attempts": {},
        "clicked_total": 0,
    }
    path = getattr(cfg, "progress_file", "output/task_progress.json") or "output/task_progress.json"
    try:
        if os.path.exists(path):
            old = safe_load_json(path, default={}) or {}
            if isinstance(old, dict) and old.get("task_id") == task_id:
                data.update(old)
    except Exception:
        pass
    return data


def _save_task_progress(cfg: RunConfig, progress: Dict[str, object]) -> None:
    path = getattr(cfg, "progress_file", "output/task_progress.json") or "output/task_progress.json"
    try:
        ensure_parent(path)
        progress["updated_at"] = datetime.now().isoformat(timespec="seconds")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(progress, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def run_keyword(keyword: str, cfg: RunConfig, whitelist: List[Dict[str, str]], blacklist: List[Dict[str, str]], proxy_provider: ProxyProvider, stop_flag: StopFlag, log: Callable[[str], None], worker_name: str, attempt_total: int = 1, stats: Optional[Dict[str, int]] = None, start_attempt: int = 1, progress_callback: Optional[Callable[[str, int, Dict[str, int]], None]] = None) -> None:
    """执行单个关键词。

    同一关键词的多次搜索复用同一个 browser/context/page，并共享店铺去重列表。
    start_attempt 用于停止后续跑：已完成的搜索轮次不会重复执行。
    """
    if stop_flag.is_stopped():
        return
    stop_flag.checkpoint()

    total_attempts = max(int(attempt_total or 1), 1)
    start_attempt = max(int(start_attempt or 1), 1)
    if start_attempt > total_attempts:
        log(f"[{worker_name}] 跳过已完成关键词：{keyword}（已完成 {total_attempts}/{total_attempts} 次搜索）")
        return

    try:
        proxy = proxy_provider.next_proxy()
    except Exception as e:
        log(f"[{worker_name}] 获取代理失败，已跳过当前关键词本轮任务：{e}")
        return
    clicked_shop_keys: set = set()
    with sync_playwright() as pw:
        browser = None
        context = None
        try:
            log(f"[{worker_name}] 新建隐藏浏览器环境，关键词：{keyword}；计划从第 {start_attempt}/{attempt_total} 次继续搜索；隐藏窗口={cfg.headless}；浏览器来源={cfg.browser_source}；环境模式={cfg.environment_mode}；地区={cfg.region_city or '未设置'}")
            browser = launch_browser(pw, cfg, proxy)
            context_kwargs = build_context_kwargs(cfg)
            context = browser.new_context(**context_kwargs)
            install_region_environment_scripts(context, cfg, log, worker_name)
            traffic_stats = install_traffic_saving_routes(context, cfg, log, worker_name)
            # 非持久模式不复用 Cookie；持久模式会从 profiles/<档案名>/storage_state.json 读取。
            if not is_persistent_env(cfg):
                context.clear_cookies()
            page = context.new_page()
            apply_fixed_search_environment(context, page, cfg, log, worker_name)

            for attempt_no in range(start_attempt, total_attempts + 1):
                if stop_flag.is_stopped():
                    break
                stop_flag.checkpoint()
                if attempt_no == 1:
                    log(f"[{worker_name}] 开始第 {attempt_no}/{attempt_total} 次搜索：{keyword}")
                else:
                    log(f"[{worker_name}] 复用当前隐藏浏览器页面，开始第 {attempt_no}/{attempt_total} 次搜索：{keyword}")

                target_opened_popup = False
                scan_page = page
                try:
                    perform_search(page, cfg, keyword, lambda m: log(f"[{worker_name}] {m}"), stop_flag)
                    scan_page, target_opened_popup = prepare_target_page_after_search(page, cfg, keyword, lambda m: log(f"[{worker_name}] {m}"), stop_flag, whitelist)
                    scan_and_click_whitelist_results(scan_page, cfg, keyword, whitelist, stop_flag, lambda m: log(f"[{worker_name}] {m}"), worker_name, clicked_shop_keys=clicked_shop_keys, stats=stats, blacklist=blacklist)
                    save_search_environment_state(context, cfg, log, worker_name)
                    if progress_callback is not None:
                        try:
                            progress_callback(keyword, attempt_no, stats or {})
                        except Exception:
                            pass
                except Exception as e:
                    log(f"[{worker_name}] 关键词本轮失败：{keyword}（第 {attempt_no}/{attempt_total} 次）/ {e}")
                    append_log(cfg.output_log_file, {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "worker": worker_name,
                        "keyword": keyword,
                        "action": "search_round_error",
                        "status": "failed",
                        "message": f"第 {attempt_no}/{attempt_total} 次失败：{repr(e)}",
                    })
                    append_log(cfg.output_log_file.replace(".csv", "_trace.csv"), {
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "worker": worker_name,
                        "keyword": keyword,
                        "action": "traceback",
                        "status": "failed",
                        "message": traceback.format_exc(),
                    })
                finally:
                    if target_opened_popup and scan_page is not page:
                        try:
                            scan_page.close()
                        except Exception:
                            pass

                if attempt_no < attempt_total:
                    # 给页面一个小间隔，且支持暂停/停止；不关闭浏览器，不重建上下文。
                    if not stop_flag.sleep(0.8):
                        break

            traffic_summary = summarize_traffic_stats(locals().get("traffic_stats"))
            if traffic_summary:
                log(f"[{worker_name}] {traffic_summary}")
            log(f"[{worker_name}] 关键词全部搜索完成：{keyword}；去重后已点击店铺/白名单 {len(clicked_shop_keys)} 个")
            append_log(cfg.output_log_file, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "worker": worker_name,
                "keyword": keyword,
                "action": "keyword_done",
                "status": "success",
                "message": f"同一关键词复用浏览器完成 {attempt_total} 次搜索；去重后点击 {len(clicked_shop_keys)} 个店铺/白名单",
            })
        except Exception as e:
            log(f"[{worker_name}] 关键词失败：{keyword} / {e}")
            append_log(cfg.output_log_file, {
                "time": datetime.now().isoformat(timespec="seconds"),
                "worker": worker_name,
                "keyword": keyword,
                "action": "keyword_error",
                "status": "failed",
                "message": repr(e),
            })
            append_log(cfg.output_log_file.replace(".csv", "_trace.csv"), {
                "time": datetime.now().isoformat(timespec="seconds"),
                "worker": worker_name,
                "keyword": keyword,
                "action": "traceback",
                "status": "failed",
                "message": traceback.format_exc(),
            })
        finally:
            try:
                if context and is_persistent_env(cfg):
                    save_search_environment_state(context, cfg, log, worker_name)
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass


def run_once(cfg: RunConfig, stop_flag: StopFlag, log: Callable[[str], None]) -> None:
    keywords = load_keywords(cfg.keywords_file)
    whitelist = load_whitelist(cfg.whitelist_file)
    blacklist = load_blacklist(getattr(cfg, "blacklist_file", "blacklist.csv"))
    clear_debug_artifacts_on_start(cfg, log)
    if blacklist:
        log(f"已读取黑名单 {len(blacklist)} 条；命中黑名单的候选绝不点击。")
    if cfg.stay_seconds_min > cfg.stay_seconds_max:
        raise ValueError("最短停留秒数不能大于最长停留秒数")
    if cfg.worker_count < 1:
        cfg.worker_count = 1
    if cfg.worker_count > 8:
        cfg.worker_count = 8

    ensure_parent(cfg.output_log_file)
    proxy_provider = ProxyProvider(proxy_config_from_run_config(cfg), log_func=log)
    if cfg.searches_per_keyword < 1:
        cfg.searches_per_keyword = 1

    progress = _load_task_progress(cfg, keywords)
    completed_attempts = progress.setdefault("completed_attempts", {})
    if not isinstance(completed_attempts, dict):
        completed_attempts = {}
        progress["completed_attempts"] = completed_attempts
    stats = {"clicked": int(progress.get("clicked_total", 0) or 0)}
    progress_lock = threading.Lock()

    def mark_attempt_done(done_keyword: str, attempt_no: int, cur_stats: Dict[str, int]) -> None:
        with progress_lock:
            done = progress.setdefault("completed_attempts", {})
            if isinstance(done, dict):
                done[done_keyword] = max(int(done.get(done_keyword, 0) or 0), int(attempt_no))
            progress["clicked_total"] = int((cur_stats or {}).get("clicked", stats.get("clicked", 0)) or 0)
            _save_task_progress(cfg, progress)
            log(f"[PROGRESS] 已保存进度：{done_keyword} 完成 {attempt_no}/{cfg.searches_per_keyword} 次搜索")

    total_searches = len(keywords) * cfg.searches_per_keyword
    already_done = sum(min(int(completed_attempts.get(k, 0) or 0), cfg.searches_per_keyword) for k in keywords)
    log(f"读取关键词 {len(keywords)} 个，每个关键词连续搜索 {cfg.searches_per_keyword} 次，共 {total_searches} 轮搜索；已完成 {already_done} 轮，将从未完成位置继续。")
    log(f"流程：先输入关键词并确认搜索结果页；命中目标板块并进入模块后，再增强扫描并点击模块内全部白名单；地区模拟={cfg.region_city or '未设置'}；点击全部白名单店铺={cfg.click_all_whitelist_in_target}")
    log("停止后再次开始会继续上次任务进度；需要从头开始请点击“重置任务进度”。")
    log(f"[STATS] clicked={stats['clicked']} 实时统计：点击白名单数量 {stats['clicked']} 次")

    if cfg.worker_count == 1:
        for idx, keyword in enumerate(keywords, start=1):
            if stop_flag.is_stopped():
                break
            try:
                stop_flag.checkpoint()
            except RuntimeError:
                break
            done = min(int(completed_attempts.get(keyword, 0) or 0), cfg.searches_per_keyword)
            if done >= cfg.searches_per_keyword:
                log(f"[T1-{idx}] 跳过已完成关键词：{keyword}（已完成 {done}/{cfg.searches_per_keyword} 次搜索）")
                continue
            try:
                run_keyword(keyword, cfg, whitelist, blacklist, proxy_provider, stop_flag, log, f"T1-{idx}", cfg.searches_per_keyword, stats, start_attempt=done + 1, progress_callback=mark_attempt_done)
            except Exception as e:
                log(f"[T1-{idx}] 关键词执行异常，已记录并继续后续任务：{e}")
    else:
        with ThreadPoolExecutor(max_workers=cfg.worker_count) as executor:
            futures = []
            for idx, keyword in enumerate(keywords, start=1):
                if stop_flag.is_stopped():
                    break
                done = min(int(completed_attempts.get(keyword, 0) or 0), cfg.searches_per_keyword)
                if done >= cfg.searches_per_keyword:
                    log(f"跳过已完成关键词：{keyword}（已完成 {done}/{cfg.searches_per_keyword} 次搜索）")
                    continue
                worker_name = f"T{((idx - 1) % cfg.worker_count) + 1}-{idx}"
                futures.append(executor.submit(run_keyword, keyword, cfg, whitelist, blacklist, proxy_provider, stop_flag, log, worker_name, cfg.searches_per_keyword, stats, done + 1, mark_attempt_done))
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    log(f"线程异常：{e}")

    completed_attempts = progress.get("completed_attempts", {})
    all_done = isinstance(completed_attempts, dict) and all(int(completed_attempts.get(k, 0) or 0) >= cfg.searches_per_keyword for k in keywords)
    if all_done:
        log("全部任务结束；进度已完整保存。")
    else:
        log("任务已停止或未全部完成；下次点击开始运行会从已保存进度继续。")
