#!/usr/bin/env python3
"""深度回填：通过 r.jina.ai 代理翻页抓取 nitter.cz 用户时间线，回补近 N 天历史推文。

用法: python backfill.py <Username> [days] [max_pages]
结果合并进 data/<username>.json（已有译文的推文保留译文）。
"""
import html as html_mod
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

USER = sys.argv[1] if len(sys.argv) > 1 else "ChrisZheng001"
DAYS = int(sys.argv[2]) if len(sys.argv) > 2 else 365
MAX_PAGES = int(sys.argv[3]) if len(sys.argv) > 3 else 60
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"


def fetch_direct(url, timeout=45):
    req = Request(url, headers={"User-Agent": UA})
    for i in range(2):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"    direct retry {i + 1}: {type(e).__name__}: {e}")
            time.sleep(5)
    return None


def fetch_jina(url, timeout=60, extra_headers=None):
    headers = {"User-Agent": UA, "X-Return-Format": "html"}
    if extra_headers:
        headers.update(extra_headers)
    req = Request("https://r.jina.ai/" + url, headers=headers)
    for i in range(2):
        try:
            with urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"    jina retry {i + 1}: {type(e).__name__}: {e}")
            time.sleep(4)
    return None


def is_timeline(content):
    return bool(content) and "timeline-item" in content and "Making sure you" not in content


def fetch_any(target_path, timeout=45):
    """遍历 nitter 实例 × 通路（直连 / Jina），返回第一份可用的时间线 HTML。"""
    instances = [i for i in INSTANCES if i not in BLOCKED_INSTANCES]
    for inst in instances:
        url = f"https://{inst}{target_path}"
        # 1) runner 直连
        content = fetch_direct(url)
        if is_timeline(content):
            WORKING_INSTANCE.append(inst)
            print(f"    OK via direct {inst}")
            return content
        # 2) Jina 渲染兜底（等待选择器出现，顺便等 Anubis 挑战自动过）
        content = fetch_jina(url, extra_headers={
            "X-Wait-For-Selector": ".timeline-item",
            "X-Timeout": "30",
        })
        if is_timeline(content):
            WORKING_INSTANCE.append(inst)
            print(f"    OK via jina {inst}")
            return content
    # 首个实例成功后，后续页面只用它（省时间、降低限流风险）
    for inst in dict.fromkeys(WORKING_INSTANCE):
        url = f"https://{inst}{target_path}"
        for fn in (fetch_direct, lambda u: fetch_jina(u, extra_headers={"X-Wait-For-Selector": ".timeline-item", "X-Timeout": "30"})):
            content = fn(url)
            if is_timeline(content):
                return content
    return None


WORKING_INSTANCE = []
# 实例池：顺序尝试；被拉黑/不可用的在 BLOCKED_INSTANCES 标记
INSTANCES = [
    "nitter.cz",
    "xcancel.com",
    "nitter.tiekoetter.com",
    "lightbrd.com",
    "nitter.space",
    "nitter.poast.org",
]
BLOCKED_INSTANCES = set()


def parse_page(page_html):
    """解析 nitter 时间线 HTML 页，返回 [{tweet_id, date, text}]。"""
    items = []
    for block in re.split(r'class="timeline-item', page_html)[1:]:
        m_id = re.search(r'tweet-link" href="/[^"]+/status/(\d+)', block)
        m_date = re.search(r'tweet-date"><a[^>]*title="([^"]+)"', block)
        if not (m_id and m_date):
            continue
        date_raw = html_mod.unescape(m_date.group(1))
        try:
            dt = datetime.strptime(date_raw, "%b %d, %Y · %H:%M:%S UTC").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        m_text = re.search(r'class="tweet-content[^"]*"[^>]*>(.*?)</div>\s*(?:<div class|</div>)', block, re.DOTALL)
        text = ""
        if m_text:
            text = html_mod.unescape(re.sub(r"<[^>]+>", " ", m_text.group(1)))
            text = re.sub(r"\s+", " ", text).strip()
        items.append({
            "tweet_id": m_id.group(1),
            "date": dt.strftime("%Y-%m-%d %H:%M"),
            "text": text,
        })
    return items


def probe_syndication():
    """诊断：runner 直连 syndication，看 NEXT_DATA 里有无 cursor、时间覆盖范围。"""
    url = (
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{USER}"
        "?count=200&showReplies=false"
    )
    content = fetch_direct(url)
    if not content:
        print("[probe] syndication direct failed, trying jina...")
        content = fetch_jina(url)
    if not content:
        print("[probe] syndication unreachable")
        return
    m = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
    if not m:
        print(f"[probe] no NEXT_DATA, len={len(content)} head={content[:150]!r}")
        return
    d = json.loads(m.group(1))
    pp = d.get("props", {}).get("pageProps", {})
    print("[probe] pageProps keys:", list(pp.keys()))
    tl = pp.get("timeline", {})
    if isinstance(tl, dict):
        print("[probe] timeline keys:", list(tl.keys()))
        entries = tl.get("entries", [])
        print(f"[probe] entries x{len(entries)}")
        if entries:
            print("[probe] entry[0]:", json.dumps(entries[0], ensure_ascii=False)[:900])
        for k in ("bottom_cursor", "bottomCursor", "next_cursor"):
            if k in tl:
                print(f"[probe] {k} =", str(tl[k])[:120])
    s = json.dumps(d)
    for kw in ["cursor", "Cursor"]:
        positions = [i for i in range(len(s)) if s.startswith(kw, i)]
        if positions:
            print(f"[probe] '{kw}' x{len(positions)}, first ctx: ...{s[max(0, positions[0] - 30):positions[0] + 90]}...")
    dates = re.findall(r'"created_at":"([^"]+)"', s)
    if dates:
        print(f"[probe] created_at x{len(dates)}: newest={dates[0][:24]} oldest={dates[-1][:24]}")


def main():
    probe_syndication()
    cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
    all_items = {}
    for page in range(1, MAX_PAGES + 1):
        target = f"https://nitter.cz/{USER}" + (f"/page/{page}" if page > 1 else "")
        print(f"[page {page}] {target}")
        content = fetch_any(f"/{USER}" + (f"/page/{page}" if page > 1 else ""))
        if not content:
            print("    fetch failed, stop")
            break
        if page == 1:
            print(f"    [debug] len={len(content)} head={content[:400]!r}")
        items = parse_page(content)
        if not items and page == 1:
            print(f"    [debug] has timeline-item={'timeline-item' in content} has tweet-link={'tweet-link' in content}")
        new = [i for i in items if i["tweet_id"] not in all_items]
        print(f"    parsed {len(items)} items, {len(new)} new")
        for it in items:
            all_items.setdefault(it["tweet_id"], it)
        if page > 1 and not new:
            print("    no new items, stop")
            break
        dates = [i["date"] for i in new]
        if dates and min(dates) < cutoff.strftime("%Y-%m-%d"):
            print(f"    reached cutoff ({min(dates)}), stop")
            break
        time.sleep(5)

    keep = {k: v for k, v in all_items.items() if v["date"] >= cutoff.strftime("%Y-%m-%d")}
    print(f"[merge] fetched {len(all_items)} total, {len(keep)} within {DAYS} days")

    path = f"data/{USER.lower()}.json"
    existing = {}
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                for t in json.load(f).get("tweets", []):
                    key = str(t.get("tweet_id") or t.get("id") or hash(t.get("text", "")))
                    existing[key] = t
        except (json.JSONDecodeError, OSError):
            existing = {}

    for tid, it in keep.items():
        old = existing.get(tid)
        if old and old.get("text_zh"):
            it["text_zh"] = old["text_zh"]
        existing[tid] = it

    merged = sorted(existing.values(), key=lambda t: t.get("date", ""), reverse=True)
    out = {
        "user": USER,
        "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "tweets": merged,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"[done] {len(merged)} tweets -> {path}")


if __name__ == "__main__":
    main()
