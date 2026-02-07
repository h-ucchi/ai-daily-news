#!/usr/bin/env python3
"""
手動記事投稿案生成スクリプト
"""
import os
import sys
import requests
from bs4 import BeautifulSoup
import anthropic


# 共通モジュールから import
from article_fetcher import fetch_article_content
from post_prompt import get_system_prompt, create_user_prompt_from_article


def generate_post(url: str, title: str, content: str) -> str:
    """Claude APIで投稿案を生成"""
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


def main():
    if len(sys.argv) < 2:
        print("使用方法: python generate_post_manual.py <URL>")
        return 1

    url = sys.argv[1]

    # 環境変数チェック
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("❌ エラー: ANTHROPIC_API_KEY 環境変数が設定されていません")
        return 1

    print("=" * 60)
    print("手動投稿案生成")
    print("=" * 60)
    print()

    try:
        # 記事取得
        print(f"📥 記事を取得中: {url}")
        title, content = fetch_article_content(url)
        print(f"✅ タイトル: {title[:50]}...")
        print()

        # 投稿案生成
        print("✍️  投稿案を生成中...")
        post_text = generate_post(url, title, content)
        print("✅ 生成完了")
        print()

        # 結果表示
        print("=" * 60)
        print("生成された投稿案:")
        print("=" * 60)
        print()
        print(post_text)
        print()
        print("=" * 60)

        return 0

    except Exception as e:
        print(f"❌ エラー: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
