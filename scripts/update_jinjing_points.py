#!/usr/bin/env python3
"""Refresh Jinjing camera points JSON from jinjing365.com."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path


HERE = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = HERE / "jinjing365_points.json"
SOURCE_URL = "https://www.jinjing365.com/index.asp?sessionid="
COMMENT_URL = "https://www.jinjing365.com/plug/comment/commentList.asp"
SIXTH_RING_MARKERS = (
    "六环",
    "卧龙岗",
    "西王佐南桥",
    "王佐南桥",
)


def fetch(url: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def post_fetch(url: str) -> str:
    request = urllib.request.Request(url, data=b"", headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def unquote_js(value: str) -> str:
    return value.replace("\\'", "'").replace("\\\\", "\\")


def clean_text(value: str) -> str:
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", html.unescape(value).replace("\xa0", " ")).strip()


def parse_points(html: str) -> list[dict]:
    labels = re.search(r"var\s+LabelsData\s*=\s*\[(.*?)\];", html, re.S)
    if not labels:
        raise SystemExit("LabelsData not found in source page")

    points = []
    expected = 0
    for idx, block in enumerate(re.findall(r"\{(.*?)\}", labels.group(1), re.S), 1):
        pos = re.search(r"position:\s*\[\s*([0-9.\-]+)\s*,\s*([0-9.\-]+)\s*\]", block)
        if not pos:
            continue
        expected += 1
        name = re.search(r"name:\s*'((?:\\'|[^'])*)'", block)
        category = re.search(r"aa:\s*'((?:\\'|[^'])*)'", block)
        date = re.search(r"time:\s*'((?:\\'|[^'])*)'", block)
        edit_date = re.search(r"edittime:\s*'((?:\\'|[^'])*)'", block)
        href = re.search(r"href:\s*'((?:\\'|[^'])*)'", block)
        rel_url = unquote_js(href.group(1)) if href else ""
        id_match = re.search(r"\?(\d+)\.html$", rel_url)
        points.append(
            {
                "id": int(id_match.group(1)) if id_match else idx,
                "name": unquote_js(name.group(1)) if name else "",
                "longitude": float(pos.group(1)),
                "latitude": float(pos.group(2)),
                "url": urllib.parse.urljoin(SOURCE_URL, rel_url),
                "category": unquote_js(category.group(1)) if category else "",
                "date": unquote_js(date.group(1)) if date else "",
                "editDate": unquote_js(edit_date.group(1)) if edit_date else "",
                "isValid": None,
                "validityStatus": "unknown",
                "validityCheckedAt": "",
                "latestComment": None,
                "latestDecisiveComment": None,
                "validityReason": "",
            }
        )

    if len(points) != expected or not points:
        raise SystemExit(f"parsed {len(points)} points, expected {expected}")
    return points


def keep_inside_sixth_ring(point: dict) -> bool:
    name = point["name"]
    return not any(marker in name for marker in SIXTH_RING_MARKERS) and point["category"] != "6"


def parse_comments(fragment: str) -> list[dict]:
    comments = []
    pattern = re.compile(
        r'<div class="clistbox">\s*<div class="line1">.*?发表于：([^<]+).*?</div>\s*<div class="line2">(.*?)</div>\s*</div>',
        re.S | re.I,
    )
    for published_at, text in pattern.findall(fragment):
        comments.append({"publishedAt": clean_text(published_at), "text": clean_text(text)})
    return comments


def parse_comment_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y/%m/%d %H:%M:%S")
    except ValueError:
        return None


def is_question(text: str) -> bool:
    compact = re.sub(r"\s+", "", text)
    return bool(re.search(r"(拍吗|拍不拍|能走吗|还能走|有被拍.*吗|吗[？?]?|[？?])", compact))


def classify_comment(text: str) -> str:
    compact = re.sub(r"\s+", "", text)
    if is_question(compact):
        return "unknown"
    if re.search(r"(这个|这里|这儿|此处|该点|本点|当前点|这个摄像头)(不拍|不会拍|不抓拍|不会抓拍)", compact):
        return "invalid"

    # Match positive evidence only when it is not negated by phrases like
    # "没被拍", "不拍了", or "没有收到".
    valid_patterns = (
        r"(?<!没)(?<!未)(?<!有)被拍",
        r"(?<!不)拍了",
        r"(?<!不)会拍",
        r"还拍",
        r"开始拍",
        r"抓拍",
        r"罚款",
        r"扣分",
        r"中招",
        r"不能走",
        r"走不了",
        r"走不了了",
        r"(?<!没)(?<!未)(?<!有)收到违章",
        r"(?<!没)(?<!未)(?<!有)收到罚单",
    )
    if re.search("|".join(valid_patterns), compact):
        return "valid"
    invalid_patterns = (
        r"没收到",
        r"没有收到",
        r"未收到",
        r"没被拍",
        r"没有被拍",
        r"未被拍",
        r"没拍",
        r"没有拍",
        r"未拍",
        r"不拍",
        r"不会拍",
        r"不再拍",
        r"不抓拍",
        r"不会抓拍",
        r"没事",
        r"可以走",
        r"(?<!不)能走",
        r"取消了?",
        r"撤了?",
        r"拆了?",
    )
    if re.search("|".join(invalid_patterns), compact):
        return "invalid"
    return "unknown"


def is_recent_invalid_override(comment: dict, now: datetime | None = None, days: int = 31) -> bool:
    if is_question(comment["text"]):
        return False
    if classify_comment(comment["text"]) == "valid":
        return False
    published = parse_comment_datetime(comment.get("publishedAt", ""))
    if published is None:
        return False
    now = now or datetime.now()
    if published < now - timedelta(days=days):
        return False
    compact = re.sub(r"\s+", "", comment["text"])
    return bool(re.search(r"(不会拍|不拍了|不拍啦|不再拍|已经不拍|现在不拍|目前不拍|已不拍|没有拍了|没拍了|不会抓拍|不抓拍)", compact))


def comment_page_url(point_id: int, page: int = 1) -> str:
    return f"{COMMENT_URL}?{urllib.parse.urlencode({'id': point_id, 'page': page})}"


def judge_validity(point_id: int, pages: int = 1) -> dict:
    comments = []
    for page in range(1, pages + 1):
        comments.extend(parse_comments(post_fetch(comment_page_url(point_id, page))))
    latest = comments[0] if comments else None
    recent_invalid = next((comment for comment in comments if is_recent_invalid_override(comment)), None)
    if recent_invalid:
        return {
            "isValid": False,
            "validityStatus": "invalid",
            "validityCheckedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "latestComment": latest,
            "latestDecisiveComment": recent_invalid,
            "validityReason": "recent_invalid_comment",
        }
    decisive = None
    status = "unknown"
    for comment in comments:
        status = classify_comment(comment["text"])
        if status != "unknown":
            decisive = comment
            break
    return {
        "isValid": True if status == "valid" else False if status == "invalid" else None,
        "validityStatus": status,
        "validityCheckedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "latestComment": latest,
        "latestDecisiveComment": decisive,
        "validityReason": "latest_decisive_comment" if decisive else "no_decisive_comment",
    }


def read_points(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def merge_cached_validity(points: list[dict], cached: list[dict]) -> list[dict]:
    cached_by_id = {p.get("id"): p for p in cached}
    fields = ("isValid", "validityStatus", "validityCheckedAt", "latestComment", "latestDecisiveComment", "validityReason")
    for point in points:
        old = cached_by_id.get(point.get("id"), {})
        for field in fields:
            if field in old:
                point[field] = old[field]
    return points


def update_validity(points: list[dict], ids: set[int] | None = None, pages: int = 1, delay_s: float = 0.1) -> list[dict]:
    for point in points:
        if ids is not None and point["id"] not in ids:
            continue
        try:
            point.update(judge_validity(point["id"], pages))
        except Exception as exc:
            point.update(
                {
                    "isValid": None,
                    "validityStatus": "unknown",
                    "validityCheckedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "validityReason": "error",
                    "validityError": str(exc),
                }
            )
        if delay_s:
            time.sleep(delay_s)
    return points


def update_validity_for_ids(path: str | Path, ids: set[int], pages: int = 1) -> dict[int, dict]:
    target = Path(path)
    points = read_points(target)
    update_validity(points, ids, pages)
    write_json(target, points)
    return {p["id"]: p for p in points if p.get("id") in ids}


def write_json(path: Path, data: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def self_test() -> None:
    html = """
    <script>var LabelsData = [
      {name: '内点', position: [116.1,39.9], aa:'2', time:'2026-01-01', edittime:'', href:'/content/?1.html', icon,},
      {name: '外点【六环外】', position: [116.2,39.8], aa:'2', time:'2026-01-01', edittime:'', href:'/content/?2.html', icon,},
      {name: '分类外点', position: [116.3,39.7], aa:'6', time:'2026-01-01', edittime:'', href:'/content/?3.html', icon,},
      {name: '西六环入口', position: [116.4,39.6], aa:'2', time:'2026-01-01', edittime:'', href:'/content/?4.html', icon,},
      {name: '卧龙岗桥下', position: [116.5,39.5], aa:'2', time:'2026-01-01', edittime:'', href:'/content/?5.html', icon,},
      {name: '魏各庄路西王佐南桥', position: [116.6,39.4], aa:'2', time:'2026-01-01', edittime:'', href:'/content/?6.html', icon,},
    ];</script>
    """
    points = parse_points(html)
    kept = [p for p in points if keep_inside_sixth_ring(p)]
    assert len(points) == 6
    assert len(kept) == 1
    assert kept[0]["id"] == 1
    comments = parse_comments(
        """
        <div class="clistbox"><div class="line1"><span>发表于：2026/7/3 20:17:08</span> 评论者：冀F</div><div class="line2">还拍不拍了</div></div>
        <div class="clistbox"><div class="line1"><span>发表于：2026/7/3 17:05:25</span> 评论者：张三</div><div class="line2">2026.7.1号出京方向被拍</div></div>
        """
    )
    assert comments[0]["text"] == "还拍不拍了"
    assert classify_comment(comments[0]["text"]) == "unknown"
    assert classify_comment(comments[1]["text"]) == "valid"
    assert classify_comment("2026.7.1号出京方向被拍，以前从来没拍过！！！注意") == "valid"
    assert classify_comment("不拍了，3月7日走的没有收到") == "invalid"
    assert classify_comment("很久都不拍了，取消了吧，经常走") == "invalid"
    assert classify_comment("经常这么走，没被拍过啊") == "invalid"
    assert classify_comment("以为不拍了，结果6.26晚上被拍了") == "valid"
    assert classify_comment("这个不拍，但是京新的那个会拍了") == "invalid"
    assert classify_comment("经常去三元加油，没有收到罚单") == "invalid"
    assert classify_comment("不能走，收到违章") == "valid"
    assert classify_comment("可以走，没事") == "invalid"
    assert is_recent_invalid_override(
        {"publishedAt": "2026/7/3 17:05:25", "text": "现在不会拍"},
        now=datetime(2026, 7, 4),
    )
    assert not is_recent_invalid_override(
        {"publishedAt": "2026/6/30 13:23:02", "text": "以为不拍了，结果6.26晚上被拍了"},
        now=datetime(2026, 7, 4),
    )
    assert not is_recent_invalid_override(
        {"publishedAt": "2026/5/1 17:05:25", "text": "现在不会拍"},
        now=datetime(2026, 7, 4),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=SOURCE_URL)
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--check-validity", action="store_true")
    parser.add_argument("--ids", help="Comma-separated point IDs to refresh validity for")
    parser.add_argument("--comment-pages", type=int, default=1)
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        print("self-test ok")
        return

    output = Path(args.output)
    ids = {int(value) for value in args.ids.split(",")} if args.ids else None
    if ids and not args.check_validity:
        updated = update_validity_for_ids(output, ids, args.comment_pages)
        print(json.dumps({"output": str(output.resolve()), "updated_ids": sorted(updated)}, ensure_ascii=False, indent=2))
        return

    all_points = parse_points(fetch(args.url))
    inside_points = merge_cached_validity([p for p in all_points if keep_inside_sixth_ring(p)], read_points(output))
    if args.check_validity:
        update_validity(inside_points, ids, args.comment_pages)
    write_json(output, inside_points)
    print(json.dumps({
        "output": str(output.resolve()),
        "source_total": len(all_points),
        "removed_sixth_ring_outside": len(all_points) - len(inside_points),
        "remaining": len(inside_points),
        "validity_checked": args.check_validity,
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
