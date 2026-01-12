#!/usr/bin/env python3
"""
AIデイリーレポート自動生成スクリプト
X API Basic ($200/月) に収まるよう設計
"""

import os
import json
import yaml
import requests
import feedparser
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, asdict
from collections import defaultdict
import time


@dataclass
class Item:
    """収集した情報アイテム"""
    source: str  # "x_account", "x_search", "rss", "github"
    title: str
    url: str
    published_at: str
    score: int = 0
    metadata: Dict[str, Any] = None

    def __post_init__(self):
        if self.metadata is None:
            self.metadata = {}


class StateManager:
    """状態管理（state.json）"""

    def __init__(self, state_path: str = "data/state.json"):
        self.state_path = state_path
        self.state = self._load()

    def _load(self) -> Dict:
        """state.json を読み込み"""
        if os.path.exists(self.state_path):
            with open(self.state_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {
            "x_accounts": {},
            "x_keywords": {},
            "rss": {},
            "github": {},
            "meta": {"last_run_at": None, "version": "1.0.0"}
        }

    def save(self):
        """state.json を保存"""
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

    def get_github_last_tag(self, repo: str) -> Optional[str]:
        """GitHubリポジトリの最終tagを取得"""
        return self.state["github"].get(repo, {}).get("tag")

    def set_github_last_tag(self, repo: str, tag: str):
        """GitHubリポジトリの最終tagを更新"""
        if repo not in self.state["github"]:
            self.state["github"][repo] = {}
        self.state["github"][repo]["tag"] = tag


class XAPIClient:
    """X (Twitter) API v2 クライアント"""

    def __init__(self, bearer_token: str):
        self.bearer_token = bearer_token
        self.base_url = "https://api.twitter.com/2"
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

    def get_user_id(self, username: str) -> Optional[str]:
        """ユーザー名からユーザーIDを取得"""
        url = f"{self.base_url}/users/by/username/{username}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", {}).get("id")
        return None

    def get_user_tweets(self, user_id: str, since_id: Optional[str] = None, max_results: int = 10) -> List[Dict]:
        """ユーザーのツイートを取得（新着のみ）"""
        url = f"{self.base_url}/users/{user_id}/tweets"
        params = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics",
            "expansions": "author_id"
        }
        if since_id:
            params["since_id"] = since_id
        else:
            # 初回実行時は直近24時間
            start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            params["start_time"] = start_time

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", [])
        return []

    def search_tweets(self, query: str, since_id: Optional[str] = None, max_results: int = 10) -> List[Dict]:
        """キーワードでツイート検索（新着のみ）"""
        url = f"{self.base_url}/tweets/search/recent"
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics",
            "expansions": "author_id"
        }
        if since_id:
            params["since_id"] = since_id
        else:
            # 初回実行時は直近24時間
            start_time = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            params["start_time"] = start_time

        response = requests.get(url, headers=self.headers, params=params)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", [])
        return []


class DataCollector:
    """データ収集オーケストレーター"""

    def __init__(self, config: Dict, state: StateManager, x_client: XAPIClient):
        self.config = config
        self.state = state
        self.x_client = x_client
        self.items: List[Item] = []
        self.stats = {
            "x_accounts_fetched": 0,
            "x_search_fetched": 0,
            "x_total_fetched": 0,
            "x_limit_reached": False,
            "rss_fetched": 0,
            "github_fetched": 0,
            "duplicates_removed": 0
        }

    def collect_all(self):
        """全データソースから収集"""
        print("🔍 データ収集を開始...")

        # X (Twitter)
        self._collect_x_accounts()
        self._collect_x_search()

        # RSS
        self._collect_rss()

        # GitHub
        self._collect_github()

        # 重複排除
        self._deduplicate()

        print(f"✅ 収集完了: {len(self.items)} 件")

    def _collect_x_accounts(self):
        """Xアカウントからツイート収集"""
        accounts = self.config["x"]["accounts"]
        limit = self.config["x"]["limits"]["accounts"]
        fetched = 0

        print(f"📱 Xアカウント監視: {len(accounts)} アカウント")

        for username in accounts:
            if fetched >= limit:
                print(f"⚠️  アカウント監視の上限 {limit} 件に到達")
                self.stats["x_limit_reached"] = True
                break

            user_id = self.x_client.get_user_id(username)
            if not user_id:
                continue

            since_id = self.state.get_x_account_since_id(username)
            tweets = self.x_client.get_user_tweets(user_id, since_id, max_results=10)

            if not tweets:
                continue

            # 最新のtweet_idを保存
            max_id = max(int(t["id"]) for t in tweets)
            self.state.set_x_account_since_id(username, user_id, str(max_id))

            for tweet in tweets:
                if fetched >= limit:
                    break

                item = Item(
                    source="x_account",
                    title=tweet["text"][:100],
                    url=f"https://twitter.com/{username}/status/{tweet['id']}",
                    published_at=tweet["created_at"],
                    score=self._calculate_engagement_score(tweet),
                    metadata={"username": username, "tweet": tweet}
                )
                self.items.append(item)
                fetched += 1

        self.stats["x_accounts_fetched"] = fetched
        self.stats["x_total_fetched"] += fetched

    def _collect_x_search(self):
        """Xキーワード検索"""
        keywords = self.config["x"]["keywords"]
        limit = self.config["x"]["limits"]["search"]
        fetched = 0

        print(f"🔎 Xキーワード検索: {len(keywords)} キーワード")

        for keyword in keywords:
            if fetched >= limit:
                print(f"⚠️  検索の上限 {limit} 件に到達")
                self.stats["x_limit_reached"] = True
                break

            since_id = self.state.get_x_keyword_since_id(keyword)
            tweets = self.x_client.search_tweets(keyword, since_id, max_results=10)

            if not tweets:
                continue

            # 最新のtweet_idを保存
            max_id = max(int(t["id"]) for t in tweets)
            self.state.set_x_keyword_since_id(keyword, str(max_id))

            for tweet in tweets:
                if fetched >= limit:
                    break

                item = Item(
                    source="x_search",
                    title=tweet["text"][:100],
                    url=f"https://twitter.com/i/web/status/{tweet['id']}",
                    published_at=tweet["created_at"],
                    score=self._calculate_engagement_score(tweet),
                    metadata={"keyword": keyword, "tweet": tweet}
                )
                self.items.append(item)
                fetched += 1

        self.stats["x_search_fetched"] = fetched
        self.stats["x_total_fetched"] += fetched

    def _collect_rss(self):
        """RSS収集"""
        feeds = self.config["rss"]["feeds"]
        fetched = 0

        print(f"📰 RSS収集: {len(feeds)} フィード")

        for feed_config in feeds:
            feed_url = feed_config["url"]
            feed_name = feed_config["name"]

            feed = feedparser.parse(feed_url)
            last_published = self.state.get_rss_last_published(feed_url)

            for entry in feed.entries:
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published:
                    continue

                published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                published_iso = published_dt.isoformat()

                # 新着のみ
                if last_published and published_iso <= last_published:
                    continue

                item = Item(
                    source="rss",
                    title=entry.title,
                    url=entry.link,
                    published_at=published_iso,
                    score=self.config["slack"]["scoring"]["rss_bonus"],
                    metadata={"feed_name": feed_name}
                )
                self.items.append(item)
                fetched += 1

            # 最新の published_at を保存
            if feed.entries:
                latest = max(feed.entries, key=lambda e: e.get("published_parsed") or e.get("updated_parsed"))
                published = latest.get("published_parsed") or latest.get("updated_parsed")
                if published:
                    published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    self.state.set_rss_last_published(feed_url, published_dt.isoformat())

        self.stats["rss_fetched"] = fetched

    def _collect_github(self):
        """GitHub Releases収集"""
        repos = self.config["github"]["repositories"]
        fetched = 0

        print(f"🐙 GitHub Releases: {len(repos)} リポジトリ")

        github_token = os.environ.get("GITHUB_TOKEN")
        headers = {"Authorization": f"token {github_token}"} if github_token else {}

        for repo in repos:
            url = f"https://api.github.com/repos/{repo}/releases/latest"
            response = requests.get(url, headers=headers)

            if response.status_code != 200:
                continue

            release = response.json()
            tag = release["tag_name"]
            last_tag = self.state.get_github_last_tag(repo)

            # 同じtagの場合はスキップ
            if last_tag == tag:
                continue

            self.state.set_github_last_tag(repo, tag)

            item = Item(
                source="github",
                title=f"{repo} {tag}: {release['name']}",
                url=release["html_url"],
                published_at=release["published_at"],
                score=self.config["slack"]["scoring"]["github_bonus"],
                metadata={"repo": repo, "tag": tag}
            )
            self.items.append(item)
            fetched += 1

        self.stats["github_fetched"] = fetched

    def _calculate_engagement_score(self, tweet: Dict) -> int:
        """エンゲージメントスコア計算"""
        metrics = tweet.get("public_metrics", {})
        scoring = self.config["slack"]["scoring"]

        score = (
            metrics.get("like_count", 0) * scoring["like_weight"] +
            metrics.get("retweet_count", 0) * scoring["retweet_weight"] +
            metrics.get("reply_count", 0) * scoring["reply_weight"]
        )
        return score

    def _deduplicate(self):
        """重複排除（URL基準）"""
        seen_urls = set()
        unique_items = []

        for item in self.items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                unique_items.append(item)
            else:
                self.stats["duplicates_removed"] += 1

        self.items = unique_items


class SlackReporter:
    """Slackレポート生成・投稿"""

    def __init__(self, webhook_url: str, config: Dict, items: List[Item], stats: Dict):
        self.webhook_url = webhook_url
        self.config = config
        self.items = items
        self.stats = stats

    def send(self):
        """レポートを生成してSlackに投稿"""
        print("📤 Slackレポート生成中...")

        # スコア順にソート
        sorted_items = sorted(self.items, key=lambda x: x.score, reverse=True)

        # セクション分け
        top_items = sorted_items[:self.config["slack"]["limits"]["top"]]
        provider_items = [i for i in sorted_items if i.source == "rss"][:self.config["slack"]["limits"]["provider_official"]]
        github_items = [i for i in sorted_items if i.source == "github"][:self.config["slack"]["limits"]["github_updates"]]
        x_items = [i for i in sorted_items if i.source in ["x_account", "x_search"]][:self.config["slack"]["limits"]["x_signals"]]

        # Slack Blocks構築
        blocks = []

        # ヘッダー
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 AI Daily Report - {datetime.now().strftime('%Y-%m-%d')}"}
        })

        # Top
        if top_items:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🔥 Top Highlights*"}})
            for item in top_items:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• <{item.url}|{item.title}>"}})

        # Provider Official
        if provider_items:
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📰 Provider Official / RSS*"}})
            for item in provider_items:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• <{item.url}|{item.title}>"}})

        # GitHub Updates
        if github_items:
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🐙 GitHub Updates*"}})
            for item in github_items:
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• <{item.url}|{item.title}>"}})

        # X Signals
        if x_items:
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*📱 X (Twitter) Signals*"}})
            for item in x_items[:10]:  # 最大10件表示
                blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"• <{item.url}|{item.title}>"}})

        # Stats
        blocks.append({"type": "divider"})
        stats_text = (
            f"*📈 Stats*\n"
            f"• X取得数: {self.stats['x_total_fetched']} 件 "
            f"(アカウント: {self.stats['x_accounts_fetched']}, 検索: {self.stats['x_search_fetched']})\n"
            f"• RSS: {self.stats['rss_fetched']} 件\n"
            f"• GitHub: {self.stats['github_fetched']} 件\n"
            f"• 重複除外: {self.stats['duplicates_removed']} 件\n"
        )
        if self.stats['x_limit_reached']:
            stats_text += "⚠️  *X API上限到達により一部取得を打ち切り*"
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": stats_text}})

        # X投稿素案を生成
        x_post_draft = self._generate_x_post_draft(top_items, provider_items, github_items)
        if x_post_draft:
            blocks.append({"type": "divider"})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*🐦 X投稿素案*"}})
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"```{x_post_draft}```"}})

        # 送信
        payload = {"blocks": blocks}
        response = requests.post(self.webhook_url, json=payload)

        if response.status_code == 200:
            print("✅ Slackに投稿しました")
        else:
            print(f"❌ Slack投稿失敗: {response.status_code} {response.text}")
            raise Exception("Slack投稿に失敗しました")

    def _generate_x_post_draft(self, top_items: List[Item], provider_items: List[Item], github_items: List[Item]) -> str:
        """X投稿素案を生成"""
        today = datetime.now().strftime('%Y/%m/%d')
        lines = [f"📊 AI Daily Report - {today}", ""]

        # 主要ニュースをピックアップ
        highlights = []

        # RSS（公式発表）を優先
        for item in provider_items[:2]:
            feed_name = item.metadata.get("feed_name", "")
            highlights.append(f"🔹 {feed_name}: {item.title}")

        # GitHub重要リリース
        for item in github_items[:2]:
            repo = item.metadata.get("repo", "")
            tag = item.metadata.get("tag", "")
            highlights.append(f"🔹 {repo} {tag} リリース")

        # トップハイライト
        for item in top_items[:2]:
            if item.source == "rss" or item.source == "github":
                continue  # 既に追加済み
            title = item.title[:80] + "..." if len(item.title) > 80 else item.title
            highlights.append(f"🔹 {title}")

        # ハイライトを追加
        if highlights:
            lines.extend(highlights[:4])  # 最大4件
            lines.append("")

        # フッター
        lines.append("詳細はSlackをチェック👀")
        lines.append("#AI #MachineLearning #LLM")

        return "\n".join(lines)


def main():
    """メイン処理"""
    print("=" * 60)
    print("AIデイリーレポート自動生成")
    print("=" * 60)

    # 設定読み込み
    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 環境変数チェック
    x_bearer_token = os.environ.get("X_BEARER_TOKEN")
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not x_bearer_token:
        raise ValueError("環境変数 X_BEARER_TOKEN が設定されていません")
    if not slack_webhook_url:
        raise ValueError("環境変数 SLACK_WEBHOOK_URL が設定されていません")

    # 初期化
    state = StateManager()
    x_client = XAPIClient(x_bearer_token)
    collector = DataCollector(config, state, x_client)

    try:
        # データ収集
        collector.collect_all()

        # Slackレポート送信
        reporter = SlackReporter(slack_webhook_url, config, collector.items, collector.stats)
        reporter.send()

        # 状態保存（最後に実行）
        state.save()
        print("💾 状態を保存しました")

    except Exception as e:
        print(f"❌ エラー発生: {e}")
        raise

    print("=" * 60)
    print("✅ 完了")
    print("=" * 60)


if __name__ == "__main__":
    main()
