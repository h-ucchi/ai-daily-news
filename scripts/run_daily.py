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
from post_prompt import get_system_prompt, create_user_prompt_from_tweet, create_user_prompt_from_article, create_user_prompt_from_thread
from article_fetcher import fetch_article_content_safe, fetch_rss_feed_safe
from state_manager import StateManager
from ai_lint_checker import AILintChecker
from x_api_client import XAPIClient


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
        """Xアカウントからツイート収集（カテゴリ別フォロワー数フィルタリング付き）"""
        accounts_config = self.config["x"]["accounts"]
        limit = self.config["x"]["limits"]["accounts"]
        fetched = 0

        # カテゴリ別フォロワー数フィルタ設定
        follower_filters = self.config["x"].get("follower_filters", {})

        # 後方互換性：accountsがリストの場合は従来のロジック
        if isinstance(accounts_config, list):
            print("⚠️  旧形式のアカウントリスト検出。新形式（カテゴリ別）への移行を推奨します。")
            accounts_list = accounts_config
            tier = "unknown"
            filter_config = self.config["x"].get("follower_filter", {})
        else:
            # 新形式：カテゴリ別処理
            accounts_list = []
            total_accounts = sum(len(accounts_config.get(tier, [])) for tier in ["official", "developers", "practitioners"])
            print(f"📱 Xアカウント監視（カテゴリ別）: 合計 {total_accounts} アカウント")

            # カテゴリ順に処理（official → developers → practitioners）
            for tier in ["official", "developers", "practitioners"]:
                tier_accounts = accounts_config.get(tier, [])
                if not tier_accounts:
                    continue

                filter_config = follower_filters.get(tier, {})
                filter_enabled = filter_config.get("enabled", False)
                min_followers = filter_config.get("min_followers", 0)

                print(f"  【{tier}】 {len(tier_accounts)} アカウント", end="")
                if filter_enabled:
                    print(f"（フォロワー {min_followers:,}人以上）")
                else:
                    print("（フィルタなし）")

                for username in tier_accounts:
                    if fetched >= limit:
                        print(f"⚠️  アカウント監視の上限 {limit} 件に到達")
                        self.stats["x_limit_reached"] = True
                        break

                    user_id = self.x_client.get_user_id(username)
                    if not user_id:
                        print(f"  ⚠️  ユーザーID取得失敗: @{username}")
                        continue

                    since_id = self.state.get_x_account_since_id(username)
                    tweets, users = self.x_client.get_user_tweets(user_id, since_id, max_results=10)

                    if not tweets:
                        print(f"    ℹ️  @{username}: ツイートなし（過去24時間）")
                        continue

                    print(f"    📥 @{username}: {len(tweets)} 件取得")

                    # 最新のtweet_idを保存
                    max_id = max(int(t["id"]) for t in tweets)
                    self.state.set_x_account_since_id(username, user_id, str(max_id))

                    for tweet in tweets:
                        if fetched >= limit:
                            break

                        # カテゴリ別フォロワー数フィルタリング
                        if filter_enabled:
                            author_id = tweet.get("author_id")
                            user = users.get(author_id, {})
                            followers_count = user.get("public_metrics", {}).get("followers_count", 0)

                            if followers_count < min_followers:
                                tweet_text_short = tweet["text"][:50]
                                print(f"    ⏭️  除外（フォロワー数: {followers_count:,}）: @{username}")
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
                                print(f"    ⏭️  除外（{category}）: @{username}")
                                continue
                        else:
                            category = "UNKNOWN"

                        # エンゲージメントスコア計算
                        initial_score = self._calculate_engagement_score(tweet)

                        # 最低エンゲージメント閾値チェック
                        min_engagement_config = self.config["x"].get("min_engagement", {})
                        if min_engagement_config.get("enabled", False):
                            threshold = min_engagement_config.get("threshold", 10)
                            metrics = tweet.get("public_metrics", {})
                            likes = metrics.get("like_count", 0)
                            rts = metrics.get("retweet_count", 0)
                            replies = metrics.get("reply_count", 0)

                            if initial_score < threshold:
                                print(f"    ⏭️  除外（エンゲージメント低: {initial_score} < {threshold}）")
                                print(f"       👍{likes} 🔄{rts} 💬{replies} | @{username}")
                                self.stats["x_low_engagement_filtered"] = self.stats.get("x_low_engagement_filtered", 0) + 1
                                continue
                            else:
                                print(f"    ✓ エンゲージメント OK: {initial_score} (👍{likes} 🔄{rts} 💬{replies})")

                        # カテゴリ分類とスコア調整
                        if self.classifier:
                            final_score = self.classifier.calculate_final_score(
                                initial_score, category, "x_account"
                            )
                        else:
                            final_score = initial_score

                        # OpenAI関連アカウントのスレッド検出・取得
                        OPENAI_ACCOUNTS = ["openai", "ChatGPTapp", "openaidevs"]
                        is_openai_account = username in OPENAI_ACCOUNTS
                        tweet_id = tweet["id"]
                        conversation_id = tweet.get("conversation_id")
                        is_thread = conversation_id and conversation_id != tweet_id

                        # スレッド重複チェック
                        if is_thread and self.state.is_conversation_processed(conversation_id):
                            print(f"    ⏭️  スレッド処理済み: {conversation_id}")
                            continue

                        # スレッド取得（OpenAI関連のみ）
                        if is_openai_account and is_thread:
                            print(f"    🧵 スレッド検出: {tweet_id}")
                            try:
                                thread_tweets = self.x_client.get_conversation_thread(
                                    conversation_id, user_id, max_tweets=10
                                )

                                if len(thread_tweets) > 1:
                                    print(f"    ✅ スレッド取得: {len(thread_tweets)}ツイート")

                                    # スレッド全体を1つのItemとして処理
                                    item = Item(
                                        source="x_account",
                                        title=thread_tweets[0]["text"][:100],
                                        url=f"https://twitter.com/{username}/status/{conversation_id}",
                                        published_at=thread_tweets[0]["created_at"],
                                        score=final_score,
                                        metadata={
                                            "username": username,
                                            "tier": tier,
                                            "tweet": thread_tweets[0],
                                            "thread_tweets": thread_tweets,  # 全ツイートを保存
                                            "is_thread": True,
                                            "category": category
                                        }
                                    )
                                    self.items.append(item)
                                    self.state.mark_conversation_processed(conversation_id)
                                    fetched += 1
                                    print(f"    ✅ @{username} [{tier}] スレッド (スコア: {final_score})")
                                    continue  # スレッド処理完了、単一ツイート処理をスキップ
                                else:
                                    print(f"    ⚠️  スレッド取得失敗、単一ツイートとして処理")
                            except Exception as e:
                                print(f"    ⚠️  スレッド取得エラー: {e}")
                                print(f"    → 単一ツイートとして処理します")

                        # 単一ツイート処理（既存ロジック）
                        item = Item(
                            source="x_account",
                            title=tweet_text[:100],
                            url=tweet_url,
                            published_at=tweet["created_at"],
                            score=final_score,
                            metadata={
                                "username": username,
                                "tier": tier,  # 著者tierを保存
                                "tweet": tweet,
                                "category": category
                            }
                        )
                        self.items.append(item)
                        fetched += 1
                        print(f"    ✅ @{username} [{tier}] (スコア: {final_score})")

                if fetched >= limit:
                    break

        self.stats["x_accounts_fetched"] = fetched
        self.stats["x_total_fetched"] += fetched

        # フィルタリング統計を表示
        print(f"\n📊 Xアカウント収集統計:")
        print(f"  収集成功: {fetched} 件")
        if self.stats.get("x_followers_filtered", 0) > 0:
            print(f"  フォロワー数フィルタ除外: {self.stats['x_followers_filtered']} 件")
        if self.stats.get("x_low_engagement_filtered", 0) > 0:
            print(f"  低エンゲージメント除外: {self.stats['x_low_engagement_filtered']} 件")

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

            feed = fetch_rss_feed_safe(feed_url)
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
        """重複排除（URL基準 + 過去3日分のdrafts.jsonチェック）"""
        seen_urls = set()

        # 過去3日分のdrafts.jsonから既存URLを読み込む
        dedup_config = self.config["slack"].get("deduplication", {})
        if dedup_config.get("enabled", False):
            lookback_days = dedup_config.get("lookback_days", 3)
            past_urls = self._load_past_urls_from_drafts(lookback_days)
            seen_urls.update(past_urls)
            print(f"  🔍 過去{lookback_days}日分のURL: {len(past_urls)}件を除外対象に追加")

        unique_items = []

        for item in self.items:
            if item.url not in seen_urls:
                seen_urls.add(item.url)
                unique_items.append(item)
            else:
                self.stats["duplicates_removed"] += 1

        self.items = unique_items

    def _load_past_urls_from_drafts(self, lookback_days: int) -> set:
        """過去N日分のdrafts.jsonからURLを取得"""
        import os
        drafts_path = os.path.join(os.path.dirname(__file__), "..", "data", "drafts.json")

        if not os.path.exists(drafts_path):
            print(f"  ⚠️  drafts.jsonが見つかりません: {drafts_path}")
            return set()

        try:
            with open(drafts_path, 'r', encoding='utf-8') as f:
                drafts_data = json.load(f)

            past_urls = set()
            cutoff_date = datetime.now(timezone.utc) - timedelta(days=lookback_days)

            for draft in drafts_data.get("drafts", []):
                created_at_str = draft.get("created_at", "")
                if not created_at_str:
                    continue

                # ISO形式の日時をパース
                created_at = datetime.fromisoformat(created_at_str.replace('Z', '+00:00'))

                # 過去N日以内のURLを収集
                if created_at >= cutoff_date:
                    url = draft.get("item", {}).get("url")
                    if url:
                        past_urls.add(url)

            return past_urls
        except Exception as e:
            print(f"  ⚠️  drafts.json読み込みエラー: {e}")
            return set()


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

    def _select_items_with_source_quotas(self, items: List[Item]) -> List[Item]:
        """ソース別最低保証枠を考慮したアイテム選択"""
        quotas_config = self.config["slack"].get("source_quotas", {})

        if not quotas_config.get("enabled", False):
            # 保証枠無効の場合は従来のスコア順
            print("  📊 ソース別保証枠: 無効（スコア順のみ）")
            return sorted(items, key=lambda x: (x.published_at, x.score), reverse=True)[:15]

        print("  📊 ソース別保証枠: 有効")

        # 1. 必須表示アイテムを抽出（must_include_feeds）
        selected = []
        if quotas_config.get("must_include", False):
            must_include_items = [
                item for item in items
                if item.metadata.get("must_include", False)
            ]
            selected.extend(must_include_items)
            print(f"    ✅ 必須表示: {len(must_include_items)}件")
            # 必須アイテムを残りのプールから除外
            items = [item for item in items if not item.metadata.get("must_include", False)]

        # 2. ソース別にグループ化してスコア順にソート
        by_source = defaultdict(list)
        for item in items:
            # x_account と x_search を "x" にまとめる
            source = "x" if item.source in ["x_account", "x_search"] else item.source
            by_source[source].append(item)

        # 各ソースをスコア順にソート
        for source in by_source:
            by_source[source].sort(key=lambda x: (x.published_at, x.score), reverse=True)

        # 3. ソース別保証枠を確保
        for source in ["rss", "x"]:
            quota = quotas_config.get(source, 0)
            selected.extend(by_source[source][:quota])
            print(f"    ✅ {source.upper()}保証枠: {len(by_source[source][:quota])}件 / {quota}件")

        # 4. 残り枠をスコア順に埋める
        remaining_quota = quotas_config.get("remaining", 7)

        # 保証枠で使われなかったアイテムをプールに入れる
        pool = []
        for source in ["rss", "x"]:
            quota = quotas_config.get(source, 0)
            pool.extend(by_source[source][quota:])  # 保証枠以降

        # プールをスコア順にソート
        pool.sort(key=lambda x: (x.published_at, x.score), reverse=True)
        selected.extend(pool[:remaining_quota])
        print(f"    ✅ 残りスコア順: {len(pool[:remaining_quota])}件 / {remaining_quota}件")

        # 最終的に日付×スコアでソート
        selected.sort(key=lambda x: (x.published_at, x.score), reverse=True)

        print(f"  📊 合計選択: {len(selected)}件")
        return selected

    def send(self):
        """レポートを生成してSlackに投稿（投稿案ごとに個別メッセージ）"""
        print("📤 Slackレポート生成中...")

        # ソース別保証枠を考慮したアイテム選択
        sorted_items = self._select_items_with_source_quotas(self.items)

        # セクション分け
        provider_items = self._select_diverse_provider_items(sorted_items, self.config["slack"]["limits"]["provider_official"])

        # ① ヘッダー + データソース集計メッセージ（1回のみ）
        header_blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🐦 X投稿素案 - {datetime.now().strftime('%Y-%m-%d')}"}
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": self._format_source_summary(self._count_sources())}
            }
        ]
        self._send_blocks(header_blocks)
        print("✅ ヘッダーを送信しました")

        # ② 投稿案を個別送信
        # 必見の更新アイテムを抽出
        must_include_items = [i for i in sorted_items if i.metadata.get("must_include")]

        # X由来のアイテムを抽出
        x_items = [i for i in sorted_items if i.source in ["x_account", "x_search"]]

        # 投稿案を個別送信
        self._send_individual_draft_posts(
            must_include_items=must_include_items,
            provider_items=provider_items,
            x_items=x_items
        )

        print("✅ 全ての投稿案をSlackに送信しました")

    def _send_blocks(self, blocks: List[Dict]) -> None:
        """
        指定されたブロックをSlackに送信する汎用ヘルパー

        Args:
            blocks: Slack Block Kit形式のブロックリスト
        """
        if not blocks:
            return

        payload = {"blocks": blocks}

        try:
            response = requests.post(
                self.webhook_url,
                json=payload,
                headers={"Content-Type": "application/json"},
                timeout=30
            )
            response.raise_for_status()
        except Exception as e:
            print(f"❌ Slack送信エラー: {e}")

    def _send_single_draft_post(
        self,
        item: Item,
        draft_number: int,
        source_type: str
    ) -> None:
        """
        1つの投稿案を生成してSlackに個別送信

        Args:
            item: 投稿案の元となるアイテム
            draft_number: 投稿案番号（1, 2, 3...）
            source_type: ソースタイプ（「必見の更新」「公式発表」「X注目投稿」）
        """
        today = datetime.now().strftime('%Y/%m/%d')

        # X投稿の場合は短縮フォーマットを使用
        use_shorter_format = (item.source in ["x_account", "x_search"])

        # 投稿案生成
        source_name = item.metadata.get("feed_name", "") or item.metadata.get("username", "") or item.metadata.get("keyword", "")
        post = self._create_single_post(
            title=item.title,
            url=item.url,
            source_type=source_type,
            source_name=source_name,
            date=today,
            item=item
        )

        if not post:
            return

        # ブロック構築
        blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*【投稿案 {draft_number}】{item.title}*\n"
                        f"```{post}```\n"
                        f"<{item.url}|元記事を見る>"
                    )
                }
            }
        ]

        # Slack送信
        self._send_blocks(blocks)
        print(f"  ✅ 投稿案 {draft_number} を送信: {item.title[:50]}...")

        # ★ レート制限対策: 1秒待機（必須）
        import time
        time.sleep(1)

    def _send_individual_draft_posts(
        self,
        must_include_items: List[Item],
        provider_items: List[Item],
        x_items: List[Item]
    ) -> None:
        """
        投稿案を1件ずつ生成して個別メッセージとして送信

        処理順序:
        1. 必見の更新（must_include_items）
        2. 公式発表（RSS、provider_items）最大5件
        3. X由来の投稿（x_items）最大2件
        """
        draft_count = 0

        # ① 必見の更新セクション
        if must_include_items:
            section_header_blocks = [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*⭐ 必見の更新*"}
            }]
            self._send_blocks(section_header_blocks)
            print("\n📌 必見の更新セクション")

            for item in must_include_items:
                draft_count += 1
                self._send_single_draft_post(item, draft_count, "必見の更新")

        # ② 公式発表（RSS）最大5件
        if provider_items:
            section_header_blocks = [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*📢 公式発表*"}
            }]
            self._send_blocks(section_header_blocks)
            print("\n📌 公式発表セクション")

            for item in provider_items[:5]:
                draft_count += 1
                self._send_single_draft_post(item, draft_count, "公式発表")

        # ③ X由来の投稿 最大2件
        if x_items:
            section_header_blocks = [{
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*🐦 X投稿から*"}
            }]
            self._send_blocks(section_header_blocks)
            print("\n📌 X投稿セクション")

            for item in x_items[:2]:
                draft_count += 1
                self._send_single_draft_post(item, draft_count, "X注目投稿")

        print(f"\n📊 合計 {draft_count} 件の投稿案を送信しました")

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
        thread_tweets = None
        is_thread = False
        if item.source in ["x_account", "x_search"]:
            is_thread = item.metadata.get("is_thread", False)
            if is_thread:
                # スレッドの場合
                thread_tweets = item.metadata.get("thread_tweets", [])
            else:
                # 単一ツイートの場合
                tweet_text = item.metadata.get("tweet", {}).get("text", "")

        # Claude API でサマライズ生成
        summary = self._generate_summary_with_claude(
            title, url, source_type, category, tweet_text=tweet_text, thread_tweets=thread_tweets
        )

        return summary

    def _generate_summary_with_claude(self, title: str, url: str, source_type: str, category: str = "UNKNOWN", tweet_text: Optional[str] = None, thread_tweets: Optional[List[Dict]] = None) -> str:
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
            if thread_tweets:
                # Xスレッドの場合：スレッド内のURLから記事本文を取得
                import re
                article_content = None

                for tweet in thread_tweets:
                    urls_in_tweet = re.findall(r'https?://[^\s]+', tweet["text"])
                    for url_in_tweet in urls_in_tweet:
                        _, content = fetch_article_content_safe(url_in_tweet)
                        if content:
                            article_content = content
                            break
                    if article_content:
                        break

                user_prompt = create_user_prompt_from_thread(url, thread_tweets, article_content)
            elif tweet_text:
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
    state = StateManager("data/state.json")
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
