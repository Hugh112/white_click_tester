from __future__ import annotations

import json
import sys
import urllib.request
import webbrowser
from pathlib import Path

APP_VERSION = "1.0.0"
CONFIG_FILE = "更新配置.json"


def parse_ver(v: str):
    nums = []
    for part in str(v).strip().lstrip("vV").split("."):
        nums.append(int("".join(ch for ch in part if ch.isdigit()) or "0"))
    return tuple((nums + [0, 0, 0])[:3])


def main() -> int:
    cfg_path = Path(CONFIG_FILE)
    if not cfg_path.exists():
        print(f"缺少 {CONFIG_FILE}")
        return 1
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    url = str(cfg.get("update_url", "")).strip()
    if not url or "你的用户名" in url or "你的仓库" in url:
        print("请先在 更新配置.json 里填写你的 GitHub raw update.json 地址。")
        return 1
    print("正在检查更新...")
    with urllib.request.urlopen(url, timeout=15) as resp:
        info = json.loads(resp.read().decode("utf-8-sig"))
    remote_version = str(info.get("version", "")).strip()
    download_url = str(info.get("download_url", "")).strip()
    notes = str(info.get("notes", "")).strip()
    if not remote_version:
        print("update.json 缺少 version")
        return 1
    print(f"当前版本：{APP_VERSION}")
    print(f"线上版本：{remote_version}")
    if parse_ver(remote_version) <= parse_ver(APP_VERSION):
        print("已经是最新版本。")
        return 0
    print("发现新版本。")
    if notes:
        print("更新说明：")
        print(notes)
    if download_url:
        print("正在打开下载地址...")
        webbrowser.open(download_url)
    else:
        print("update.json 缺少 download_url")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
