"""
コンテンツ検証モジュール - メタメッセージ検出と不適切コンテンツフィルタリング
"""
import re
import os
from typing import Optional, List, Dict
from dataclasses import dataclass, field


@dataclass
class ValidationResult:
    """検証結果"""
    is_valid: bool
    rejection_reason: Optional[str] = None
    detected_issues: List[str] = field(default_factory=list)


class ContentValidator:
    """コンテンツ検証器（軽量・正規表現ベース + Claude APIレビュー）"""

    # メタメッセージパターン
    META_MESSAGE_PATTERNS = [
        r"フォーマット.*?適していません",
        r"投稿.*?作成には不適",
        r"以下のニュースは.*?ではない",
        r"指定されたフォーマットで",
        r"投稿案として.*?不適切",
        r"記事内容が.*?適していない",
        r"投稿すべきでない",
        r"このニュースは.*?対象外",
        r"申し訳ありませんが",
        r"生成できません",
        r"作成できません",
        r"該当しません",
        r"適切ではありません",
    ]

    # 訴訟・炎上キーワード
    LAWSUIT_PATTERNS = [
        r"訴訟",
        r"提訴",
        r"集団訴訟",
        r"被告",
        r"原告",
        r"賠償請求",
        r"法的措置",
        r"lawsuit",
        r"sued",
        r"litigation",
        r"plaintiff",
        r"defendant",
    ]

    # 政治・センシティブキーワード
    POLITICAL_PATTERNS = [
        r"大統領令",
        r"政権",
        r"政治的",
        r"規制当局.*?批判",
        r"議会.*?非難",
        r"政府批判",
        r"political.*?controversy",
        r"administration.*?criticism",
    ]

    def __init__(self, config: Dict = None):
        self.config = config or {}
        self.validation_enabled = self.config.get("content_validation", {}).get("enabled", True)
        self.claude_review_enabled = self.config.get("content_validation", {}).get("claude_review", {}).get("enabled", True)
        self.claude_client = None

    def validate_post(self, post_text: str, title: str = "") -> ValidationResult:
        """投稿案を検証（軽量・正規表現ベース）"""
        if not self.validation_enabled:
            return ValidationResult(is_valid=True)

        detected_issues = []

        # 1. メタメッセージ検出
        if self._contains_meta_message(post_text):
            detected_issues.append("meta_message")

        # 2. 訴訟・法的問題の検出
        if self._contains_lawsuit_content(post_text, title):
            detected_issues.append("lawsuit")

        # 3. 政治的コンテンツの検出
        if self._contains_political_content(post_text, title):
            detected_issues.append("political")

        # 4. 最小文字数チェック
        if len(post_text.strip()) < 50:
            detected_issues.append("too_short")

        # 判定
        if detected_issues:
            return ValidationResult(
                is_valid=False,
                rejection_reason=self._format_rejection_reason(detected_issues),
                detected_issues=detected_issues
            )

        return ValidationResult(is_valid=True)

    def review_post_with_claude(self, post_text: str, title: str, url: str) -> ValidationResult:
        """Claude APIで投稿案をレビュー（記事内容との整合性チェック）"""
        if not self.claude_review_enabled:
            return ValidationResult(is_valid=True)

        try:
            import anthropic

            if self.claude_client is None:
                api_key = os.environ.get("ANTHROPIC_API_KEY")
                if not api_key:
                    return ValidationResult(is_valid=True)
                self.claude_client = anthropic.Anthropic(api_key=api_key)

            system_prompt = """あなたは投稿案の事実確認を行う専門家です。
以下の観点で投稿案をレビューしてください：

【最重要】固有名詞の正確性
- 記事タイトルに含まれる固有名詞（製品名、バージョン、機能名）のみを使用しているか
- 投稿案に記事タイトルにない固有名詞が含まれていないか
- 例: 記事が "Claude Code v2.1.16" なのに、投稿案で "Claude Engineer Mode" と記載していないか

【重要】推測の排除
- 記事タイトルから推測できない情報を追加していないか
- 旧称や別名を勝手に補完していないか

【出力フォーマット】
{
  "is_valid": true/false,
  "issues": ["問題1", "問題2"],
  "explanation": "判定理由"
}

【判定基準】
- is_valid = false: 記事にない固有名詞が1つでも含まれる場合
- is_valid = true: 記事の情報のみで構成されている場合"""

            user_prompt = f"""以下の投稿案をレビューしてください。

【記事タイトル】
{title}

【参照URL】
{url}

【投稿案】
{post_text}

記事タイトルに含まれない固有名詞や推測された情報がないか検証してください。
結果をJSON形式で返してください。"""

            message = self.claude_client.messages.create(
                model="claude-sonnet-4-5-20250929",
                max_tokens=500,
                system=system_prompt,
                messages=[{
                    "role": "user",
                    "content": user_prompt
                }]
            )

            # JSONパース
            import json
            response_text = message.content[0].text
            json_match = re.search(r'\{.*\}', response_text, re.DOTALL)

            if json_match:
                result = json.loads(json_match.group())

                if not result.get("is_valid", True):
                    return ValidationResult(
                        is_valid=False,
                        rejection_reason=f"Claude レビュー失敗: {result.get('explanation', '')}",
                        detected_issues=result.get("issues", [])
                    )

            return ValidationResult(is_valid=True)

        except Exception as e:
            print(f"⚠️ Claude レビューエラー: {e}")
            # エラー時は通過させる（False positiveを防ぐ）
            return ValidationResult(is_valid=True)

    def _contains_meta_message(self, text: str) -> bool:
        """メタメッセージを検出"""
        for pattern in self.META_MESSAGE_PATTERNS:
            if re.search(pattern, text, re.IGNORECASE):
                return True
        return False

    def _contains_lawsuit_content(self, text: str, title: str = "") -> bool:
        """訴訟関連コンテンツを検出"""
        combined = f"{title} {text}"
        for pattern in self.LAWSUIT_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                return True
        return False

    def _contains_political_content(self, text: str, title: str = "") -> bool:
        """政治的コンテンツを検出"""
        combined = f"{title} {text}"
        for pattern in self.POLITICAL_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                return True
        return False

    def _format_rejection_reason(self, issues: List[str]) -> str:
        """却下理由を整形"""
        reasons = {
            "meta_message": "メタメッセージ検出",
            "lawsuit": "訴訟関連コンテンツ",
            "political": "政治的コンテンツ",
            "too_short": "文字数不足"
        }
        return " / ".join([reasons.get(issue, issue) for issue in issues])
