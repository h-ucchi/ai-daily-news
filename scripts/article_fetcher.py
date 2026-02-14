"""
記事取得ユーティリティ

URLから記事のタイトルと本文を取得する共通関数。
generate_post_manual.py の記事取得ロジックを共通化。
X (Twitter) URL対応版。
"""

import os
import re
import requests
from bs4 import BeautifulSoup
from typing import Tuple, Optional
import feedparser


def is_x_url(url: str) -> Optional[str]:
    """XのツイートURLか判定し、ツイートIDを返す

    Args:
        url: チェック対象URL

    Returns:
        ツイートID（文字列）またはNone
    """
    # 対応パターン:
    # - https://twitter.com/{username}/status/{tweet_id}
    # - https://x.com/{username}/status/{tweet_id}
    # - https://mobile.twitter.com/{username}/status/{tweet_id}
    pattern = r'https?://(?:www\.)?(twitter\.com|x\.com|mobile\.twitter\.com)/\w+/status/(\d+)'

    match = re.search(pattern, url)
    if match:
        return match.group(2)  # ツイートID
    return None


def fetch_tweet_content(url: str) -> Tuple[str, str]:
    """Xツイートから投稿内容を取得

    Args:
        url: ツイートURL

    Returns:
        (title, content) のタプル

    Raises:
        ValueError: X_BEARER_TOKEN未設定、またはツイート取得失敗
    """
    tweet_id = is_x_url(url)
    if not tweet_id:
        raise ValueError("無効なX URL")

    # 環境変数チェック
    bearer_token = os.environ.get("X_BEARER_TOKEN")
    if not bearer_token:
        raise ValueError(
            "❌ エラー: 環境変数 X_BEARER_TOKEN が設定されていません\n\n"
            ".claude/settings.local.json に以下を追加してください：\n\n"
            "{\n"
            '  "env": {\n'
            '    "X_BEARER_TOKEN": "..."\n'
            "  }\n"
            "}"
        )

    # X API呼び出し
    from x_api_client import XAPIClient
    client = XAPIClient(bearer_token)
    result = client.get_tweet_by_id(tweet_id)

    if not result:
        raise ValueError(f"ツイートを取得できませんでした: {tweet_id}")

    # レスポンスからデータ抽出
    tweet_data = result.get("data")
    includes = result.get("includes", {})
    users = includes.get("users", [])

    # タイトル: @username の投稿
    if users:
        author = users[0]
        title = f"@{author['username']} の投稿"
    else:
        title = "X (Twitter) の投稿"

    # 本文: ツイートテキスト
    content = tweet_data.get("text", "")

    return title, content


def fetch_article_content(url: str, timeout: int = 30) -> Tuple[str, str]:
    """記事を取得してタイトルと本文を返す（X URL対応版）

    Args:
        url: 記事URL（通常のWebページ or X URL）
        timeout: タイムアウト時間（秒）

    Returns:
        (title, content) のタプル

    Raises:
        requests.exceptions.RequestException: HTTP リクエストが失敗した場合
    """
    # X URLの場合は専用ロジック
    if is_x_url(url):
        return fetch_tweet_content(url)

    # 既存のHTMLスクレイピングロジック
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')

    # タイトル取得
    title = soup.find('title')
    title_text = title.get_text(strip=True) if title else "Unknown"

    # 不要タグを削除
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()

    # 本文取得
    content = soup.get_text(separator='\n', strip=True)

    return title_text, content


def fetch_article_content_safe(url: str, timeout: int = 30) -> Tuple[Optional[str], Optional[str]]:
    """記事を取得してタイトルと本文を返す（エラーを握りつぶす版）

    Args:
        url: 記事URL
        timeout: タイムアウト時間（秒）

    Returns:
        (title, content) のタプル。失敗時は (None, None)
    """
    try:
        return fetch_article_content(url, timeout)
    except Exception as e:
        print(f"⚠️ 記事取得失敗: {url} - {e}")
        return None, None


def fetch_rss_feed_safe(feed_url: str, timeout: int = 30) -> feedparser.FeedParserDict:
    """Cloudflare保護を回避してRSSフィードを取得

    Args:
        feed_url: RSSフィードのURL
        timeout: タイムアウト時間（秒）

    Returns:
        feedparser.FeedParserDict: パース済みのRSSフィード

    Note:
        OpenAIなどCloudflare保護があるサイトに対応するため、
        cloudscraperを使用してRSS XMLを取得
    """
    try:
        import cloudscraper

        # Cloudflare回避可能なscraperを作成
        scraper = cloudscraper.create_scraper(
            browser={
                'browser': 'chrome',
                'platform': 'darwin',
                'desktop': True
            }
        )

        response = scraper.get(feed_url, timeout=timeout)
        response.raise_for_status()

        # XMLテキストをfeedparserに渡す
        return feedparser.parse(response.text)

    except ImportError:
        print(f"⚠️ cloudscraperが未インストール。通常のrequestsで試行")
        # フォールバック: 通常のrequests
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        }
        response = requests.get(feed_url, headers=headers, timeout=timeout)
        response.raise_for_status()
        return feedparser.parse(response.text)

    except Exception as e:
        print(f"⚠️ RSS取得失敗: {feed_url} - {e}")
        # エラー時は空のフィードを返す
        return feedparser.FeedParserDict()
