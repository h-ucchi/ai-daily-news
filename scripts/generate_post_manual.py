#!/usr/bin/env python3
"""
手動記事投稿案生成スクリプト（AI-lint自動修正機能付き）
"""
import os
import sys
from typing import Optional, List
import anthropic

# 共通モジュールから import
from article_fetcher import fetch_article_content
from post_prompt import get_system_prompt, create_user_prompt_from_article
from ai_lint_checker import AILintChecker, Detection


def generate_post(url: str, title: str, content: str, detected_issues: Optional[List[Detection]] = None) -> str:
    """Claude APIで投稿案を生成（修正指示オプション付き）

    Args:
        url: 記事URL
        title: 記事タイトル
        content: 記事本文
        detected_issues: 検出されたAI表現のリスト（2回目以降の生成で使用）

    Returns:
        生成された投稿案
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)

    # 共通プロンプトを使用
    system_prompt = get_system_prompt()
    user_prompt = create_user_prompt_from_article(url, title, content)

    # 検出された問題があれば修正指示を追加
    if detected_issues:
        fix_instructions = "\n\n【重要：以下の表現が検出されたので必ず修正してください】\n"
        for issue in detected_issues:
            fix_instructions += f"❌ 「{issue.matched_text}」→ {issue.suggestion}\n"
        user_prompt += fix_instructions

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
    print("手動投稿案生成（AI-lint自動修正）")
    print("=" * 60)
    print()

    try:
        # 記事取得
        print(f"📥 記事を取得中: {url}")
        title, content = fetch_article_content(url)
        print(f"✅ タイトル: {title[:50]}...")
        print()

        # AI-lintチェッカー初期化
        rules_path = os.path.join(os.path.dirname(__file__), "..", "ai-lint", ".claude", "skills", "ai-lint", "rules", "ai-lint-rules.yml")
        if os.path.exists(rules_path):
            checker = AILintChecker(rules_path)
            print("✓ AI-lintルールを読み込みました")
        else:
            checker = AILintChecker()  # デフォルトルールを使用
            print("✓ デフォルトAI-lintルールを使用します")

        # 投稿案生成（最大3回リトライ）
        max_retries = 2
        score_threshold = 15  # この値以下ならOK
        post_text = None
        lint_result = None

        for attempt in range(max_retries + 1):
            print(f"✍️  投稿案を生成中... (試行 {attempt + 1}/{max_retries + 1})")

            # 投稿案生成
            if attempt == 0:
                post_text = generate_post(url, title, content)
            else:
                # 2回目以降は検出された問題を修正指示として追加（上位5件）
                post_text = generate_post(url, title, content, detected_issues=lint_result.detections[:5])

            print("✅ 生成完了")
            print()

            # AI-lintチェック
            print("🔍 AI的表現をチェック中...")
            lint_result = checker.check(post_text)

            if lint_result.score == 0:
                print("✅ AI的表現は検出されませんでした")
                break
            elif lint_result.score < score_threshold:
                print(f"✅ AIスコア: {lint_result.score} (許容範囲内)")
                break
            else:
                print(f"⚠️  AIスコア: {lint_result.score} (検出数: {lint_result.total_patterns}件)")
                if attempt < max_retries:
                    print(f"   → 修正して再生成します...")
                    print()
                else:
                    print(f"   → 最大試行回数に達しました")
                    print()

        # 最終結果表示
        print("=" * 60)
        print("生成された投稿案:")
        print("=" * 60)
        print()
        print("```")
        print(post_text)
        print("```")
        print()
        print("=" * 60)

        # 検出された問題があれば詳細表示
        if lint_result and lint_result.score > 0:
            print()
            print(checker.format_result(lint_result))

        return 0

    except Exception as e:
        print(f"❌ エラー: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
