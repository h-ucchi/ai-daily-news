#!/usr/bin/env python3
"""
手動記事投稿案生成スクリプト
"""
import os
import sys
import requests
from bs4 import BeautifulSoup
import anthropic


def fetch_article_content(url: str) -> tuple[str, str]:
    """記事を取得してタイトルと本文を返す"""
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; AIResearchBot/1.0)"
    }
    response = requests.get(url, headers=headers, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, 'html.parser')

    # タイトル取得
    title = soup.find('title')
    title_text = title.get_text(strip=True) if title else "Unknown"

    # 不要タグを削除
    for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
        tag.decompose()

    # 本文取得
    content = soup.get_text(separator='\n', strip=True)

    return title_text, content


def generate_post(url: str, title: str, content: str) -> str:
    """Claude APIで投稿案を生成"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)

    # 既存のrun_daily.pyのプロンプトを簡略化して使用
    system_prompt = """あなたはAI業界のトレンドを追うX（Twitter）アカウントの投稿作成者です。
読者は生成AI活用に積極的なWebエンジニアです。

【重要な原則】
- 具体的で実用的な情報を提供する
- 読者が「自分も使ってみたい」と思える内容にする
- 抽象的な表現（「革新的」「画期的」）は避け、何ができるかを明示する
- 絵文字は最小限に抑える
- セクション番号は「1.」「2.」「3.」の形式で構造化する

【出力フォーマット】
企業名/製品名、主要な内容を1行で要約

[2-3文で核心を要約]

{url}

💡 業界インパクト（または主要ポイント）
・[具体的な効果や特徴1]
・[具体的な効果や特徴2]
・[具体的な効果や特徴3]

1. [セクション名]
・ポイント1
・ポイント2
・ポイント3

2. [セクション名]
・ポイント4
・ポイント5

3. [利用方法・対象者]
・対象ユーザー: [具体的に]
・提供開始: [いつから]

【制約】
- 各セクションの箇条書きは3-5項目
- 箇条書きには「・」（中黒）のみ使用
- 全体で600-800文字程度
- 記事にない情報は推測しない"""

    user_prompt = f"""以下の記事について、X投稿スレッドの素案を作成してください。

【URL】
{url}

【記事タイトル】
{title}

【記事本文（抜粋）】
{content[:4000]}

上記フォーマットに従って投稿案を作成してください。"""

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
