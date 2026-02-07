"""
記事取得ユーティリティ

URLから記事のタイトルと本文を取得する共通関数。
generate_post_manual.py の記事取得ロジックを共通化。
"""

import requests
from bs4 import BeautifulSoup
from typing import Tuple, Optional


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
        "User-Agent": "Mozilla/5.0 (compatible; AIResearchBot/1.0)"
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
