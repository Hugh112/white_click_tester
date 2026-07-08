from __future__ import annotations

import json
import os
import queue
import sys
import threading
import shutil
import urllib.request
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# 打包为 exe 后，优先使用程序目录内置 Chromium；也支持免下载模式自动使用本机 Edge/Chrome。
if getattr(sys, "frozen", False):
    exe_dir = Path(sys.executable).resolve().parent
    browser_dir = exe_dir / "ms-playwright"
    if browser_dir.exists():
        os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browser_dir))

from engine import RunConfig, StopFlag, run_once, safe_load_json


APP_NAME = "白名单点击"
APP_VERSION = "1.0.0"
APP_TITLE = f"{APP_NAME} v{APP_VERSION}"
FIRST_RUN_MARKER = ".baimingdan_click_v1_first_run_cleaned"


DEFAULTS = {
    "start_url": "https://www.baidu.com/",
    "search_input_selector": "input[name='word'], input[name='wd'], textarea[name='word'], textarea[name='wd']",
    "search_submit_mode": "自动回车（推荐）",
    "search_button_selector": "",
    "force_search_submit": True,
    "result_extract_mode": "自动识别（推荐）",
    "result_item_selector": ".result, .c-result, .result-op, .ec-tuiguang, .waimai-card, .poi-card, article, section, div[role='listitem'], div[class*='item'], div[class*='card']",
    "result_link_selector": "a",
    "result_title_selector": "",
    "target_section_selector": "",
    "target_section_text": "百度本地生活|本地生活|精选服务|商家|相关商家|附近商家|服务商家",
    "target_source_selector": "span.cosc-source-text.cos-line-clamp-1, .cosc-source-text",
    "target_source_text": "百度本地生活",
    "merchant_div_selector": "div",
    "enter_target_page_selector": "",
    "enter_target_page_text": "查看更多|更多商家|查看全部",
    "target_page_wait_selector": "",
    "target_page_wait_seconds": "3",
    "target_required": True,
    "target_first_strict": True,
    "target_scroll_rounds": "8",
    "target_scroll_pixels": "850",
    "keywords_file": "keywords.csv",
    "searches_per_keyword": "1",
    "search_buffer_seconds": "3",
    "whitelist_file": "whitelist.csv",
    "blacklist_file": "blacklist.csv",
    "output_log_file": "output/run_log.csv",
    "progress_file": "output/task_progress.json",
    "stay_seconds_min": "2",
    "stay_seconds_max": "5",
    "max_clicks_per_keyword": "10",
    "page_timeout_ms": "30000",
    "result_wait_ms": "20000",
    "reset_mode": "new_browser_per_keyword",
    "browser_source": "自动免下载：优先本机 Edge/Chrome（推荐）",
    "browser_executable_path": "",
    "headless": True,
    "worker_count": "2",
    "viewport_width": "390",
    "viewport_height": "844",
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1",
    "mobile_mode": True,
    "environment_mode": "固定搜索环境：保留CK+地区定位（推荐）",
    "fixed_search_environment": True,
    "profile_name": "default",
    "region_city": "",
    "region_latitude": "",
    "region_longitude": "",
    "region_accuracy": "100",
    "region_timezone": "Asia/Shanghai",
    "region_locale": "zh-CN",
    "isolate_profile_by_region": True,
    "force_region_city_in_target_url": True,
    "enhanced_target_detection": True,
    "debug_enabled": False,
    "traffic_saving_mode": "标准省流量：拦截图片/视频/音频/字体（推荐）",
    "block_ads_and_trackers": True,
    "block_maps": True,
    "only_debug_on_failure": True,
    "debug_dir": "output/debug",
    "save_debug_html": True,
    "save_debug_screenshot": True,
    "clear_debug_on_start": True,
    "stop_after_all_whitelist_clicked": True,
    "click_all_whitelist_in_target": True,
    "fuzzy_threshold": "0.66",
    "min_fuzzy_chars": "3",
    "scan_scroll_rounds": "5",
    "scan_scroll_pixels": "900",
    "scroll_wait_ms": "900",
    "max_items_to_scan": "80",
    "verify_remaining_rounds": "3",
    "click_open_timeout_ms": "8000",
    "proxy_mode": "none",
    "proxy_list_file": "proxies.txt",
    "proxy_api_url": "",
    "fixed_proxy": "",
    "change_ip_url": "",
    "proxy_username": "",
    "proxy_password": "",
    "proxy_change_wait_seconds": "3",
    "update_url": "https://raw.githubusercontent.com/你的用户名/你的仓库/main/update.json",
}


class ScrollableFrame(ttk.Frame):
    """带纵向滚动条的配置页，避免小屏幕上底部按钮被内容挤没。"""
    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = ttk.Frame(self.canvas)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.window_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel, add="+")

    def _on_canvas_configure(self, event):
        try:
            self.canvas.itemconfigure(self.window_id, width=event.width)
        except Exception:
            pass

    def _on_mousewheel(self, event):
        try:
            widget = self.winfo_containing(event.x_root, event.y_root)
            if widget is not None and str(widget).startswith(str(self)):
                self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except Exception:
            pass


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1280x900")
        self.minsize(1100, 760)

        self.vars: dict[str, tk.StringVar] = {}
        self.bool_vars: dict[str, tk.BooleanVar] = {}
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_flag = StopFlag()
        self.clicked_count_var = tk.StringVar(value="实时点击白名单数量：0")
        self._first_run_cleanup()

        self._init_vars()
        self._build_ui()
        self.after(200, self._drain_log)


    def _first_run_cleanup(self) -> None:
        """新包首次打开时清理旧浏览器环境，避免沿用上个城市的 Cookie/LocalStorage/任务进度。"""
        marker = Path(FIRST_RUN_MARKER)
        if marker.exists():
            return
        for target in ["profiles", "output/debug"]:
            try:
                p = Path(target)
                if p.exists():
                    shutil.rmtree(p, ignore_errors=True)
            except Exception:
                pass
        for target in ["output/task_progress.json"]:
            try:
                p = Path(target)
                if p.exists():
                    p.unlink()
            except Exception:
                pass
        try:
            marker.write_text(APP_VERSION, encoding="utf-8")
        except Exception:
            pass

    def _region_fingerprint(self, cfg: RunConfig) -> str:
        return "|".join([
            str(getattr(cfg, "region_city", "") or "").strip(),
            str(getattr(cfg, "region_latitude", "") or "").strip(),
            str(getattr(cfg, "region_longitude", "") or "").strip(),
            str(getattr(cfg, "region_timezone", "") or "").strip(),
            str(getattr(cfg, "region_locale", "") or "").strip(),
        ])

    def _clean_state_if_region_changed(self, cfg: RunConfig) -> None:
        """地区变化时清空任务进度和浏览器状态，避免进入模块后仍出现旧城市。"""
        if not getattr(cfg, "isolate_profile_by_region", True):
            return
        state_dir = Path("output")
        state_dir.mkdir(parents=True, exist_ok=True)
        fp_path = state_dir / "region_fingerprint.json"
        current = self._region_fingerprint(cfg)
        old = ""
        try:
            if fp_path.exists():
                old = (safe_load_json(str(fp_path), default={}) or {}).get("fingerprint", "")
        except Exception:
            old = ""
        if old and old != current:
            try:
                if cfg.progress_file and Path(cfg.progress_file).exists():
                    Path(cfg.progress_file).unlink()
            except Exception:
                pass
            try:
                if Path("profiles").exists():
                    shutil.rmtree("profiles", ignore_errors=True)
            except Exception:
                pass
            self.clicked_count_var.set("实时点击白名单数量：0")
            self.log("检测到地区配置变化，已清理旧任务进度和浏览器环境。")
        try:
            fp_path.write_text(json.dumps({"fingerprint": current}, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    def _init_vars(self) -> None:
        for key, value in DEFAULTS.items():
            if isinstance(value, bool):
                self.bool_vars[key] = tk.BooleanVar(value=value)
            else:
                self.vars[key] = tk.StringVar(value=str(value))

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill=tk.BOTH, expand=True)

        title = ttk.Label(root, text="白名单点击｜v1.0", font=("Microsoft YaHei", 13, "bold"))
        title.pack(anchor="w", pady=(0, 3))
        top_stats = ttk.Frame(root)
        top_stats.pack(fill=tk.X, pady=(0, 8))
        ttk.Label(top_stats, textvariable=self.clicked_count_var, font=("Microsoft YaHei", 11, "bold"), foreground="#0a6").pack(side=tk.LEFT)

        nb = ttk.Notebook(root)
        nb.pack(fill=tk.BOTH, expand=True)

        basic = ttk.Frame(nb, padding=10)
        search_tab = ttk.Frame(nb, padding=10)
        target_tab = ttk.Frame(nb, padding=10)
        browser_tab = ttk.Frame(nb, padding=10)
        env_tab = ttk.Frame(nb, padding=10)
        click_tab = ttk.Frame(nb, padding=10)
        traffic_tab = ttk.Frame(nb, padding=10)
        proxy_tab = ttk.Frame(nb, padding=10)
        debug_tab = ttk.Frame(nb, padding=10)
        runlog = ttk.Frame(nb, padding=10)

        nb.add(basic, text="基础配置")
        nb.add(search_tab, text="搜索/结果")
        nb.add(target_tab, text="目标板块")
        nb.add(browser_tab, text="浏览器/并发")
        nb.add(env_tab, text="搜索环境/地区")
        nb.add(click_tab, text="点击/白名单")
        nb.add(traffic_tab, text="省流量")
        nb.add(proxy_tab, text="动态 IP")
        nb.add(debug_tab, text="调试判断")
        nb.add(runlog, text="运行日志")

        self._build_basic(basic)
        self._build_search_result(search_tab)
        self._build_target(target_tab)
        self._build_browser(browser_tab)
        self._build_environment(env_tab)
        self._build_click_whitelist(click_tab)
        self._build_traffic(traffic_tab)
        self._build_proxy(proxy_tab)
        self._build_debug(debug_tab)
        self._build_runlog(runlog)

        buttons = ttk.Frame(root)
        buttons.pack(fill=tk.X, pady=(8, 0))
        ttk.Button(buttons, text="保存配置", command=self.save_config).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="加载配置", command=self.load_config).pack(side=tk.LEFT, padx=4)
        ttk.Button(buttons, text="重置任务进度", command=self.reset_progress).pack(side=tk.LEFT, padx=12)
        ttk.Button(buttons, text="开始运行", command=self.start).pack(side=tk.RIGHT, padx=4)
        self.stop_btn = ttk.Button(buttons, text="停止", command=self.stop)
        self.stop_btn.pack(side=tk.RIGHT, padx=4)
        self.pause_btn = ttk.Button(buttons, text="暂停", command=self.pause_resume)
        self.pause_btn.pack(side=tk.RIGHT, padx=4)

    def row(self, parent, r: int, label: str, key: str, browse: str | None = None, width: int = 72):
        ttk.Label(parent, text=label).grid(row=r, column=0, sticky="w", padx=4, pady=5)
        ent = ttk.Entry(parent, textvariable=self.vars[key], width=width)
        ent.grid(row=r, column=1, sticky="we", padx=4, pady=5)
        if browse:
            cmd = (lambda k=key, b=browse: self.browse(k, b))
            ttk.Button(parent, text="选择", command=cmd).grid(row=r, column=2, sticky="w", padx=4, pady=5)
        parent.columnconfigure(1, weight=1)
        return ent

    def _build_basic(self, tab) -> None:
        ttk.Label(tab, text="基础任务文件", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        self.row(tab, 1, "搜索入口 URL", "start_url")
        self.row(tab, 2, "关键词表格（csv/xlsx/xls）", "keywords_file", browse="file")
        self.row(tab, 3, "白名单表格（csv/xlsx/xls）", "whitelist_file", browse="file")
        self.row(tab, 4, "黑名单表格（csv/xlsx/xls，可空，命中绝不点击）", "blacklist_file", browse="file")
        self.row(tab, 5, "输出日志 CSV", "output_log_file", browse="save")
        self.row(tab, 6, "任务进度文件", "progress_file", browse="save")
        ttk.Separator(tab, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        self.row(tab, 8, "每个关键词搜索次数", "searches_per_keyword", width=20)
        self.row(tab, 9, "搜索后缓冲等待秒数", "search_buffer_seconds", width=20)
        ttk.Label(tab, text="提示：停止后再次开始会读取任务进度文件继续执行；需要从头运行请点底部“重置任务进度”。", foreground="#555").grid(row=10, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_search_result(self, tab) -> None:
        ttk.Label(tab, text="搜索方式", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        ttk.Label(tab, text="搜索方式").grid(row=1, column=0, sticky="w", padx=4, pady=5)
        ttk.Combobox(
            tab,
            textvariable=self.vars["search_submit_mode"],
            values=["自动回车（推荐）", "点击百度搜索按钮", "手动填写按钮识别规则"],
            width=32,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 2, "搜索框识别规则（默认不用改）", "search_input_selector")
        self.row(tab, 3, "搜索按钮识别规则（高级，可空）", "search_button_selector")
        ttk.Checkbutton(tab, text="强制先提交搜索，再判断目标板块/白名单（推荐勾选）", variable=self.bool_vars["force_search_submit"]).grid(row=4, column=1, sticky="w", padx=4, pady=5)

        ttk.Separator(tab, orient=tk.HORIZONTAL).grid(row=5, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(tab, text="搜索结果识别", font=("Microsoft YaHei", 10, "bold")).grid(row=6, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        ttk.Label(tab, text="结果识别方式").grid(row=7, column=0, sticky="w", padx=4, pady=5)
        ttk.Combobox(
            tab,
            textvariable=self.vars["result_extract_mode"],
            values=["自动识别（推荐）", "百度/本地生活通用", "手动填写结果识别规则"],
            width=32,
            state="readonly",
        ).grid(row=7, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 8, "结果项识别规则（高级）", "result_item_selector")
        self.row(tab, 9, "可点击链接识别规则（高级，可空）", "result_link_selector")
        self.row(tab, 10, "标题识别规则（高级，可空）", "result_title_selector")
        ttk.Label(tab, text="普通用户只选“自动回车”和“自动识别”即可，不懂识别规则可以不改。", foreground="#555").grid(row=11, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_target(self, tab) -> None:
        ttk.Label(tab, text="目标板块/指定页面", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        self.row(tab, 1, "目标板块文字", "target_section_text")
        self.row(tab, 2, "进入页面按钮文字", "enter_target_page_text")
        self.row(tab, 3, "进入模块后等待秒数", "target_page_wait_seconds", width=20)
        self.row(tab, 4, "目标板块下滑轮数", "target_scroll_rounds", width=20)
        self.row(tab, 5, "目标板块每次下滑像素", "target_scroll_pixels", width=20)
        ttk.Checkbutton(tab, text="必须找到目标板块，否则跳过该关键词", variable=self.bool_vars["target_required"]).grid(row=6, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="严格目标板块优先：先进入目标板块，再判断/点击白名单（推荐勾选）", variable=self.bool_vars["target_first_strict"]).grid(row=7, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="进入目标模块后增强识别白名单：识别商家卡片结构、电话、地址、评分、距离、咨询按钮", variable=self.bool_vars["enhanced_target_detection"]).grid(row=8, column=1, sticky="w", padx=4, pady=5)

        ttk.Separator(tab, orient=tk.HORIZONTAL).grid(row=9, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(tab, text="高级识别规则，可不填", font=("Microsoft YaHei", 10, "bold")).grid(row=10, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        self.row(tab, 11, "目标来源标签规则（可改）", "target_source_selector")
        self.row(tab, 12, "目标来源标签文字（可改）", "target_source_text")
        self.row(tab, 13, "模块商家DIV规则（可改）", "merchant_div_selector")
        self.row(tab, 14, "目标板块识别规则（高级，可空）", "target_section_selector")
        self.row(tab, 15, "进入页面按钮识别规则（高级，可空）", "enter_target_page_selector")
        self.row(tab, 16, "进入后等待识别规则（高级，可空）", "target_page_wait_selector")
        ttk.Label(tab, text="目标模块规则可在前端配置：先识别来源标签，再进入模块页面，进入后按商家 DIV 文本匹配白名单/黑名单。", foreground="#555").grid(row=17, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_browser(self, tab) -> None:
        ttk.Label(tab, text="隐藏浏览器与并发", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=4, sticky="w", padx=4, pady=(0, 8))
        for i, (label, key) in enumerate([
            ("并发线程数", "worker_count"),
            ("页面超时 ms", "page_timeout_ms"),
            ("等待结果 ms", "result_wait_ms"),
            ("窗口宽度", "viewport_width"),
            ("窗口高度", "viewport_height"),
        ], start=1):
            self.row(tab, i, label, key, width=20)
        ttk.Checkbutton(tab, text="隐藏浏览器运行，不拉起浏览器窗口（推荐勾选）", variable=self.bool_vars["headless"]).grid(row=1, column=3, sticky="w", padx=8)
        ttk.Checkbutton(tab, text="模拟移动端环境", variable=self.bool_vars["mobile_mode"]).grid(row=2, column=3, sticky="w", padx=8)
        ttk.Label(tab, text="浏览器来源").grid(row=6, column=0, sticky="w", padx=4, pady=5)
        ttk.Combobox(
            tab,
            textvariable=self.vars["browser_source"],
            values=[
                "自动免下载：优先本机 Edge/Chrome（推荐）",
                "只用本机 Edge/Chrome（不下载）",
                "只用软件内置 Chromium（完整绿色版）",
                "手动选择浏览器路径",
            ],
            width=42,
            state="readonly",
        ).grid(row=6, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 7, "手动浏览器路径，可空", "browser_executable_path", browse="file", width=72)
        self.row(tab, 8, "User-Agent，可空", "user_agent", width=88)
        ttk.Label(tab, text="免下载模式会使用本机 Edge/Chrome 的内核新建独立隐藏进程；固定搜索环境开启时会保存/更新软件自己的 CK，不读取你的日常浏览器历史。", foreground="#555").grid(row=9, column=0, columnspan=4, sticky="w", padx=4, pady=10)

    def _build_environment(self, tab) -> None:
        ttk.Label(tab, text="搜索环境模式", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        ttk.Label(tab, text="搜索环境模式").grid(row=1, column=0, sticky="w", padx=4, pady=5)
        ttk.Combobox(
            tab,
            textvariable=self.vars["environment_mode"],
            values=[
                "固定搜索环境：保留CK+地区定位（推荐）",
                "全新干净环境：每个关键词新环境",
                "同关键词复用环境（推荐）",
                "本地增强模拟：地区/语言/定位",
                "持久环境：跨运行保留 Cookie（调试用）",
            ],
            width=42,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="启用固定搜索环境：固定移动端UA/窗口/语言，并保存更新CK（推荐）", variable=self.bool_vars["fixed_search_environment"]).grid(row=2, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="地区变化时自动隔离CK/Profile（推荐）", variable=self.bool_vars["isolate_profile_by_region"]).grid(row=3, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="进入百度商家模块后强制同步URL城市参数为模拟城市（推荐）", variable=self.bool_vars["force_region_city_in_target_url"]).grid(row=4, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 5, "固定环境名称/Profile", "profile_name", width=32)
        ttk.Label(tab, text="说明：修改城市/经纬度后会隔离本地环境，并在进入模块页后同步当前城市和当前关键词。", foreground="#555").grid(row=6, column=0, columnspan=3, sticky="w", padx=4, pady=8)

        ttk.Separator(tab, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(tab, text="地区模拟 / 定位", font=("Microsoft YaHei", 10, "bold")).grid(row=8, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        self.row(tab, 9, "模拟城市", "region_city", width=32)
        self.row(tab, 10, "纬度 latitude", "region_latitude", width=32)
        self.row(tab, 11, "经度 longitude", "region_longitude", width=32)
        self.row(tab, 12, "定位精度 accuracy", "region_accuracy", width=32)
        self.row(tab, 13, "时区 timezone", "region_timezone", width=32)
        self.row(tab, 14, "语言 locale", "region_locale", width=32)
        quick = ttk.Frame(tab)
        quick.grid(row=15, column=1, sticky="w", padx=4, pady=8)
        ttk.Button(quick, text="填入中山", command=lambda: self._set_region("中山", "22.5176", "113.3926")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(quick, text="填入北京", command=lambda: self._set_region("北京", "39.9042", "116.4074")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Button(quick, text="填入上海", command=lambda: self._set_region("上海", "31.2304", "121.4737")).pack(side=tk.LEFT, padx=(0, 6))
        ttk.Label(tab, text="动态 IP 出口地区最好和这里的城市一致；否则目标站点仍可能按代理出口城市展示结果。", foreground="#555").grid(row=16, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _set_region(self, city: str, lat: str, lon: str) -> None:
        self.vars["region_city"].set(city)
        self.vars["region_latitude"].set(lat)
        self.vars["region_longitude"].set(lon)
        self.vars["region_accuracy"].set("100")
        self.vars["region_timezone"].set("Asia/Shanghai")
        self.vars["region_locale"].set("zh-CN")
        self.vars["environment_mode"].set("固定搜索环境：保留CK+地区定位（推荐）")
        self.bool_vars["fixed_search_environment"].set(True)

    def _build_click_whitelist(self, tab) -> None:
        ttk.Label(tab, text="点击停留", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        for i, (label, key) in enumerate([
            ("最短停留秒数", "stay_seconds_min"),
            ("最长停留秒数", "stay_seconds_max"),
            ("每轮搜索最多点击白名单店铺数", "max_clicks_per_keyword"),
            ("点击打开超时 ms", "click_open_timeout_ms"),
        ], start=1):
            self.row(tab, i, label, key, width=20)
        ttk.Checkbutton(tab, text="进入目标模块后点击全部白名单店铺（每家只点一次，不受最多点击数限制）", variable=self.bool_vars["click_all_whitelist_in_target"]).grid(row=5, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="目标模块内白名单点完后立即结束当前关键词，减少代理流量", variable=self.bool_vars["stop_after_all_whitelist_clicked"]).grid(row=6, column=1, sticky="w", padx=4, pady=5)

        ttk.Separator(tab, orient=tk.HORIZONTAL).grid(row=7, column=0, columnspan=3, sticky="ew", pady=12)
        ttk.Label(tab, text="白名单模糊识别", font=("Microsoft YaHei", 10, "bold")).grid(row=8, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        for j, (label, key) in enumerate([
            ("模糊相似度阈值 0-1", "fuzzy_threshold"),
            ("短词保护，最少字符数", "min_fuzzy_chars"),
            ("最多扫描结果数", "max_items_to_scan"),
            ("剩余白名单复查轮数", "verify_remaining_rounds"),
            ("下滑加载轮数", "scan_scroll_rounds"),
            ("每次下滑像素", "scan_scroll_pixels"),
            ("下滑后等待 ms", "scroll_wait_ms"),
        ], start=9):
            self.row(tab, j, label, key, width=20)
        ttk.Label(tab, text="白名单支持模糊词：一行可写多个词，用 |、，、/ 分隔，例如：品牌词A|地址词B|机构简称C。", foreground="#555").grid(row=15, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_traffic(self, tab) -> None:
        ttk.Label(tab, text="动态 IP 省流量模式", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        ttk.Label(tab, text="省流量等级").grid(row=1, column=0, sticky="w", padx=4, pady=5)
        ttk.Combobox(
            tab,
            textvariable=self.vars["traffic_saving_mode"],
            values=[
                "关闭省流量：正常加载",
                "标准省流量：拦截图片/视频/音频/字体（推荐）",
                "强力省流量：再拦截广告/统计/地图资源",
                "极限省流量：只保留文档/脚本/XHR，可能影响页面",
            ],
            width=48,
            state="readonly",
        ).grid(row=1, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="拦截广告、统计、埋点请求", variable=self.bool_vars["block_ads_and_trackers"]).grid(row=2, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="拦截地图瓦片/地图图片资源", variable=self.bool_vars["block_maps"]).grid(row=3, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="只在失败时保存调试截图/HTML，成功时不保存", variable=self.bool_vars["only_debug_on_failure"]).grid(row=4, column=1, sticky="w", padx=4, pady=5)
        ttk.Label(tab, text="建议动态 IP 正式运行使用“标准省流量”，先不要选极限；极限模式可能导致页面布局异常。", foreground="#555").grid(row=5, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_debug(self, tab) -> None:
        ttk.Label(tab, text="调试结果判断", font=("Microsoft YaHei", 10, "bold")).grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=(0, 8))
        ttk.Checkbutton(tab, text="启用调试判断：找不到目标板块/白名单时保存截图和页面HTML", variable=self.bool_vars["debug_enabled"]).grid(row=1, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 2, "调试输出目录", "debug_dir", browse="dir", width=72)
        ttk.Checkbutton(tab, text="保存页面 HTML", variable=self.bool_vars["save_debug_html"]).grid(row=3, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="保存页面截图", variable=self.bool_vars["save_debug_screenshot"]).grid(row=4, column=1, sticky="w", padx=4, pady=5)
        ttk.Checkbutton(tab, text="每次开始运行前清空上次 debug 截图/HTML（推荐）", variable=self.bool_vars["clear_debug_on_start"]).grid(row=5, column=1, sticky="w", padx=4, pady=5)
        ttk.Label(tab, text="日志会判断：页面没有返回目标板块时直接按未找到处理；增强识别只用于进入模块后的白名单扫描。", foreground="#555").grid(row=5, column=0, columnspan=3, sticky="w", padx=4, pady=10)

    def _build_proxy(self, tab) -> None:
        ttk.Label(tab, text="更换搜索词时会为该关键词新建全新隐藏浏览器环境，并按所选模式获取/切换代理。", foreground="#555").grid(row=0, column=0, columnspan=3, sticky="w", padx=4, pady=6)
        ttk.Label(tab, text="代理模式").grid(row=1, column=0, sticky="w", padx=4, pady=5)
        combo = ttk.Combobox(tab, textvariable=self.vars["proxy_mode"], values=["none", "list", "api", "fixed_with_change_url"], width=28, state="readonly")
        combo.grid(row=1, column=1, sticky="w", padx=4, pady=5)
        self.row(tab, 2, "代理列表文件", "proxy_list_file", browse="file")
        self.row(tab, 3, "代理 API URL", "proxy_api_url")
        self.row(tab, 4, "固定代理网关", "fixed_proxy")
        self.row(tab, 5, "换 IP 接口 URL", "change_ip_url")
        self.row(tab, 6, "代理用户名（账号密码验证时填写）", "proxy_username", width=36)
        self.row(tab, 7, "代理密码（账号密码验证时填写）", "proxy_password", width=36)
        self.row(tab, 8, "换 IP 后等待秒数", "proxy_change_wait_seconds", width=20)
        ttk.Label(tab, text="如果浏览器弹出“代理需要用户名和密码”，说明 API 只返回 ip:port 但供应商要求账号密码验证；在这里填用户名/密码，或改用供应商白名单验证。", foreground="#a60").grid(row=9, column=0, columnspan=3, sticky="w", padx=4, pady=6)
        ttk.Label(tab, text="多线程时：list/api 模式每个关键词会拿独立代理；fixed_with_change_url 模式会加锁依次调用换 IP 接口。", foreground="#555").grid(row=10, column=0, columnspan=3, sticky="w", padx=4, pady=6)

    def _build_runlog(self, tab) -> None:
        stats_bar = ttk.Frame(tab)
        stats_bar.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(stats_bar, textvariable=self.clicked_count_var, font=("Microsoft YaHei", 11, "bold"), foreground="#0a6").pack(side=tk.LEFT)
        self.log_text = tk.Text(tab, height=28, wrap="word")
        self.log_text.pack(fill=tk.BOTH, expand=True)
        self.log(f"程序已启动：{APP_TITLE}")

    def browse(self, key: str, mode: str) -> None:
        if mode == "file":
            path = filedialog.askopenfilename(filetypes=[("表格文件", "*.csv *.xlsx *.xls *.xlsm *.txt *.tsv *.json"), ("CSV", "*.csv"), ("Excel", "*.xlsx *.xls *.xlsm"), ("All", "*.*")])
        elif mode == "save":
            path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        else:
            path = filedialog.askdirectory()
        if path:
            self.vars[key].set(path)

    def log(self, msg: str) -> None:
        self.log_queue.put(str(msg))

    def _drain_log(self) -> None:
        try:
            while True:
                msg = self.log_queue.get_nowait()
                if "[STATS] clicked=" in msg:
                    try:
                        n = msg.split("[STATS] clicked=", 1)[1].split()[0]
                        self.clicked_count_var.set(f"实时点击白名单数量：{n}")
                    except Exception:
                        pass
                self.log_text.insert(tk.END, msg + "\n")
                self.log_text.see(tk.END)
        except queue.Empty:
            pass
        self.after(200, self._drain_log)

    def _int(self, key: str) -> int:
        return int(self.vars[key].get().strip())

    def _float(self, key: str) -> float:
        return float(self.vars[key].get().strip())

    def _resolve_search_button_selector(self) -> str:
        mode = self.vars.get("search_submit_mode").get().strip() if "search_submit_mode" in self.vars else "自动回车（推荐）"
        custom = self.vars["search_button_selector"].get().strip()
        if mode.startswith("自动回车"):
            return ""
        if mode.startswith("点击百度"):
            return "#index-bn, #su, button:has-text('百度一下'), button:has-text('搜索'), input[value='百度一下'], input[value='搜索']"
        return custom

    def _resolve_result_item_selector(self) -> str:
        mode = self.vars.get("result_extract_mode").get().strip() if "result_extract_mode" in self.vars else "自动识别（推荐）"
        custom = self.vars["result_item_selector"].get().strip()
        if mode.startswith("手动"):
            return custom
        if mode.startswith("百度"):
            return ".result, .c-result, .result-op, .ec-tuiguang, .waimai-card, .poi-card, [class*='poi'], [class*='shop'], [class*='card'], [class*='item'], article, section"
        # 自动识别：尽量覆盖百度普通结果、本地生活卡片、移动端卡片，不需要用户理解代码。
        return custom or ".result, .c-result, .result-op, .ec-tuiguang, .waimai-card, .poi-card, [role='listitem'], [class*='result'], [class*='card'], [class*='item'], article, section"

    def collect(self) -> RunConfig:
        return RunConfig(
            start_url=self.vars["start_url"].get().strip(),
            search_input_selector=self.vars["search_input_selector"].get().strip(),
            search_button_selector=self._resolve_search_button_selector(),
            result_item_selector=self._resolve_result_item_selector(),
            result_link_selector=self.vars["result_link_selector"].get().strip(),
            result_title_selector=self.vars["result_title_selector"].get().strip(),
            target_section_selector=self.vars["target_section_selector"].get().strip(),
            target_section_text=self.vars["target_section_text"].get().strip(),
            target_source_selector=self.vars["target_source_selector"].get().strip(),
            target_source_text=self.vars["target_source_text"].get().strip(),
            merchant_div_selector=self.vars["merchant_div_selector"].get().strip(),
            enter_target_page_selector=self.vars["enter_target_page_selector"].get().strip(),
            enter_target_page_text=self.vars["enter_target_page_text"].get().strip(),
            target_page_wait_selector=self.vars["target_page_wait_selector"].get().strip(),
            target_page_wait_seconds=self._float("target_page_wait_seconds"),
            target_required=self.bool_vars["target_required"].get(),
            target_first_strict=self.bool_vars["target_first_strict"].get(),
            target_scroll_rounds=self._int("target_scroll_rounds"),
            target_scroll_pixels=self._int("target_scroll_pixels"),
            keywords_file=self.vars["keywords_file"].get().strip(),
            whitelist_file=self.vars["whitelist_file"].get().strip(),
            blacklist_file=self.vars["blacklist_file"].get().strip(),
            output_log_file=self.vars["output_log_file"].get().strip(),
            progress_file=self.vars["progress_file"].get().strip(),
            searches_per_keyword=self._int("searches_per_keyword"),
            search_buffer_seconds=self._float("search_buffer_seconds"),
            stay_seconds_min=self._int("stay_seconds_min"),
            stay_seconds_max=self._int("stay_seconds_max"),
            max_clicks_per_keyword=self._int("max_clicks_per_keyword"),
            page_timeout_ms=self._int("page_timeout_ms"),
            result_wait_ms=self._int("result_wait_ms"),
            force_search_submit=self.bool_vars["force_search_submit"].get(),
            reset_mode=self.vars["reset_mode"].get().strip(),
            browser_source=self.vars["browser_source"].get().strip(),
            browser_executable_path=self.vars["browser_executable_path"].get().strip(),
            headless=self.bool_vars["headless"].get(),
            worker_count=self._int("worker_count"),
            viewport_width=self._int("viewport_width"),
            viewport_height=self._int("viewport_height"),
            user_agent=self.vars["user_agent"].get().strip(),
            mobile_mode=self.bool_vars["mobile_mode"].get(),
            environment_mode=self.vars["environment_mode"].get().strip(),
            fixed_search_environment=self.bool_vars["fixed_search_environment"].get(),
            profile_name=self.vars["profile_name"].get().strip(),
            region_city=self.vars["region_city"].get().strip(),
            region_latitude=self.vars["region_latitude"].get().strip(),
            region_longitude=self.vars["region_longitude"].get().strip(),
            region_accuracy=self.vars["region_accuracy"].get().strip(),
            region_timezone=self.vars["region_timezone"].get().strip(),
            region_locale=self.vars["region_locale"].get().strip(),
            isolate_profile_by_region=self.bool_vars["isolate_profile_by_region"].get(),
            force_region_city_in_target_url=self.bool_vars["force_region_city_in_target_url"].get(),
            enhanced_target_detection=self.bool_vars["enhanced_target_detection"].get(),
            debug_enabled=self.bool_vars["debug_enabled"].get(),
            debug_dir=self.vars["debug_dir"].get().strip(),
            save_debug_html=self.bool_vars["save_debug_html"].get(),
            save_debug_screenshot=self.bool_vars["save_debug_screenshot"].get(),
            clear_debug_on_start=self.bool_vars["clear_debug_on_start"].get(),
            traffic_saving_mode=self.vars["traffic_saving_mode"].get().strip(),
            block_ads_and_trackers=self.bool_vars["block_ads_and_trackers"].get(),
            block_maps=self.bool_vars["block_maps"].get(),
            only_debug_on_failure=self.bool_vars["only_debug_on_failure"].get(),
            stop_after_all_whitelist_clicked=self.bool_vars["stop_after_all_whitelist_clicked"].get(),
            click_all_whitelist_in_target=self.bool_vars["click_all_whitelist_in_target"].get(),
            fuzzy_threshold=self._float("fuzzy_threshold"),
            min_fuzzy_chars=self._int("min_fuzzy_chars"),
            scan_scroll_rounds=self._int("scan_scroll_rounds"),
            scan_scroll_pixels=self._int("scan_scroll_pixels"),
            scroll_wait_ms=self._int("scroll_wait_ms"),
            max_items_to_scan=self._int("max_items_to_scan"),
            verify_remaining_rounds=self._int("verify_remaining_rounds"),
            click_open_timeout_ms=self._int("click_open_timeout_ms"),
            proxy_mode=self.vars["proxy_mode"].get().strip(),
            proxy_list_file=self.vars["proxy_list_file"].get().strip(),
            proxy_api_url=self.vars["proxy_api_url"].get().strip(),
            fixed_proxy=self.vars["fixed_proxy"].get().strip(),
            change_ip_url=self.vars["change_ip_url"].get().strip(),
            proxy_username=self.vars["proxy_username"].get().strip(),
            proxy_password=self.vars["proxy_password"].get().strip(),
            proxy_change_wait_seconds=self._int("proxy_change_wait_seconds"),
        )

    def save_config(self) -> None:
        try:
            self.validate(self.collect())
        except Exception as e:
            messagebox.showerror("配置错误", str(e))
            return
        cfg = {}
        for k, var in self.vars.items():
            cfg[k] = var.get()
        for k, var in self.bool_vars.items():
            cfg[k] = bool(var.get())
        path = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON", "*.json")])
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        messagebox.showinfo("保存成功", path)

    def load_config(self) -> None:
        path = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if not path:
            return
        data = safe_load_json(path, default={}) or {}
        for k, v in data.items():
            if k in self.vars:
                self.vars[k].set(str(v))
            if k in self.bool_vars:
                self.bool_vars[k].set(bool(v))

    def validate(self, cfg: RunConfig) -> None:
        if cfg.stay_seconds_min > cfg.stay_seconds_max:
            raise ValueError("最短停留秒数不能大于最长停留秒数")
        if not cfg.start_url or not cfg.search_input_selector or not cfg.result_item_selector:
            raise ValueError("搜索入口、搜索框选择器、结果项选择器不能为空")
        if not os.path.exists(cfg.keywords_file):
            raise ValueError("关键词表格（csv/xlsx/xls） 不存在")
        if not os.path.exists(cfg.whitelist_file):
            raise ValueError("白名单表格（csv/xlsx/xls） 不存在")
        if not 0 < cfg.fuzzy_threshold <= 1:
            raise ValueError("模糊相似度阈值必须在 0-1 之间")
        if not cfg.progress_file:
            raise ValueError("任务进度文件不能为空")
        if cfg.searches_per_keyword < 1:
            raise ValueError("每个关键词搜索次数不能小于 1")
        if cfg.searches_per_keyword > 50:
            raise ValueError("每个关键词搜索次数最多建议 50，避免长时间无响应")
        if cfg.search_buffer_seconds < 0:
            raise ValueError("搜索后缓冲等待秒数不能小于 0")
        if cfg.worker_count < 1:
            raise ValueError("并发线程数不能小于 1")
        if cfg.worker_count > 8:
            raise ValueError("并发线程数最多建议 8；如需更多请先评估代理和目标页面承载")

    def reset_progress(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("正在运行", "请先停止当前任务，再重置任务进度")
            return
        path = self.vars.get("progress_file").get().strip() if "progress_file" in self.vars else "output/task_progress.json"
        if not path:
            messagebox.showwarning("未设置", "任务进度文件为空")
            return
        if not messagebox.askyesno("确认重置", "确定要清空任务进度吗？下次开始会从第一个关键词重新运行。"):
            return
        try:
            if os.path.exists(path):
                os.remove(path)
            self.clicked_count_var.set("实时点击白名单数量：0")
            self.log(f"已重置任务进度：{path}")
        except Exception as e:
            messagebox.showerror("重置失败", str(e))

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showwarning("正在运行", "任务已经在运行中")
            return
        try:
            cfg = self.collect()
            self.validate(cfg)
        except Exception as e:
            messagebox.showerror("配置错误", str(e))
            return

        self._clean_state_if_region_changed(cfg)
        self.stop_flag = StopFlag()
        try:
            n = 0
            if cfg.progress_file and os.path.exists(cfg.progress_file):
                saved = safe_load_json(cfg.progress_file, default={}) or {}
                n = int(saved.get("clicked_total", 0) or 0)
            self.clicked_count_var.set(f"实时点击白名单数量：{n}")
        except Exception:
            self.clicked_count_var.set("实时点击白名单数量：0")
        self.worker = threading.Thread(target=self._run_worker, args=(cfg,), daemon=True)
        self.worker.start()
        self.log("任务已启动：白名单点击 v1.0")

    def _run_worker(self, cfg: RunConfig) -> None:
        try:
            run_once(cfg, self.stop_flag, self.log)
        except Exception as e:
            import traceback
            self.log(f"程序异常：{type(e).__name__}: {e}")
            self.log(traceback.format_exc(limit=6))
            self.log("提示：已拦截异常弹窗；请根据上方 traceback 定位具体文件/步骤。")
        finally:
            try:
                self.pause_btn.config(text="暂停")
            except Exception:
                pass

    def pause_resume(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            self.log("当前没有正在运行的任务")
            return
        if self.stop_flag.is_paused():
            self.stop_flag.resume()
            self.pause_btn.config(text="暂停")
            self.log("已继续运行")
        else:
            self.stop_flag.pause()
            self.pause_btn.config(text="继续")
            self.log("已暂停：当前浏览器操作结束后会停在暂停点")

    def stop(self) -> None:
        self.stop_flag.stop()
        try:
            self.pause_btn.config(text="暂停")
        except Exception:
            pass
        self.log("收到停止指令：正在中断任务，最多等待当前浏览器动作结束")


if __name__ == "__main__":
    App().mainloop()
