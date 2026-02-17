#!/usr/bin/env python3
"""
状態管理モジュール
run_daily.pyとrun_hourly.pyで共有される状態管理機能
"""

import os
import json
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional


class StateManager:
    """状態管理（state.json / state_hourly.json）"""

    def __init__(self, state_path: str):
        """
        Args:
            state_path: 状態ファイルのパス（例: "data/state.json"）
        """
        self.state_path = state_path
        self.state = self._load()

    def _load(self) -> Dict:
        """状態ファイルを読み込み"""
        if os.path.exists(self.state_path):
            with open(self.state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            "x_accounts": {},
            "x_keywords": {},
            "rss": {},
            "rss_articles": {},
            "rss_last_checked": {},
            "github": {},
            "recently_posted_urls": {},
            "meta": {"last_run_at": None, "version": "1.0.0"}
        }

    def save(self):
        """状態ファイルを保存"""
        self.state["meta"]["last_run_at"] = datetime.now(timezone.utc).isoformat()
        with open(self.state_path, 'w', encoding='utf-8') as f:
            json.dump(self.state, f, indent=2, ensure_ascii=False)

    def get_x_account_since_id(self, username: str) -> Optional[str]:
        """Xアカウントの since_id を取得"""
        return self.state["x_accounts"].get(username, {}).get("since_id")

    def set_x_account_since_id(self, username: str, user_id: str, since_id: str):
        """Xアカウントの since_id を更新"""
        if username not in self.state["x_accounts"]:
            self.state["x_accounts"][username] = {}
        self.state["x_accounts"][username]["user_id"] = user_id
        self.state["x_accounts"][username]["since_id"] = since_id

    def get_x_keyword_since_id(self, keyword: str) -> Optional[str]:
        """Xキーワードの since_id を取得"""
        return self.state["x_keywords"].get(keyword, {}).get("since_id")

    def set_x_keyword_since_id(self, keyword: str, since_id: str):
        """Xキーワードの since_id を更新"""
        if keyword not in self.state["x_keywords"]:
            self.state["x_keywords"][keyword] = {}
        self.state["x_keywords"][keyword]["since_id"] = since_id

    def get_rss_last_published(self, feed_url: str) -> Optional[str]:
        """RSSの最終取得日時を取得"""
        return self.state["rss"].get(feed_url)

    def set_rss_last_published(self, feed_url: str, published_at: str):
        """RSSの最終取得日時を更新"""
        self.state["rss"][feed_url] = published_at

    def get_rss_article_urls(self, feed_url: str) -> Optional[List[str]]:
        """RSSフィードの前回取得記事URLリストを取得"""
        if "rss_articles" not in self.state:
            self.state["rss_articles"] = {}
        return self.state["rss_articles"].get(feed_url)

    def set_rss_article_urls(self, feed_url: str, urls: List[str]):
        """RSSフィードの記事URLリストを保存（全件）"""
        if "rss_articles" not in self.state:
            self.state["rss_articles"] = {}
        self.state["rss_articles"][feed_url] = urls  # 全件保存

    def get_rss_last_checked(self, feed_url: str) -> Optional[str]:
        """RSSフィードの最終確認日時を取得"""
        if "rss_last_checked" not in self.state:
            self.state["rss_last_checked"] = {}
        return self.state["rss_last_checked"].get(feed_url)

    def set_rss_last_checked(self, feed_url: str, checked_at: str):
        """RSSフィードの最終確認日時を更新"""
        if "rss_last_checked" not in self.state:
            self.state["rss_last_checked"] = {}
        self.state["rss_last_checked"][feed_url] = checked_at

    def get_github_last_tag(self, repo: str) -> Optional[str]:
        """GitHubリポジトリの最終tagを取得"""
        return self.state["github"].get(repo, {}).get("tag")

    def set_github_last_tag(self, repo: str, tag: str):
        """GitHubリポジトリの最終tagを更新"""
        if repo not in self.state["github"]:
            self.state["github"][repo] = {}
        self.state["github"][repo]["tag"] = tag

    def is_recently_posted(self, url: str, hours: int = 24) -> bool:
        """過去N時間以内に投稿済みかチェック"""
        posted_urls = self.state.get("recently_posted_urls", {})
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

        if url in posted_urls:
            posted_at = posted_urls[url]
            return posted_at > cutoff
        return False

    def mark_as_posted(self, url: str):
        """投稿済みにマーク"""
        if "recently_posted_urls" not in self.state:
            self.state["recently_posted_urls"] = {}
        self.state["recently_posted_urls"][url] = datetime.now(timezone.utc).isoformat()

    def cleanup_old_posted_urls(self, hours: int = 24):
        """古い投稿履歴をクリーンアップ"""
        if "recently_posted_urls" not in self.state:
            return

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
        posted_urls = self.state["recently_posted_urls"]
        self.state["recently_posted_urls"] = {
            url: posted_at
            for url, posted_at in posted_urls.items()
            if posted_at > cutoff
        }

    def is_conversation_processed(self, conversation_id: str) -> bool:
        """スレッドが処理済みかチェック

        Args:
            conversation_id: チェックするスレッドのID

        Returns:
            処理済みの場合True
        """
        if "processed_conversations" not in self.state:
            self.state["processed_conversations"] = {}
        return conversation_id in self.state["processed_conversations"]

    def mark_conversation_processed(self, conversation_id: str):
        """スレッドを処理済みにマーク

        Args:
            conversation_id: マークするスレッドのID
        """
        if "processed_conversations" not in self.state:
            self.state["processed_conversations"] = {}
        self.state["processed_conversations"][conversation_id] = datetime.now(timezone.utc).isoformat()

    def cleanup_old_conversations(self, days: int = 7):
        """古いスレッド履歴をクリーンアップ（7日以上前）

        Args:
            days: 保持する日数（デフォルト: 7日）
        """
        if "processed_conversations" not in self.state:
            return

        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        processed = self.state["processed_conversations"]
        self.state["processed_conversations"] = {
            cid: ts
            for cid, ts in processed.items()
            if ts > cutoff
        }
