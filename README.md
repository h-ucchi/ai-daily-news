# AIデイリーレポート自動生成基盤

AI関連情報を毎日自動で収集・整理し、Slackに読みやすいレポートとして投稿するシステムです。

## 特徴

- **X公式API Basic ($200/月) に最適化**
  - 新着のみ取得 (`since_id` 利用)
  - 1日あたり320投稿の上限管理
  - 無駄なAPI消費を防止

- **多様な情報ソース**
  - X (Twitter): アカウント監視 + キーワード検索
  - RSS: AI企業公式ブログ
  - GitHub: リポジトリのリリース情報

- **自動運用**
  - GitHub Actionsで毎日実行
  - 状態管理で新着のみ取得
  - Slackへ自動投稿

## セットアップ

### 1. リポジトリ作成

GitHubで新規リポジトリを作成し、本プロジェクトをプッシュします。

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/YOUR_REPO.git
git push -u origin main
```

### 2. Secrets設定

GitHubリポジトリの Settings > Secrets and variables > Actions で以下を設定:

| Secret名 | 説明 |
|---------|------|
| `X_BEARER_TOKEN` | X API Bearer Token (Basic $200/月プラン) |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |
| `GITHUB_TOKEN` | (自動設定済み) GitHub API用トークン |

#### X API Bearer Tokenの取得方法

1. [X Developer Portal](https://developer.twitter.com/) にアクセス
2. プロジェクトを作成し、Basic ($200/月) プランに加入
3. Bearer Tokenを生成してコピー

#### Slack Webhook URLの取得方法

1. Slack Workspace で Apps > Incoming Webhooks を検索
2. 投稿先チャンネルを選択
3. Webhook URLをコピー

### 3. 設定カスタマイズ

[config.yaml](config.yaml) を編集:

```yaml
# 監視対象アカウントを追加
x:
  accounts:
    - your_account_name

# キーワードを追加
x:
  keywords:
    - "Your Keyword"

# RSSフィードを追加
rss:
  feeds:
    - url: "https://example.com/feed.xml"
      name: "Example Blog"

# GitHubリポジトリを追加
github:
  repositories:
    - "owner/repo"
```

### 4. 初回実行

GitHub Actions タブで `AI Daily Report` ワークフローを手動実行:

1. Actions タブを開く
2. `AI Daily Report` を選択
3. `Run workflow` > `Run workflow` をクリック

### 5. 自動実行確認

翌日の朝5:00（JST）に自動実行されることを確認します。

## ファイル構成

```
.
├── .github/
│   └── workflows/
│       └── ai-daily.yml        # GitHub Actionsワークフロー
├── config.yaml                 # 設定ファイル（アカウント・キーワード等）
├── data/
│   └── state.json              # 状態管理（since_id等を保存）
├── scripts/
│   └── run_daily.py            # メインスクリプト
├── requirements.txt            # Python依存パッケージ
└── README.md                   # このファイル
```

## 動作フロー

```
GitHub Actions (毎日 5:00 JST)
  ↓
run_daily.py 実行
  ↓
データ収集
  ├─ X API (新着のみ)
  ├─ RSS (新着のみ)
  └─ GitHub API (新着リリース)
  ↓
正規化・重複排除・スコアリング
  ↓
Slackに投稿
  ↓
state.json 更新してコミット
```

## Slackレポート構成

以下のセクションで構成されます:

1. **Top Highlights** (最大5件)
   - スコアが最も高い情報
2. **Provider Official / RSS** (最大10件)
   - AI企業公式ブログの更新
3. **GitHub Updates** (最大10件)
   - リポジトリのリリース情報
4. **X (Twitter) Signals** (最大20件)
   - アカウント監視・キーワード検索結果
5. **Stats**
   - 取得数、重複除外数、上限到達の有無

## 上限管理

X API Basic ($200/月) に収まるよう、以下の上限を設定:

- **アカウント監視**: 200投稿/日
- **キーワード検索**: 120投稿/日
- **合計**: 320投稿/日

上限に到達した場合、Slackレポートに警告が表示されます。

## トラブルシューティング

### ワークフローが失敗する

1. Secrets が正しく設定されているか確認
2. Actions タブで詳細ログを確認
3. `state.json` が破損していないか確認

### X APIエラー

- Bearer Tokenが有効か確認
- Basicプランの課金状態を確認
- レート制限に達していないか確認

### Slack投稿されない

- Webhook URLが有効か確認
- チャンネルが存在するか確認

## カスタマイズ

### 実行時刻を変更

[.github/workflows/ai-daily.yml](.github/workflows/ai-daily.yml) の `cron` を編集:

```yaml
schedule:
  - cron: '0 20 * * *'  # UTC時刻で指定（JST-9時間）
```

### 取得上限を変更

[config.yaml](config.yaml) の `limits` を編集:

```yaml
x:
  limits:
    accounts: 200
    search: 120
    total: 320
```

### スコアリングロジックを変更

[config.yaml](config.yaml) の `scoring` を編集:

```yaml
slack:
  scoring:
    like_weight: 1
    retweet_weight: 3
    reply_weight: 2
```

## 今後の拡張案

- [ ] Grokを使った検索要約（X検索API節約）
- [ ] Slackスレッド分割対応
- [ ] LLMによる影響度分類（Breaking / Info等）
- [ ] 複数Slackチャンネル対応
- [ ] Discord / Webhook対応

## ライセンス

MIT License

## 作成者

あなたの名前
