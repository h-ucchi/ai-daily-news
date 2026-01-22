#!/usr/bin/env python3
"""
AI Hourly Report - 1時間ごとのchange log調査とX投稿下書き生成

使い方:
  python scripts/run_hourly.py
"""

import os
import sys
from datetime import datetime, timezone
from dataclasses import asdict
import yaml

# 既存モジュールをインポート
from run_daily import (
    XAPIClient, StateManager, DataCollector, SlackReporter
)
from draft_manager import DraftManager


def main():
    """メイン処理"""
    print("=" * 60)
    print("AI Hourlyレポート - 1時間ごとのchange log調査")
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
    state = StateManager("data/state_hourly.json")  # hourly専用のstate
    collector = DataCollector(config, state, x_client)

    try:
        # データ収集
        collector.collect_all()

        # 更新がない場合は早期終了
        if not collector.items:
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

        if not new_items:
            print("✅ 新しいアイテムはありません（全て投稿済み）")
            state.save()
            return

        collector.items = new_items

        # Slackレポート送信
        reporter = SlackReporter(
            slack_webhook_url,
            config,
            collector.items,
            collector.stats
        )
        reporter.send()

        # 下書き管理
        draft_manager = DraftManager()

        # 上位3件を下書きとして保存（dailyは5件だが、hourlyは少なめ）
        for item in collector.items[:3]:
            post_text = reporter._create_single_post(
                title=item.title,
                url=item.url,
                source_type=item.source,
                source_name=item.metadata.get("feed_name", ""),
                date=datetime.now().strftime('%Y/%m/%d'),
                item=item
            )
            draft_id = draft_manager.save_draft(asdict(item), post_text)
            print(f"📝 下書き保存: {draft_id} - {item.title[:50]}...")

            # 投稿済みにマーク
            state.mark_as_posted(item.url)

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
