#!/usr/bin/env python3
"""
GitHub Actions 运行脚本。
方法优先级:
  1. Twitter Syndication API (timeline embed，无需认证，最稳定)
  2. Twitter Guest API (GraphQL，需 guest token)
  3. Nitter RSS (动态测试实例列表)
读取 config.json 中配置的用户列表，结果保存到 data/{username}.json。
"""

import os
import json
import time
import re
import urllib.request
import urllib.error
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# ── Twitter Guest API ──────────────────────────────────────────────────────────
# Twitter 网页客户端使用的公开 bearer token（非个人密钥）
TWITTER_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs"
    "%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
)

# Nitter 实例列表（按可靠性排序）
NITTER_INSTANCES = [
    "https://nitter.net",
    "https://nitter.privacydev.net",
    "https://nitter.poast.org",
    "https://nitter.cz",
    "https://nitter.lunar.icu",
    "https://nitter.tiekoetter.com",
    "https://xcancel.com",
    "https://nitter.1d4.us",
    "https://bird.trom.tf",
    "https://nitter.rawbit.ninja",
    "https://nitter.moomoo.me",
    "https://nitter.kavin.rocks",
]

COUNT = 200   # 每次抓取向 API 请求的条数（服务端可能自行截断）
MAX_TWEETS = 200  # 每个用户在 data/*.json 中保留的最大条数（与历史快照合并后截取）
KEEP_DAYS = 31    # 默认只保留近 31 天的推文，更早的从数据文件中剔除
KEEP_DAYS_OVERRIDES = {"chriszheng001": 366}  # 个别用户保留更久（按用户名小写）
TRANSLATE_PER_RUN = 100   # 每次运行最多新翻译的推文数（存量回填限速，避免超时）
TRANSLATE_MAX_CHARS = 4000  # gtx 单次请求文本上限
HEADERS_COMMON = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}


def fetch(url, headers=None, timeout=20):
    h = {**HEADERS_COMMON, **(headers or {})}
    try:
        req = urllib.request.Request(url, headers=h)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"    FAIL {url}: {type(e).__name__}: {e}")
        return None


def fetch_json(url, headers=None, timeout=20):
    content = fetch(url, headers=headers, timeout=timeout)
    if content:
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            pass
    return None


def strip_html(text):
    return re.sub(r"<[^>]+>", "", text).strip()


# ── Method 1: Twitter Syndication API ─────────────────────────────────────────
# 这是 Twitter 官方 embed timeline 使用的 API，无需认证，对高流量账号也有效

def fetch_via_syndication(username):
    """通过 syndication.twitter.com 获取用户时间线（无需认证）。"""
    print(f"  [Method 1] Twitter Syndication API...")
    url = (
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        f"?count={COUNT}&showReplies=false"
    )
    headers = {
        **HEADERS_COMMON,
        "Referer": f"https://twitter.com/{username}",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "en-US,en;q=0.9",
        "x-requested-with": "XMLHttpRequest",
    }
    content = fetch(url, headers=headers, timeout=20)
    if not content:
        return None

    # 响应可能是 JSON 或包含 JSON 的 HTML
    # 尝试直接解析
    try:
        data = json.loads(content)
        tweets = _parse_syndication_data(data, username)
        if tweets:
            print(f"    Got {len(tweets)} tweets via Syndication API")
            return tweets
    except json.JSONDecodeError:
        pass

    # 尝试从 HTML 中提取 JSON
    match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(1))
            tweets = _parse_next_data(data, username)
            if tweets:
                print(f"    Got {len(tweets)} tweets via Syndication (NEXT_DATA)")
                return tweets
        except (json.JSONDecodeError, KeyError):
            pass

    print("    Syndication API returned no usable data")
    return None


def _parse_syndication_data(data, username):
    """解析 syndication API 的 JSON 响应。"""
    tweets = []
    try:
        # 格式1: {"timeline": {"entries": [...]}}
        entries = data.get("timeline", {}).get("entries", [])
        for entry in entries:
            tweet = entry.get("tweet") or entry.get("content", {}).get("tweet", {})
            if not tweet:
                continue
            text = tweet.get("full_text") or tweet.get("text", "")
            tweet_id = tweet.get("id_str") or tweet.get("id", "")
            created_at = tweet.get("created_at", "")
            if text and tweet_id:
                tweets.append({
                    "text": text,
                    "link": f"https://x.com/{username}/status/{tweet_id}",
                    "date": created_at,
                    "tweet_id": str(tweet_id),
                })
    except (KeyError, TypeError, AttributeError):
        pass
    return tweets


def _walk_tweet_nodes(node, out):
    """递归遍历 JSON 树，收集所有形似推文的对象（有数字 id + 文本）。"""
    if isinstance(node, dict):
        tid = node.get("id_str") or node.get("id")
        text = node.get("full_text") or node.get("text")
        if (
            tid is not None
            and isinstance(text, str)
            and text.strip()
            and str(tid).isdigit()
            and ("created_at" in node or "favorite_count" in node)
        ):
            out.append(node)
        for v in node.values():
            _walk_tweet_nodes(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_tweet_nodes(v, out)


def _parse_next_data(data, username):
    """解析 __NEXT_DATA__ 中的推文数据：先试已知路径，失败则递归全树搜索。"""
    tweets = []
    try:
        # 已知路径: props.pageProps.timeline.entries
        entries = data.get("props", {}).get("pageProps", {}).get("timeline", {}).get("entries", [])
        for entry in entries:
            tweet = entry.get("tweet", {})
            text = tweet.get("full_text") or tweet.get("text", "")
            tweet_id = tweet.get("id_str") or tweet.get("id", "")
            created_at = tweet.get("created_at", "")
            if text and tweet_id:
                tweets.append({
                    "text": text,
                    "link": f"https://x.com/{username}/status/{tweet_id}",
                    "date": created_at,
                    "tweet_id": str(tweet_id),
                })
    except (KeyError, TypeError, AttributeError):
        pass
    if tweets:
        return tweets

    # 兜底: 递归搜索整棵树（syndication 页面结构可能嵌套在 contextProvider 下）
    nodes = []
    try:
        _walk_tweet_nodes(data, nodes)
    except (TypeError, AttributeError):
        pass
    seen = set()
    for tweet in nodes:
        text = tweet.get("full_text") or tweet.get("text", "")
        tid = str(tweet.get("id_str") or tweet.get("id"))
        if tid in seen or not text.strip():
            continue
        seen.add(tid)
        screen = ""
        user = tweet.get("user")
        if isinstance(user, dict):
            screen = user.get("screen_name") or ""
        handle = screen or username
        tweets.append({
            "text": text,
            "link": f"https://x.com/{handle}/status/{tid}",
            "date": tweet.get("created_at", ""),
            "tweet_id": tid,
        })
    return tweets


# ── Method 2: Twitter Guest API ───────────────────────────────────────────────

def get_guest_token():
    data = fetch_json(
        "https://api.twitter.com/1.1/guest/activate.json",
        headers={
            "Authorization": f"Bearer {TWITTER_BEARER}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    if data:
        token = data.get("guest_token")
        if token:
            print(f"    Guest token: {token[:8]}...")
            return token
    return None


def get_user_id(screen_name, auth_headers):
    """通过 GraphQL 获取用户 ID。"""
    variables = urllib.parse.quote(json.dumps({
        "screen_name": screen_name,
        "withSafetyModeUserFields": True,
    }))
    features = urllib.parse.quote(json.dumps({
        "hidden_profile_likes_enabled": True,
        "hidden_profile_subscriptions_enabled": True,
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "subscriptions_verification_info_is_identity_verified_enabled": True,
        "subscriptions_verification_info_verified_since_enabled": True,
        "highlights_tweets_tab_ui_enabled": True,
        "responsive_web_twitter_article_notes_tab_enabled": True,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "responsive_web_graphql_timeline_navigation_enabled": True,
    }))
    url = f"https://api.twitter.com/graphql/G3KGOASz96M-Qu0nwmGXNg/UserByScreenName?variables={variables}&features={features}"
    data = fetch_json(url, headers=auth_headers)
    if data:
        try:
            return data["data"]["user"]["result"]["rest_id"]
        except (KeyError, TypeError):
            pass
    return None


def parse_tweet_results(data):
    """从 GraphQL 响应中提取推文列表。"""
    tweets = []
    try:
        instructions = data["data"]["user"]["result"]["timeline_v2"]["timeline"]["instructions"]
        for instruction in instructions:
            for entry in instruction.get("entries", []):
                content = entry.get("content", {})
                item_content = content.get("itemContent", {})
                tweet_result = item_content.get("tweet_results", {}).get("result", {})
                legacy = tweet_result.get("legacy") or tweet_result.get("tweet", {}).get("legacy", {})
                if not legacy:
                    continue
                text = legacy.get("full_text", "")
                created_at = legacy.get("created_at", "")
                tweet_id = legacy.get("id_str", "")
                screen_name = legacy.get("user_id_str", "")
                # Get screen name from core
                try:
                    screen_name = tweet_result["core"]["user_results"]["result"]["legacy"]["screen_name"]
                except (KeyError, TypeError):
                    pass
                if text and tweet_id:
                    tweets.append({
                        "text": text,
                        "link": f"https://x.com/{screen_name}/status/{tweet_id}",
                        "date": created_at,
                        "tweet_id": tweet_id,
                    })
    except (KeyError, TypeError, AttributeError):
        pass
    return tweets


def fetch_via_guest_api(username):
    print(f"  [Method 2] Twitter Guest API...")
    guest_token = get_guest_token()
    if not guest_token:
        print("    Could not get guest token")
        return None

    auth_headers = {
        "Authorization": f"Bearer {TWITTER_BEARER}",
        "x-guest-token": guest_token,
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
    }

    user_id = get_user_id(username, auth_headers)
    if not user_id:
        print(f"    Could not get user ID for @{username}")
        return None
    print(f"    User ID: {user_id}")

    variables = urllib.parse.quote(json.dumps({
        "userId": user_id,
        "count": COUNT,
        "includePromotedContent": False,
        "withQuickPromoteEligibilityTweetFields": True,
        "withVoice": True,
        "withV2Timeline": True,
    }))
    features = urllib.parse.quote(json.dumps({
        "rweb_tipjar_consumption_enabled": True,
        "responsive_web_graphql_exclude_directive_enabled": True,
        "verified_phone_label_enabled": False,
        "creator_subscriptions_tweet_preview_api_enabled": True,
        "responsive_web_graphql_timeline_navigation_enabled": True,
        "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
        "communities_web_enable_tweet_community_results_fetch": True,
        "c9s_tweet_anatomy_moderator_badge_enabled": True,
        "articles_preview_enabled": True,
        "tweetypie_unmention_optimization_enabled": True,
        "responsive_web_edit_tweet_api_enabled": True,
        "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
        "view_counts_everywhere_api_enabled": True,
        "longform_notetweets_consumption_enabled": True,
        "responsive_web_twitter_article_tweet_consumption_enabled": True,
        "tweet_awards_web_tipping_enabled": False,
        "creator_subscriptions_quote_tweet_preview_enabled": False,
        "freedom_of_speech_not_reach_fetch_enabled": True,
        "standardized_nudges_misinfo": True,
        "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
        "rweb_video_timestamps_enabled": True,
        "longform_notetweets_rich_text_read_enabled": True,
        "longform_notetweets_inline_media_enabled": True,
        "responsive_web_enhance_cards_enabled": False,
    }))

    url = f"https://api.twitter.com/graphql/V1ze5q3ijDS1VeLwLY0m7g/UserTweets?variables={variables}&features={features}"
    data = fetch_json(url, headers=auth_headers)
    if not data:
        print("    No data from GraphQL")
        return None

    tweets = parse_tweet_results(data)
    if tweets:
        print(f"    Got {len(tweets)} tweets via Guest API")
        return tweets
    print("    Guest API returned no tweets (may need updated query IDs)")
    return None


# ── Method 3: Nitter RSS ───────────────────────────────────────────────────────

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
            for esc, ch in [("&amp;","&"),("&lt;","<"),("&gt;",">"),("&quot;",'"'),("&#39;","'")]:
                desc, title = desc.replace(esc,ch), title.replace(esc,ch)
            tweets.append({
                "text": desc or title,
                "link": item.findtext("link","").strip(),
                "date": item.findtext("pubDate","").strip(),
            })
        return tweets or None
    except ET.ParseError:
        return None


def fetch_via_nitter(username):
    print(f"  [Method 3] Nitter RSS...")
    for instance in NITTER_INSTANCES:
        print(f"    Trying {instance}")
        content = fetch(f"{instance}/{username}/rss", timeout=15)
        if content and "<rss" in content:
            tweets = parse_rss(content)
            if tweets:
                print(f"    OK: {instance} ({len(tweets)} tweets)")
                return tweets
        time.sleep(0.3)
    return None


# ── Main ───────────────────────────────────────────────────────────────────────

# ── 翻译（中文译文） ─────────────────────────────────────────────────────

def translate_to_zh(text):
    """用 Google 翻译公开端点（无需 key）把推文翻成中文。失败返回 None，下次运行重试。"""
    text = text.strip()
    if not text:
        return None
    # 长推文按行切成不超过上限的片段，分别翻译后拼接
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > TRANSLATE_MAX_CHARS:
            if cur:
                chunks.append(cur)
            cur = line[:TRANSLATE_MAX_CHARS]
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        chunks.append(cur)

    parts = []
    for chunk in chunks:
        url = (
            "https://translate.googleapis.com/translate_a/single"
            "?client=gtx&sl=auto&tl=zh-CN&dt=t&q=" + urllib.parse.quote(chunk)
        )
        data = None
        for attempt in range(2):
            try:
                data = fetch_json(url, timeout=15)
            except Exception:
                data = None
            # gtx 成功响应是数组：[[["译文","原文",...],...],...]
            if isinstance(data, list) and data and data[0]:
                break
            time.sleep(1)
        if isinstance(data, list) and data and data[0]:
            try:
                parts.append("".join(seg[0] for seg in data[0] if seg and seg[0]))
                continue
            except (TypeError, KeyError, IndexError):
                pass
        # gtx 被限流时（返回 Sorry 页），走 clients5 备用端点
        zh = translate_via_clients5(chunk)
        if zh is None:
            return None
        parts.append(zh)
    return "\n".join(parts)


def translate_via_clients5(text):
    """备用翻译通道：Chrome 词典扩展公开端点。成功返回译文，失败返回 None。
    响应格式：[["译文","源语言"], ...]"""
    url = (
        "https://clients5.google.com/translate_a/t"
        "?client=dict-chrome-ex&sl=auto&tl=zh-CN&q=" + urllib.parse.quote(text)
    )
    for attempt in range(3):
        try:
            data = fetch_json(url, timeout=15)
        except Exception:
            data = None
        if isinstance(data, list) and data:
            try:
                # 响应：[["译文","源语言"]] 或多段 [["译文1","en"],["译文2","en"]]
                if isinstance(data[0], list):
                    return "".join(s[0] for s in data if isinstance(s, list) and s and isinstance(s[0], str))
                if isinstance(data[0], str):  # 扁平形式：["译文","en"]
                    return "".join(s for s in data if isinstance(s, str))
                return None
            except (TypeError, KeyError, IndexError):
                return None
        time.sleep(1.5 * (attempt + 1))
    return None


def fill_translations(tweets, budget):
    """为缺失 text_zh 的推文补翻译，最多消耗 budget 条。返回实际翻译条数。
    列表已按时间倒序，优先翻译最新的推文。"""
    done = 0
    for t in tweets:
        if done >= budget:
            break
        if t.get("text_zh"):
            continue
        try:
            zh = translate_to_zh(t.get("text", ""))
        except Exception as e:
            print(f"    translate error: {type(e).__name__}: {e}")
            zh = None
        if zh:
            t["text_zh"] = zh
            done += 1
            time.sleep(0.2)  # 温和限速，降低被限流概率
        else:
            print(f"    translate failed (will retry next run): {t.get('date', '')[:20]}")
    return done


def parse_date_flex(s):
    """兼容 RFC 2822（GMT）与 ISO 8601 两种日期格式。"""
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(s).astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def tweet_key(t):
    """推文去重键：优先用 link 中的推文 ID（统一 nitter/x.com 域名），
    其次 tweet_id，最后退化为归一化文本前缀。"""
    m = re.search(r"/status/(\d+)", t.get("link", ""))
    if m:
        return "id:" + m.group(1)
    if t.get("tweet_id"):
        return "id:" + str(t["tweet_id"])
    return "txt:" + re.sub(r"\s+", " ", t.get("text", ""))[:80]


def merge_with_existing(username, tweets):
    """与 data/{username}.json 中的历史数据合并，去重后按时间倒序截取前 MAX_TWEETS 条。
    这样即使单次抓取只返回 20 条，每 3 小时累积也能存满 200 条，且短暂漏跑不丢推文。"""
    path = f"data/{username.lower()}.json"
    existing = []
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                existing = json.load(f).get("tweets", [])
        except (json.JSONDecodeError, OSError):
            existing = []

    merged = {}
    for t in existing + tweets:
        key = tweet_key(t)
        if key and key not in merged:
            merged[key] = t

    epoch = datetime.min.replace(tzinfo=timezone.utc)
    result = sorted(
        merged.values(),
        key=lambda t: parse_date_flex(t.get("date", "")) or epoch,
        reverse=True,
    )
    # 保留期过滤：只留近 KEEP_DAYS 天（用户可覆写；解析不出时间的推文保留，避免误删）
    keep_days = KEEP_DAYS_OVERRIDES.get(username.lower(), KEEP_DAYS)
    cutoff = datetime.now(timezone.utc) - timedelta(days=keep_days)
    result = [
        t for t in result
        if (parse_date_flex(t.get("date", "")) or cutoff) >= cutoff
    ]
    return result[:MAX_TWEETS]


# ── Method 4: Jina Reader 代理（绕过 GitHub runner IP 限流）───────────────────

def fetch_via_jina_proxy(username):
    """通过 r.jina.ai 中转访问 Syndication API：出口 IP 为 Jina 机房，
    可绕过 Twitter 对 GitHub runner IP 段的 429 限流。仅在直连全部失败时兜底。"""
    print("  [Method 4] Syndication via r.jina.ai proxy...")
    # Jina 会按 URL 缓存响应，导致拿到旧快照：加时间戳参数破缓存，并显式禁缓存
    bust = int(time.time())
    target = (
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        f"?count={COUNT}&showReplies=false&_cb={bust}"
    )
    headers = {
        "X-Return-Format": "html",
        "X-No-Cache": "true",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    content = fetch("https://r.jina.ai/" + target, headers=headers, timeout=45)
    if not content:
        return None

    def _try_parse(raw, tag):
        try:
            tweets = _parse_syndication_data(json.loads(raw), username)
            if tweets:
                print(f"    Got {len(tweets)} tweets via Jina proxy ({tag})")
                return tweets
        except json.JSONDecodeError:
            pass
        return None

    # 1) __NEXT_DATA__ 结构化 JSON（html 模式：含推文 ID/精确时间/链接，最佳）
    match = re.search(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', content, re.DOTALL)
    if match:
        raw = match.group(1)
        print(f"    [debug2] next_data raw len={len(raw)} head={raw[:180]!r}")
        try:
            nd = json.loads(raw)
            m_lt = re.search(r'"latest_tweet_id":"?(\d+)', raw)
            if m_lt:
                print(f"    [debug3] latest_tweet_id in payload: {m_lt.group(1)}")
            tweets = _parse_next_data(nd, username)
            if tweets:
                newest = max((t.get("date", "") for t in tweets), default="-")
                print(f"    Got {len(tweets)} tweets via Jina proxy (NEXT_DATA), newest fetched: {newest}")
                return tweets
            print("    [debug2] NEXT_DATA parsed but 0 tweets")
        except json.JSONDecodeError as e:
            print(f"    [debug2] NEXT_DATA json error: {e}")
        except KeyError as e:
            print(f"    [debug2] NEXT_DATA missing key: {e}")
    else:
        print("    [debug2] NEXT_DATA regex not matched")
    # 2) 响应本身是 JSON
    tweets = _try_parse(content, "direct")
    if tweets:
        return tweets
    # 3) markdown/text 包装：截取第一个 { 起的 JSON
    idx = content.find("{")
    if idx > 0:
        tweets = _try_parse(content[idx:], "stripped")
        if tweets:
            return tweets
    print("    Jina proxy returned no usable data")
    print(
        f"    [debug] len={len(content)} has_next_data={'__NEXT_DATA__' in content} "
        f"has_entries={'entries' in content} head={content[:200]!r}"
    )
    return None


def fetch_tweets(username):
    # 方法1: Syndication API（最稳定，对高流量账号也有效）
    tweets = fetch_via_syndication(username)
    if tweets:
        return tweets

    # 方法2: Guest API
    tweets = fetch_via_guest_api(username)
    if tweets:
        return tweets

    # 方法3: Nitter RSS
    tweets = fetch_via_nitter(username)
    if tweets:
        return tweets

    # 方法4: Jina 代理中转 Syndication（绕过 GitHub IP 限流的兜底）
    tweets = fetch_via_jina_proxy(username)
    if tweets:
        return tweets

    # 最后重试 Syndication（等待后再试一次）
    print("  Retrying Syndication after 5s...")
    time.sleep(5)
    tweets = fetch_via_syndication(username)
    return tweets


def main():
    env_usernames = os.environ.get("INPUT_USERNAMES", "").strip()
    if env_usernames:
        usernames = [u.strip().lstrip("@") for u in env_usernames.split(",") if u.strip()]
    elif os.path.exists("config.json"):
        with open("config.json") as f:
            usernames = [u.lstrip("@") for u in json.load(f).get("usernames", [])]
    else:
        print("No usernames configured.")
        return

    os.makedirs("data", exist_ok=True)
    summary = {}

    for username in usernames:
        print(f"\n{'='*40}\nFetching @{username}...")
        tweets = fetch_tweets(username)
        translated = 0
        if tweets:
            merged_tweets = merge_with_existing(username, tweets)
            translated = fill_translations(merged_tweets, TRANSLATE_PER_RUN)
            output = {
                "username": username,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(merged_tweets),
                "tweets": merged_tweets,
            }
            path = f"data/{username.lower()}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"  Saved {len(merged_tweets)} tweets (fetched {len(tweets)}, translated {translated}) -> {path}")
            summary[username] = len(merged_tweets)
        else:
            print(f"  FAILED: @{username}")
            # 抓取失败（如被限流）也让存量推文继续补翻译，逐轮消化
            path = f"data/{username.lower()}.json"
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        existing_data = json.load(f)
                    n = fill_translations(existing_data.get("tweets", []), TRANSLATE_PER_RUN)
                    if n:
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(existing_data, f, ensure_ascii=False, indent=2)
                        print(f"  Backfilled {n} translations -> {path}")
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  Backfill skipped: {e}")
            summary[username] = 0

    with open("data/_summary.json", "w") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "users": summary,
        }, f, ensure_ascii=False, indent=2)

    print(f"\n{'='*40}\nResult: {summary}")
    failed = [u for u, c in summary.items() if c == 0]
    if failed:
        print(f"WARNING: {len(failed)} user(s) failed: {failed}")


if __name__ == "__main__":
    main()
