"""动态 IP / 代理接入模块。

支持模式：
1) none: 不使用代理
2) list: 从 proxies.txt 逐个轮换
3) api: 每个关键词调用一次供应商 API 获取代理
4) fixed_with_change_url: 每个关键词先调用换 IP 接口，再使用固定代理网关

代理格式支持：
- http://host:port
- http://user:pass@host:port
- socks5://host:port
- host:port  默认按 http://host:port 处理
"""
from __future__ import annotations

import itertools
import re
from pathlib import Path
import threading
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional
from urllib.parse import urlparse

import requests
from requests import RequestException


@dataclass
class ProxyConfig:
    mode: str = "none"
    proxy_list_file: str = "proxies.txt"
    proxy_api_url: str = ""
    fixed_proxy: str = ""
    change_ip_url: str = ""
    # 当代理 API 只返回 ip:port，但代理商采用“账号密码验证”时，
    # 在软件里填写这里的账号/密码，Playwright 会随代理一起传入，避免弹出浏览器登录框。
    proxy_username: str = ""
    proxy_password: str = ""
    request_timeout: int = 20
    retry_count: int = 3
    retry_sleep_seconds: float = 1.5
    change_wait_seconds: int = 3


def safe_decode_bytes(raw: bytes) -> str:
    if raw is None:
        return ""
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        for enc in ("utf-16", "utf-16-le", "utf-16-be"):
            try:
                return raw.decode(enc)
            except UnicodeDecodeError:
                pass
    for enc in ("utf-8-sig", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "gb18030", "gbk"):
        try:
            text = raw.decode(enc)
            if text and text.count("\x00") > max(2, len(text) // 10):
                continue
            return text
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def safe_read_lines(path: str) -> List[str]:
    text = safe_decode_bytes(Path(path).read_bytes())
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


def parse_proxy(proxy_text: str, default_username: str = "", default_password: str = "") -> Optional[Dict[str, str]]:
    text = (proxy_text or "").strip()
    if not text:
        return None
    if "://" not in text:
        text = "http://" + text

    parsed = urlparse(text)
    if not parsed.hostname or not parsed.port:
        raise ValueError(f"代理格式不正确：{proxy_text}")

    server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
    result: Dict[str, str] = {"server": server}
    username = parsed.username or (default_username or "").strip()
    password = parsed.password or (default_password or "").strip()
    if username:
        result["username"] = username
    if password:
        result["password"] = password
    return result


def extract_proxy_from_text(text: str) -> str:
    body = (text or "").strip()
    if not body:
        raise ValueError("代理 API 返回为空")

    match = re.search(r"((?:https?|socks5)://[^\s\"',]+)", body, re.I)
    if match:
        return match.group(1)

    match = re.search(r"([A-Za-z0-9._~%+-]+:[^@\s]+@)?((?:\d{1,3}\.){3}\d{1,3}|[A-Za-z0-9.-]+):(\d{2,5})", body)
    if match:
        return match.group(0)

    first_line = body.splitlines()[0].strip()
    if ":" in first_line:
        return first_line

    raise ValueError("无法从代理 API 返回内容中识别代理地址")


class ProxyProvider:
    def __init__(self, cfg: ProxyConfig, log_func=print) -> None:
        self.cfg = cfg
        self.log = log_func
        self._cycle: Optional[Iterable[str]] = None
        self._lock = threading.Lock()
        if cfg.mode == "list":
            proxies = self._load_proxy_list(cfg.proxy_list_file)
            self._cycle = itertools.cycle(proxies)

    def _load_proxy_list(self, path: str) -> List[str]:
        rows = safe_read_lines(path)
        if not rows:
            raise ValueError("代理列表为空")
        return rows

    def _http_get_with_retry(self, url: str, label: str) -> requests.Response:
        last_err = None
        attempts = max(int(getattr(self.cfg, "retry_count", 3) or 3), 1)
        for i in range(1, attempts + 1):
            try:
                resp = requests.get(url, timeout=self.cfg.request_timeout)
                resp.raise_for_status()
                return resp
            except RequestException as e:
                last_err = e
                self.log(f"{label}失败：第 {i}/{attempts} 次，{e}")
                if i < attempts:
                    time.sleep(float(getattr(self.cfg, "retry_sleep_seconds", 1.5) or 1.5))
        raise RuntimeError(f"{label}失败，已重试 {attempts} 次：{last_err}")

    def next_proxy(self) -> Optional[Dict[str, str]]:
        mode = (self.cfg.mode or "none").strip()
        if mode == "none":
            return None

        # 多线程时每个关键词仍会单独获取/切换一次代理；这里加锁避免代理池下标冲突。
        with self._lock:
            if mode == "list":
                assert self._cycle is not None
                proxy_text = next(self._cycle)
                self.log(f"使用代理列表节点：{mask_proxy(proxy_text)}")
                return parse_proxy(proxy_text, self.cfg.proxy_username, self.cfg.proxy_password)

            if mode == "api":
                if not self.cfg.proxy_api_url:
                    raise ValueError("proxy_api_url 不能为空")
                resp = self._http_get_with_retry(self.cfg.proxy_api_url, "代理 API 获取")
                proxy_text = extract_proxy_from_text(safe_decode_bytes(resp.content))
                self.log(f"代理 API 获取节点：{mask_proxy(proxy_text)}")
                if self.cfg.proxy_username and "@" not in proxy_text:
                    self.log("代理 API 返回 ip:port，已使用软件中填写的代理账号/密码。")
                if self.cfg.change_wait_seconds > 0:
                    time.sleep(self.cfg.change_wait_seconds)
                return parse_proxy(proxy_text, self.cfg.proxy_username, self.cfg.proxy_password)

            if mode == "fixed_with_change_url":
                if not self.cfg.fixed_proxy:
                    raise ValueError("fixed_proxy 不能为空")
                if self.cfg.change_ip_url:
                    self.log("调用供应商换 IP 地址接口...")
                    resp = self._http_get_with_retry(self.cfg.change_ip_url, "换 IP 接口调用")
                    if self.cfg.change_wait_seconds > 0:
                        time.sleep(self.cfg.change_wait_seconds)
                self.log(f"使用固定代理网关：{mask_proxy(self.cfg.fixed_proxy)}")
                return parse_proxy(self.cfg.fixed_proxy, self.cfg.proxy_username, self.cfg.proxy_password)

        raise ValueError(f"未知代理模式：{mode}")


def mask_proxy(proxy_text: str) -> str:
    if not proxy_text:
        return ""
    text = re.sub(r"//([^:/@]+):([^@]+)@", r"//***:***@", proxy_text)
    text = re.sub(r"([?&](?:key|token|secret|password|pwd)=)[^&]+", r"\1***", text, flags=re.I)
    return text
