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

# 既存モジュールをインポート
from run_daily import (
    XAPIClient, StateManager, DataCollector, SlackReporter
)
from draft_manager import DraftManager
from content_validator import ContentValidator


@dataclass
class PageSnapshot:
    """ページスナップショット"""
    url: str
    name: str
    content_hash: str
    content: str
    timestamp: str


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

    def check_for_changes(self, url: str, name: str) -> Optional[PageSnapshot]:
        """ページの変更をチェック（前回分のみ保持）"""
        try:
            # 新しいコンテンツを取得
            new_content = self.fetch_page_content(url)
            new_hash = hashlib.sha256(new_content.encode()).hexdigest()

            # 前回のスナップショットを読み込み
            old_snapshot = self.load_snapshot(url)

            # 初回または変更あり
            if old_snapshot is None or old_snapshot.content_hash != new_hash:
                new_snapshot = PageSnapshot(
                    url=url,
                    name=name,
                    content_hash=new_hash,
                    content=new_content,
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                # 前回のスナップショットを上書き（累積保存しない）
                self.save_snapshot(new_snapshot)

                if old_snapshot is None:
                    print(f"📸 初回スナップショット: {name}")
                    return None  # 初回は変更として扱わない
                else:
                    print(f"🔄 変更検出: {name}")
                    return new_snapshot
            else:
                # 変更がない場合も最新のタイムスタンプで上書き
                # （前回のスナップショットを最新に保つ）
                new_snapshot = PageSnapshot(
                    url=url,
                    name=name,
                    content_hash=new_hash,
                    content=new_content,
                    timestamp=datetime.now(timezone.utc).isoformat()
                )
                self.save_snapshot(new_snapshot)
                print(f"✅ 変更なし: {name}")
                return None

        except Exception as e:
            print(f"❌ スナップショット取得失敗: {name} - {e}")
            return None


def main():
    """メイン処理"""
    print("=" * 60)
    print("AI Semi-Dailyレポート - 8時・15時のchange log調査")
    print("=" * 60)

    # 設定読み込み
    with open("config.yaml", 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)

    # 環境変数取得
    x_bearer_token = os.environ.get("X_BEARER_TOKEN")
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not x_bearer_token:
        raise ValueError("環境変数 X_BEARER_TOKEN が設定されていません")
    if not slack_webhook_url:
        raise ValueError("環境変数 SLACK_WEBHOOK_URL が設定されていません")

    # OAuth認証情報の取得（X API投稿用）
    oauth_credentials = None
    if all([
        os.environ.get("X_API_KEY"),
        os.environ.get("X_API_SECRET"),
        os.environ.get("X_ACCESS_TOKEN"),
        os.environ.get("X_ACCESS_TOKEN_SECRET")
    ]):
        oauth_credentials = {
            "api_key": os.environ.get("X_API_KEY"),
            "api_secret": os.environ.get("X_API_SECRET"),
            "access_token": os.environ.get("X_ACCESS_TOKEN"),
            "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET")
        }

    # クライアント初期化
    x_client = XAPIClient(x_bearer_token, oauth_credentials)
    state = StateManager("data/state_hourly.json")  # semi-daily専用のstate
    collector = DataCollector(config, state, x_client)

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

            snapshot_changes = []
            for page in pages_to_monitor:
                changed_snapshot = snapshot_manager.check_for_changes(
                    page["url"],
                    page["name"]
                )
                if changed_snapshot:
                    snapshot_changes.append(changed_snapshot)

        # スナップショット変更があればSlackに通知
        if snapshot_changes:
            print(f"\n🔔 {len(snapshot_changes)}件のページ変更を検出")
            # TODO: Slack通知を実装（後ほど）

        # 既存のデータ収集
        print("\n📊 データ収集開始")
        collector.collect_all()

        # 更新がない場合は早期終了
        if not collector.items and not snapshot_changes:
            print("✅ 新しいアイテムはありません")
            state.save()
            return

        # 重複チェック
        new_items = []
        for item in collector.items:
            if not state.is_recently_posted(item.url):
                new_items.append(item)
            else:
                print(f"⏭️  スキップ（24時間以内に投稿済み）: {item.url}")

        if not new_items and not snapshot_changes:
            print("✅ 新しいアイテムはありません（全て投稿済み）")
            state.save()
            return

        collector.items = new_items

        # Slackレポート送信
        if collector.items:
            reporter = SlackReporter(
                slack_webhook_url,
                config,
                collector.items,
                collector.stats
            )
            reporter.send()

        # 下書き管理
        draft_manager = DraftManager()
        validator = ContentValidator(config)  # 検証器初期化

        # 上位3件を下書きとして保存
        for item in collector.items[:3]:
            post_text = reporter._create_single_post(
                title=item.title,
                url=item.url,
                source_type=item.source,
                source_name=item.metadata.get("feed_name", ""),
                date=datetime.now().strftime('%Y/%m/%d'),
                item=item
            )

            # 検証フェーズ1: 正規表現ベース
            if post_text is None:
                print(f"⏭️  下書きスキップ（検証失敗）: {item.title[:50]}...")
                continue

            validation_result = validator.validate_post(post_text, item.title)
            if not validation_result.is_valid:
                print(f"⏭️  下書きスキップ（検証失敗）: {item.title[:50]}...")
                print(f"    理由: {validation_result.rejection_reason}")
                continue

            # 検証フェーズ2: Claude APIレビュー
            review_result = validator.review_post_with_claude(post_text, item.title, item.url)
            if not review_result.is_valid:
                print(f"⏭️  下書きスキップ（レビュー失敗）: {item.title[:50]}...")
                print(f"    理由: {review_result.rejection_reason}")
                continue

            draft_id = draft_manager.save_draft(asdict(item), post_text)
            print(f"📝 下書き保存: {draft_id} - {item.title[:50]}...")

            # 投稿済みにマーク
            state.mark_as_posted(item.url)

        # スナップショット変更も下書きとして保存
        for snapshot in snapshot_changes:
            # 簡易的な投稿テキスト生成
            post_text = f"{snapshot.name}が更新されました\n\n{snapshot.url}\n\n{datetime.now().strftime('%Y/%m/%d')}"
            draft_id = draft_manager.save_draft(
                {
                    "title": f"{snapshot.name} 更新",
                    "url": snapshot.url,
                    "source": "snapshot",
                    "metadata": {"snapshot": True}
                },
                post_text
            )
            print(f"📝 スナップショット変更を下書き保存: {draft_id}")

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


if __name__ == "__main__":
    main()
