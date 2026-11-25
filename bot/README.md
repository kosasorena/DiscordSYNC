# Discord Bridge for Minecraft Bedrock

Minecraft Bedrock ⇄ Discord のチャット / VC / 複数ワールド同期アドオン。  
**BDS専用ではありません**（BDSは HTTP、BDS以外は公式の `/connect` WebSocket で動きます）。

| ファイル | 役割 |
|---|---|
| `bot.py` + `requirements.txt` | Discord Bot。Render の Web Service で動かす（discord.py + aiohttp、1プロセス） |
| `DiscordBridge.mcaddon` | アドオン一式（4パック入り。下表） |

## 1. どうやって「Script APIだけでは出来ないこと」を実現しているか

アドオン(Script API)は **BDS以外では外部通信できません**（`@minecraft/server-net` はBDS専用）。そこで経路を2本用意しました。

```
(A) BDS      : アドオン ─ @minecraft/server-net ─ HTTPS POST /api/sync ───────────┐
(B) BDS以外  : ゲーム内 /connect wss://BOT/ws/<SECRET>  (Minecraft公式のWebSocket) ┴─> bot.py ─> Discord
                 ├ Bot→アドオン : `scriptevent dcb:rx <json>` を送りつける
                 ├ アドオン→Bot : Botが `dcb:poll` というカスタムコマンドを叩き、その戻り値(commandResponse)で受け取る
                 └ チャット     : PlayerMessage イベントを購読して拾う (β APIのchatSend不要)
```
- Script APIのカスタムコマンドの「戻り値メッセージ」を **WebSocketの返信で読む** のが肝です（アドオンから外へ出す唯一の隙間）。
- (B) でアドオンが無反応でも、Bot単体で **チャット双方向 + `!discord ...` チャットコマンド + tellraw** にフォールバックします。
- 設定の正は **アドオン側**（ワールドのDynamic Property）。Botは再起動しても、アドオンが再送する設定を **HMACレシート** で検証して復元します（Render無料枠のディスク消去でも再認証不要）。

## 2. パックの使い分け（.mcaddon に4つ入っています）

| パック | 有効にする場所 | 内容 |
|---|---|---|
| `Discord Bridge [Core] ★必須` | **全環境** | コマンド・GUI・VC表示・Botとの通信 |
| `Discord Bridge [RP]` | **全環境** | VCアイコン（フォント/パーティクル）と**テクスチャ設定画面** |
| `Discord Bridge [Net] BDS専用` | BDSのみ | HTTPでBotと通信 |
| `Discord Bridge [ChatHook] BDS専用(β)` | BDSのみ | MCチャットを拾う（`chatSend`はβAPI） |

BDS以外のワールドでは **Core と RP の2つだけ** ONにしてください（Net/ChatHookはBDS以外では読み込めません）。

## 3. Discord Bot の準備

1. [Developer Portal](https://discord.com/developers/applications) でアプリ作成 → **Bot** タブでトークンを発行。
2. 同じ画面の **Privileged Gateway Intents** で **Message Content Intent** と **Server Members Intent** をON。
3. 招待は `/discord add channel` で未参加だとゲーム内にURLが出ます（`bot` + `applications.commands`、権限: チャンネルを見る/メッセージ送信/履歴を読む/接続）。
4. IDの取得: Discordの設定 → 詳細設定 → **開発者モード**ON → サーバー/チャンネルを右クリック → **IDをコピー**。

## 4. Render へデプロイ

1. `bot.py` と `requirements.txt` をGitHubのリポジトリに置く（ルート直下）。
2. Render → New → **Web Service** → リポジトリを選択。
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `python bot.py`
3. Environment に設定:

| 変数 | 必須 | 内容 |
|---|---|---|
| `DISCORD_TOKEN` | ✔ | Botトークン |
| `BRIDGE_SECRET` | ✔ | 長いランダム文字列（HTTP認証・`/connect`のURL・レシート署名に使用。**途中で変えるとレシートが無効になり再認証が必要**） |
| `PYTHON_VERSION` | 任意 | 例 `3.12.3`（動作確認した版） |
| `ALLOWED_GUILDS` | 任意 | 許可するギルドID（カンマ区切り） |
| `RELAY_BOTS` | 任意 | `1` で他Botの発言もMCへ |
| `VC_SELF_DEAF` | 任意 | `1` でBotがVCで自分をスピーカーミュート |
| `DATA_DIR` | 任意 | アドオン無し(`!`コマンド)モードの保存先。Renderの有料Diskを付けたときだけ意味あり |

デプロイ後、`https://<サービス名>.onrender.com/healthz` が `ok` を返せばOK。無料枠は無通信15分でスリープしますが、`RENDER_EXTERNAL_URL` へ自己pingして起こし続けます（`KEEPALIVE=0`で停止）。

## 5. Minecraft側のセットアップ

### 5-A. BDS（HTTP経路）
1. `DiscordBridge.mcaddon` を展開し、`DiscordBridge_RP` を `resource_packs/`、他3つを `behavior_packs/` へ。
2. ワールドの `world_behavior_packs.json` / `world_resource_packs.json` に追加（バージョンは `[1,0,0]`）:

| パック | pack_id |
|---|---|
| Core BP | `5a3c9e17-2b64-4f80-a1d7-e60b8c4f2193` |
| Net BP | `e42b7a90-6c13-4d8f-95a2-0b8e3f1d7c56` |
| ChatHook BP | `9f06c3b5-d7a1-4e28-8c94-3a5b1e7f0d62` |
| RP | `b7e1c0a4-3f52-4d6e-9a18-5c2d7f0e4b31` |

3. **Beta APIs** 実験をワールドでON（`server-net`がβのため）。
4. `config/default/permissions.json` の `allowed_modules` に `"@minecraft/server-net"` を追加。必要なら許可リストも:
```json
"module_permissions": { "@minecraft/server-net": {
  "allowed_uris": ["https://YOUR-SERVICE.onrender.com/"], "force_https": true, "max_concurrent_requests": 1 } }
```
5. サーバー起動後、OPが `/discord setup https://YOUR-SERVICE.onrender.com <BRIDGE_SECRET>`（BDSコンソールからも可）。`/discord status` で「HTTP(BDS)」になればOK。
6. ChatHookは `@minecraft/server` のβ版番号(`2.12.0-beta`)を使います。**BDSが更新されてログに「version not found」と出たら**、ログに出る受理可能なβ版番号へ `DiscordBridge_ChatHook_BDS_BP/manifest.json` の `version` を1か所書き換えてください（他の機能はこの影響を受けません）。

### 5-B. BDS以外（自分のPC/スマホで開くワールド・LAN）
1. `.mcaddon` をダブルクリックでインポート → ワールドの「アドオン」で **Core と RP のみ** ON。チートを有効に。
2. ゲーム内で `/connect wss://YOUR-SERVICE.onrender.com/ws/<BRIDGE_SECRET>`  
   （名前を付けるなら末尾に `/ws/<SECRET>/<名前>`。つながらない時は 設定 → 一般 の「暗号化されたWebSocketを要求」をOFFにして再試行）
3. `/discord status` が「WebSocket(/connect)」ならOK。ワールドに入り直すたび、再度 `/connect` が必要です（ゲーム側の仕様）。
4. Realms・コンソール・他人のサーバーでは `/connect` が使えず対応できません。

## 6. 使い方

### チャット同期
```
/discord add channel <ギルドID> <チャンネルID>   → そのチャンネルに認証コードを送信（OP）
/discord verify <コード>                         → 認証して同期開始（OP）
/discord stop [チャンネルID]  /  /discord start   → 一時停止 / 再開（OP）
/discord del <ギルドID> <チャンネルID>           → 削除（OP）
```
- Botがサーバーに居ない → 招待URLを表示。チャンネル権限が無い → **足りない権限名**を表示。

| 方向 | 表示 |
|---|---|
| Discord → MC | `[Discord][#MinecraftChat] Panasonic(@_kosasorena) ≫ いやアホすぎるだろw`（Discordは緑） |
| MC → Discord | `[Minecraft]kosasorena ≫ なにしとるんw`（同じチャンネルに複数ワールドがあると `[Minecraft][s1] kosasorena ≫ …`） |
| サーバーメッセージ | `[SERVER]kosasorenaがゲームに参加しました`（参加/退出/死亡） |
| `/minecraft message:30秒で鯖落とします`（Discord、サーバー管理権限） | DiscordもMCも `[SERVER]30秒で鯖落とします` |

メンション(@everyone等)は無効化、`§`色コードは除去、返信・画像・スタンプは `↪名前` `[画像]` `[スタンプ:名]` で表示します。

### VC
```
/discord vc join <ギルドID> <VCのID>   /discord vc leave    （OP）
/vc <メッセージ>                        VCのチャットだけに送る（長い文は "…" で囲む。引用符なしは8語まで）
```
- BotがVCに入り、**同じVCにいる人のゲーマータグの横に状態**を表示: 待機(灰) / 話し中(緑) / ミュート(赤斜線) / スピーカーミュート(ヘッドホン斜線)。
- 名前の対応: Discordの表示名/ユーザー名がゲーマータグと一致すれば自動。違えば `/discord link` → Discordで `/mclink code:XXXXX`。
- VCのテキストチャットは **そのVCにいるMCプレイヤーにだけ** tellraw相当で届き、表示は `[Discord][VC] nick(@user) ≫ msg`。

### 複数ワールド（ルーム）
```
/server room join <ルームID> <パスワード>     /server room name s1     /server room leave | info
```
同じルームのワールドには `[Minecraft][s1] gamertag ≫ message` が出ます。

### GUI（インベントリ風メニュー）
`/discord menu`、または `/discord item` で受け取る **コンパス（右クリックで開く）**。
- **個人設定**: Discordチャットを表示 / 自分のチャットをDiscordへ送る / VCチャット / [SERVER] / ルーム / ネームタグのVC表示 / 受信音 / アクションバーにVC一覧
- **OP用**: 同期のON/OFF・ネームタグ表示方式(アイコン/文字/なし)・チャンネル管理・ルーム/サーバー名・BDS接続設定

### テクスチャ設定画面
リソースパック一覧の **Discord Bridge [RP]** の設定（歯車）で、**自分の画面だけ**: 頭上VCマークの表示ON/OFF、ミュート状態も出すか、大きさ、高さ。

## 7. 正直な検証状況

**実際に確認したこと**（自動テスト）: Bot本体（書式・権限判定・ルーム・VC状態・`/minecraft`）、`/connect`経路のプロトコル全体（偽ゲームクライアント）、アドオンJS（Script APIモック。コマンド/GUI/配信フィルタ/VC表示/再起動耐性）、**本物のBotとアドオンJSをHTTPでつないだ通しテスト**（認証→コード→verify→双方向チャット→VC→ルーム→Bot再起動後の復元）、discord.py 2.7.1の実物でVCの話し中フックが繋がること。

**実機のMinecraft/Discordでは未確認**（環境が無いため。ここが動かなければ報告してください）:
- `/connect wss://…` を実機が受け付けるか、`dcb:poll` の戻り値がWebSocketの返信に載るか（載らなければBot単体の `!discord …` モードに自動で落ちます）
- RPのパーティクル/テクスチャ設定の見た目、フォント絵文字の表示
- 実際のDiscord通話での「話し中」検知（DAVE下）
- ChatHookのβ版番号がお使いのBDSと一致するか

**仕様上の制限**: `/vc` は引用符なしだと8語まで（カスタムコマンドの引数上限）／本物のチェストGUI（JSON UI改造）は他UIを壊すリスクがあるため作らず、フォームGUI＋メニューアイテムにしています／チャット取得(BDS)はβAPI依存。

## 8. 困ったとき
- `/discord` が出ない → CoreがON/ゲームが最新(1.26系)か、コンテンツログを確認。
- 「Botと未接続」 → BDS: `/discord setup` の再実行と permissions.json、BDS以外: `/connect` のやり直し。
- 認証コードが来ない → Botの権限（チャンネルを見る/メッセージ送信/履歴）。
- ネームタグが□表示 → RPがONか確認、OPメニューで表示方式を `text` に。
- `/discord debug`（OP）でアドオン側ログを出せます。
