#!/usr/bin/env python3
"""
下書きを手動でX投稿するスクリプト

使い方:
  python scripts/post_drafts.py                 # 全ての承認待ち下書きを投稿
  python scripts/post_drafts.py <draft_id>      # 特定の下書きのみ投稿
"""

import os
import sys
from draft_manager import DraftManager
from run_daily import XAPIClient


def post_all_pending_drafts():
    """全ての承認待ち下書きを投稿"""
    # OAuth認証情報の取得
    oauth_credentials = {
        "api_key": os.environ.get("X_API_KEY"),
        "api_secret": os.environ.get("X_API_SECRET"),
        "access_token": os.environ.get("X_ACCESS_TOKEN"),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET")
    }

    # XAPIClient の初期化
    x_client = XAPIClient(
        bearer_token=os.environ.get("X_BEARER_TOKEN"),
        oauth_credentials=oauth_credentials
    )

    # 下書き管理
    draft_manager = DraftManager()
    pending_drafts = draft_manager.get_pending_drafts()

    if not pending_drafts:
        print("✅ 承認待ちの下書きはありません")
        return

    print(f"📝 {len(pending_drafts)}件の下書きを投稿します\n")

    for draft in pending_drafts:
        draft_id = draft["id"]
        post_text = draft["post_text"]
        title = draft["item"]["title"][:50]

        print(f"🐦 投稿中: {draft_id} - {title}...")

        try:
            result = x_client.post_tweet(post_text)
            draft_manager.mark_as_posted(draft_id)
            print(f"✅ 投稿成功: {result.get('data', {}).get('id')}\n")
        except Exception as e:
            print(f"❌ 投稿失敗: {e}\n")


def post_specific_draft(draft_id: str):
    """特定の下書きを投稿"""
    # OAuth認証情報の取得
    oauth_credentials = {
        "api_key": os.environ.get("X_API_KEY"),
        "api_secret": os.environ.get("X_API_SECRET"),
        "access_token": os.environ.get("X_ACCESS_TOKEN"),
        "access_token_secret": os.environ.get("X_ACCESS_TOKEN_SECRET")
    }

    # XAPIClient の初期化
    x_client = XAPIClient(
        bearer_token=os.environ.get("X_BEARER_TOKEN"),
        oauth_credentials=oauth_credentials
    )

    # 下書き管理
    draft_manager = DraftManager()
    pending_drafts = draft_manager.get_pending_drafts()

    # 指定されたIDの下書きを探す
    target_draft = None
    for draft in pending_drafts:
        if draft["id"] == draft_id:
            target_draft = draft
            break

    if not target_draft:
        print(f"❌ 下書きID {draft_id} が見つかりません")
        return

    post_text = target_draft["post_text"]
    title = target_draft["item"]["title"][:50]

    print(f"🐦 投稿中: {draft_id} - {title}...")

    try:
        result = x_client.post_tweet(post_text)
        draft_manager.mark_as_posted(draft_id)
        print(f"✅ 投稿成功: {result.get('data', {}).get('id')}")
    except Exception as e:
        print(f"❌ 投稿失敗: {e}")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        post_specific_draft(sys.argv[1])
    else:
        post_all_pending_drafts()
