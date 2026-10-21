#!/usr/bin/env python3
"""导入 X（Twitter）官方数据档案中的推文，回补完整历史。

用法: python import_archive.py <archive_Data_dir_or_tweets.js> [username] [days]
  例: python import_archive.py ~/Downloads/x_archive_xx/Data ChrisZheng001 366

X 档案获取方式：X 网页版 → 设置与隐私 → 你的账号 → 下载你的数据归档，
收到邮件后下载 zip，解压出 Data/tweets.js（多个分片则一起传入目录）。
结果合并进 data/<username>.json，已有译文（text_zh）保留。
"""
import glob
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone

ARCHIVE = sys.argv[1] if len(sys.argv) > 1 else ""
USER = sys.argv[2] if len(sys.argv) > 2 else "ChrisZheng001"
DAYS = int(sys.argv[3]) if len(sys.argv) > 3 else 366

if not ARCHIVE:
    print(__doc__)
    sys.exit(1)

# 定位所有 tweets*.js 分片
if os.path.isdir(ARCHIVE):
    files = sorted(glob.glob(os.path.join(ARCHIVE, "tweets*.js")))
else:
    files = [ARCHIVE]
if not files:
    print(f"[error] 没找到 tweets*.js: {ARCHIVE}")
    sys.exit(1)
print(f"[input] {len(files)} file(s): {[os.path.basename(f) for f in files]}")

tweets = []
for path in files:
    with open(path, encoding="utf-8") as f:
        raw = f.read()
    # 档案格式：首行形如 window.YTD.tweets.part0 = [  ...  ]; 去掉后为纯 JSON
    body = raw[raw.index("=") + 1:].strip().rstrip(";").strip()
    arr = json.loads(body)
    for item in arr:
        # 新格式 {"tweet": {...}}，旧格式顶层即推文对象
        t = item.get("tweet", item)
        tweets.append(t)
print(f"[parse] {len(tweets)} tweets from archive")

cutoff = datetime.now(timezone.utc) - timedelta(days=DAYS)
kept = {}
for t in tweets:
    tid = str(t.get("id_str") or t.get("id") or "")
    created = t.get("created_at", "")
    try:
        dt = datetime.strptime(created, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        continue
    if dt < cutoff:
        continue
    text = (t.get("full_text") or t.get("text") or "").strip()
    if not tid or not text:
        continue
    kept[tid] = {
        "tweet_id": tid,
        "date": dt.strftime("%Y-%m-%d %H:%M"),
        "text": re.sub(r"\s+", " ", text),
        "link": f"https://x.com/{USER}/status/{tid}",
    }
print(f"[filter] {len(kept)} tweets within {DAYS} days "
      f"(newest={max(v['date'] for v in kept.values()) if kept else '-'} "
      f"oldest={min(v['date'] for v in kept.values()) if kept else '-'})")

path = f"data/{USER.lower()}.json"
existing = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as f:
        for t in json.load(f).get("tweets", []):
            key = str(t.get("tweet_id") or t.get("id") or "")
            if key:
                existing[key] = t

before_ids = set(existing)
for tid, it in kept.items():
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
print(f"[done] {len(merged)} tweets -> {path} (其中 {len(set(kept) - before_ids)} 条来自档案)")
