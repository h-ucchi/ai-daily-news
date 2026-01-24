"""
コンテンツ分類とスコアリングモジュール
"""
import re
from typing import Dict, List
from dataclasses import dataclass
from langdetect import detect, LangDetectException


@dataclass
class ClassificationResult:
    """分類結果"""
    category: str
    confidence: float
    matched_keywords: List[str]


class ContentClassifier:
    """コンテンツ分類とスコアリング"""

    CATEGORY_BONUSES = {
        "PRACTICAL": 400,              # 実用的情報（削減: 700 → 400）
        "PRACTICAL_OFFICIAL": 700,     # 公式発表の実用的情報（新規）
        "TECHNICAL": 300,              # 技術詳細（削減: 400 → 300）
        "GENERAL": 0,                  # 一般発表
        "MARKETING": -300,             # マーケティング的発表
        "EXCLUDED": -800,              # 実験的プロジェクト（新規）
        "PERSONAL_USAGE": -500,        # 個人利用報告（新規）
        "LOW_CREDIBILITY": -600,       # 信頼性の低いソース（新規）
        "NON_ENGLISH": -900,           # 非英語コンテンツ（新規）
        "JAPAN_ORIGIN": -700,          # 日本の記事の英語版（新規）
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
        ],
        # 除外対象（実験的プロジェクト）
        "experimental_project": [
            r"experimental", r"experiment", r"PoC",
            r"proof of concept", r"試作", r"prototype",
            r"pet project", r"side project", r"hobby",
            r"learning", r"勉強会", r"ハッカソン",
            r"hackathon", r"just for fun", r"playing with"
        ],
        # 除外対象（個人利用報告）
        "personal_usage": [
            r"使ってみた", r"試してみた", r"やってみた",
            r"I tried", r"I'm using", r"testing it",
            r"my setup", r"my workflow", r"指示出し",
            r"新幹線", r"移動中"
        ],
        # 除外対象（信頼性の低いソース）
        "low_credibility": [
            r"Congrats", r"heard that", r"rumor",
            r"噂", r"らしい", r"みたい", r"っぽい"
        ],
        # 業界洞察
        "industry_insight": [
            r"industry impact", r"business impact",
            r"workflow", r"productivity", r"efficiency",
            r"cost reduction", r"業界への影響",
            r"ビジネスへの影響", r"生産性"
        ]
    }

    def __init__(self, config: Dict):
        self.config = config
        self.claude_client = None  # 必要時に初期化
        self.classification_cache = {}

    def detect_language(self, text: str) -> str:
        """言語を検出"""
        if not text or len(text.strip()) < 30:  # 30文字未満は精度が低いためunknownを返す
            return "unknown"
        try:
            return detect(text)
        except LangDetectException:
            return "unknown"

    def is_english_content(self, title: str, description: str = "") -> bool:
        """英語コンテンツかどうかを判定"""
        config = self.config.get("content_filtering", {}).get("language_filtering", {})

        if not config.get("enabled", True):
            return True

        # タイトルチェック
        if config.get("check_title", True) and title:
            title_lang = self.detect_language(title)
            if title_lang != "en" and title_lang != "unknown":
                return False

        # 本文チェック
        if config.get("check_text", True) and description:
            text_lang = self.detect_language(description)
            if text_lang != "en" and text_lang != "unknown":
                return False

        return True

    def is_japan_origin(self, url: str) -> bool:
        """日本の記事かどうかを判定"""
        config = self.config.get("content_filtering", {}).get("region_filtering", {})

        if not config.get("enabled", True):
            return False

        # ドメイン判定
        for domain in config.get("excluded_domains", []):
            if domain in url:
                return True

        # URLパターン判定
        for pattern in config.get("excluded_url_patterns", []):
            if pattern in url:
                return True

        return False

    def _match_all_patterns(self, text: str, pattern_categories: List[str]) -> bool:
        """
        複数のパターンカテゴリにすべてマッチするかを判定

        Args:
            text: チェック対象のテキスト
            pattern_categories: チェックするパターンカテゴリのリスト

        Returns:
            すべてのカテゴリに少なくとも1つのパターンがマッチした場合True
        """
        for category in pattern_categories:
            patterns = self.KEYWORD_PATTERNS.get(category, [])
            if not patterns:
                continue

            # このカテゴリに少なくとも1つマッチするか
            category_matched = False
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    category_matched = True
                    break

            # このカテゴリにマッチしなかった場合はFalse
            if not category_matched:
                return False

        # すべてのカテゴリにマッチした
        return True

    def classify(self, title: str, description: str = "", url: str = "") -> ClassificationResult:
        """総合的なコンテンツ分類（言語・地域・キーワード）"""

        # ステップ0: 言語チェック（最優先）
        if not self.is_english_content(title, description):
            detected_lang = self.detect_language(title) or self.detect_language(description)
            return ClassificationResult("NON_ENGLISH", 0.99, [f"detected_language:{detected_lang}"])

        # ステップ0.5: 地域チェック
        if url and self.is_japan_origin(url):
            return ClassificationResult("JAPAN_ORIGIN", 0.98, [f"japan_domain:{url}"])

        # ステップ1以降: 既存のキーワードベース分類
        return self.classify_by_keywords(title, description)

    def classify_by_keywords(self, title: str, description: str = "") -> ClassificationResult:
        """キーワードベース分類（第1層）- 厳格化版"""
        text = f"{title} {description}".lower()
        matched = {
            "tool_release": [],
            "api_change": [],
            "implementation": [],
            "integration": [],
            "use_case": [],
            "technical": [],
            "marketing": [],
            "experimental_project": [],
            "personal_usage": [],
            "low_credibility": [],
            "industry_insight": []
        }

        for category, patterns in self.KEYWORD_PATTERNS.items():
            for pattern in patterns:
                if re.search(pattern, text, re.IGNORECASE):
                    matched[category].append(pattern)

        # ステップ1: 除外パターンの判定（最優先）
        if matched["experimental_project"]:
            return ClassificationResult("EXCLUDED", 0.95, matched["experimental_project"])
        if matched["personal_usage"]:
            return ClassificationResult("PERSONAL_USAGE", 0.95, matched["personal_usage"])
        if matched["low_credibility"]:
            return ClassificationResult("LOW_CREDIBILITY", 0.95, matched["low_credibility"])

        # ステップ2: 実用的情報の判定（厳格化）
        practical_matches = (
            matched["tool_release"] +
            matched["api_change"] +
            matched["implementation"] +
            matched["integration"] +
            matched["use_case"]
        )

        if practical_matches:
            # PRACTICAL判定の厳格化設定を確認
            practical_config = self.config.get("content_filtering", {}).get("practical_scoring", {})
            require_multiple = practical_config.get("require_multiple_categories", False)
            minimum_categories = practical_config.get("minimum_category_matches", 2)

            if require_multiple:
                # 複数カテゴリに一致する必要がある
                practical_category_count = sum([
                    1 for matches in [
                        matched["tool_release"],
                        matched["api_change"],
                        matched["implementation"],
                        matched["integration"],
                        matched["use_case"]
                    ] if matches
                ])

                if practical_category_count >= minimum_categories:
                    return ClassificationResult("PRACTICAL", 0.9, practical_matches)
                else:
                    # 単一カテゴリのみの場合はGENERALに降格
                    return ClassificationResult("GENERAL", 0.6, practical_matches)
            else:
                # 従来通り（単一カテゴリでもPRACTICAL）
                return ClassificationResult("PRACTICAL", 0.9, practical_matches)

        # ステップ3: その他のカテゴリ判定
        elif matched["technical"]:
            return ClassificationResult("TECHNICAL", 0.8, matched["technical"])
        elif matched["marketing"]:
            return ClassificationResult("MARKETING", 0.7, matched["marketing"])
        elif matched["industry_insight"]:
            return ClassificationResult("GENERAL", 0.6, matched["industry_insight"])
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

    def calculate_final_score(self, base_score: int, category: str, source: str, is_official: bool = False) -> int:
        """
        最終スコア計算（第3層）

        Args:
            base_score: ベーススコア（エンゲージメントスコア等）
            category: コンテンツカテゴリ
            source: ソースタイプ（rss, github, x_account, x_search）
            is_official: 公式ソースかどうか（RSSやGitHubなど）

        Returns:
            最終スコア
        """
        # カテゴリボーナスを決定
        # 公式ソースのPRACTICALは PRACTICAL_OFFICIAL を適用
        if category == "PRACTICAL" and (is_official or source in ["rss", "github", "must_include"]):
            category_bonus = self.CATEGORY_BONUSES.get("PRACTICAL_OFFICIAL", 700)
        else:
            category_bonus = self.CATEGORY_BONUSES.get(category, 0)

        # ソース別重み付け
        source_multipliers = self.config.get("content_filtering", {}).get("source_multipliers", {})
        multiplier = source_multipliers.get(source, 1.0)

        return int((base_score + category_bonus) * multiplier)
