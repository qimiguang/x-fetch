#!/usr/bin/env python3
"""
X (Twitter) 推文获取工具。
数据来源：GitHub Actions 境外自动抓取，存入 qimiguang/x-fetch 仓库。
每次调用自动从 raw.githubusercontent.com 拉取最新数据（无需手动 --pull）。

用法:
  python3 x_fetch.py <username>                # 获取全部（最多 20 条）
  python3 x_fetch.py <username> --since 1d     # 最近 1 天
  python3 x_fetch.py <username> --since 3d     # 最近 3 天
  python3 x_fetch.py <username> --since 1w     # 最近 1 周
  python3 x_fetch.py <username> --since 1m     # 最近 1 个月
  python3 x_fetch.py <username> --count 5      # 最新 5 条（不按时间过滤）
  python3 x_fetch.py --list                    # 查看已追踪用户
"""

import sys
import json
import argparse
import re
import urllib.request
import ssl
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

GITHUB_REPO = "qimiguang/x-fetch"
BRANCH = "main"
LOCAL_REPO = "/Users/jeremy/.catpaw/skills/skills-market/x-fetch"
BASE_URL = f"https://raw.githubusercontent.com/{GITHUB_REPO}/{BRANCH}"

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


# ── 数据获取 ───────────────────────────────────────────────────────────────────

def fetch_raw(path):
    url = f"{BASE_URL}/{path}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "x-fetch-skill/1.0"})
        with urllib.request.urlopen(req, context=ctx, timeout=12) as r:
            return r.read().decode("utf-8")
    except Exception:
        return None


def get_summary():
    content = fetch_raw("data/_summary.json")
    return json.loads(content) if content else None


def get_tweets_raw(username):
    content = fetch_raw(f"data/{username.lower()}.json")
    return json.loads(content) if content else None


# ── 时间解析 ───────────────────────────────────────────────────────────────────

def parse_since(since_str):
    """
    解析时间参数，返回 datetime（UTC）。
    支持: 1d / 3d / 1w / 2w / 1m / 自然语言（一天/一周/一个月）
    """
    if not since_str:
        return None
    s = since_str.strip().lower()

    # 自然语言映射
    natural = {
        "今天": "1d", "一天": "1d", "最近一天": "1d", "24小时": "1d",
        "三天": "3d", "最近三天": "3d",
        "一周": "1w", "最近一周": "1w", "7天": "7d", "最近7天": "7d",
        "两周": "2w", "最近两周": "2w",
        "一个月": "1m", "最近一个月": "1m", "30天": "30d",
    }
    s = natural.get(s, s)

    m = re.match(r"(\d+)\s*([dwm])", s)
    if not m:
        return None
    n, unit = int(m.group(1)), m.group(2)
    delta = {"d": timedelta(days=n), "w": timedelta(weeks=n), "m": timedelta(days=n * 30)}[unit]
    return datetime.now(timezone.utc) - delta


def parse_tweet_date(date_str):
    """解析推文日期字符串，返回 aware datetime（UTC）。"""
    if not date_str:
        return None
    try:
        dt = parsedate_to_datetime(date_str)
        return dt.astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(date_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def filter_by_since(tweets, since_dt):
    if not since_dt:
        return tweets
    result = []
    for t in tweets:
        dt = parse_tweet_date(t.get("date", ""))
        if dt and dt >= since_dt:
            result.append(t)
    return result


# ── 格式化输出 ─────────────────────────────────────────────────────────────────

def format_tweet(t, idx):
    dt = parse_tweet_date(t.get("date", ""))
    date_str = dt.strftime("%Y-%m-%d %H:%M") if dt else t.get("date", "")[:16]
    text = t.get("text", "").strip()
    link = t.get("link", "")
    return f"{'─'*60}\n[{idx}] {date_str}\n{text}\n🔗 {link}"


# ── 主逻辑 ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="获取 X (Twitter) 用户推文")
    parser.add_argument("username", nargs="?", help="用户名（不含 @）")
    parser.add_argument("--since", default=None,
                        help="时间范围: 1d / 3d / 1w / 2w / 1m，或「最近一天」「一周」等")
    parser.add_argument("--count", type=int, default=None, help="最多显示条数")
    parser.add_argument("--list", action="store_true", help="查看已追踪用户")
    parser.add_argument("--json", dest="output_json", action="store_true", help="输出 JSON")
    args = parser.parse_args()

    if args.list or not args.username:
        summary = get_summary()
        if not summary:
            print("[ERROR] 无法读取数据，请检查网络")
            sys.exit(1)
        print(f"📋 已追踪用户（数据更新: {summary.get('updated_at', '未知')}）\n")
        for user, count in summary.get("users", {}).items():
            status = f"✅ {count} 条" if count > 0 else "❌ 无数据"
            print(f"  @{user:<20} {status}")
        print("\n💡 用法: python3 x_fetch.py <username> [--since 1d/1w/1m]")
        return

    username = args.username.lstrip("@")
    data = get_tweets_raw(username)

    if not data:
        print(f"[ERROR] 未找到 @{username} 的数据（数据每 3 小时自动更新）")
        print(f"提示：可在 {LOCAL_REPO}/config.json 中添加该用户")
        sys.exit(1)

    tweets = data.get("tweets", [])
    since_dt = parse_since(args.since)

    # 按时间过滤
    if since_dt:
        filtered = filter_by_since(tweets, since_dt)
        time_label = args.since
    else:
        filtered = tweets
        time_label = None

    # 按条数截断
    if args.count:
        filtered = filtered[:args.count]

    if args.output_json:
        out = {**data, "tweets": filtered, "count": len(filtered)}
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return

    # 标题行
    updated = data.get("updated_at", "")
    if updated:
        try:
            dt = datetime.strptime(updated, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            updated = dt.strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

    if time_label:
        title = f"\n@{username} 最近 {time_label} 的推文（共 {len(filtered)} 条 | 数据更新: {updated}）\n"
    elif args.count:
        title = f"\n@{username} 最新 {len(filtered)} 条推文（数据更新: {updated}）\n"
    else:
        title = f"\n@{username} 的推文（共 {len(filtered)} 条 | 数据更新: {updated}）\n"

    print(title)

    if not filtered:
        print(f"  该时间范围内没有推文（数据最多保留最近 20 条）")
    else:
        for i, t in enumerate(filtered, 1):
            print(format_tweet(t, i))
        print(f"{'─'*60}")


if __name__ == "__main__":
    main()
