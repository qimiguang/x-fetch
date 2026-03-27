#!/usr/bin/env python3
"""
GitHub Actions 运行脚本。
读取 config.json 中配置的用户列表，抓取推文保存到 data/{username}.json。
"""

import os
import json
import time
import re
import xml.etree.ElementTree as ET
import urllib.request
import urllib.error
from datetime import datetime, timezone
from urllib.request import Request

NITTER_INSTANCES = [
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.lunar.icu",
    "https://xcancel.com",
    "https://nitter.tiekoetter.com",
    "https://bird.trom.tf",
    "https://nitter.1d4.us",
]

RSSHUB_INSTANCES = [
    "https://rsshub.app",
    "https://rsshub.rssforever.com",
]

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0.0.0 Safari/537.36"}
COUNT = 20


def fetch(url, timeout=15):
    try:
        req = Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"  FAIL {url}: {e}")
        return None


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text).strip()


def parse_rss(content):
    try:
        root = ET.fromstring(content)
        channel = root.find("channel")
        if not channel:
            return None
        tweets = []
        for item in channel.findall("item")[:COUNT]:
            desc = strip_html(item.findtext("description", ""))
            title = strip_html(item.findtext("title", ""))
            tweets.append({
                "text": desc or title,
                "link": item.findtext("link", "").strip(),
                "date": item.findtext("pubDate", "").strip(),
            })
        return tweets or None
    except ET.ParseError:
        return None


def fetch_tweets(username):
    # Try Nitter instances
    for instance in NITTER_INSTANCES:
        print(f"  Trying {instance}")
        content = fetch(f"{instance}/{username}/rss")
        if content and "<rss" in content:
            tweets = parse_rss(content)
            if tweets:
                print(f"  OK: {instance}")
                return tweets
        time.sleep(0.5)

    # Try RSSHub
    for instance in RSSHUB_INSTANCES:
        print(f"  Trying RSSHub {instance}")
        content = fetch(f"{instance}/twitter/user/{username}")
        if content and "<rss" in content:
            tweets = parse_rss(content)
            if tweets:
                print(f"  OK: {instance}")
                return tweets
        time.sleep(0.5)

    return None


def main():
    # Read user list from config.json (or env var for manual dispatch)
    env_usernames = os.environ.get("INPUT_USERNAMES", "").strip()
    if env_usernames:
        usernames = [u.strip().lstrip("@") for u in env_usernames.split(",") if u.strip()]
    elif os.path.exists("config.json"):
        with open("config.json") as f:
            cfg = json.load(f)
        usernames = [u.lstrip("@") for u in cfg.get("usernames", [])]
    else:
        print("No usernames configured. Create config.json with {\"usernames\": [\"elonmusk\"]}")
        return

    os.makedirs("data", exist_ok=True)
    summary = {}

    for username in usernames:
        print(f"\nFetching @{username}...")
        tweets = fetch_tweets(username)
        if tweets:
            output = {
                "username": username,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(tweets),
                "tweets": tweets,
            }
            path = f"data/{username.lower()}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"  Saved {len(tweets)} tweets to {path}")
            summary[username] = len(tweets)
        else:
            print(f"  FAILED: could not fetch @{username}")
            summary[username] = 0

    # Save summary
    with open("data/_summary.json", "w") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "users": summary,
        }, f, ensure_ascii=False, indent=2)

    print(f"\nDone: {summary}")


if __name__ == "__main__":
    main()
