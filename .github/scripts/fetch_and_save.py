#!/usr/bin/env python3
"""
GitHub Actions 运行脚本。
方法优先级:
  1. Twitter Guest API (直连 Twitter，GitHub Actions 服务器在境外，可直接访问)
  2. Nitter RSS (动态测试实例列表)
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
from datetime import datetime, timezone

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

COUNT = 20
HEADERS_COMMON = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) Chrome/122.0.0.0 Safari/537.36"
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


# ── Method 1: Twitter Guest API ───────────────────────────────────────────────

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
    print(f"  [Method 1] Twitter Guest API...")
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


# ── Method 2: Nitter RSS ───────────────────────────────────────────────────────

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
    print(f"  [Method 2] Nitter RSS...")
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

def fetch_tweets(username):
    tweets = fetch_via_guest_api(username)
    if not tweets:
        tweets = fetch_via_nitter(username)
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
            print(f"  Saved {len(tweets)} tweets -> {path}")
            summary[username] = len(tweets)
        else:
            print(f"  FAILED: @{username}")
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
