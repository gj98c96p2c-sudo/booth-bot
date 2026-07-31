# BOOTH VRChat 新作通知 Bot

BOOTHのVRChat向け新作アイテムを自動で検知し、Discordチャンネルに通知するBotです。

## できること

- 5分に1回、BOOTHの「VRChat」検索結果を自動巡回
- 衣装・髪・小物・ギミック・無料アイテムをカテゴリ別に通知
- チャンネルごとに通知カテゴリを設定
- アバター名やショップ名でフィルター
- R-18商品の表示/非表示切り替え
- 巡回失敗時の自己申告（管理者通知＋ユーザー告知）
- Discord DM経由でBoothBOT Managerと会話（DMブリッジ）

## セットアップ手順

### 1. 必要なもの

- Python 3.11以上
- Discord Bot Token
- （推奨）Render アカウント
- （推奨）Turso アカウント（DB用）

### 2. 環境変数の設定

`.env` ファイルを作成し、以下を記入してください。

```bash
DISCORD_BOT_TOKEN=your_discord_bot_token
ADMIN_CHANNEL_ID=1234567890123456789
BOT_TEST_CHANNEL_ID=1234567890123456789
MANAGER_USER_ID=1234567890123456789
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your_turso_auth_token
```

各項目の説明は `.env.example` を参照してください。

### 3. 依存ライブラリのインストール

```bash
pip install -r requirements.txt
```

### 4. ローカルで動かす

```bash
python main.py
```

### 5. Renderで動かす

1. GitHubリポジトリをRenderに連携
2. 「Web Service」として作成
3. 環境変数をRenderダッシュボードで設定
4. デプロイされると自動で起動

## コマンド一覧

| コマンド | 説明 | 必要権限 |
|---|---|---|
| `/set-channel` | 通知チャンネルとジャンルを設定 | チャンネル管理 |
| `/remove-channel` | 通知設定を解除 | チャンネル管理 |
| `/filter` | アバター名/ショップ名フィルターの管理 | - |
| `/set-nsfw allow/deny` | R-18商品の表示/非表示 | - |
| `/status` | 現在の設定を確認 | - |
| `/test-notify` | テスト通知を送信 | チャンネル管理 |
| `/reply` | Manager用：DM返信 | MANAGER_USER_ID一致 |

## DBについて

Tursoが設定されていればTurso、なければローカルの `bot_data.db` を使用します。

## 注意点

- `/set-nsfw` は `/set-channel` の後に実行することを推奨します。
- `/test-notify` は既存のチャンネル設定を上書きしません。
- DMブリッジを使う場合は `MANAGER_USER_ID` を必ず設定してください。

## トラブルシューティング

### Botが応答しない

- Renderのログを確認
- `DISCORD_BOT_TOKEN` が正しく設定されているか確認
- Botがサーバーに招待されているか確認

### 通知が来ない

- `/status` で設定を確認
- `/set-channel` でカテゴリが選択されているか確認
- BOOTH側のHTML構造変更の可能性（ログを確認）

## ライセンス

MIT License（自由に改変・配布OK）
