"""
記事取得ユーティリティ

URLから記事のタイトルと本文を取得する共通関数。
generate_post_manual.py の記事取得ロジックを共通化。
"""

import requests
from bs4 import BeautifulSoup
from typing import Tuple, Optional
import feedparser


def fetch_article_content(url: str, timeout: int = 30) -> Tuple[str, str]:
    """記事を取得してタイトルと本文を返す

    Args:
        url: 記事URL
        timeout: タイムアウト時間（秒）

    Returns:
        (title, content) のタプル

    Raises:
        requests.exceptions.RequestException: HTTP リクエストが失敗した場合
    """
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
