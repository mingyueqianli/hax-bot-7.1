from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

from app import config
from app.storage import atomic_write_json, atomic_write_text

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)

TOTAL_KEYWORDS = (
    "online vps",
    "number of vps online",
    "total",
    "vps online",
    "在线vps",
    "在线 vps",
    "在线服务器",
    "总数",
    "全部",
)


def _parse_int(value: str) -> int | None:
    match = re.search(r"\d[\d,\.]*", value or "")
    if not match:
        return None
    try:
        return int(match.group(0).replace(",", "").split(".")[0])
    except ValueError:
        return None


def _clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name or "").strip()
    return name.replace("./", "").strip(" :-—|")


def _is_total_name(name: str) -> bool:
    low = name.lower().replace("\u3000", " ")
    return any(k in low for k in TOTAL_KEYWORDS)


IGNORE_CENTER_NAMES = {
    "server statistics",
    "statistics",
    "stats",
}


def _is_ignored_center_name(name: str) -> bool:
    return name.lower().replace("\u3000", " ").strip() in IGNORE_CENTER_NAMES


def _extract_from_cards(soup: BeautifulSoup) -> tuple[dict[str, int], int | None]:
    centers: dict[str, int] = {}
    total: int | None = None

    # HAX 页面常见结构是 card + h5 + h1；这里放宽匹配，避免网页 class 微调后失效。
    candidates = soup.find_all(
        lambda tag: tag.name in {"div", "section", "article"}
        and tag.get_text(" ", strip=True)
        and ("card" in " ".join(tag.get("class", [])).lower() or tag.find(["h1", "h2", "h3", "h4", "h5"]))
    )

    seen: set[tuple[str, int]] = set()
    for card in candidates:
        title_tag = card.find(["h5", "h4", "h3", "p", "span"])
        number_tag = card.find(["h1", "h2", "strong"])
        if not title_tag or not number_tag:
            continue
        name = _clean_name(title_tag.get_text(" ", strip=True))
        count = _parse_int(number_tag.get_text(" ", strip=True))
        if not name or count is None:
            continue
        key = (name, count)
        if key in seen:
            continue
        seen.add(key)
        if _is_total_name(name):
            total = count
        elif _is_ignored_center_name(name):
            continue
        elif len(name) <= 80:
            centers[name] = count

    return centers, total


def _extract_from_text(soup: BeautifulSoup) -> tuple[dict[str, int], int | None]:
    centers: dict[str, int] = {}
    total: int | None = None
    text = soup.get_text("\n", strip=True)
    for line in text.splitlines():
        line = _clean_name(line)
        if not line:
            continue
        count = _parse_int(line)
        if count is None:
            continue
        # 常见文本格式：Location Name 12 / 数据中心: xxx VPS 数量: 12
        if any(word in line.lower() for word in ("vps", "数据中心", "dc", "server")):
            name = re.sub(r"\d[\d,\.]*", "", line)
            name = re.sub(r"(vps|数量|数据中心|servers?|online|number of)", "", name, flags=re.I)
            name = _clean_name(name)
            if not name:
                continue
            if _is_total_name(line):
                total = count
            elif _is_ignored_center_name(name):
                continue
            elif len(name) <= 80:
                centers.setdefault(name, count)
    return centers, total


def fetch_snapshot(url: str | None = None, timeout: int | None = None) -> dict[str, Any] | None:
    url = url or config.HAX_DATA_CENTER_URL
    timeout = timeout or config.REQUEST_TIMEOUT_SECONDS
    headers = {"User-Agent": USER_AGENT}

    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "lxml")

    centers, total = _extract_from_cards(soup)
    if not centers:
        centers, total = _extract_from_text(soup)

    # 如果页面不提供总数，就用数据中心明细求和，保证监控可用。
    if total is None and centers:
        total = sum(centers.values())

    if not centers and not total:
        logger.warning("未解析到有效 HAX 数据中心数据")
        return None

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    snapshot: dict[str, Any] = {
        "updated_at": updated_at,
        "url": url,
        "total": int(total or 0),
        "centers": dict(sorted(centers.items(), key=lambda item: item[0].lower())),
    }
    snapshot["lines"] = format_snapshot_lines(snapshot)
    return snapshot


def format_snapshot_lines(snapshot: dict[str, Any]) -> list[str]:
    lines = [f"--- HAX.CO.ID 数据中心状态 (更新于: {snapshot.get('updated_at', '')}) ---"]
    total = int(snapshot.get("total") or 0)
    if total:
        lines.append(f"📊 在线VPS总数: {total}")
    centers = snapshot.get("centers") or {}
    for name, count in centers.items():
        lines.append(f"✅ 数据中心: {name}, VPS 数量: {count}")
    return lines


def save_snapshot(snapshot: dict[str, Any]) -> None:
    config.ensure_runtime_dirs()
    lines = snapshot.get("lines") or format_snapshot_lines(snapshot)
    text = "\n".join(lines) + "\n"
    atomic_write_json(config.SNAPSHOT_JSON_FILE, snapshot)
    atomic_write_text(config.SNAPSHOT_TEXT_FILE, text)
    # 兼容旧版 bot.py 读取的 test.txt。
    atomic_write_text(config.LEGACY_TEST_FILE, text)
