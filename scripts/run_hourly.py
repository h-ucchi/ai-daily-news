#!/usr/bin/env python3
"""
AI Semi-Daily Report - 8時と15時のchange log調査とX投稿下書き生成

使い方:
  python scripts/run_hourly.py
"""

import os
import sys
import hashlib
import requests
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from typing import List, Dict, Optional
import yaml
from bs4 import BeautifulSoup
import anthropic
import feedparser

# 既存モジュールをインポート
from run_daily import StateManager
from draft_manager import DraftManager
from article_fetcher import fetch_article_content_safe
from post_prompt import get_system_prompt, create_user_prompt_from_article


@dataclass
class PageSnapshot:
    """ページスナップショット"""
    url: str
    name: str
    content_hash: str
    content: str
    timestamp: str


def extract_text_from_html(html: str) -> str:
    """HTMLから本文テキストを抽出"""
    soup = BeautifulSoup(html, 'html.parser')

    # 不要タグを削除
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()

    # 本文取得
    return soup.get_text(separator='\n', strip=True)


class SnapshotManager:
    """ページスナップショット管理"""

    def __init__(self, snapshots_dir: str = "data/snapshots"):
        self.snapshots_dir = Path(snapshots_dir)
        self.snapshots_dir.mkdir(parents=True, exist_ok=True)

    def _get_snapshot_path(self, url: str) -> Path:
        """URLからスナップショットファイルパスを生成"""
        url_hash = hashlib.md5(url.encode()).hexdigest()
        return self.snapshots_dir / f"{url_hash}.txt"

    def fetch_page_content(self, url: str) -> str:
        """ページコンテンツを取得"""
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; AIResearchBot/1.0)"
        }
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        return response.text

    def save_snapshot(self, snapshot: PageSnapshot):
        """スナップショットを保存"""
        snapshot_path = self._get_snapshot_path(snapshot.url)
        with open(snapshot_path, 'w', encoding='utf-8') as f:
            f.write(f"# {snapshot.name}\n")
            f.write(f"# URL: {snapshot.url}\n")
            f.write(f"# Timestamp: {snapshot.timestamp}\n")
            f.write(f"# Hash: {snapshot.content_hash}\n")
            f.write("\n")
            f.write(snapshot.content)

    def load_snapshot(self, url: str) -> Optional[PageSnapshot]:
        """保存済みスナップショットを読み込み"""
        snapshot_path = self._get_snapshot_path(url)
        if not snapshot_path.exists():
            return None

        with open(snapshot_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
            if len(lines) < 5:
                return None

            name = lines[0].replace("# ", "").strip()
            url_line = lines[1].replace("# URL: ", "").strip()
            timestamp = lines[2].replace("# Timestamp: ", "").strip()
            content_hash = lines[3].replace("# Hash: ", "").strip()
            content = "".join(lines[5:])

            return PageSnapshot(
                url=url_line,
                name=name,
                content_hash=content_hash,
                content=content,
                timestamp=timestamp
            )

    def check_for_changes(self, url: str, name: str) -> Optional[tuple]:
        """ページの変更をチェック（前回と今回のスナップショットを返す）

        Returns:
            (old_snapshot, new_snapshot) のタプル（変更あり時）
            None（変更なし時または初回時）
        """
        try:
            # 新しいコンテンツを取得
            new_content = self.fetch_page_content(url)

            # ★ HTMLではなく、テキスト抽出後の内容をハッシュ化
            new_text = extract_text_from_html(new_content)
            new_text_hash = hashlib.sha256(new_text.encode()).hexdigest()

            # 前回のスナップショットを読み込み
            old_snapshot = self.load_snapshot(url)

            # 前回のテキストハッシュを計算
            if old_snapshot:
                old_text = extract_text_from_html(old_snapshot.content)
                old_text_hash = hashlib.sha256(old_text.encode()).hexdigest()
            else:
                old_text_hash = None

            # 初回または変更あり（テキストハッシュで比較）
            if old_snapshot is None or old_text_hash != new_text_hash:
                new_snapshot = PageSnapshot(
                    url=url,
                    name=name,
                    content_hash=new_text_hash,  # ★ テキストハッシュを保存
                    content=new_content,  # HTMLは参照用に保存
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                # 前回のスナップショットを上書き（累積保存しない）
                self.save_snapshot(new_snapshot)

                if old_snapshot is None:
                    print(f"📸 初回スナップショット: {name}")
                    return None  # 初回は変更として扱わない
                else:
                    print(f"🔄 テキスト内容の変更検出: {name}")
                    return (old_snapshot, new_snapshot)  # 前回と今回を返す
            else:
                # 変更がない場合も最新のタイムスタンプで上書き
                # （前回のスナップショットを最新に保つ）
                new_snapshot = PageSnapshot(
                    url=url,
                    name=name,
                    content_hash=new_text_hash,  # ★ テキストハッシュを保存
                    content=new_content,  # HTMLは参照用に保存
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                self.save_snapshot(new_snapshot)
                print(f"   ℹ️ {name}: テキスト内容に変更なし（HTML変更のみ）")
                return None

        except Exception as e:
            print(f"❌ スナップショット取得失敗: {name} - {e}")
            return None


def generate_post_from_snapshot(old_snapshot: Optional[PageSnapshot], new_snapshot: PageSnapshot, config: Dict) -> Optional[str]:
    """スナップショットから投稿案を生成

    Args:
        old_snapshot: 前回のスナップショット（初回はNone）
        new_snapshot: 今回のスナップショット
        config: config.yaml の設定
    """
    try:
        # 1. HTMLから本文を抽出（前回と今回）
        new_text = extract_text_from_html(new_snapshot.content)
        old_text = extract_text_from_html(old_snapshot.content) if old_snapshot else ""

        # 2. Claude API初期化
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("⚠️ ANTHROPIC_API_KEY 未設定")
            return None

        client = anthropic.Anthropic(api_key=api_key)

        # 3. 共通プロンプトを使用
        system_prompt = get_system_prompt()

        # 4. 共通プロンプトを使用（changelog用）
        from post_prompt import create_user_prompt_from_changelog
        user_prompt = create_user_prompt_from_changelog(
            new_snapshot.url,
            new_snapshot.name,
            old_text if old_text else "",
            new_text
        )

        # 5. API呼び出し
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        response_text = message.content[0].text.strip()

        # ★ NOCHANGEチェック
        if response_text == "NOCHANGE" or response_text.startswith("NOCHANGE"):
            print(f"   ℹ️ Claude APIが変更なしと判断: {new_snapshot.name}")
            return None  # 投稿案なし

        return response_text

    except Exception as e:
        print(f"❌ 投稿案生成エラー: {e}")
        return None


def collect_rss_articles(config: Dict) -> List[Dict]:
    """当日公開のRSS記事を収集

    Returns:
        [{
            "title": "記事タイトル",
            "url": "記事URL",
            "feed_name": "フィード名",
            "published_at": "ISO8601形式",
            "description": "記事の説明"
        }, ...]
    """
    articles = []
    feeds = config.get("rss", {}).get("feeds", [])

    # 今日の日付を取得（UTC）
    today = datetime.now(timezone.utc).date()

    print(f"\n📡 RSS記事収集開始: {len(feeds)}フィード")

    for feed_config in feeds:
        feed_url = feed_config["url"]
        feed_name = feed_config["name"]

        try:
            # フィード取得
            feed = feedparser.parse(feed_url)

            # エラーチェック
            if hasattr(feed, 'status') and feed.status >= 400:
                print(f"   ⚠️ {feed_name}: HTTP {feed.status}")
                continue

            if not feed.entries:
                print(f"   ℹ️ {feed_name}: 記事0件")
                continue

            # 当日公開の記事のみフィルター
            today_articles = []
            for entry in feed.entries:
                published = entry.get("published_parsed") or entry.get("updated_parsed")
                if not published:
                    continue

                published_date = datetime(*published[:6], tzinfo=timezone.utc).date()

                # 当日公開の記事のみ
                if published_date == today:
                    article = {
                        "title": entry.get("title", "Untitled"),
                        "url": entry.get("link", ""),
                        "feed_name": feed_name,
                        "published_at": datetime(*published[:6], tzinfo=timezone.utc).isoformat(),
                        "description": entry.get("summary", "") or entry.get("description", "")
                    }
                    today_articles.append(article)

            if today_articles:
                print(f"   ✅ {feed_name}: {len(today_articles)}件（当日公開）")
                articles.extend(today_articles)
            else:
                print(f"   ℹ️ {feed_name}: 当日公開の記事なし")

        except Exception as e:
            print(f"   ❌ {feed_name}: {e}")
            continue

    print(f"\n📊 RSS記事収集完了: {len(articles)}件")
    return articles


def generate_post_from_article(article: Dict, config: Dict) -> Optional[str]:
    """RSS記事から投稿案を生成

    Args:
        article: RSS記事情報（title, url, feed_name, description）
        config: config.yaml の設定
    """
    try:
        # 1. 記事本文を取得（HTMLから抽出）
        headers = {"User-Agent": "Mozilla/5.0 (compatible; AIResearchBot/1.0)"}
        response = requests.get(article["url"], headers=headers, timeout=30)
        response.raise_for_status()

        # HTMLから本文を抽出
        content_text = extract_text_from_html(response.text)

        # 2. Claude API初期化
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            print("⚠️ ANTHROPIC_API_KEY 未設定")
            return None

        client = anthropic.Anthropic(api_key=api_key)

        # 3. システムプロンプト（ブログ記事用、changelogと同じフォーマット）
        system_prompt = """あなたはAI開発ツールのブログ記事を分析し、X投稿案を作成する専門家です。
読者は生成AI活用に積極的なWebエンジニアです。

【重要な原則】
- ブログ記事の重要なポイントを抽出する
- 具体的で実用的な情報を提供する（抽象的な表現は避ける）
- カテゴリ（新機能、改善点、ユースケースなど）を明確にする
- 技術的な詳細を省略せず、エンジニアが理解できるレベルで記載

【出力フォーマット】
## 概要
・[ポイント1: 簡潔に1行で]
・[ポイント2: 簡潔に1行で]
・[ポイント3: 簡潔に1行で]
・[ポイント4: 簡潔に1行で]
（3-5項目）

## 詳細
・新機能: [機能名と詳細な説明]
・新機能: [機能名と詳細な説明]
・ユースケース: [具体的な活用方法]
・改善点: [改善内容と詳細な説明]
・技術詳細: [技術的なポイント]
・対象ユーザー: [どのような開発者に有用か]
・提供開始: [リリース時期や利用方法]

{url}

【カテゴリの使い分け】
- 新機能: 新たに発表された機能やサービス
- ユースケース: 具体的な活用方法や事例
- 改善点: 既存機能の強化・最適化
- 技術詳細: アーキテクチャや実装の詳細
- バグ修正: 不具合の修正（該当する場合のみ）

【制約】
- 箇条書きには「・」（中黒）のみ使用
- 全体で600-800文字程度
- 記事にない情報は推測しない
- カテゴリのプレフィックス（「新機能:」など）を必ず含める"""

        # 4. ユーザープロンプト
        user_prompt = f"""以下のブログ記事について、X投稿案を作成してください。

【記事タイトル】
{article["title"]}

【URL】
{article["url"]}

【フィード名】
{article["feed_name"]}

【記事内容（抜粋）】
{content_text[:4000]}

上記フォーマットに従って投稿案を作成してください。"""

        # 5. API呼び出し
        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1500,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}]
        )

        return message.content[0].text

    except Exception as e:
        print(f"❌ 投稿案生成エラー: {e}")
        return None


def generate_post_from_rss_article(url: str, title: str, content: str, config: Dict) -> Optional[str]:
    """RSS記事から投稿案を生成

    Args:
        url: 記事URL
        title: 記事タイトル
        content: 記事本文
        config: 設定

    Returns:
        投稿案テキスト、生成失敗時はNone
    """
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY が設定されていません")

        client = anthropic.Anthropic(api_key=api_key)

        # 共通プロンプトを使用
        system_prompt = get_system_prompt()
        user_prompt = create_user_prompt_from_article(url, title, content)

        message = client.messages.create(
            model="claude-sonnet-4-5-20250929",
            max_tokens=1500,
            system=system_prompt,
            messages=[{
                "role": "user",
                "content": user_prompt
            }]
        )

        return message.content[0].text

    except Exception as e:
        print(f"❌ RSS投稿案生成エラー: {e}")
        return None


def process_rss_feeds(state: StateManager, config: Dict) -> List[Dict]:
    """RSSフィードを処理して新規記事の投稿案を生成

    Args:
        state: 状態管理
        config: 設定

    Returns:
        新規記事の投稿案リスト [{"url": str, "post_text": str, "title": str}, ...]
    """
    feeds = config.get("rss", {}).get("feeds", [])
    if not feeds:
        print("📰 RSS監視: フィード設定なし")
        return []

    print(f"\n📰 RSS監視: {len(feeds)} フィード")

    new_posts = []

    for feed_config in feeds:
        feed_url = feed_config["url"]
        feed_name = feed_config["name"]

        print(f"\n📡 {feed_name}")
        print(f"   URL: {feed_url}")

        try:
            # フィード取得
            feed = feedparser.parse(feed_url)

            # エラーチェック
            if hasattr(feed, 'status') and feed.status >= 400:
                print(f"   ⚠️  HTTP {feed.status}: 取得失敗")
                continue

            if not feed.entries:
                print(f"   ℹ️  記事0件")
                continue

            print(f"   ✅ 記事取得: {len(feed.entries)}件")

            # 前回取得した記事URLリストを取得
            previous_urls = state.get_rss_article_urls(feed_url)
            if previous_urls is None:
                previous_urls = []
                print(f"   ℹ️  初回取得（全記事を対象）")

            # 今回取得した記事URLリスト
            current_urls = [entry.link for entry in feed.entries]

            # 差分（新規記事）を抽出
            new_urls = set(current_urls) - set(previous_urls)

            if new_urls:
                print(f"   🆕 新規記事: {len(new_urls)}件")
            else:
                print(f"   ℹ️  新規記事なし")
                # 記事URLリストを更新
                state.set_rss_article_urls(feed_url, current_urls)
                continue

            # 新規記事を処理
            for entry in feed.entries:
                if entry.link not in new_urls:
                    continue

                print(f"\n   📄 新規記事: {entry.title[:60]}...")

                # 記事本文を取得
                article_title, article_content = fetch_article_content_safe(entry.link)

                if not article_content:
                    print(f"      ⚠️  記事本文取得失敗: {entry.link}")
                    continue

                print(f"      ✅ 記事本文取得成功: {len(article_content)}文字")

                # 投稿案を生成
                post_text = generate_post_from_rss_article(
                    entry.link,
                    article_title or entry.title,
                    article_content,
                    config
                )

                if post_text:
                    print(f"      ✅ 投稿案生成成功")
                    new_posts.append({
                        "url": entry.link,
                        "post_text": post_text,
                        "title": article_title or entry.title,
                        "feed_name": feed_name
                    })
                else:
                    print(f"      ⚠️  投稿案生成失敗")

            # 記事URLリストを更新
            state.set_rss_article_urls(feed_url, current_urls)
            print(f"   💾 記事URLリスト保存: {len(current_urls[:20])}件")

        except Exception as e:
            print(f"   ❌ エラー: {e}")
            continue

    return new_posts


def is_meta_message(post_text: str) -> bool:
    """投稿案がメタメッセージかどうかを判定

    Args:
        post_text: 生成された投稿案

    Returns:
        True: メタメッセージ（不正な内容）
        False: 正常な投稿案
    """
    # メタメッセージを示すキーワード
    meta_keywords = [
        "完全に同一",
        "完全に一致",
        "変更点は見つかりませんでした",
        "変更が見られません",
        "変更はありません",
        "新たに追加された変更点はありません",
        "両者のテキスト内容が完全に一致",
        "新規の変更点は検出されませんでした",
        "前回のスナップショットと今回のスナップショット",  # メタ的な表現
        "## 状況の説明",  # メタ的なセクション
        "結論：今回の比較では"  # メタ的な結論
    ]

    # いずれかのキーワードが含まれているか
    for keyword in meta_keywords:
        if keyword in post_text:
            return True

    # 投稿案が短すぎる（100文字未満）
    if len(post_text) < 100:
        return True

    # 必須セクションがない（## 概要 または ## 詳細）
    if "## " not in post_text:
        return True

    return False


def main():
    """メイン処理"""
    print("=" * 60)
    print("AI Semi-Dailyレポート - 8時・15時のchange log調査")
    print("=" * 60)

    # 設定読み込み
    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 環境変数取得
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not slack_webhook_url:
        raise ValueError("環境変数 SLACK_WEBHOOK_URL が設定されていません")

    # 状態管理初期化（run_daily.pyと共有）
    state = StateManager("data/state.json")

    try:
        # ページスナップショット監視
        print("\n📸 ページスナップショット監視開始")
        snapshot_manager = SnapshotManager()

        # 監視対象ページをconfig.yamlから読み込み
        page_config = config.get("page_monitoring", {})
        if not page_config.get("enabled", True):
            print("📸 ページスナップショット監視は無効化されています")
            snapshot_changes = []
        else:
            pages_to_monitor = page_config.get("pages", [])
            print(f"📸 ページスナップショット監視開始: {len(pages_to_monitor)}ページ")

            snapshot_changes = []  # [(old_snapshot, new_snapshot), ...]
            for page in pages_to_monitor:
                snapshot_pair = snapshot_manager.check_for_changes(
                    page["url"],
                    page["name"]
                )
                if snapshot_pair:  # (old_snapshot, new_snapshot)
                    snapshot_changes.append(snapshot_pair)

        # 下書きマップを初期化
        draft_map = {}  # {url: {"id": draft_id, "post_text": post_text}}

        # 下書き管理
        draft_manager = DraftManager()

        # スナップショット変更がある場合のみ処理
        if snapshot_changes:
            print(f"\n📊 変更検出: {len(snapshot_changes)} 件")

        # スナップショット変更を下書きとして保存（投稿案生成）
        for old_snapshot, new_snapshot in snapshot_changes:
            # 投稿案生成（前回と今回を渡す）
            post_text = generate_post_from_snapshot(old_snapshot, new_snapshot, config)

            if not post_text:
                print(f"⚠️ 投稿案生成失敗: {new_snapshot.name} - スキップ")
                # 失敗理由をdraft_mapに保存（NOCHANGEまたはAPI失敗）
                draft_map[new_snapshot.url] = {
                    "id": None,
                    "post_text": None,
                    "failure_reason": "NOCHANGE"
                }
                continue  # ★ フォールバックではなくスキップ

            # ★ メタメッセージ検証
            if is_meta_message(post_text):
                print(f"⚠️ メタメッセージ検出、スキップ: {new_snapshot.name}")
                # 失敗理由をdraft_mapに保存
                draft_map[new_snapshot.url] = {
                    "id": None,
                    "post_text": None,
                    "failure_reason": "META_MESSAGE"
                }
                continue

            # 下書き保存（正常な投稿案のみ）
            draft_id = draft_manager.save_draft(
                {
                    "title": new_snapshot.name,
                    "url": new_snapshot.url,
                    "source": "snapshot",
                    "metadata": {
                        "snapshot_timestamp": new_snapshot.timestamp,
                        "content_hash": new_snapshot.content_hash,
                        "old_hash": old_snapshot.content_hash if old_snapshot else None
                    }
                },
                post_text
            )
            print(f"📝 スナップショット変更を下書き保存: {draft_id}")
            draft_map[new_snapshot.url] = {
                "id": draft_id,
                "post_text": post_text,
                "failure_reason": None
            }

        # RSS記事収集と投稿案生成（新規記事のみ）
        print("\n📰 RSS監視開始")
        new_rss_posts = process_rss_feeds(state, config)

        rss_articles = []  # Slack通知用のリスト
        for post_data in new_rss_posts:
            # 下書き保存
            draft_id = draft_manager.save_draft(
                {
                    "title": post_data["title"],
                    "url": post_data["url"],
                    "source": "rss",
                    "published_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "feed_name": post_data["feed_name"],
                        "semi_daily": True  # semi-daily由来
                    }
                },
                post_data["post_text"]
            )
            print(f"📝 RSS記事を下書き保存: {draft_id} - {post_data['title'][:50]}...")
            draft_map[post_data["url"]] = {
                "id": draft_id,
                "post_text": post_data["post_text"],
                "failure_reason": None
            }

            # Slack通知用のリストに追加
            rss_articles.append({
                "title": post_data["title"],
                "url": post_data["url"],
                "feed_name": post_data["feed_name"],
                "published_at": datetime.now(timezone.utc).isoformat()
            })

        # 必見の更新をSlackに通知（changelogとブログ記事の両方）
        must_include_snapshots = [
            new_snapshot for old_snapshot, new_snapshot in snapshot_changes
            if any(p.get("must_include", False) and p["url"] == new_snapshot.url
                   for p in pages_to_monitor)
        ]
        send_snapshot_updates_to_slack(must_include_snapshots, rss_articles, slack_webhook_url, draft_map)
        if must_include_snapshots or rss_articles:
            print(f"\n🔔 {len(must_include_snapshots)}件のChangelog変更 + {len(rss_articles)}件のブログ記事を検出")

        # 古い履歴をクリーンアップ
        state.cleanup_old_posted_urls()

        # 状態保存
        state.save()
        print("💾 状態を保存しました")

    except Exception as e:
        print(f"❌ エラー発生: {e}")
        raise

    print("=" * 60)
    print("✅ 処理完了")
    print("=" * 60)


def send_snapshot_updates_to_slack(snapshots: List, rss_articles: List, webhook_url: str, draft_map: Dict):
    """スナップショット変更とRSS記事をSlackに送信（必見の更新）"""
    import requests

    message = {
        "text": f"⭐ 必見の更新: {len(snapshots) + len(rss_articles)}件",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "⭐ 必見の更新"
                }
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"📊 分析対象:\n・Changelogスナップショット {len(snapshots)}件\n・ブログ記事 {len(rss_articles)}件"
                }
            }
        ]
    }

    # 各スナップショットの詳細を追加
    for snapshot in snapshots:
        draft_info = draft_map.get(snapshot.url)

        # 失敗理由を判定
        if not draft_info:
            post_text = "❌ 投稿案生成失敗（不明なエラー）"
        elif draft_info.get("failure_reason") == "NOCHANGE":
            post_text = "ℹ️ 実質的な変更なし（Claude API判断）"
        elif draft_info.get("failure_reason") == "META_MESSAGE":
            post_text = "ℹ️ メタメッセージ検出（投稿案として不適切）"
        elif draft_info.get("failure_reason") == "API_FAILURE":
            post_text = "❌ API呼び出し失敗"
        else:
            post_text = draft_info["post_text"]

        # ページ名 + ソースリンク
        message["blocks"].append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📝 *{snapshot.name}*\n<{snapshot.url}|ソースを確認>"
            }
        })

        # 投稿案（コードブロック）
        message["blocks"].append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```\n{post_text}\n```"
            }
        })

        # 区切り線
        message["blocks"].append({"type": "divider"})

    # RSS記事を追加
    for article in rss_articles:
        draft_info = draft_map.get(article["url"])

        # 失敗理由を判定
        if not draft_info:
            post_text = "❌ 投稿案生成失敗（不明なエラー）"
        elif draft_info.get("failure_reason") == "NOCHANGE":
            post_text = "ℹ️ 実質的な変更なし（Claude API判断）"
        elif draft_info.get("failure_reason") == "META_MESSAGE":
            post_text = "ℹ️ メタメッセージ検出（投稿案として不適切）"
        elif draft_info.get("failure_reason") == "API_FAILURE":
            post_text = "❌ API呼び出し失敗"
        else:
            post_text = draft_info["post_text"]

        # フィード名 + ソースリンク + 記事タイトル
        message["blocks"].append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📝 *{article['feed_name']}*\n<{article['url']}|ソースを確認>\n_{article['title']}_"
            }
        })

        # 投稿案（コードブロック）
        message["blocks"].append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```\n{post_text}\n```"
            }
        })

        # 区切り線（最後の記事以外）
        if article != rss_articles[-1]:
            message["blocks"].append({"type": "divider"})

    # 更新なしの場合
    if not snapshots and not rss_articles:
        message = {
            "text": "📭 本日の更新なし",
            "blocks": [
                {
                    "type": "header",
                    "text": {
                        "type": "plain_text",
                        "text": "⭐ 必見の更新"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "📭 本日の更新なし\n・Changelogスナップショット: 0件\n・ブログ記事（15フィード）: 0件\n\n対象: Claude Code, GitHub Copilot, Cursor（Changelog） + OpenAI Blog, Anthropic News等（RSS）"
                    }
                }
            ]
        }

    # Slack送信
    try:
        response = requests.post(webhook_url, json=message)
        if response.status_code == 200:
            print(f"✅ 必見の更新をSlackに送信しました（Changelog {len(snapshots)}件 + ブログ記事 {len(rss_articles)}件）")
        else:
            print(f"⚠️  Slack送信失敗: {response.status_code}")
    except Exception as e:
        print(f"⚠️  Slack送信エラー: {e}")


if __name__ == "__main__":
    main()
