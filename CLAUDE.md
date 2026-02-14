# AIリサーチプロジェクト - Claude Code運用ガイド

## 🎯 投稿案生成の2つのフロー

このプロジェクトには「手動」と「自動」の2つの投稿案生成フローがあります。

| フロー | トリガー | 実行者 | スクリプト | 出力先 |
|--------|----------|--------|-----------|--------|
| 手動 | ユーザーがClaude CodeでURL依頼 | Claude Code | generate_post_manual.py | チャット画面 |
| 自動 | GitHub Actionsスケジュール | GitHub Actions | run_daily.py, run_hourly.py | Slack |

---

## 📱 手動フロー（ユーザー依頼時）

### トリガー
- ユーザーがClaude CodeでURLを提示して投稿案の作成を依頼
- 例: 「https://example.com/article の投稿案作って」

### ⚙️ 環境変数の設定（必須）

このスクリプトを実行するには、`.claude/settings.local.json`に以下の環境変数を設定する必要があります：

```json
{
  "env": {
    "X_BEARER_TOKEN": "...",           // X (Twitter) API Bearer Token
    "ANTHROPIC_API_KEY": "sk-ant-..."  // Anthropic API Key（必須）
  }
}
```

**ANTHROPIC_API_KEY取得方法**:
1. [Anthropic Console](https://console.anthropic.com/)にアクセス
2. 「API Keys」からAPI Keyを作成
3. `.claude/settings.local.json`に追加

---

### 重要：Claude Codeは必ず以下を実行

```bash
python3 scripts/generate_post_manual.py <URL>
```

### ❌ 絶対禁止：スクリプト失敗時の代替生成

**スクリプトが失敗した場合、決して直接投稿案を生成してはいけません。**

以下のような対応は**絶対禁止**です：
- ❌ 「代わりに、リポジトリの内容を取得して、投稿案をここで直接生成します」
- ❌ 「スクリプトが使えないので、私が直接投稿案を作成します」
- ❌ Claude自身が投稿案を考えること

**理由**:
- スクリプトを使わないと、以下の厳格なルールが適用されない：
  - 冒頭1文: 30-50文字（短く、インパクト重視）
  - 全体: 600-800文字に厳格に制限
  - 箇条書き: 「・」（全角中黒）のみ使用（半角中点「·」や「-」は不可）
  - AI-lint: 最大3回のリトライで「AI的表現」を自動除去

---

### ✅ スクリプト失敗時の正しい対処

スクリプトがエラーで失敗した場合は、以下の手順で対応してください：

1. **エラーメッセージをユーザーに報告**
   ```
   ❌ エラー: スクリプトの実行に失敗しました

   エラー内容: [エラーメッセージ]
   ```

2. **環境変数の確認を依頼**
   ```
   環境変数 ANTHROPIC_API_KEY が設定されていない可能性があります。

   以下のコマンドで確認してください：
   cat .claude/settings.local.json | grep ANTHROPIC_API_KEY
   ```

3. **設定方法を案内**
   ```
   .claude/settings.local.json に以下を追加してください：

   {
     "env": {
       "ANTHROPIC_API_KEY": "sk-ant-..."
     }
   }

   API Keyは Anthropic Console (https://console.anthropic.com/) から取得できます。
   ```

4. **スクリプトの再実行を促す**
   ```
   環境変数を設定後、以下のコマンドを再実行してください：
   python3 scripts/generate_post_manual.py <URL>
   ```

**重要**: 代替手段として直接生成することは、品質保証ができないため絶対に行わないでください。

### 処理の流れ
1. URLから記事を取得（requests + BeautifulSoup）
2. Claude APIで投稿案を生成（企業発表形式）
3. ターミナルに表示される投稿案を**チャットに貼り付ける**

### 生成される投稿案の特徴
- 企業発表形式（600-800文字）
- セクション番号と箇条書きで構造化
- 💡 業界インパクト（主要ポイント）を含む
- 具体的で実践的な内容
- 抽象的表現を避ける

### 実行例
```bash
# MCP Apps記事
python3 scripts/generate_post_manual.py https://blog.modelcontextprotocol.io/posts/2026-01-26-mcp-apps/

# Claude Code ドキュメント
python3 scripts/generate_post_manual.py https://code.claude.com/docs/en/keybindings

# OpenAI Blog記事（重要）
python3 scripts/generate_post_manual.py https://openai.com/index/harness-engineering/
```

### 💡 OpenAI Blogの特別な扱い

OpenAIのウェブサイトは**Cloudflare Bot保護**が厳格で、自動収集（run_daily.py/run_hourly.py）では取得失敗する場合があります。

**重要なOpenAI Blog記事が公開されたら、手動トリガーを活用してください：**

```bash
python3 scripts/generate_post_manual.py <OpenAI記事URL>
```

**例**:
- GPTモデル更新: `https://openai.com/index/gpt-5-updates/`
- 新機能発表: `https://openai.com/index/new-features/`
- エンジニアリングブログ: `https://openai.com/index/harness-engineering/`

生成された投稿案はチャット画面に表示されます。

### 注意事項
- **ファイル保存はしない**。チャット画面に表示するだけ。
- ユーザーが「投稿案作って」「サマリ作って」などと依頼した場合も同様に実行。
- スクリプトの実行結果（`============================================================` で囲まれた部分）をそのままチャットに表示。

### スクリプト仕様
- **ファイル**: `scripts/generate_post_manual.py`
- **依存**: requests, beautifulsoup4, anthropic
- **環境変数**: `ANTHROPIC_API_KEY`

---

## 🤖 自動フロー（GitHub Actions）

このセクションは**Claude Codeの実行対象外**です。GitHub Actionsが自動実行します。

### 1. デイリーレポート（run_daily.py）

**トリガー**: GitHub Actionsで1日1回自動実行

**処理内容**:
- X投稿、RSS、GitHubから自動収集
- Claude APIで投稿案を生成
- **Slackに投稿**（チャットには表示しない）

**出力先**: Slack Webhook

**実行者**: GitHub Actions（Claude Codeは関与しない）

### 2. セミデイリーレポート（run_hourly.py）

**トリガー**: GitHub Actionsで8時・15時に実行

**処理内容**:
- Webページの変更を監視
- 変更があれば下書き保存（drafts.json）
- 必見の更新はSlack通知

**出力先**: drafts.json + Slack（必見の更新のみ）

**実行者**: GitHub Actions（Claude Codeは関与しない）

---

## 🔍 フロー判定の基準

### Claude Codeが「手動フロー」を実行すべき場合
✅ ユーザーがURLを提示
✅ 「投稿案作って」「サマリ作って」と依頼
✅ チャット画面で対話中

### Claude Codeが「自動フロー」に関与しない場合
❌ GitHub Actionsによる自動実行
❌ Slackへの投稿
❌ 定期的なデータ収集

---

## 📋 まとめ

| 項目 | 手動フロー | 自動フロー |
|------|-----------|-----------|
| 実行タイミング | ユーザー依頼時 | GitHub Actions定期実行 |
| Claude Codeの役割 | スクリプト実行＋チャット表示 | **関与しない** |
| スクリプト | generate_post_manual.py | run_daily.py, run_hourly.py |
| 出力先 | チャット画面 | Slack |
| データソース | ユーザー指定URL | X/RSS/GitHub（自動収集） |
