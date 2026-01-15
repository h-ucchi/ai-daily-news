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
from content_classifier import ContentClassifier, ClassificationResult


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

    # 優先度の高いRSSフィード（重要ベンダーの最新ニュースを漏らさないため）
    PRIORITY_FEEDS = {
        # 公式ブログ
        "https://www.anthropic.com/news/rss.xml": 1000,  # Anthropic最優先
        "https://openai.com/blog/rss.xml": 1000,          # OpenAI最優先
        "https://github.blog/feed/": 800,                 # GitHub Blog
        "https://code.visualstudio.com/updates/feed.xml": 800,  # VSCode Updates

        # GitHub Releases Atom Feed（必須フィードはmust_include_feedsで管理）
        "https://github.com/anthropics/claude-code/releases.atom": 800,  # 必須フィードに移行
        "https://github.blog/changelog/label/copilot/feed/": 800,        # 必須フィードに移行
        "https://github.com/langchain-ai/langchain/releases.atom": 800,   # LangChain
        "https://github.com/openai/openai-python/releases.atom": 800,     # OpenAI Python SDK
        "https://github.com/run-llama/llama_index/releases.atom": 600,    # LlamaIndex
        "https://github.com/huggingface/transformers/releases.atom": 600, # Transformers
    }

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
        # コンテンツ分類器の初期化
        if self.config.get("content_filtering", {}).get("enabled"):
            self.classifier = ContentClassifier(config)
        else:
            self.classifier = None

    def collect_all(self):
        """全データソースから収集"""
        print("🔍 データ収集を開始...")

        # X (Twitter)
        self._collect_x_accounts()
        self._collect_x_search()

        # RSS（GitHub Releases Atom Feedを含む）
        self._collect_rss()

        # 必須フィード（当日の更新があれば必ず含める）
        must_include_items = self._collect_must_include_feeds()
        self.items.extend(must_include_items)
        self.stats["must_include_fetched"] = len(must_include_items)

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

                # カテゴリ分類とスコア調整
                initial_score = self._calculate_engagement_score(tweet)
                category = "UNKNOWN"
                if self.classifier:
                    classification = self.classifier.classify_by_keywords(tweet["text"])
                    category = classification.category
                    final_score = self.classifier.calculate_final_score(initial_score, category, "x_account")
                else:
                    final_score = initial_score

                item = Item(
                    source="x_account",
                    title=tweet["text"][:100],
                    url=f"https://twitter.com/{username}/status/{tweet['id']}",
                    published_at=tweet["created_at"],
                    score=final_score,
                    metadata={
                        "username": username,
                        "tweet": tweet,
                        "category": category
                    }
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

                # カテゴリ分類とスコア調整
                initial_score = self._calculate_engagement_score(tweet)
                category = "UNKNOWN"
                if self.classifier:
                    classification = self.classifier.classify_by_keywords(tweet["text"])
                    category = classification.category
                    final_score = self.classifier.calculate_final_score(initial_score, category, "x_search")
                else:
                    final_score = initial_score

                item = Item(
                    source="x_search",
                    title=tweet["text"][:100],
                    url=f"https://twitter.com/i/web/status/{tweet['id']}",
                    published_at=tweet["created_at"],
                    score=final_score,
                    metadata={
                        "keyword": keyword,
                        "tweet": tweet,
                        "category": category
                    }
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

                # スコアリング: 基本スコア + 優先度ボーナス
                base_score = self.config["slack"]["scoring"]["rss_bonus"]  # 500
                priority_bonus = self.PRIORITY_FEEDS.get(feed_url, 0)
                initial_score = base_score + priority_bonus

                # カテゴリ分類とスコア調整
                category = "UNKNOWN"
                if self.classifier:
                    description = entry.get("summary", "")
                    classification = self.classifier.classify_by_keywords(entry.title, description)
                    category = classification.category
                    final_score = self.classifier.calculate_final_score(initial_score, category, "rss")
                else:
                    final_score = initial_score

                item = Item(
                    source="rss",
                    title=entry.title,
                    url=entry.link,
                    published_at=published_iso,
                    score=final_score,  # 重要フィード + カテゴリボーナス
                    metadata={
                        "feed_name": feed_name,
                        "feed_url": feed_url,
                        "category": category
                    }
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

    def _collect_must_include_feeds(self) -> List[Item]:
        """必ず含めるフィードから当日の更新を取得"""
        must_include_config = self.config.get("rss", {}).get("must_include_feeds", [])
        must_include_items = []

        if not must_include_config:
            return must_include_items

        print(f"⭐ 必須フィード収集: {len(must_include_config)} フィード")

        # 当日の日付（UTCで00:00:00）
        today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

        for feed_config in must_include_config:
            feed_url = feed_config["url"]
            feed_name = feed_config["name"]
            max_items = feed_config.get("max_items", 3)

            feed = feedparser.parse(feed_url)
            count = 0

            for entry in feed.entries:
                if count >= max_items:
                    break

                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published:
                    continue

                published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                published_iso = published_dt.isoformat()

                # 当日の更新のみ（00:00:00以降）
                if published_dt < today:
                    continue

                # カテゴリ分類
                category = "PRACTICAL"  # 必須フィードはデフォルトでPRACTICAL
                if self.classifier:
                    description = entry.get("summary", "")
                    classification = self.classifier.classify_by_keywords(entry.title, description)
                    category = classification.category

                item = Item(
                    source="must_include",
                    title=entry.title,
                    url=entry.link,
                    published_at=published_iso,
                    score=9999,  # 最高スコア（必ず含まれるようにするため）
                    metadata={
                        "feed_name": feed_name,
                        "feed_url": feed_url,
                        "category": category,
                        "must_include": True
                    }
                )
                must_include_items.append(item)
                count += 1
                print(f"  ✓ {feed_name}: {entry.title[:50]}...")

        return must_include_items

    def _select_diverse_provider_items(self, sorted_items: List[Item], limit: int) -> List[Item]:
        """
        優先度の高いフィードから最新記事を1件ずつバランスよく選択

        Args:
            sorted_items: スコア順にソート済みのアイテムリスト
            limit: 選択する最大件数

        Returns:
            バランスよく選択されたRSSアイテムのリスト
        """
        from collections import defaultdict

        # RSSアイテムのみ抽出
        rss_items = [i for i in sorted_items if i.source == "rss"]

        # フィード別にグループ化
        feed_groups = defaultdict(list)
        for item in rss_items:
            feed_url = item.metadata.get("feed_url", "")
            if feed_url:
                feed_groups[feed_url].append(item)

        # 優先順位順にソート（PRIORITY_FEEDS の定義順）
        priority_order = list(self.PRIORITY_FEEDS.keys())
        sorted_feed_urls = sorted(
            feed_groups.keys(),
            key=lambda url: priority_order.index(url) if url in priority_order else 9999
        )

        # ラウンドロビン方式で選択（各フィードから1件ずつ）
        selected = []
        round_num = 0

        while len(selected) < limit:
            added_this_round = False

            for feed_url in sorted_feed_urls:
                if len(selected) >= limit:
                    break

                items = feed_groups[feed_url]
                if round_num < len(items):
                    selected.append(items[round_num])
                    added_this_round = True

            # 全フィードから選択し終えた
            if not added_this_round:
                break

            round_num += 1

        return selected[:limit]

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

    # DataCollectorのPRIORITY_FEEDSと同じ定義
    PRIORITY_FEEDS = {
        # 公式ブログ
        "https://www.anthropic.com/news/rss.xml": 1000,
        "https://openai.com/blog/rss.xml": 1000,
        "https://github.blog/feed/": 800,
        "https://code.visualstudio.com/updates/feed.xml": 800,

        # GitHub Releases Atom Feed（必須フィードはmust_include_feedsで管理）
        "https://github.com/anthropics/claude-code/releases.atom": 800,
        "https://github.blog/changelog/label/copilot/feed/": 800,
        "https://github.com/langchain-ai/langchain/releases.atom": 800,
        "https://github.com/openai/openai-python/releases.atom": 800,
        "https://github.com/run-llama/llama_index/releases.atom": 600,
        "https://github.com/huggingface/transformers/releases.atom": 600,
    }

    def __init__(self, webhook_url: str, config: Dict, items: List[Item], stats: Dict):
        self.webhook_url = webhook_url
        self.config = config
        self.items = items
        self.stats = stats

    def _select_diverse_provider_items(self, sorted_items: List[Item], limit: int) -> List[Item]:
        """
        優先度の高いフィードから最新記事を1件ずつバランスよく選択

        Args:
            sorted_items: スコア順にソート済みのアイテムリスト
            limit: 選択する最大件数

        Returns:
            バランスよく選択されたRSSアイテムのリスト
        """
        from collections import defaultdict

        # RSSアイテムのみ抽出
        rss_items = [i for i in sorted_items if i.source == "rss"]

        # フィード別にグループ化
        feed_groups = defaultdict(list)
        for item in rss_items:
            feed_url = item.metadata.get("feed_url", "")
            if feed_url:
                feed_groups[feed_url].append(item)

        # 優先順位順にソート（PRIORITY_FEEDS の定義順）
        priority_order = list(self.PRIORITY_FEEDS.keys())
        sorted_feed_urls = sorted(
            feed_groups.keys(),
            key=lambda url: priority_order.index(url) if url in priority_order else 9999
        )

        # ラウンドロビン方式で選択（各フィードから1件ずつ）
        selected = []
        round_num = 0

        while len(selected) < limit:
            added_this_round = False

            for feed_url in sorted_feed_urls:
                if len(selected) >= limit:
                    break

                items = feed_groups[feed_url]
                if round_num < len(items):
                    selected.append(items[round_num])
                    added_this_round = True

            # 全フィードから選択し終えた
            if not added_this_round:
                break

            round_num += 1

        return selected[:limit]

    def send(self):
        """レポートを生成してSlackに投稿"""
        print("📤 Slackレポート生成中...")

        # スコア順にソート
        sorted_items = sorted(self.items, key=lambda x: x.score, reverse=True)

        # セクション分け
        top_items = sorted_items[:self.config["slack"]["limits"]["top"]]
        provider_items = self._select_diverse_provider_items(sorted_items, self.config["slack"]["limits"]["provider_official"])
        github_items = [i for i in sorted_items if i.source == "github"][:self.config["slack"]["limits"]["github_updates"]]

        # Slack Blocks構築
        blocks = []

        # ヘッダー
        blocks.append({
            "type": "header",
            "text": {"type": "plain_text", "text": f"🐦 X投稿素案 - {datetime.now().strftime('%Y-%m-%d')}"}
        })

        # X投稿素案を生成（個別のブロックとして追加）
        draft_blocks = self._generate_x_post_draft_blocks(top_items, provider_items, github_items, sorted_items)
        blocks.extend(draft_blocks)

        # 送信
        payload = {"blocks": blocks}
        response = requests.post(self.webhook_url, json=payload)

        if response.status_code == 200:
            print("✅ Slackに投稿しました")
        else:
            print(f"❌ Slack投稿失敗: {response.status_code} {response.text}")
            raise Exception("Slack投稿に失敗しました")

    def _generate_x_post_draft(self, top_items: List[Item], provider_items: List[Item], github_items: List[Item]) -> str:
        """X投稿素案を生成（記事ごとに個別投稿を作成）"""
        drafts = []
        seen_urls = set()  # URL重複チェック用
        today = datetime.now().strftime('%Y/%m/%d')

        # RSS（公式発表）を優先的に投稿素案作成（Anthropicなどの重要な公式発表を確実に含める）
        for item in provider_items[:7]:  # 5→7に増加
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)

            feed_name = item.metadata.get("feed_name", "")
            post = self._create_single_post(
                title=item.title,
                url=item.url,
                source_type="公式発表",
                source_name=feed_name,
                date=today,
                item=item
            )
            drafts.append(f"【投稿案 {len(drafts) + 1}】\n{post}")

        # GitHub Releaseは削除（RSSで取得するため不要）

        # トップハイライトから追加（2→1に削減）
        for item in top_items[:1]:
            if item.url in seen_urls:
                continue
            if item.source in ["rss", "github"]:
                continue  # 既に追加済み

            seen_urls.add(item.url)

            source_name = item.metadata.get("username", "") or item.metadata.get("keyword", "")
            post = self._create_single_post(
                title=item.title,
                url=item.url,
                source_type="X注目投稿",
                source_name=source_name,
                date=today,
                item=item
            )
            drafts.append(f"【投稿案 {len(drafts) + 1}】\n{post}")

        return "\n\n" + ("-" * 50) + "\n\n".join(drafts) if drafts else ""

    def _generate_x_post_draft_blocks(self, top_items: List[Item], provider_items: List[Item], github_items: List[Item], all_items: List[Item]) -> List[Dict]:
        """X投稿素案をSlack Blocksとして生成（各投稿を個別ブロックに）"""
        blocks = []
        seen_urls = set()
        today = datetime.now().strftime('%Y/%m/%d')
        draft_count = 0

        # 【必見の更新】セクション
        must_include_items = [i for i in all_items if i.metadata.get("must_include")]
        must_include_config = self.config.get("rss", {}).get("must_include_feeds", [])

        if must_include_config:
            blocks.append({"type": "divider"})
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": "⭐ 必見の更新"}
            })

            if must_include_items:
                # 必須フィードごとにグループ化
                from collections import defaultdict
                grouped = defaultdict(list)
                for item in must_include_items:
                    feed_name = item.metadata.get("feed_name", "Unknown")
                    grouped[feed_name].append(item)

                # 各フィードの更新を表示
                for feed_name, items in grouped.items():
                    for item in items:
                        seen_urls.add(item.url)
                        draft_count += 1

                        post = self._create_single_post(
                            title=item.title,
                            url=item.url,
                            source_type="必見の更新",
                            source_name=feed_name,
                            date=today,
                            item=item
                        )

                        blocks.append({
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"*【投稿案 {draft_count}】{feed_name}*"}
                        })
                        blocks.append({
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": f"```{post}```"}
                        })
                        blocks.append({"type": "divider"})
            else:
                # 更新がない場合
                feed_names = [f["name"] for f in must_include_config]
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"📭 本日の更新なし\n対象: {', '.join(feed_names)}"}
                })
                blocks.append({"type": "divider"})

        # RSS（公式発表）を5件に削減（7→5）
        for item in provider_items[:5]:
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            draft_count += 1

            feed_name = item.metadata.get("feed_name", "")
            post = self._create_single_post(
                title=item.title,
                url=item.url,
                source_type="公式発表",
                source_name=feed_name,
                date=today,
                item=item
            )

            # 各投稿を個別のsectionブロックに（3000文字制限回避）
            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```【投稿案 {draft_count}】\n{post}```"}
            })

            # 区切り線を追加（最後以外）
            if draft_count < 7:
                blocks.append({"type": "divider"})

        # X由来のアイテムを確実に含める（新規追加）
        # 重要: top_items ではなく all_items から抽出（top_items は3件しかないため）
        x_items = [i for i in all_items if i.source in ["x_account", "x_search"]]
        for item in x_items[:2]:  # X由来を最大2件追加
            if item.url in seen_urls:
                continue
            seen_urls.add(item.url)
            draft_count += 1

            source_name = item.metadata.get("username", "") or item.metadata.get("keyword", "")
            post = self._create_single_post(
                title=item.title,
                url=item.url,
                source_type="X注目投稿",
                source_name=source_name,
                date=today,
                item=item
            )

            blocks.append({
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"```【投稿案 {draft_count}】\n{post}```"}
            })

            # 区切り線を追加（最後以外）
            if draft_count < 7:
                blocks.append({"type": "divider"})

        return blocks

    def _create_single_post(self, title: str, url: str, source_type: str, source_name: str, date: str, item: Item) -> str:
        """個別のX投稿を生成"""
        # TwitterのURLをx.comに変換
        if "twitter.com" in url:
            url = url.replace("twitter.com", "x.com")

        # カテゴリ情報を取得
        category = item.metadata.get("category", "UNKNOWN")

        # Claude API でサマライズ生成
        summary = self._generate_summary_with_claude(title, url, source_type, category)

        return summary

    def _generate_summary_with_claude(self, title: str, url: str, source_type: str, category: str = "UNKNOWN") -> str:
        """Claude API で高品質なX投稿スレッドを生成"""
        try:
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                print("⚠️  ANTHROPIC_API_KEY が設定されていません。簡易要約にフォールバックします。")
                return self._generate_simple_summary(title, source_type, url)

            client = anthropic.Anthropic(api_key=api_key)

            # ターゲット情報を取得
            target = self.config.get("target_audience", {})
            target_name = target.get("name", "AIに関心のあるビジネスパーソンやエンジニア")

            # カテゴリ別のフォーカス
            category_focus = {
                "PRACTICAL": "実装方法、具体的な機能、使い方、統合パターン、実践的なTipsを重視してください。",
                "TECHNICAL": "技術的な仕組み、比較分析、詳細な解説を重視してください。",
                "GENERAL": "新機能の概要、利用開始時期、対象ユーザーを重視してください。"
            }.get(category, "")

            # システムプロンプト（全リクエスト共通）
            system_prompt = f"""あなたはAI業界のトレンドを追うX（Twitter）アカウントの投稿作成者です。
読者は{target_name}です。

【記事カテゴリ】{category}
{category_focus}

【重要な原則】
- 具体的で実用的な情報を提供する
- 読者が「自分も使ってみたい」と思える内容にする
- 抽象的な表現（「革新的」「画期的」）は避け、何ができるかを明示する
- 絵文字は最小限に抑える
- セクション番号は「1.」「2.」「3.」の形式で構造化する"""

            # ユーザープロンプト
            user_prompt = f"""以下のAI関連ニュースについて、X投稿スレッドの素案を作成してください。

【記事タイトル】
{title}

【ソースタイプ】
{source_type}

【出力フォーマット】

【速報】または【注目】企業名、「製品/機能名」を発表/リリース

[2-3文で核心を要約。何が新しいのか、なぜ重要なのかを明確に]

{url}

1. [セクション名]

・具体的な機能や特徴1
・具体的な機能や特徴2
・具体的な機能や特徴3

[このセクションの補足説明を一文で]

2. [セクション名]

・具体的な機能や特徴4
・具体的な機能や特徴5

[このセクションの補足説明を一文で]

3. [利用方法・対象者]

・対象ユーザー: [具体的に]
・提供開始: [いつから]
・今後の予定: [あれば]

【重要な制約】
- 各セクションの箇条書きは3-5項目
- 箇条書きには「・」（中黒）のみ使用
- ■、▸、💡、🔗 などの装飾記号は不要
- URLは冒頭の要約の直後に配置
- セクション番号は「1.」「2.」「3.」の形式で構造化
- 全体で600-800文字程度（Xスレッドとして適切な長さ）
- タイトルから推測して具体的に記述してください"""

            message = client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=1500,  # 800文字要求なので余裕を持たせる
                system=system_prompt,  # システムプロンプト追加
                messages=[{
                    "role": "user",
                    "content": user_prompt
                }]
            )

            return message.content[0].text

        except Exception as e:
            print(f"⚠️ Claude API エラー: {e}")
            # フォールバック: 簡易要約
            return self._generate_simple_summary(title, source_type, url)

    def _generate_simple_summary(self, title: str, source_type: str, url: str) -> str:
        """フォールバック用の簡易要約"""
        if source_type == "公式発表":
            emoji = "🚀"
            comment = "重要な公式アナウンスです"
        elif source_type == "GitHub Release":
            emoji = "📦"
            comment = "新バージョンがリリースされました"
        else:
            emoji = "💡"
            comment = "注目の話題です"

        # 簡潔な形式
        lines = [
            f"【{emoji} {title[:60]}{'...' if len(title) > 60 else ''}】",
            "",
            f"💡 {comment}。詳細をチェックしましょう。",
            "",
            f"🔗 {url}"
        ]

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
