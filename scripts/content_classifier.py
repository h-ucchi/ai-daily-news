"""
コンテンツ分類とスコアリングモジュール
"""
import re
from typing import Dict, List
from dataclasses import dataclass


@dataclass
class ClassificationResult:
    """分類結果"""
    category: str
    confidence: float
    matched_keywords: List[str]


class ContentClassifier:
    """コンテンツ分類とスコアリング"""

    CATEGORY_BONUSES = {
        "PRACTICAL": 700,        # 実用的情報（ツールリリース、API変更、実装例等）
        "TECHNICAL": 400,        # 技術詳細
        "GENERAL": 0,            # 一般発表（既存の優先度ボーナスを維持）
        "MARKETING": -300,       # マーケティング的発表
        "UNKNOWN": 0
    }

    KEYWORD_PATTERNS = {
        # 実用的情報のキーワード（広範囲）
        "tool_release": [
            r"release", r"v\d+\.\d+", r"version", r"リリース",
            r"released", r"available", r"launched", r"ship",
            r"new feature", r"新機能", r"update", r"アップデート"
        ],
        "api_change": [
            r"API", r"endpoint", r"parameter", r"パラメータ",
            r"breaking change", r"deprecated", r"migration",
            r"specification", r"仕様"
        ],
        "implementation": [
            r"実装", r"implementation", r"how to", r"tutorial",
            r"guide", r"設計", r"architecture", r"pattern",
            r"example", r"サンプル", r"デモ", r"コード例",
            r"step-by-step", r"walkthrough"
        ],
        "integration": [
            r"integration", r"統合", r"連携", r"combined",
            r"workflow", r"pipeline", r"orchestration",
            r"together with", r"と組み合わせ"
        ],
        "use_case": [
            r"use case", r"事例", r"case study", r"活用例",
            r"実践", r"実例", r"プロダクション", r"production",
            r"実装例", r"応用", r"best practice"
        ],
        # 技術詳細
        "technical": [
            r"deep dive", r"詳細", r"analysis", r"比較",
            r"benchmark", r"評価", r"検証", r"測定",
            r"performance", r"optimization"
        ],
        # 除外対象（マーケティング）
        "marketing": [
            r"革新的", r"画期的", r"breakthrough", r"revolutionary",
            r"game-changing", r"transformative", r"変革",
            r"次世代", r"未来"
        ]
    }

    def __init__(self, config: Dict):
        self.config = config
        self.claude_client = None  # 必要時に初期化
        self.classification_cache = {}

    def classify_by_keywords(self, title: str, description: str = "") -> ClassificationResult:
        """キーワードベース分類（第1層）"""
        text = f"{title} {description}".lower()
        matched = {
            "tool_release": [],
            "api_change": [],
            "implementation": [],
            "integration": [],
            "use_case": [],
            "technical": [],
            "marketing": []
        }

        for category, patterns in self.KEYWORD_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    matched[category].append(pattern)

        # 判定ロジック：実用的情報を幅広く拾う
        practical_matches = (
            matched["tool_release"] +
            matched["api_change"] +
            matched["implementation"] +
            matched["integration"] +
            matched["use_case"]
        )

        if practical_matches:
            return ClassificationResult("PRACTICAL", 0.9, practical_matches)
        elif matched["technical"]:
            return ClassificationResult("TECHNICAL", 0.8, matched["technical"])
        elif matched["marketing"]:
            return ClassificationResult("MARKETING", 0.7, matched["marketing"])
        else:
            return ClassificationResult("UNKNOWN", 0.0, [])

    def classify_by_claude(self, title: str, description: str = "") -> ClassificationResult:
        """Claude API分類（第2層）- Phase 2で実装予定"""
        # キャッシュチェック
        cache_key = f"{title}:{description[:50]}"
        if cache_key in self.classification_cache:
            return self.classification_cache[cache_key]

        # TODO: Claude API呼び出し実装（Phase 2）
        # 現在はUNKNOWNを返す
        result = ClassificationResult("UNKNOWN", 0.5, ["claude_api_not_implemented"])
        self.classification_cache[cache_key] = result
        return result

    def calculate_final_score(self, base_score: int, category: str, source: str) -> int:
        """最終スコア計算（第3層）"""
        category_bonus = self.CATEGORY_BONUSES.get(category, 0)

        # ソース別重み付け
        source_multipliers = self.config.get("content_filtering", {}).get("source_multipliers", {})
        multiplier = source_multipliers.get(source, 1.0)

        return int((base_score + category_bonus) * multiplier)
