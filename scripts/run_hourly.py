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
from run_daily import StateManager
from draft_manager import DraftManager


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
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL")

    if not slack_webhook_url:
        raise ValueError("環境変数 SLACK_WEBHOOK_URL が設定されていません")

    # 状態管理初期化（semi-daily専用のstate）
    state = StateManager("data/state_hourly.json")

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

        # 必見の更新をSlackに通知（変更あり・なし両方）
        must_include_snapshots = [
            snapshot for snapshot in snapshot_changes
            if any(p.get("must_include", False) and p["url"] == snapshot.url
                   for p in pages_to_monitor)
        ]
        send_snapshot_updates_to_slack(must_include_snapshots, slack_webhook_url)
        if must_include_snapshots:
            print(f"\n🔔 {len(must_include_snapshots)}件の必見ページ変更を検出")

        # changelogのみを監視（X/RSSは収集しない）
        if not snapshot_changes:
            print("✅ 新しい変更はありません")
            state.save()
            return

        print(f"\n📊 変更検出: {len(snapshot_changes)} 件")

        # 下書き管理
        draft_manager = DraftManager()

        # スナップショット変更を下書きとして保存
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


def send_snapshot_updates_to_slack(snapshots: List, webhook_url: str):
    """スナップショット変更をSlackに送信（必見の更新）"""
    import requests

    message = {
        "text": f"⭐ 必見の更新: {len(snapshots)}件のページ変更",
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
                    "text": f"監視対象ページに *{len(snapshots)}件* の更新がありました。"
                }
            }
        ]
    }

    # 各スナップショットの詳細を追加
    for snapshot in snapshots:
        message["blocks"].append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📝 *{snapshot.name}*\n<{snapshot.url}|変更を確認>"
            }
        })

    # 更新なしの場合
    if not snapshots:
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
                        "text": "📭 *本日の更新なし*\n対象: Claude Code, GitHub Copilot, Cursor"
                    }
                }
            ]
        }

    # Slack送信
    try:
        response = requests.post(webhook_url, json=message)
        if response.status_code == 200:
            print(f"✅ 必見の更新をSlackに送信しました（{len(snapshots)}件）")
        else:
            print(f"⚠️  Slack送信失敗: {response.status_code}")
    except Exception as e:
        print(f"⚠️  Slack送信エラー: {e}")


if __name__ == "__main__":
    main()
