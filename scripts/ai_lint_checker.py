#!/usr/bin/env python3
"""
AI-Lint Checker
AIっぽい表現を検出するチェッカー（プロダクト公式X向け）
"""
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
import yaml
import re


@dataclass
class Detection:
    """検出されたAI表現"""
    rule_id: str
    severity: str  # "critical", "high", "medium", "low"
    pattern: str
    matched_text: str
    position: int
    suggestion: str


@dataclass
class AILintResult:
    """AI-lintチェック結果"""
    score: int  # 総合AIスコア
    detections: List[Detection]
    total_patterns: int
    text_length: int

    @property
    def ai_density(self) -> float:
        """AI表現の密度（検出数/1000文字）"""
        return (self.total_patterns / self.text_length) * 1000 if self.text_length > 0 else 0


class AILintChecker:
    """AI的表現チェッカー（プロダクト公式X向け）"""

    # severityごとのimpactスコア
    IMPACT_SCORES = {
        "critical": 10,
        "high": 7,
        "medium": 4,
        "low": 1
    }

    # まさたん個人スタイル強制ルール（除外対象）
    # プロダクト公式Xではこれらのルールは不要
    EXCLUDED_RULES = {
        "AI-TONE-006",   # 感情語の欠如
        "AI-TONE-009",   # 括弧内補足の欠如
        "AI-SYNTAX-003", # 語尾の均一性
        "AI-SYNTAX-007", # 文長の均一性
        "AI-TONE-008",   # 口語表現の欠如
    }

    def __init__(self, rules_path: str):
        """
        Args:
            rules_path: ai-lint-rules.ymlへのパス
        """
        with open(rules_path, 'r', encoding='utf-8') as f:
            self.rules_config = yaml.safe_load(f)

        self.rules = self._compile_rules()

    def _compile_rules(self) -> List[Dict[str, Any]]:
        """パターンを正規表現にコンパイル（除外ルール適用）"""
        compiled_rules = []

        for rule in self.rules_config.get('rules', []):
            # 除外ルール（まさたん個人スタイル）をスキップ
            if rule['id'] in self.EXCLUDED_RULES:
                continue

            # pattern_typeが"analysis", "structure", "composite"のルールはスキップ
            # （構文解析が必要なため）
            if rule.get('pattern_type') in ['analysis', 'structure', 'composite']:
                continue

            pattern = rule.get('pattern')
            if not pattern:
                continue

            # pattern_excludeがあれば負の先読みを追加
            pattern_exclude = rule.get('pattern_exclude')
            if pattern_exclude:
                pattern = f"(?!{pattern_exclude}){pattern}"

            try:
                compiled_rules.append({
                    'id': rule['id'],
                    'severity': rule['severity'],
                    'pattern': re.compile(pattern),
                    'description': rule['description'],
                    'suggestion': rule['suggestion'],
                    'category': rule['category']
                })
            except re.error as e:
                # 正規表現のコンパイルエラーは警告を出してスキップ
                print(f"Warning: {rule['id']}のパターンコンパイルに失敗: {e}")
                continue

        return compiled_rules

    def check(self, text: str) -> AILintResult:
        """テキストをチェック"""
        detections = []

        for rule in self.rules:
            matches = rule['pattern'].finditer(text)
            for match in matches:
                detections.append(Detection(
                    rule_id=rule['id'],
                    severity=rule['severity'],
                    pattern=rule['pattern'].pattern,
                    matched_text=match.group(),
                    position=match.start(),
                    suggestion=rule['suggestion']
                ))

        # スコア計算
        score = sum(self.IMPACT_SCORES[d.severity] for d in detections)

        return AILintResult(
            score=score,
            detections=sorted(detections, key=lambda x: self.IMPACT_SCORES[x.severity], reverse=True),
            total_patterns=len(detections),
            text_length=len(text)
        )

    def format_result(self, result: AILintResult) -> str:
        """結果を整形して表示"""
        if result.score == 0:
            return "✅ AI的表現は検出されませんでした"

        lines = [
            f"⚠️  AIスコア: {result.score}",
            f"   検出数: {result.total_patterns}件",
            f"   密度: {result.ai_density:.2f}件/1000文字",
            "",
            "【検出されたAI的表現】"
        ]

        for i, detection in enumerate(result.detections[:10], 1):
            lines.append(f"{i}. [{detection.severity.upper()}] {detection.rule_id}")
            lines.append(f"   該当: 「{detection.matched_text}」")
            lines.append(f"   改善案: {detection.suggestion}")
            lines.append("")

        if result.total_patterns > 10:
            lines.append(f"... 他 {result.total_patterns - 10}件")

        return "\n".join(lines)
