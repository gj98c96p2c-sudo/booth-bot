# BOOTH VRChat 新作通知 Bot（非公式）

BOOTHのVRChat向け新作アイテムを自動で検知し、Discordチャンネルに通知するBotです。

> ⚠️ このBotはBOOTH（pixiv）の公式Botではありません。個人が開発した非公式ツールです。

---

## 🚀 いますぐ使う（招待するだけ）

Botをホスティングする必要はありません。下のリンクからサーバーに招待してください。

**[👉 Botをサーバーに招待する](https://discord.com/oauth2/authorize?client_id=1531860064061882368&permissions=2147503104&scope=bot%20applications.commands)**

招待後は3ステップだけ：

| ステップ | コマンド | 内容 |
|---|---|---|
| 1️⃣ 必須 | `/set-channel` | 通知チャンネルとジャンルを設定 |
| 2️⃣ 任意 | `/filter` | 通知したいアバターを **BOOTHのURL（末尾の7桁ID）** で登録 |
| 3️⃣ 任意 | `/set-nsfw allow` | R-18商品も通知する（初期は非表示） |

`/help` でいつでも使い方を確認できます。

### 必要な権限

招待リンクには以下の権限が含まれています。

- チャンネルを見る
- メッセージを送信
- 埋め込みリンクを送信
- スラッシュコマンドを使う

---

## ✨ できること

- 5分に1回、BOOTHの「VRChat」検索結果を自動巡回
- 公開から10分以内の新作だけを通知（速報性重視）
- 衣装・髪・小物・ギミック・無料アイテムをカテゴリ別に通知
- チャンネルごとに通知カテゴリを設定
- アバターは **BOOTHの商品ID（URL末尾の7桁）** でフィルター（通知にはアバター名を表示）
- ショップ名でもフィルター（部分一致・ひらがなカタカナ・全角半角を同一視）
- R-18商品の表示/非表示切り替え
- 通知メッセージに「BOOTHで見る」「ショップを見る」ボタン
- 巡回失敗時の自己申告（管理者通知＋ユーザー告知＋管理者へのDMフォールバック）
- Discord DM経由でBoothBOT Managerと会話（DMブリッジ）

---

## 📌 コマンド一覧

| コマンド | 説明 | 必要権限 |
|---|---|---|
| `/set-channel` | 通知チャンネルとジャンルを設定 | チャンネル管理 |
| `/remove-channel` | 通知設定を解除 | チャンネル管理 |
| `/filter` | フィルターの一覧・追加・削除（ボタン操作） | - |
| `/set-nsfw allow\|deny` | R-18商品の表示/非表示 | - |
| `/status` | 現在の設定を確認 | - |
| `/help` | 使い方を表示 | - |
| `/stats` | Botの稼働状況 | 管理者のみ |
| `/reply` | Manager用：DM返信 | 管理者のみ |

### フィルターの動作

フィルターは「通知を**絞り込む**」機能です。BOOTHから探す範囲を広げるものではありません。

- **フィルター未登録** → 選んだジャンルの新作は**全部**通知される
- **フィルター登録済み** → 登録したアバター / ショップに一致する商品**だけ**通知される

#### アバターフィルター（BOOTH商品ID基準）

アバターは名前ではなく **BOOTHの商品ID（URLの末尾7桁）** で登録します。
同名アバターや表記ゆれによる取りこぼし・誤爆を避けるためです。

```
/filter → 「👤 アバター（URL / ID）」 → https://booth.pm/ja/items/6106863 を貼る
→ ✅ アバター「しなの」を登録（ID: 6106863）
```

判定ロジック（上から優先）:

1. **商品ID一致** — 新作の商品説明に、登録したアバターのBOOTH URLが貼られている（＝出品者が「対応アバター」として明記している）
2. **名前一致（保険）** — URLが無くても、登録IDから取得したアバター名（および `-kaguya-` のような英字別名）が新作のタグ／商品名に含まれている

通知には常に**アバター名**が表示されます（`🏷️ アバター: しなの`）。
2 で拾ったものは `（名前一致）` が付きます。

制限:

- 1チャンネルあたり最大 **50件**（アバター / ショップそれぞれ）
- 商品IDは 5〜10桁の数字（URLをそのまま貼ってもOK）
- ショップ名は **2〜50文字**（正規化後）、URL・メンション・記号のみは登録不可

---

## 🛠️ 自分でホスティングする

### 1. 必要なもの

- Python 3.11以上
- Discord Bot Token
- （推奨）Render アカウント
- （推奨）Turso アカウント（DB永続化用）

### 2. Discord Developer Portal の設定

[Discord Developer Portal](https://discord.com/developers/applications) で以下をONにしてください。

- `MESSAGE CONTENT INTENT`
- `SERVER MEMBERS INTENT`

これがOFFだと `PrivilegedIntentsRequired` エラーで起動しません。

### 3. 環境変数

| 変数名 | 必須 | 説明 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ✅ | Discord Bot のトークン |
| `ADMIN_CHANNEL_ID` | 推奨 | 障害アラートを受け取るテキストチャンネルID |
| `MANAGER_USER_ID` | 推奨 | 管理者のDiscordユーザーID（`/stats` `/reply` 用、アラートDMフォールバック先） |
| `BOT_TEST_CHANNEL_ID` | 任意 | テスト通知の送信先チャンネルID |
| `TURSO_DATABASE_URL` | 推奨 | Turso DB の URL（未設定だとRenderでデータが揮発します） |
| `TURSO_AUTH_TOKEN` | 推奨 | Turso の認証トークン |

`.env` ファイルを作成し、以下を記入してください。

```bash
DISCORD_BOT_TOKEN=your_discord_bot_token
ADMIN_CHANNEL_ID=1234567890123456789
MANAGER_USER_ID=1234567890123456789
TURSO_DATABASE_URL=libsql://your-db.turso.io
TURSO_AUTH_TOKEN=your_turso_auth_token
```

チャンネルIDの取得方法: Discord設定 → 詳細設定 → 開発者モードをON → チャンネルを右クリック →「チャンネルIDをコピー」

### 4. 依存ライブラリ

```bash
pip install -r requirements.txt
```

### 5. ローカルで動かす

```bash
python main.py
```

### 6. Renderで動かす

1. GitHubリポジトリをRenderに連携
2. 「Web Service」として作成
3. Start Command: `python main.py`
4. 環境変数をRenderダッシュボードの Environment タブで設定
5. デプロイ後、`Manual Deploy → Deploy latest commit` で反映

> ⚠️ Renderの無料枠はファイルシステムが揮発します。`TURSO_*` を設定しないと再起動のたびに設定が消えます。

---

## 🧪 開発

```bash
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

### ファイル構成

| ファイル | 役割 |
|---|---|
| `main.py` | Bot起動、スラッシュコマンド、通知処理 |
| `booth.py` | BOOTH巡回・パース・カテゴリ判定 |
| `database.py` | DB接続（Turso/SQLite）、マイグレーション、クエリ |
| `utils.py` | 名前の正規化、入力バリデーション |
| `logging_utils.py` | 構造化ログ |
| `dm_bridge.py` | DMブリッジ |
| `tests/` | ユニットテスト |

---

## 🩺 トラブルシューティング

### Botがオフラインになる

- Renderのログで `PrivilegedIntentsRequired` が出ていないか確認
- 出ていたら Developer Portal で `MESSAGE CONTENT INTENT` / `SERVER MEMBERS INTENT` をON
- `DISCORD_BOT_TOKEN` が正しいか確認

### 通知が来ない

- `/status` で設定を確認
- `/set-channel` でカテゴリが選択されているか確認
- フィルターを登録していると、一致する商品だけしか通知されません
- BOOTH側のHTML構造変更の可能性（ログの `parse_search_page` 警告を確認）

### 管理者アラートが届かない

- `ADMIN_CHANNEL_ID` が正しいテキストチャンネルIDか確認
- Botがそのチャンネルに書き込み権限を持っているか確認
- ログに `Unknown Channel` が出ていたらIDが間違っています
- チャンネル送信に失敗した場合は `MANAGER_USER_ID` へDMでフォールバックします

### 設定が再起動のたびに消える

- `TURSO_DATABASE_URL` / `TURSO_AUTH_TOKEN` を設定してください
- 起動ログで `🗄️ データベース: Turso` になっているか確認

### 新着なのに通知されない商品がある

公開から10分以内（`LOOKBACK_MINUTES`）の商品のみを新作と判定しています。
それより前に公開された商品は対象外です。

---

## 📄 ライセンス

MIT License（自由に改変・配布OK）
