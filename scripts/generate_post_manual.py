#!/usr/bin/env python3
"""
手動記事投稿案生成スクリプト（AI-lint自動修正機能付き）
"""
import os
import sys
import json
import argparse
from typing import Optional, List
import anthropic

# 共通モジュールから import
from article_fetcher import fetch_article_content
from post_prompt import get_system_prompt, create_user_prompt_from_article
from ai_lint_checker import AILintChecker, Detection


def load_config_from_file() -> dict:
    """`.claude/settings.local.json`から設定を読み込む

    Returns:
        設定辞書（env キーに環境変数が含まれる）
    """
    config_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        ".claude",
        "settings.local.json"
    )

    if not os.path.exists(config_path):
        return {}

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def parse_arguments():
    """コマンドライン引数をパース"""
    parser = argparse.ArgumentParser(
        description="手動記事投稿案生成スクリプト（AI-lint自動修正機能付き）"
    )

    # 排他的グループ: URLまたはテキストファイルのどちらか一方のみ指定可能
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        "url",
        nargs="?",
        help="記事URL（通常のWebページまたはX URL）"
    )
    input_group.add_argument(
        "--text-file",
        type=str,
        metavar="FILE",
        help="テキストファイルパス（投稿案生成元のテキスト）"
    )

    return parser.parse_args()


def get_api_key() -> Optional[str]:
    """APIキーを取得（環境変数 → 設定ファイルの順）

    Returns:
        APIキー、または None
    """
    # 1. 環境変数から取得
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if api_key:
        return api_key

    # 2. 設定ファイルから取得
    config = load_config_from_file()
    return config.get("env", {}).get("ANTHROPIC_API_KEY")


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
    api_key = get_api_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません（環境変数または .claude/settings.local.json を確認してください）")

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


def generate_post_from_text(content: str, detected_issues: Optional[List[Detection]] = None) -> str:
    """テキストからClaude APIで投稿案を生成（URL・タイトルなし）

    Args:
        content: ユーザーが提供したテキスト本文
        detected_issues: 検出されたAI表現のリスト（2回目以降の生成で使用）

    Returns:
        生成された投稿案
    """
    api_key = get_api_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません（環境変数または .claude/settings.local.json を確認してください）")

    client = anthropic.Anthropic(api_key=api_key)

    # テキスト用プロンプトを使用
    from post_prompt import get_system_prompt, create_user_prompt_from_text
    system_prompt = get_system_prompt()
    user_prompt = create_user_prompt_from_text(content)

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


def generate_title_from_post(post_text: str) -> str:
    """投稿案から惹きのあるタイトルを生成

    Args:
        post_text: 生成された投稿案

    Returns:
        生成されたタイトル
    """
    api_key = get_api_key()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY が設定されていません")

    client = anthropic.Anthropic(api_key=api_key)

    # タイトル生成用プロンプト
    from post_prompt import create_title_generation_prompt
    user_prompt = create_title_generation_prompt(post_text)

    message = client.messages.create(
        model="claude-sonnet-4-5-20250929",
        max_tokens=200,  # タイトルなので短い
        system="あなたは惹きのあるタイトルを生成する専門家です。",
        messages=[{
            "role": "user",
            "content": user_prompt
        }]
    )

    title = message.content[0].text.strip()

    # タイトルが長すぎる場合は警告
    if len(title) > 80:
        print(f"⚠️  生成されたタイトルが長い（{len(title)}文字）: {title[:50]}...")

    return title


def main():
    args = parse_arguments()

    # APIキーチェック
    if not get_api_key():
        print("❌ エラー: ANTHROPIC_API_KEY が設定されていません")
        print()
        print("以下のいずれかで設定してください：")
        print("1. 環境変数: export ANTHROPIC_API_KEY='sk-ant-...'")
        print("2. 設定ファイル: .claude/settings.local.json の env.ANTHROPIC_API_KEY")
        return 1

    print("=" * 60)
    print("手動投稿案生成（AI-lint自動修正）")
    print("=" * 60)
    print()

    try:
        # AI-lintチェッカー初期化
        rules_path = os.path.join(os.path.dirname(__file__), "..", "ai-lint", ".claude", "skills", "ai-lint", "rules", "ai-lint-rules.yml")
        if os.path.exists(rules_path):
            checker = AILintChecker(rules_path)
            print("✓ AI-lintルールを読み込みました")
        else:
            checker = AILintChecker()  # デフォルトルールを使用
            print("✓ デフォルトAI-lintルールを使用します")

        # パターン分岐: URL入力 vs テキスト入力
        if args.text_file:
            # ============================================
            # テキスト入力モード
            # ============================================
            print(f"📄 テキストファイルを読み込み中: {args.text_file}")

            # ファイル存在チェック
            if not os.path.exists(args.text_file):
                print(f"❌ エラー: ファイルが見つかりません: {args.text_file}")
                return 1

            # ファイル読み込み
            with open(args.text_file, "r", encoding="utf-8") as f:
                content = f.read().strip()

            # 空ファイルチェック
            if not content:
                print(f"❌ エラー: ファイルが空です: {args.text_file}")
                return 1

            print(f"✅ テキストを読み込みました（{len(content)}文字）")
            print()

            # 投稿案生成（最大3回リトライ）
            max_retries = 2
            score_threshold = 15
            post_text = None
            lint_result = None

            for attempt in range(max_retries + 1):
                print(f"✍️  投稿案を生成中... (試行 {attempt + 1}/{max_retries + 1})")

                # 投稿案生成（テキストモード）
                if attempt == 0:
                    post_text = generate_post_from_text(content)
                else:
                    post_text = generate_post_from_text(content, detected_issues=lint_result.detections[:5])

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

            # タイトル生成（投稿案生成後）
            print("📝 タイトルを生成中...")
            title = generate_title_from_post(post_text)
            print(f"✅ タイトル: {title}")
            print()

            # 最終結果表示（タイトル + 投稿案）
            print("=" * 60)
            print("生成されたタイトルと投稿案:")
            print("=" * 60)
            print()
            print("【タイトル】")
            print(title)
            print()
            print("【投稿案】")
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

        else:
            # ============================================
            # URL入力モード（既存のフロー）
            # ============================================
            url = args.url

            # 記事取得
            print(f"📥 記事を取得中: {url}")
            title, content = fetch_article_content(url)
            print(f"✅ タイトル: {title[:50]}...")
            print()

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
