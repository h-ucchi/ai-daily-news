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
from content_validator import ContentValidator
from post_prompt import get_system_prompt, create_user_prompt_from_tweet, create_user_prompt_from_article
from article_fetcher import fetch_article_content_safe
from state_manager import StateManager
from ai_lint_checker import AILintChecker


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


class XAPIClient:
    """X (Twitter) API v2 クライアント"""

    def __init__(self, bearer_token: str, oauth_credentials: Optional[Dict] = None):
        # 読み取り用（既存）
        self.bearer_token = bearer_token
        self.base_url = "https://api.twitter.com/2"
        self.headers = {"Authorization": f"Bearer {bearer_token}"}

        # 書き込み用（新規）
        if oauth_credentials:
            from requests_oauthlib import OAuth1Session
            self.oauth = OAuth1Session(
                client_key=oauth_credentials['api_key'],
                client_secret=oauth_credentials['api_secret'],
                resource_owner_key=oauth_credentials['access_token'],
                resource_owner_secret=oauth_credentials['access_token_secret']
            )
        else:
            self.oauth = None

    def get_user_id(self, username: str) -> Optional[str]:
        """ユーザー名からユーザーIDを取得"""
        url = f"{self.base_url}/users/by/username/{username}"
        response = requests.get(url, headers=self.headers)
        if response.status_code == 200:
            data = response.json()
            return data.get("data", {}).get("id")
        return None

    def get_user_tweets(self, user_id: str, since_id: Optional[str] = None, max_results: int = 10) -> tuple:
        """ユーザーのツイートを取得（新着のみ、フォロワー数情報付き）"""
        url = f"{self.base_url}/users/{user_id}/tweets"
        params = {
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics",
            "expansions": "author_id",
            "user.fields": "public_metrics"
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
            tweets = data.get("data", [])
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            return tweets, users
        return [], {}

    def search_tweets(self, query: str, since_id: Optional[str] = None, max_results: int = 10) -> tuple:
        """キーワードでツイート検索（新着のみ、フォロワー数情報付き）"""
        url = f"{self.base_url}/tweets/search/recent"
        params = {
            "query": query,
            "max_results": min(max_results, 100),
            "tweet.fields": "created_at,public_metrics",
            "expansions": "author_id",
            "user.fields": "public_metrics"
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
            tweets = data.get("data", [])
            users = {u["id"]: u for u in data.get("includes", {}).get("users", [])}
            return tweets, users
        return [], {}

    def post_tweet(self, text: str) -> Dict:
        """ツイート投稿"""
        if not self.oauth:
            raise ValueError("OAuth credentials not configured")

        url = f"{self.base_url}/tweets"
        payload = {"text": text}

        response = self.oauth.post(url, json=payload)

        if response.status_code == 201:
            return response.json()
        else:
            raise Exception(
                f"Failed to post tweet: {response.status_code} {response.text}"
            )


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
            "x_followers_filtered": 0,
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
        # ★ RSS処理はrun_hourly.pyに移行したため無効化
        # self._collect_rss()

        # 必須フィード（当日の更新があれば必ず含める）
        must_include_items = self._collect_must_include_feeds()
        self.items.extend(must_include_items)
        self.stats["must_include_fetched"] = len(must_include_items)

        # 重複排除
        self._deduplicate()

        print(f"✅ 収集完了: {len(self.items)} 件")

    def _collect_x_accounts(self):
        """Xアカウントからツイート収集（フォロワー数フィルタリング付き）"""
        accounts = self.config["x"]["accounts"]
        limit = self.config["x"]["limits"]["accounts"]
        fetched = 0

        # フォロワー数フィルタ設定
        follower_filter = self.config["x"].get("follower_filter", {})
        filter_enabled = follower_filter.get("enabled", False)
        min_followers = follower_filter.get("min_followers", 0)

        print(f"📱 Xアカウント監視: {len(accounts)} アカウント")
        if filter_enabled:
            print(f"   フォロワー数フィルタ: {min_followers:,}人以上")

        for username in accounts:
            if fetched >= limit:
                print(f"⚠️  アカウント監視の上限 {limit} 件に到達")
                self.stats["x_limit_reached"] = True
                break

            user_id = self.x_client.get_user_id(username)
            if not user_id:
                continue

            since_id = self.state.get_x_account_since_id(username)
            tweets, users = self.x_client.get_user_tweets(user_id, since_id, max_results=10)

            if not tweets:
                continue

            # 最新のtweet_idを保存
            max_id = max(int(t["id"]) for t in tweets)
            self.state.set_x_account_since_id(username, user_id, str(max_id))

            for tweet in tweets:
                if fetched >= limit:
                    break

                # フォロワー数フィルタリング
                if filter_enabled:
                    author_id = tweet.get("author_id")
                    user = users.get(author_id, {})
                    followers_count = user.get("public_metrics", {}).get("followers_count", 0)

                    if followers_count < min_followers:
                        tweet_text_short = tweet["text"][:50]
                        print(f"  ⏭️  除外（フォロワー数: {followers_count:,}）: {tweet_text_short}...")
                        self.stats["x_followers_filtered"] += 1
                        continue

                # 言語・地域フィルタリング
                tweet_text = tweet["text"]
                tweet_url = f"https://twitter.com/{username}/status/{tweet['id']}"

                if self.classifier:
                    # 総合的な分類（言語・地域チェックを含む）
                    classification = self.classifier.classify(tweet_text, "", tweet_url)
                    category = classification.category

                    # 非英語コンテンツまたは日本由来のコンテンツは除外
                    if category in ["NON_ENGLISH", "JAPAN_ORIGIN"]:
                        print(f"  ⏭️  除外（{category}）: {tweet_text[:50]}...")
                        continue
                else:
                    category = "UNKNOWN"

                # カテゴリ分類とスコア調整
                initial_score = self._calculate_engagement_score(tweet)
                if self.classifier:
                    final_score = self.classifier.calculate_final_score(
                        initial_score, category, "x_account"
                    )
                else:
                    final_score = initial_score

                item = Item(
                    source="x_account",
                    title=tweet_text[:100],
                    url=tweet_url,
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
        """Xキーワード検索（フォロワー数フィルタリング付き）"""
        keywords = self.config["x"]["keywords"]
        limit = self.config["x"]["limits"]["search"]
        fetched = 0

        # フォロワー数フィルタ設定
        follower_filter = self.config["x"].get("follower_filter", {})
        filter_enabled = follower_filter.get("enabled", False)
        min_followers = follower_filter.get("min_followers", 0)

        print(f"🔎 Xキーワード検索: {len(keywords)} キーワード")
        if filter_enabled:
            print(f"   フォロワー数フィルタ: {min_followers:,}人以上")

        for keyword in keywords:
            if fetched >= limit:
                print(f"⚠️  検索の上限 {limit} 件に到達")
                self.stats["x_limit_reached"] = True
                break

            since_id = self.state.get_x_keyword_since_id(keyword)
            tweets, users = self.x_client.search_tweets(keyword, since_id, max_results=10)

            if not tweets:
                continue

            # 最新のtweet_idを保存
            max_id = max(int(t["id"]) for t in tweets)
            self.state.set_x_keyword_since_id(keyword, str(max_id))

            for tweet in tweets:
                if fetched >= limit:
                    break

                # フォロワー数フィルタリング
                if filter_enabled:
                    author_id = tweet.get("author_id")
                    user = users.get(author_id, {})
                    followers_count = user.get("public_metrics", {}).get("followers_count", 0)

                    if followers_count < min_followers:
                        tweet_text_short = tweet["text"][:50]
                        print(f"  ⏭️  除外（フォロワー数: {followers_count:,}）: {tweet_text_short}...")
                        self.stats["x_followers_filtered"] += 1
                        continue

                # 言語・地域フィルタリング
                tweet_text = tweet["text"]
                tweet_url = f"https://twitter.com/i/web/status/{tweet['id']}"

                if self.classifier:
                    # 総合的な分類（言語・地域チェックを含む）
                    classification = self.classifier.classify(tweet_text, "", tweet_url)
                    category = classification.category

                    # 非英語コンテンツまたは日本由来のコンテンツは除外
                    if category in ["NON_ENGLISH", "JAPAN_ORIGIN"]:
                        print(f"  ⏭️  除外（{category}）: {tweet_text[:50]}...")
                        continue
                else:
                    category = "UNKNOWN"

                # カテゴリ分類とスコア調整
                initial_score = self._calculate_engagement_score(tweet)
                if self.classifier:
                    final_score = self.classifier.calculate_final_score(
                        initial_score, category, "x_search"
                    )
                else:
                    final_score = initial_score

                item = Item(
                    source="x_search",
                    title=tweet_text[:100],
                    url=tweet_url,
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
        """RSS収集（記事URLリスト比較方式）"""
        feeds = self.config["rss"]["feeds"]

        # 統計情報の初期化
        rss_stats = {
            "total_feeds": len(feeds),
            "success_feeds": 0,
            "failed_feeds": 0,
            "total_entries": 0,
            "filtered_out": 0,
            "new_articles": 0,
            "old_articles_filtered": 0,
            "added": 0
        }

        print(f"📰 RSS収集: {len(feeds)} フィード")

        for feed_config in feeds:
            feed_url = feed_config["url"]
            feed_name = feed_config["name"]

            print(f"\n📡 {feed_name}")
            print(f"   URL: {feed_url}")

            # フィード取得
            feed = feedparser.parse(feed_url)

            # エラーチェック1: HTTPステータス
            if hasattr(feed, 'status') and feed.status >= 400:
                print(f"   ⚠️  HTTP {feed.status}: 取得失敗")
                rss_stats["failed_feeds"] += 1
                continue

            # エラーチェック2: パースエラー
            if hasattr(feed, 'bozo') and feed.bozo and not feed.entries:
                print(f"   ⚠️  パース失敗: {feed.get('bozo_exception', 'Unknown error')}")
                rss_stats["failed_feeds"] += 1
                continue

            # エラーチェック3: 記事が0件
            if not feed.entries:
                print(f"   ℹ️  記事0件")
                rss_stats["failed_feeds"] += 1
                continue

            rss_stats["success_feeds"] += 1
            rss_stats["total_entries"] += len(feed.entries)
            print(f"   ✅ 記事取得: {len(feed.entries)}件")

            # 前回取得した記事URLリストを取得
            previous_urls = self.state.get_rss_article_urls(feed_url)
            if previous_urls is None:
                previous_urls = []
                print(f"   ℹ️  初回取得（全記事を対象）")

            # 今回取得した記事URLリスト
            current_urls = [entry.link for entry in feed.entries]

            # 差分（新規記事）を抽出
            new_urls = set(current_urls) - set(previous_urls)

            if new_urls:
                print(f"   🆕 新規記事: {len(new_urls)}件")
                rss_stats["new_articles"] += len(new_urls)
            else:
                print(f"   ℹ️  新規記事なし（前回と同じ内容）")

            # 24時間前のカットオフタイムスタンプを計算
            cutoff_time = datetime.now(timezone.utc) - timedelta(hours=24)

            feed_added = 0

            for entry in feed.entries:
                # 新規記事のみ処理
                if entry.link not in new_urls:
                    continue

                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published:
                    continue

                published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                published_iso = published_dt.isoformat()

                # 24時間以内の記事のみを対象
                if published_dt < cutoff_time:
                    rss_stats["old_articles_filtered"] += 1
                    continue

                # 言語・地域フィルタリング
                if self.classifier:
                    description = entry.get("summary", "")
                    url = entry.link

                    classification = self.classifier.classify(entry.title, description, url)
                    category = classification.category

                    if category in ["NON_ENGLISH", "JAPAN_ORIGIN"]:
                        print(f"   ⏭️  除外（{category}）: {entry.title[:50]}...")
                        rss_stats["filtered_out"] += 1
                        continue
                else:
                    category = "UNKNOWN"

                # スコアリング
                base_score = self.config["slack"]["scoring"]["rss_bonus"]
                priority_bonus = self.PRIORITY_FEEDS.get(feed_url, 0)
                initial_score = base_score + priority_bonus

                if self.classifier:
                    final_score = self.classifier.calculate_final_score(
                        initial_score, category, "rss", is_official=True
                    )
                else:
                    final_score = initial_score

                item = Item(
                    source="rss",
                    title=entry.title,
                    url=entry.link,
                    published_at=published_iso,
                    score=final_score,
                    metadata={
                        "feed_name": feed_name,
                        "feed_url": feed_url,
                        "category": category
                    }
                )
                self.items.append(item)
                feed_added += 1
                rss_stats["added"] += 1

            if feed_added > 0:
                print(f"   ➕ 追加: {feed_added}件")

            # 今回の記事URLリストを保存（最新20件のみ）
            self.state.set_rss_article_urls(feed_url, current_urls)
            print(f"   💾 記事URLリスト保存: {len(current_urls[:20])}件")

            # 最終確認時刻を保存
            current_time = datetime.now(timezone.utc).isoformat()
            self.state.set_rss_last_checked(feed_url, current_time)

            # 最新のpublished_atも保存（互換性のため）
            if feed.entries:
                latest = max(feed.entries, key=lambda e: e.get("published_parsed") or e.get("updated_parsed"))
                published = latest.get("published_parsed") or latest.get("updated_parsed")
                if published:
                    published_dt = datetime(*published[:6], tzinfo=timezone.utc)
                    self.state.set_rss_last_published(feed_url, published_dt.isoformat())

        # 統計出力
        print(f"\n📊 RSS収集統計:")
        print(f"   対象フィード: {rss_stats['total_feeds']}件")
        print(f"   取得成功: {rss_stats['success_feeds']}件")
        print(f"   取得失敗: {rss_stats['failed_feeds']}件")
        print(f"   総記事数: {rss_stats['total_entries']}件")
        print(f"   新規記事: {rss_stats['new_articles']}件")
        print(f"   古い記事除外: {rss_stats['old_articles_filtered']}件")
        print(f"   フィルタ除外: {rss_stats['filtered_out']}件")
        print(f"   追加件数: {rss_stats['added']}件")

        self.stats["rss_fetched"] = rss_stats["added"]

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

                # 言語・地域フィルタリング
                if self.classifier:
                    description = entry.get("summary", "")
                    url = entry.link

                    # 総合的な分類（言語・地域チェックを含む）
                    classification = self.classifier.classify(entry.title, description, url)
                    category = classification.category

                    # 非英語コンテンツまたは日本由来のコンテンツは除外
                    # ただし、必須フィードなので警告のみ出力
                    if category in ["NON_ENGLISH", "JAPAN_ORIGIN"]:
                        print(f"  ⚠️  必須フィードだが非英語/日本由来（{category}）: {entry.title[:50]}...")
                        # 必須フィードなので除外せずに含める（スコアは低くする）
                        category = category  # そのまま使う
                    elif category == "PRACTICAL":
                        category = "PRACTICAL"  # 必須フィードのPRACTICALはそのまま
                else:
                    category = "PRACTICAL"  # 必須フィードはデフォルトでPRACTICAL

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
        # 検証器の初期化
        self.validator = ContentValidator(config)
        # AI-lintチェッカーの初期化
        rules_path = os.path.join(os.path.dirname(__file__), "..", "ai-lint", ".claude", "skills", "ai-lint", "rules", "ai-lint-rules.yml")
        if os.path.exists(rules_path):
            self.ai_lint_checker = AILintChecker(rules_path)
        else:
            self.ai_lint_checker = AILintChecker()  # デフォルトルール

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

        # 日付×スコアの複合ソート（新しい記事を優先、同じ日付ならスコア順）
        sorted_items = sorted(
            self.items,
            key=lambda x: (x.published_at, x.score),
            reverse=True
        )

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

        # 分析対象セクション
        source_counts = self._count_sources()
        source_summary = self._format_source_summary(source_counts)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": source_summary}
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

    def _count_sources(self) -> Dict[str, int]:
        """データソースごとのアイテム数を集計"""
        counts = {
            "x_posts": 0,      # X投稿（x_account + x_search）
            "rss": 0,          # RSS
            "must_include": 0  # 必見の更新
        }

        for item in self.items:
            if item.source in ["x_account", "x_search"]:
                counts["x_posts"] += 1
            elif item.source == "rss":
                counts["rss"] += 1
            elif item.source == "must_include":
                counts["must_include"] += 1

        return counts

    def _format_source_summary(self, counts: Dict[str, int]) -> str:
        """データソース集計をフォーマット（全ソースを常に表示）"""
        parts = []

        # X投稿（常に表示）
        parts.append(f"X投稿 {counts['x_posts']}件")

        # RSS（常に表示）
        parts.append(f"RSS {counts['rss']}件")

        # 必見の更新（常に表示）
        parts.append(f"必見の更新 {counts['must_include']}件")

        return "📊 分析対象: " + "、".join(parts)

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

                        # 検証失敗時はスキップ
                        if post is None:
                            print(f"⏭️  投稿案スキップ（検証失敗）: {item.title[:50]}...")
                            draft_count -= 1
                            continue

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

            # 検証失敗時はスキップ
            if post is None:
                print(f"⏭️  投稿案スキップ（検証失敗）: {item.title[:50]}...")
                draft_count -= 1
                continue

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

            # 検証失敗時はスキップ
            if post is None:
                print(f"⏭️  投稿案スキップ（検証失敗）: {item.title[:50]}...")
                draft_count -= 1
                continue

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

        # X投稿の場合は全文も取得
        tweet_text = None
        if item.source in ["x_account", "x_search"]:
            tweet_text = item.metadata.get("tweet", {}).get("text", "")

        # Claude API でサマライズ生成
        summary = self._generate_summary_with_claude(
            title, url, source_type, category, tweet_text=tweet_text
        )

        return summary

    def _generate_summary_with_claude(self, title: str, url: str, source_type: str, category: str = "UNKNOWN", tweet_text: Optional[str] = None) -> str:
        """Claude API で高品質なX投稿スレッドを生成"""
        try:
            import anthropic

            api_key = os.environ.get("ANTHROPIC_API_KEY")
            if not api_key:
                print("⚠️  ANTHROPIC_API_KEY が設定されていません。投稿案生成をスキップします。")
                return None

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

            # 共通プロンプトを使用
            system_prompt = get_system_prompt()

            # ユーザープロンプト
            if tweet_text:
                # X投稿の場合：ツイート内のURLから記事本文を取得（フェーズ3）
                import re
                urls_in_tweet = re.findall(r'https?://[^\s]+', tweet_text)
                article_content = None

                if urls_in_tweet:
                    for url_in_tweet in urls_in_tweet:
                        _, content = fetch_article_content_safe(url_in_tweet)
                        if content:
                            article_content = content
                            break

                user_prompt = create_user_prompt_from_tweet(url, tweet_text, article_content)
            else:
                # RSS記事の場合：記事本文を取得（フェーズ2）
                article_title, article_content = fetch_article_content_safe(url)

                if not article_content:
                    print(f"⚠️ 記事本文取得失敗、タイトルのみで処理: {url}")
                    article_content = title

                user_prompt = create_user_prompt_from_article(
                    url,
                    article_title or title,
                    article_content
                )

            # AI-lint自動修正（最大2回試行、自動フローなので遅延最小化）
            max_retries = 1
            score_threshold = 15
            generated_text = None
            detected_issues = None

            for attempt in range(max_retries + 1):
                message = client.messages.create(
                    model="claude-sonnet-4-5-20250929",
                    max_tokens=1500,  # 800文字要求なので余裕を持たせる
                    system=system_prompt,  # システムプロンプト追加
                    messages=[{
                        "role": "user",
                        "content": user_prompt if attempt == 0 else user_prompt + f"\n\n【重要：以下の表現が検出されたので必ず修正してください】\n" + "\n".join([f"❌ 「{issue.matched_text}」→ {issue.suggestion}" for issue in detected_issues[:5]])
                    }]
                )

                generated_text = message.content[0].text

                # AI-lintチェック
                lint_result = self.ai_lint_checker.check(generated_text)

                if lint_result.score == 0:
                    break  # AI的表現なし
                elif lint_result.score < score_threshold:
                    break  # 許容範囲内
                else:
                    if attempt < max_retries:
                        detected_issues = lint_result.detections
                        # 再生成（次のループで修正指示を追加）
                    else:
                        # 最大試行回数到達、警告を出すが続行
                        print(f"⚠️  AI-lint: スコア {lint_result.score} (閾値超過、続行)")

            # 検証フェーズ1: 正規表現ベース
            validation_result = self.validator.validate_post(generated_text, title)

            if not validation_result.is_valid:
                print(f"⚠️  投稿案が検証失敗: {validation_result.rejection_reason}")
                print(f"    タイトル: {title[:50]}...")
                print(f"    検出問題: {validation_result.detected_issues}")
                return None

            # 検証フェーズ2: Claude APIレビュー
            review_result = self.validator.review_post_with_claude(generated_text, title, url)

            if not review_result.is_valid:
                print(f"⚠️  投稿案がレビュー失敗: {review_result.rejection_reason}")
                print(f"    タイトル: {title[:50]}...")
                print(f"    検出問題: {review_result.detected_issues}")
                return None

            return generated_text

        except Exception as e:
            print(f"⚠️ Claude API エラー: {e}")
            # フォールバックは行わず、None を返す（検証失敗と同じ扱い）
            return None



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
