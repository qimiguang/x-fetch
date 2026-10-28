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
import glob
import time
import re
import html as html_mod
import urllib.request
import urllib.error
import urllib.parse
import socket
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
MEDIA_DIR = "media"  # 媒体本地托管目录（供页面从 raw.githubusercontent 加载）
FXTWITTER_API = "https://api.fxtwitter.com/i/status/"  # 公开推文详情接口（补充回复/引用上下文）
MAX_TWEETS = 200  # 每个用户在 data/*.json 中保留的最大条数（与历史快照合并后截取）
KEEP_DAYS = 31    # 默认只保留近 31 天的推文，更早的从数据文件中剔除
KEEP_DAYS_OVERRIDES = {"chriszheng001": 366}  # 个别用户保留更久（按用户名小写）
# 个别用户开启 showReplies：Twitter 对高流量账号的 profile 缓存极强，
# showReplies=false 快照长期不更新；带回复的缓存键不同，通常能拿到更新的时间线
SHOW_REPLIES_OVERRIDES = {"elonmusk": True}
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

def fetch_via_syndication(username, show_replies=False):
    """通过 syndication.twitter.com 获取用户时间线（无需认证）。"""
    print(f"  [Method 1] Twitter Syndication API...")
    sr = "true" if show_replies else "false"
    url = (
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        f"?count={COUNT}&showReplies={sr}"
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
                item = {
                    "text": text,
                    "link": f"https://x.com/{username}/status/{tweet_id}",
                    "date": created_at,
                    "tweet_id": str(tweet_id),
                }
                media = _extract_media(tweet)
                if media:
                    item["media"] = media
                _attach_synd_context(tweet, item)
                tweets.append(item)
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


def _extract_media(tweet):
    """从推文 entities 中提取媒体：photo 返回可展示 URL，video/gif 返回 mp4 变体 + 封面。"""
    out = []
    ent_list = []
    for key in ("entities", "extended_entities"):
        ent = tweet.get(key)
        if isinstance(ent, dict):
            ent_list.append(ent)
    for ent in ent_list:
        for m in ent.get("media") or []:
            if not isinstance(m, dict):
                continue
            mtype = m.get("type")
            base = m.get("media_url_https") or m.get("media_url") or ""
            if mtype == "photo" and base:
                out.append({
                    "type": "photo",
                    "url": base + "?format=jpg&name=medium",
                    "full": base + "?format=jpg&name=large",
                })
            elif mtype in ("video", "animated_gif"):
                variants = (m.get("video_info") or {}).get("variants") or []
                mp4s = [v for v in variants if isinstance(v, dict) and v.get("content_type") == "video/mp4"]
                item = {"type": mtype, "url": "", "thumb": base}
                if mp4s:
                    item["url"] = max(mp4s, key=lambda v: v.get("bitrate", 0)).get("url", "")
                out.append(item)
    return out


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
                item = {
                    "text": text,
                    "link": f"https://x.com/{username}/status/{tweet_id}",
                    "date": created_at,
                    "tweet_id": str(tweet_id),
                }
                media = _extract_media(tweet)
                if media:
                    item["media"] = media
                _attach_synd_context(tweet, item)
                tweets.append(item)
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
        item = {
            "text": text,
            "link": f"https://x.com/{handle}/status/{tid}",
            "date": tweet.get("created_at", ""),
            "tweet_id": tid,
        }
        media = _extract_media(tweet)
        if media:
            item["media"] = media
        _attach_synd_context(tweet, item)
        tweets.append(item)
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

def _pbs_from_nitter_pic(url):
    """把 nitter /pic/ 代理地址还原成 pbs.twimg.com 原始地址。"""
    m = re.search(r"/pic/(?:orig/)?(.+)$", url)
    if not m:
        return None
    u = urllib.parse.unquote(m.group(1))
    u = u[len("orig/"):] if u.startswith("orig/") else u
    if u.startswith("profile_images"):
        return None
    if not u.startswith("http"):
        u = "https://pbs.twimg.com/" + u
    if u.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp") or "format=" in u:
        u = re.sub(r"[?&]name=[^&]*", "", u)
        u = u + ("&" if "?" in u else "?") + "name=medium"
    return u


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
            link = item.findtext("link","").strip()
            # nitter 域名用户打不开，重写为 x.com 规范链接，并顺带反推 tweet_id
            m = re.search(r"/status/(\d+)", link)
            tid = m.group(1) if m else ""
            if tid:
                link = re.sub(r"^https?://[^/]+/([^/]+)/status/", r"https://x.com/\1/status/", link)
            entry = {
                "text": desc or title,
                "link": link,
                "date": item.findtext("pubDate","").strip(),
            }
            if tid:
                entry["tweet_id"] = tid
            # enclosure 是 nitter RSS 携带的推文配图
            medias = []
            for enc in item.findall("enclosure"):
                u = _pbs_from_nitter_pic(enc.get("url", "") or "")
                if u and (enc.get("type", "") or "").startswith("image"):
                    medias.append({"type": "photo", "url": u, "full": re.sub(r"name=medium", "name=large", u)})
            if medias:
                entry["media"] = medias
            tweets.append(entry)
        return tweets or None
    except ET.ParseError:
        return None


def fetch_via_nitter(username):
    print(f"  [Method 3] Nitter RSS...")
    instances = _nitter_instances()
    for instance in instances:
        print(f"    Trying {instance}")
        content = fetch(f"{instance}/{username}/rss", timeout=15)
        if content and "<rss" in content:
            tweets = parse_rss(content)
            if tweets:
                n_enc = content.count("<enclosure")
                n_media = sum(1 for t in tweets if t.get("media"))
                print(f"    OK: {instance} ({len(tweets)} tweets, {n_enc} enclosure, {n_media} with media)")
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
    # 本次抓取的记录优先（可能携带 media 等新字段），历史记录兑底
    for t in tweets + existing:
        key = tweet_key(t)
        if key and key not in merged:
            merged[key] = t

    # 回填：本次抓取记录缺失、但历史里已有的字段（如翻译 text_zh）
    existing_map = {tweet_key(t): t for t in existing if tweet_key(t)}
    for key, t in merged.items():
        old = existing_map.get(key)
        if old and old is not t:
            for f, v in old.items():
                if v and not t.get(f):
                    t[f] = v

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

def fetch_via_jina_proxy(username, show_replies=False):
    """通过 r.jina.ai 中转访问 Syndication API：出口 IP 为 Jina 机房，
    可绕过 Twitter 对 GitHub runner IP 段的 429 限流。仅在直连全部失败时兜底。"""
    print("  [Method 4] Syndication via r.jina.ai proxy...")
    # Jina 会按 URL 缓存响应，导致拿到旧快照：加时间戳参数破缓存，并显式禁缓存
    bust = int(time.time())
    sr = "true" if show_replies else "false"
    target = (
        f"https://syndication.twitter.com/srv/timeline-profile/screen-name/{username}"
        f"?count={COUNT}&showReplies={sr}&_cb={bust}"
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


def _nitter_instances():
    """静态实例列表 + 从 status.d420.de 动态发现当前健康的实例（放前面优先试）。
    Nitter 实例存活情况变化快，静态列表经常全军覆没。"""
    dyn = []
    try:
        d = fetch_json("https://status.d420.de/api/v1/instances")
        for inst in (d or {}).get("instances") or []:
            host = (inst or {}).get("host") if isinstance(inst, dict) else None
            if host:
                url = host if host.startswith("http") else f"https://{host}"
                if url not in dyn:
                    dyn.append(url)
    except (OSError, ValueError):
        pass
    out = list(dyn)
    for b in NITTER_INSTANCES:
        if b not in out:
            out.append(b)
    return out


def fetch_via_sotwe(username):
    """Sotwe 第三方推特查看器（只读镜像），Twitter 全渠道限流时的兜底数据源。
    响应结构为非公开接口，做防御式解析；日期优先由 snowflake ID 反推（最可靠）。"""
    print("  [Method 5] Sotwe viewer API...")
    # 403 多为 WAF 拦默认请求：补齐浏览器同源头 + 多路径变体逐个尝试
    sotwe_headers = {
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://sotwe.com/{username}",
    }
    content = None
    for base in ("https://sotwe.com", "https://www.sotwe.com"):
        for path in (f"/api/3/user/{username}", f"/api/v2/user/{username}"):
            url = base + path
            print(f"    Trying {url}")
            c = fetch(url, headers={**sotwe_headers, "Referer": base + "/" + username}, timeout=20)
            if c and c.lstrip()[:1] in ("{", "["):
                content = c
                break
            if c:
                print(f"    sotwe: non-JSON response (len={len(c)})")
        if content:
            break
    if not content:
        print("    FAIL sotwe: no response")
        return None
    try:
        d = json.loads(content)
    except json.JSONDecodeError:
        print("    FAIL sotwe: not JSON")
        return None
    items = d.get("tweets") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        tid = str(it.get("id") or it.get("tweet_id") or "")
        text = it.get("full_text") or it.get("text") or ""
        if not tid.isdigit() or not text.strip():
            continue
        date = it.get("created_at") or it.get("time") or ""
        try:
            ts_ms = (int(tid) >> 22) + 1288834974657  # snowflake -> 毫秒时间戳
            date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
        except (ValueError, OSError):
            pass
        item = {"text": text, "link": f"https://x.com/{username}/status/{tid}", "date": date, "tweet_id": tid}
        medias = []
        for m in it.get("medias") or []:
            if not isinstance(m, dict):
                continue
            mt = m.get("type")
            u = m.get("url") or ""
            if mt == "photo" and u:
                medias.append({"type": "photo", "url": u, "full": u})
            elif mt in ("video", "gif"):
                medias.append({"type": "video" if mt == "video" else "animated_gif",
                               "url": "", "thumb": m.get("thumbnail") or m.get("thumbnail_url") or ""})
        if medias:
            item["media"] = medias
        out.append(item)
    if out:
        print(f"    OK: sotwe ({len(out)} tweets)")
        return out
    print("    FAIL sotwe: 0 tweets parsed")
    return None


# ── Method 6: Jina 渲染 Nitter 网页版（RSS 被 451 拦但网页活着）──────────────

NITTER_HTML_INSTANCES = (
    "https://xcancel.com",
    "https://nitter.tiekoetter.com",
    "https://nitter.cz",
)


def _nitter_media_urls(chunk):
    """从 nitter 单条推文 HTML 里提取图片/视频封面，并还原为 pbs.twimg.com 原始地址。"""
    medias = []
    # 图片：附件原图 <a href="/pic/orig/media%2Fxxx.jpg">（排除头像 profile_images）
    for href in re.findall(r'href="(/pic/[^"]+)"', chunk):
        u = urllib.parse.unquote(href[len("/pic/"):])
        u = u[len("orig/"):] if u.startswith("orig/") else u
        if u.startswith("profile_images") or u.startswith("/profile_images"):
            continue  # 头像，不是推文配图
        if not u.startswith("http"):
            u = "https://pbs.twimg.com/" + u
        if u.rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp") or "format=" in u:
            u = re.sub(r"[?&]name=[^&]*", "", u)
            sep = "&" if "?" in u else "?"
            u = u + sep + "name=medium"
        medias.append({"type": "photo", "url": u, "full": re.sub(r"name=medium", "name=large", u)})
    # 视频/GIF：video poster="/pic/ext_tw_video_thumb/..." 或 .gif 后缀封面
    posters = re.findall(r'poster="(/pic/[^"]+)"', chunk) + re.findall(
        r'<img[^>]+src="(/pic/(?:ext_tw_video_thumb|amplify_video_thumb|tweet_video_thumb)/[^"]+)"', chunk)
    for p in posters:
        u = "https://pbs.twimg.com/" + urllib.parse.unquote(p[len("/pic/"):])
        if {"type": "video", "url": "", "thumb": u} not in medias and not any(
                m.get("url") == u or m.get("thumb") == u for m in medias):
            medias.append({"type": "video", "url": "", "thumb": u})
    return medias


def fetch_via_nitter_html(username):
    """Jina 以无头浏览器渲染 nitter 网页版（返回完整 HTML），绕过 RSS 451 与 WAF。
    nitter 页面结构多年稳定：timeline-item / tweet-content / tweet-date。"""
    print("  [Method 6] Nitter HTML page via Jina render...")
    headers = {
        "X-Return-Format": "html",
        "X-No-Cache": "true",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for base in NITTER_HTML_INSTANCES:
        target = f"{base}/{username}"
        print(f"    Trying {target}")
        content = fetch("https://r.jina.ai/" + target, headers=headers, timeout=60)
        if not content:
            continue
        if "timeline-item" not in content:
            print(f"    [nitter-html] no timeline-item (len={len(content)})")
            continue
        chunks = content.split('<div class="timeline-item')[1:]
        out = []
        for c in chunks:
            c = "<div class=\"timeline-item" + c
            m_id = re.search(r'href="/[^"]*?/status/(\d+)"', c)
            m_txt = re.search(r'<div class="tweet-content[^>]*>(.*?)</div>', c, re.DOTALL)
            text = html_mod.unescape(re.sub(r"<[^>]+>", "", m_txt.group(1))).strip() if m_txt else ""
            if not m_id or (not text and not _nitter_media_urls(c)):
                continue
            tid = m_id.group(1)
            try:
                ts_ms = (int(tid) >> 22) + 1288834974657
                date = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
            except (ValueError, OSError):
                date = ""
            item = {"text": text, "link": f"https://x.com/{username}/status/{tid}", "date": date, "tweet_id": tid}
            medias = _nitter_media_urls(c)
            if medias:
                item["media"] = medias
            out.append(item)
        if out:
            print(f"    OK: nitter-html {base} ({len(out)} tweets)")
            return out
        print(f"    [nitter-html] {base}: timeline found but 0 tweets parsed")
    print("    FAIL nitter-html: all instances")
    return None


def fetch_tweets(username):
    show_replies = SHOW_REPLIES_OVERRIDES.get(username.lower(), False)
    # 方法1: Syndication API（最稳定，对高流量账号也有效）
    tweets = fetch_via_syndication(username, show_replies)
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
    tweets = fetch_via_jina_proxy(username, show_replies)
    if tweets:
        return tweets

    # 方法5: Sotwe 第三方查看器（Twitter 直接限流时的最后数据源）
    tweets = fetch_via_sotwe(username)
    if tweets:
        return tweets

    # 方法6: Jina 渲染 nitter 网页版（RSS 451 后的替代通道）
    tweets = fetch_via_nitter_html(username)
    if tweets:
        return tweets

    # 最后重试 Syndication（429 是间歇性的：Twitter 按出口 IP 段限流，
    # 多退避几次常能换到未触限的窗口；全部失败才放弃本轮）
    tweets = None
    for wait in (15, 45, 90):
        print(f"  Retrying Syndication after {wait}s...")
        time.sleep(wait)
        tweets = fetch_via_syndication(username, show_replies)
        if tweets:
            return tweets
    return tweets


def _extract_quoted(q):
    """从 quoted_status / fxtwitter 推文对象提取被引用原推的展示信息。"""
    if not isinstance(q, dict):
        return None
    user = ""
    u = q.get("user")
    if isinstance(u, dict):
        user = u.get("screen_name") or u.get("name") or ""
    out = {"user": user, "text": (q.get("full_text") or q.get("text") or "")}
    media = _extract_media(q)
    if media:
        out["media"] = media
    return out if (out["user"] or out["text"] or media) else None


def _attach_synd_context(tweet, item):
    """syndication 载荷自带 quoted_status 时直接提取（免额外请求）。"""
    if isinstance(tweet.get("quoted_status"), dict):
        quoted = _extract_quoted(tweet["quoted_status"])
        if quoted:
            item["quoted"] = quoted
            item["ctx_done"] = 1


def _quoted_from_fxt(tw):
    """从 fxtwitter 推文对象提取展示信息。"""
    author = tw.get("author") or {}
    out = {"user": author.get("screen_name") or "", "text": tw.get("text") or ""}
    media = []
    for m in ((tw.get("media") or {}).get("all")) or []:
        mt = m.get("type")
        if mt == "photo" and m.get("url"):
            media.append({"type": "photo", "url": m["url"], "full": m["url"]})
        elif mt in ("video", "gif"):
            media.append({"type": "video" if mt == "video" else "animated_gif", "url": "", "thumb": m.get("thumbnail_url") or ""})
    if media:
        out["media"] = media
    return out if (out["user"] or out["text"] or media) else None


def strip_quoted_dup(tweets):
    """nitter RSS 会把被引用推文的作者/正文/署名链接内联进正文，
    与 enrich 出的 quoted 上下文重复展示。清洗正文并让译文重译。"""
    n = 0
    for t in tweets:
        q = t.get("quoted") or {}
        qt, qu = q.get("text") or "", q.get("user") or ""
        text = t.get("text") or ""
        if not qt or qt not in text:
            continue
        # 先整串移除内联引用（quoted.text 可能含换行，逐行匹配不到）
        cleaned = text.replace(qt, "")
        keep = []
        for ln in cleaned.split("\n"):
            s = ln.strip()
            if s.startswith("—") and re.search(r"https?://\S+/status/\d+", s):
                continue  # nitter 引用署名行
            if qu and re.fullmatch(r".*\(@" + re.escape(qu) + r"\)", s):
                continue  # 被引用作者行（如 "DHH (@dhh)"）
            if ln.strip():
                keep.append(ln)
        cleaned = re.sub(r"\n{3,}", "\n\n", "\n".join(keep)).strip()
        if cleaned and cleaned != text:
            t["text"] = cleaned
            t.pop("text_zh", None)  # 旧译文基于脏文本，删除后由 fill_translations 重译
            n += 1
        elif not cleaned:
            t["text"] = text  # 清洗后为空（纯引用推）则保留原文，仅去署名行
    return n


def enrich_reply_context(tweets, limit=30):
    """为回复/引用推文补充原推内容：syndication 未携带时，
    用 fxtwitter 公开接口查推文自身，识别引用（quote）与纯回复。
    结果存入推文记录（quoted / reply_to / ctx_done），merge 后持久化。"""
    processed = 0
    for t in tweets:
        if processed >= limit:
            break
        if not t.get("tweet_id"):
            # 旧 RSS 记录没有 tweet_id，从 status 链接反推
            m = re.search(r"/status/(\d+)", t.get("link", "") or "")
            if m:
                t["tweet_id"] = m.group(1)
        if t.get("quoted") or t.get("ctx_done") or not t.get("tweet_id"):
            continue
        try:
            ts = parse_date_flex(t.get("date", ""))
            if ts and (datetime.now(timezone.utc) - ts).days > 45:
                t["ctx_done"] = 1  # 太老的回复不去查（多半是被清理的上下文）
                continue
        except (ValueError, TypeError):
            pass
        tid = t["tweet_id"]
        try:
            d = fetch_json(FXTWITTER_API + tid)
        except (OSError, ValueError):
            t["ctx_done"] = 1
            processed += 1
            continue
        tw = (d or {}).get("tweet") or {}
        if not isinstance(tw, dict):
            t["ctx_done"] = 1
            processed += 1
            continue
        q = tw.get("quote")
        if isinstance(q, dict) and (q.get("text") or q.get("author")):
            quoted = _quoted_from_fxt(q)
            if quoted:
                t["quoted"] = quoted
        rt = tw.get("replying_to")
        sn = rt.get("screen_name") if isinstance(rt, dict) else (rt if isinstance(rt, str) else "")
        if sn and not t.get("quoted"):
            t["reply_to"] = sn
        t["ctx_done"] = 1
        processed += 1
        if t.get("quoted") or sn:
            time.sleep(0.15)
    return processed


def download_media(tweets):
    """把推文媒体下载到仓库 media/ 目录：国内网络无法直连 twimg，
    页面从 raw.githubusercontent.com（与数据文件同源，已验证可达）加载图片。"""
    os.makedirs(MEDIA_DIR, exist_ok=True)
    n = 0
    for t in tweets:
        tid = t.get("tweet_id") or "x"
        media_groups = [(t.get("media") or [], "")]
        q = t.get("quoted") or {}
        if q.get("media"):
            media_groups.append((q["media"], "q"))
        for group, tag in media_groups:
            for i, m in enumerate(group):
                fname = f"{tid}{tag}_{i}.jpg"
                if m.get("local") == fname and os.path.exists(os.path.join(MEDIA_DIR, fname)):
                    continue
                url = None
                if m.get("type") == "photo":
                    base = (m.get("url") or "").split("?")[0]
                    url = (base + "?format=jpg&name=small") if base else None
                else:
                    url = m.get("thumb")
                if not url:
                    continue
                if _dl(url, os.path.join(MEDIA_DIR, fname)):
                    m["local"] = fname
                    n += 1
    return n


def cleanup_media():
    """删除 data/*.json 中已不被任何推文引用的媒体文件（旧推被 31 天清理后）"""
    referenced = set()
    for f in glob.glob("data/*.json"):
        if f.endswith("_summary.json"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                for t in json.load(fh).get("tweets", []):
                    for m in t.get("media") or []:
                        if m.get("local"):
                            referenced.add(m["local"])
        except (json.JSONDecodeError, OSError):
            pass
    removed = 0
    if os.path.isdir(MEDIA_DIR):
        for fn in os.listdir(MEDIA_DIR):
            if fn not in referenced:
                os.remove(os.path.join(MEDIA_DIR, fn))
                removed += 1
    return removed


def _dl(url, dest):
    """下载二进制文件；内容过小视为错误页。"""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
        if len(data) < 1000:
            return False
        with open(dest, "wb") as f:
            f.write(data)
        return True
    except (urllib.error.URLError, OSError, socket.timeout):
        return False


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
            n_ctx = enrich_reply_context(merged_tweets)
            n_strip = strip_quoted_dup(merged_tweets)
            translated = fill_translations(merged_tweets, TRANSLATE_PER_RUN)
            n_media = download_media(merged_tweets)
            output = {
                "username": username,
                "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "count": len(merged_tweets),
                "tweets": merged_tweets,
            }
            path = f"data/{username.lower()}.json"
            with open(path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, indent=2)
            print(f"  Saved {len(merged_tweets)} tweets (fetched {len(tweets)}, translated {translated}, ctx {n_ctx}, dedup {n_strip}, media downloaded {n_media}) -> {path}")
            summary[username] = len(merged_tweets)
        else:
            print(f"  FAILED: @{username}")
            # 抓取失败（如被限流）也让存量推文继续补翻译和引用上下文，逐轮消化
            path = f"data/{username.lower()}.json"
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        existing_data = json.load(f)
                    existing_tweets = existing_data.get("tweets", [])
                    n = fill_translations(existing_tweets, TRANSLATE_PER_RUN)
                    n_ctx = enrich_reply_context(existing_tweets)
                    n_strip = strip_quoted_dup(existing_tweets)
                    n_media = download_media(existing_tweets)
                    if n or n_ctx or n_media or n_strip:
                        existing_data["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                        existing_data["count"] = len(existing_tweets)
                        with open(path, "w", encoding="utf-8") as f:
                            json.dump(existing_data, f, ensure_ascii=False, indent=2)
                        print(f"  Backfilled {n} translations, ctx {n_ctx}, dedup {n_strip}, media {n_media} -> {path}")
                except (json.JSONDecodeError, OSError) as e:
                    print(f"  Backfill skipped: {e}")
            summary[username] = 0

    with open("data/_summary.json", "w") as f:
        json.dump({
            "updated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "users": summary,
        }, f, ensure_ascii=False, indent=2)

    removed = cleanup_media()
    if removed:
        print(f"Cleaned {removed} orphan media files")

    print(f"\n{'='*40}\nResult: {summary}")
    failed = [u for u, c in summary.items() if c == 0]
    if failed:
        print(f"WARNING: {len(failed)} user(s) failed: {failed}")


if __name__ == "__main__":
    main()
