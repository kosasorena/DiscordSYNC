"""
Discord <-> Minecraft Bedrock (BDS) 連携ボット
  - discord.py : Discord との接続
  - aiohttp    : Render 上の HTTP サーバー (/health と /api/poll)
アドオン(BDS)が /api/poll を定期的に叩き、MC→Discord のイベント送信と
Discord→MC のメッセージ受信を同時に行います。

必要な環境変数
  DISCORD_TOKEN  : Discord Bot のトークン
  API_KEY        : アドオンと共有する合言葉 (長いランダム文字列)
任意
  PORT                       : Render が自動で設定
  UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN : 設定の永続化 (推奨)
  STATE_FILE                 : Upstash を使わない場合の保存先 (既定 state.json)
  INVITE_URL                 : Bot 招待URLを固定したい場合
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from collections import defaultdict, deque

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

try:  # 発話検知(任意)。入っていなければ「ミュート/スピーカーミュート」のみ
    from discord.ext import voice_recv
except Exception:  # noqa: BLE001
    voice_recv = None

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("dsync")

TOKEN = os.environ["DISCORD_TOKEN"]
API_KEY = os.environ["API_KEY"]
PORT = int(os.environ.get("PORT", "8080"))
STATE_FILE = os.environ.get("STATE_FILE", "state.json")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
INVITE_URL = os.environ.get("INVITE_URL", "")

# ---------------------------------------------------------------- 表示フォーマット
# {} の部分が動的な値です。§a = 緑 / §r = 白に戻す(Minecraftの書式コード)
FMT_MC_DISCORD = "[§aDiscord§r][#{channel}] {nick}(@{username}) ≫ {message}"
FMT_MC_VC = "[§aDiscord§r][VC] {nick}(@{username}) ≫ {message}"
FMT_MC_VC_FROM_MC = "[Minecraft][VC] {gamertag} ≫ {message}"
FMT_MC_ROOM = "[Minecraft][{server}] {gamertag} ≫ {message}"
FMT_SERVER = "[SERVER]{message}"
# Discord側に出す MC のメッセージ (サーバーが1つ / 複数)
FMT_DC_MC = "[Minecraft]{gamertag} ≫ {message}"
FMT_DC_MC_MULTI = "[Minecraft][{server}] {gamertag} ≫ {message}"

CODE_TTL = 600  # 認証コードの有効秒数


# ---------------------------------------------------------------- 永続化
class Store:
    KEY = "dsync_state"

    def __init__(self):
        self.data = {"servers": {}, "channels": [], "rooms": {}, "accounts": {}}
        self.dirty = False
        self.session = None

    async def _upstash(self, cmd):
        async with self.session.post(
            UPSTASH_URL, json=cmd, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"}
        ) as r:
            body = await r.json()
            if "error" in body:
                raise RuntimeError(body["error"])
            return body.get("result")

    async def load(self, session):
        self.session = session
        raw = None
        try:
            if UPSTASH_URL:
                raw = await self._upstash(["GET", self.KEY])
            elif os.path.exists(STATE_FILE):
                with open(STATE_FILE, encoding="utf-8") as f:
                    raw = f.read()
        except Exception:  # noqa: BLE001
            log.exception("状態の読み込みに失敗")
        if raw:
            try:
                self.data.update(json.loads(raw))
            except Exception:  # noqa: BLE001
                log.exception("状態のJSONが壊れています")
        if not UPSTASH_URL:
            log.warning("UPSTASH未設定: Renderの再起動/再デプロイで設定が消えます")

    def mark(self):
        self.dirty = True

    async def save(self):
        raw = json.dumps(self.data, ensure_ascii=False)
        if UPSTASH_URL:
            await self._upstash(["SET", self.KEY, raw])
        else:
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(raw)
            os.replace(tmp, STATE_FILE)

    async def loop(self):
        while True:
            await asyncio.sleep(5)
            if self.dirty:
                self.dirty = False
                try:
                    await self.save()
                except Exception:  # noqa: BLE001
                    self.dirty = True
                    log.exception("状態の保存に失敗")


store = Store()

# ---------------------------------------------------------------- 実行時の状態(再起動で消えてよいもの)
queues = defaultdict(lambda: deque(maxlen=300))  # server_id -> アドオンへ送る待ち行列
online = {}  # server_id -> {小文字のゲーマータグ}
last_seen = {}
pending_channels = {}  # server_id -> {code: {guild, channel, exp}}
pending_accounts = {}  # discord_id(str) -> {gamertag, code, exp, tries}
speaking = defaultdict(set)  # voice channel id -> {member id}
vc_sessions = {}  # guild id -> {sid, channel_id, client}
last_vc_sent = {}  # sid -> (snapshot, time)
hits = defaultdict(deque)
outbox = asyncio.Queue()

_WS = re.compile(r"\s+")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


def clean(text, limit=300):
    """MCの書式コード(§)と改行を除去して長さを制限する"""
    text = (text or "").replace("§", "")
    text = _WS.sub(" ", _CTRL.sub(" ", text)).strip()
    return text[:limit]


def esc(text):
    return discord.utils.escape_markdown(text)


def hit(key, limit, window):
    """window秒の間に limit 回まで。超えたら False"""
    now = time.time()
    q = hits[key]
    while q and q[0] < now - window:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def to_id(text):
    text = str(text).strip()
    return int(text) if text.isdigit() and len(text) <= 20 else None


def srv(sid):
    return store.data["servers"].setdefault(sid, {"name": sid[:6], "room": None, "paused": False})


def push(sid, **item):
    queues[sid].append(item)


def send_discord(channel_id, text):
    outbox.put_nowait((channel_id, text))


def server_channels(sid):
    return [c for c in store.data["channels"] if c["server_id"] == sid]


def is_multi(sid, channel_id):
    """このDiscordチャンネルに複数サーバーが繋がっている / ルームに複数いる → サーバー名を出す"""
    bound = {c["server_id"] for c in store.data["channels"] if c["channel"] == channel_id}
    if len(bound) > 1:
        return True
    return room_size(srv(sid)["room"]) > 1


def room_size(room):
    if not room:
        return 0
    return sum(1 for s in store.data["servers"].values() if s.get("room") == room)


def invite_url():
    if INVITE_URL:
        return INVITE_URL
    perms = discord.Permissions(
        view_channel=True, send_messages=True, read_message_history=True, connect=True
    )
    return discord.utils.oauth_url(bot.user.id, permissions=perms, scopes=("bot", "applications.commands"))


# ---------------------------------------------------------------- VC の状態
def vc_snapshot(sid):
    """{小文字ゲーマータグ: speaking|idle|muted|deafened} 連携済みでVCにいる人だけ"""
    out = {}
    for sess in vc_sessions.values():
        if sess["sid"] != sid:
            continue
        ch = bot.get_channel(sess["channel_id"])
        if ch is None:
            continue
        for m in ch.members:
            if m.bot or m.voice is None:
                continue
            gt = store.data["accounts"].get(str(m.id))
            if not gt:
                continue
            v = m.voice
            if v.self_deaf or v.deaf:
                st = "deafened"
            elif v.self_mute or v.mute:
                st = "muted"
            elif m.id in speaking[ch.id]:
                st = "speaking"
            else:
                st = "idle"
            out[gt.lower()] = st
    return out


# ---------------------------------------------------------------- アドオンから届くイベント
async def handle_event(sid, ev):
    t = ev.get("type")
    if t == "chat":
        await on_mc_chat(sid, clean(ev.get("gamertag"), 32), clean(ev.get("message")))
    elif t == "system":
        msg = clean(ev.get("message"), 200)
        if msg and not srv(sid)["paused"]:
            text = FMT_SERVER.format(message=esc(msg))
            for c in server_channels(sid):
                send_discord(c["channel"], text)
    elif t == "cmd":
        await handle_cmd(sid, ev)


async def on_mc_chat(sid, gamertag, message):
    if not gamertag or not message:
        return
    s = srv(sid)
    if not s["paused"]:
        for c in server_channels(sid):
            fmt = FMT_DC_MC_MULTI if is_multi(sid, c["channel"]) else FMT_DC_MC
            send_discord(
                c["channel"], fmt.format(server=esc(s["name"]), gamertag=esc(gamertag), message=esc(message))
            )
    if s["room"]:  # 同じルームの他サーバーへ
        for osid, other in store.data["servers"].items():
            if osid != sid and other.get("room") == s["room"]:
                push(
                    osid,
                    type="chat",
                    cat="room",
                    text=FMT_MC_ROOM.format(server=s["name"], gamertag=gamertag, message=message),
                )


async def handle_cmd(sid, ev):
    gt = clean(ev.get("gamertag"), 32)
    if not gt:
        return
    op = bool(ev.get("isOp"))
    cmd = ev.get("cmd")
    args = [str(a) for a in (ev.get("args") or [])][:8]

    def reply(text):
        push(sid, type="reply", to=gt.lower(), text="§7[DiscordSync]§r " + text)

    try:
        if cmd == "discord":
            await cmd_discord(sid, gt, op, args, reply)
        elif cmd == "server":
            await cmd_server(sid, gt, op, args, reply)
        elif cmd == "vc":
            await cmd_vc(sid, gt, args, reply)
    except Exception:  # noqa: BLE001
        log.exception("コマンド処理中にエラー")
        reply("§c内部エラーが発生しました。Botのログを確認してください。")


NEED_OP = "§cこのコマンドはOP(管理者)のみ実行できます。"
ADMIN_ACTIONS = {"add", "verify", "stop", "resume", "del", "list", "vc"}


async def cmd_discord(sid, gt, op, args, reply):
    act = args[0] if args else "help"
    a = args[1:]
    s = srv(sid)
    if act in ADMIN_ACTIONS and not op:
        return reply(NEED_OP)

    if act == "add":
        if len(a) != 3 or a[0] != "channel":
            return reply("§e使い方: /discord add channel <guild id> <channel id>")
        return await add_channel(sid, a[1], a[2], reply)

    if act == "verify":
        if not a:
            return reply("§e使い方: /discord verify <コード>")
        if not hit(("verify", sid), 10, CODE_TTL):
            return reply("§c試行回数が多すぎます。しばらくしてからやり直してください。")
        ent = pending_channels.get(sid, {}).get(a[0])
        if not ent or ent["exp"] < time.time():
            return reply("§c認証コードが違うか、期限切れです。/discord add channel をやり直してください。")
        del pending_channels[sid][a[0]]
        entry = {"server_id": sid, "guild": ent["guild"], "channel": ent["channel"]}
        if entry not in store.data["channels"]:
            store.data["channels"].append(entry)
        s["paused"] = False
        store.mark()
        send_discord(ent["channel"], f"✅ Minecraftサーバー「{esc(s['name'])}」との連携が完了しました。チャット同期を開始します。")
        return reply("§a認証できました。チャット同期を開始します。")

    if act in ("stop", "resume"):
        s["paused"] = act == "stop"
        store.mark()
        return reply("§e同期を一時停止しました。再開は /discord resume" if s["paused"] else "§a同期を再開しました。")

    if act == "del":
        if len(a) != 2:
            return reply("§e使い方: /discord del <guild id> <channel id>")
        gid, cid = to_id(a[0]), to_id(a[1])
        before = len(store.data["channels"])
        store.data["channels"] = [
            c for c in store.data["channels"] if not (c["server_id"] == sid and c["guild"] == gid and c["channel"] == cid)
        ]
        store.mark()
        return reply("§a連携を削除しました。" if len(store.data["channels"]) < before else "§c該当する連携が見つかりません。")

    if act == "list":
        chans = server_channels(sid)
        if not chans:
            return reply("§7連携中のチャンネルはありません。")
        lines = []
        for c in chans:
            ch = bot.get_channel(c["channel"])
            lines.append(f"§f- #{ch.name if ch else '?'} §7(guild {c['guild']} / channel {c['channel']})")
        state = "§e一時停止中" if s["paused"] else "§a同期中"
        return reply(f"{state}§r サーバー名: {s['name']}\n" + "\n".join(lines))

    if act == "account":
        return await cmd_account(sid, gt, a, reply)

    if act == "vc":
        return await cmd_vc_admin(sid, a, reply)

    return reply("§e使い方は /discord help を見てください。")


async def add_channel(sid, guild_text, channel_text, reply):
    gid, cid = to_id(guild_text), to_id(channel_text)
    if gid is None or cid is None:
        return reply("§cIDは数字で指定してください。")
    if not hit(("add", sid), 5, 600):
        return reply("§c短時間に実行しすぎです。少し待ってください。")
    guild = bot.get_guild(gid)
    if guild is None:
        return reply(f"§cBotがそのDiscordサーバーに入っていません。次のURLからBotを招待してください:\n§b{invite_url()}")
    ch = guild.get_channel(cid)
    if not isinstance(ch, discord.TextChannel):
        return reply("§cテキストチャンネルが見つかりません。IDを確認するか、BotがそのチャンネルのView権限を持っているか確認してください。")
    p = ch.permissions_for(guild.me)
    missing = [
        label
        for ok, label in (
            (p.view_channel, "チャンネルを見る"),
            (p.send_messages, "メッセージを送信"),
            (p.read_message_history, "メッセージ履歴を読む"),
        )
        if not ok
    ]
    if missing:
        return reply(f"§cBotにこのチャンネルでの権限がありません: {', '.join(missing)}\n§cチャンネルの権限設定でBotに許可するか、Botを招待し直してください。")
    code = f"{secrets.randbelow(10**6):06d}"
    try:
        await ch.send(
            f"🔗 Minecraftサーバー「{esc(srv(sid)['name'])}」からのチャット連携リクエストです。\n"
            f"認証コード: `{code}` (10分有効)\n"
            f"Minecraft内で `/discord verify {code}` を実行してください。心当たりがなければ無視してください。"
        )
    except discord.Forbidden:
        return reply(f"§cBotがこのチャンネルに送信できませんでした(権限不足)。Botを招待し直すか権限を付与してください。\n§b{invite_url()}")
    pending_channels.setdefault(sid, {})[code] = {"guild": gid, "channel": cid, "exp": time.time() + CODE_TTL}
    reply(f"§a#{ch.name} に認証コードを送りました。Discordで確認し /discord verify <コード> を実行してください。")


async def cmd_account(sid, gt, a, reply):
    sub = a[0] if a else ""
    if sub == "add":
        if len(a) != 2:
            return reply("§e使い方: /discord account add <Discordのユーザー名>")
        uname = a[1].lstrip("@").lower()
        if not hit(("acc_gt", gt.lower()), 3, 600):
            return reply("§c短時間に実行しすぎです。10分ほど待ってください。")
        member = None
        for g in bot.guilds:
            member = next((m for m in g.members if m.name.lower() == uname and not m.bot), None)
            if member:
                break
        if member is None:
            return reply("§cそのユーザーが見つかりません。ユーザー名(@の後ろ)を確認し、Botと同じDiscordサーバーに入っているか確認してください。")
        if not hit(("acc_dm", member.id), 3, 600):
            return reply("§cそのユーザーには短時間に送りすぎです。しばらく待ってください。")
        code = f"{secrets.randbelow(10**6):06d}"
        try:
            await member.send(
                f"🔗 Minecraftのゲーマータグ「{esc(gt)}」とあなたのDiscordアカウントの連携リクエストです。\n"
                f"確認コード: `{code}` (10分有効)\n"
                f"Minecraft内で `/discord account verify {code}` を実行してください。\n"
                "心当たりがない場合は無視してください(何も起きません)。"
            )
        except discord.Forbidden:
            return reply("§cDMを送れませんでした。Discordの「サーバーメンバーからのDMを許可」をオンにしてください。")
        pending_accounts[str(member.id)] = {"gamertag": gt, "code": code, "exp": time.time() + CODE_TTL, "tries": 0}
        return reply(f"§a{member.name} にDMで確認コードを送りました。/discord account verify <コード> を実行してください。")

    if sub == "verify":
        if len(a) != 2:
            return reply("§e使い方: /discord account verify <コード>")
        now = time.time()
        for did, ent in list(pending_accounts.items()):
            if ent["exp"] < now:
                del pending_accounts[did]
                continue
            if ent["gamertag"].lower() != gt.lower():
                continue
            ent["tries"] += 1
            if ent["tries"] > 5:
                del pending_accounts[did]
                return reply("§c試行回数が多すぎます。最初からやり直してください。")
            if hmac.compare_digest(ent["code"], a[1]):
                for k in [k for k, v in store.data["accounts"].items() if v.lower() == gt.lower()]:
                    del store.data["accounts"][k]  # 1ゲーマータグ=1アカウント
                store.data["accounts"][did] = gt
                del pending_accounts[did]
                store.mark()
                return reply("§aDiscordアカウントを連携しました。")
        return reply("§cコードが違うか、期限切れです。")

    if sub == "del":
        keys = [k for k, v in store.data["accounts"].items() if v.lower() == gt.lower()]
        for k in keys:
            del store.data["accounts"][k]
        store.mark()
        return reply("§a連携を解除しました。" if keys else "§7連携されていません。")

    return reply("§e使い方: /discord account add <ユーザー名> | verify <コード> | del")


async def cmd_vc_admin(sid, a, reply):
    sub = a[0] if a else ""
    if sub == "join":
        if len(a) != 3:
            return reply("§e使い方: /discord vc join <guild id> <voice channel id>")
        return await vc_join(sid, to_id(a[1]), to_id(a[2]), reply)
    if sub == "leave":
        for gid, sess in list(vc_sessions.items()):
            if sess["sid"] == sid:
                vc_sessions.pop(gid, None)
                try:
                    await sess["client"].disconnect(force=True)
                except Exception:  # noqa: BLE001
                    pass
                return reply("§aVCから退出しました。")
        return reply("§7VCに接続していません。")
    return reply("§e使い方: /discord vc join <guild id> <channel id> | leave")


async def vc_join(sid, gid, cid, reply):
    if gid is None or cid is None:
        return reply("§cIDは数字で指定してください。")
    guild = bot.get_guild(gid)
    if guild is None:
        return reply(f"§cBotがそのDiscordサーバーに入っていません。招待してください:\n§b{invite_url()}")
    ch = guild.get_channel(cid)
    if not isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):
        return reply("§cボイスチャンネルが見つかりません。")
    p = ch.permissions_for(guild.me)
    missing = [l for ok, l in ((p.view_channel, "チャンネルを見る"), (p.connect, "接続"), (p.send_messages, "メッセージを送信")) if not ok]
    if missing:
        return reply(f"§cBotにVCでの権限がありません: {', '.join(missing)}")
    cur = vc_sessions.get(gid)
    if cur and cur["sid"] != sid:
        return reply("§cこのDiscordサーバーでは別のMinecraftサーバーがVCを使用中です。")
    if cur:
        try:
            await cur["client"].disconnect(force=True)
        except Exception:  # noqa: BLE001
            pass
        vc_sessions.pop(gid, None)
    try:
        if voice_recv:
            client = await ch.connect(cls=voice_recv.VoiceRecvClient, self_deaf=False, timeout=30)
            try:
                client.listen(NullSink())  # 発話検知(パケット到着)のために受信だけする。音声は保存しない
            except Exception:  # noqa: BLE001
                log.exception("音声受信の開始に失敗(発話表示なしで続行)")
        else:
            client = await ch.connect(self_deaf=True, timeout=30)
    except Exception as e:  # noqa: BLE001
        log.exception("VC接続失敗")
        return reply(f"§cVCに接続できませんでした: {type(e).__name__}")
    vc_sessions[gid] = {"sid": sid, "channel_id": ch.id, "client": client}
    extra = "" if voice_recv else "\n§7(発話検知ライブラリ未導入のため、話している表示は出ません)"
    reply(f"§a#{ch.name} に参加しました。連携済みの人のゲーマータグ横に状態を表示します。{extra}")


async def cmd_server(sid, gt, op, args, reply):
    if not args or args[0] != "room":
        return reply("§e使い方: /server room join <id> <password> | leave | name <名前>")
    sub = args[1] if len(args) > 1 else ""
    s = srv(sid)
    if sub in ("join", "leave") and not op:
        return reply(NEED_OP)
    if sub == "join":
        if len(args) != 4:
            return reply("§e使い方: /server room join <room id> <password>")
        rid, pw = args[2], args[3]
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", rid) or not (1 <= len(pw) <= 64):
            return reply("§croom idは英数字/_/-の32文字まで、passwordは64文字までです。")
        if not hit(("room", sid), 5, 300):
            return reply("§c試行回数が多すぎます。5分ほど待ってください。")
        rooms = store.data["rooms"]
        if rid in rooms:
            salt = bytes.fromhex(rooms[rid]["salt"])
            h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000).hex()
            if not hmac.compare_digest(h, rooms[rid]["hash"]):
                return reply("§cパスワードが違います。")
            created = False
        else:
            salt = os.urandom(16)
            rooms[rid] = {"salt": salt.hex(), "hash": hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 100_000).hex()}
            created = True
        s["room"] = rid
        store.mark()
        verb = "を作成して参加" if created else "に参加"
        return reply(f"§aルーム「{rid}」{verb}しました。このサーバーの名前: {s['name']} (/server room name で変更)")
    if sub == "leave":
        rid = s["room"]
        s["room"] = None
        if rid and room_size(rid) == 0:
            store.data["rooms"].pop(rid, None)
        store.mark()
        return reply("§aルームから退出しました。" if rid else "§7ルームに参加していません。")
    return reply("§e使い方: /server room join <id> <password> | leave | name <名前>")


async def cmd_vc(sid, gt, args, reply):
    msg = clean(args[0] if args else "", 300)
    if not msg:
        return reply("§e使い方: /vc <メッセージ>")
    sess = next((v for v in vc_sessions.values() if v["sid"] == sid), None)
    if not sess:
        return reply("§cこのサーバーはVCに接続していません。(管理者が /discord vc join を実行します)")
    snap = vc_snapshot(sid)
    if gt.lower() not in snap:
        return reply("§cあなたはVCにいません。/discord account add でDiscordを連携し、同じVCに参加してください。")
    s = srv(sid)
    fmt = FMT_DC_MC_MULTI if room_size(s["room"]) > 1 else FMT_DC_MC
    send_discord(sess["channel_id"], fmt.format(server=esc(s["name"]), gamertag=esc(gt), message=esc(msg)))
    push(sid, type="vc", targets=list(snap), text=FMT_MC_VC_FROM_MC.format(gamertag=gt, message=msg))


# ---------------------------------------------------------------- Discord 側
intents = discord.Intents.default()
intents.message_content = True  # 特権インテント: Developer Portal で有効化
intents.members = True  # 特権インテント: ユーザー名検索・VC参加者の把握に必要
intents.voice_states = True

if voice_recv:

    class NullSink(voice_recv.AudioSink):
        def wants_opus(self):
            return True  # デコードしない(CPU節約)

        def write(self, user, data):
            pass

        def cleanup(self):
            pass


class SyncBot(commands.Bot):
    async def setup_hook(self):
        self.http_session = aiohttp.ClientSession()
        await store.load(self.http_session)
        app = web.Application()
        app.add_routes([web.get("/", health), web.get("/health", health), web.post("/api/poll", api_poll)])
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        await web.TCPSite(self.runner, "0.0.0.0", PORT).start()
        log.info("HTTP server on :%s (voice_recv=%s)", PORT, bool(voice_recv))
        asyncio.create_task(store.loop())
        asyncio.create_task(outbox_worker())
        await self.tree.sync()


bot = SyncBot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)


async def outbox_worker():
    while True:
        cid, text = await outbox.get()
        try:
            ch = bot.get_channel(cid) or await bot.fetch_channel(cid)
            await ch.send(text[:2000], allowed_mentions=discord.AllowedMentions.none())
        except Exception as e:  # noqa: BLE001
            log.warning("Discord送信失敗 ch=%s: %s", cid, e)


@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.guild is None:
        return
    ch = message.channel
    text = clean(message.clean_content)
    if message.attachments:
        text = (text + " " if text else "") + f"[添付ファイル×{len(message.attachments)}]"
    if not text:
        return
    nick = clean(message.author.display_name, 32)
    username = clean(message.author.name, 32)

    if isinstance(ch, (discord.VoiceChannel, discord.StageChannel)):  # VCのチャット
        sess = vc_sessions.get(message.guild.id)
        if sess and sess["channel_id"] == ch.id:
            targets = list(vc_snapshot(sess["sid"]).keys())
            if targets:
                push(
                    sess["sid"],
                    type="vc",
                    targets=targets,
                    text=FMT_MC_VC.format(nick=nick, username=username, message=text),
                )
        return

    for c in store.data["channels"]:
        if c["channel"] != ch.id or srv(c["server_id"])["paused"]:
            continue
        push(
            c["server_id"],
            type="chat",
            cat="discord",
            text=FMT_MC_DISCORD.format(channel=clean(getattr(ch, "name", "?"), 32), nick=nick, username=username, message=text),
        )


@bot.event
async def on_voice_state_update(member, before, after):
    if before.channel is not None and (after.channel is None or after.channel.id != before.channel.id):
        speaking[before.channel.id].discard(member.id)
    if bot.user and member.id == bot.user.id and after.channel is None:
        vc_sessions.pop(member.guild.id, None)


@bot.event
async def on_voice_member_speaking_start(member):  # discord-ext-voice-recv が発火
    sess = vc_sessions.get(member.guild.id)
    if sess:
        speaking[sess["channel_id"]].add(member.id)


@bot.event
async def on_voice_member_speaking_stop(member):
    sess = vc_sessions.get(member.guild.id)
    if sess:
        speaking[sess["channel_id"]].discard(member.id)


@bot.tree.command(name="minecraft", description="連携中のMinecraftサーバーにお知らせを送ります")
@app_commands.describe(message="送信するメッセージ (例: 30秒で鯖落とします)")
@app_commands.guild_only()
@app_commands.default_permissions(manage_guild=True)
async def minecraft_cmd(interaction: discord.Interaction, message: str):
    if not interaction.permissions.manage_guild:
        return await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
    text = clean(message, 200)
    sids = {c["server_id"] for c in store.data["channels"] if c["channel"] == interaction.channel_id}
    if not text or not sids:
        return await interaction.response.send_message(
            "このチャンネルにはMinecraftサーバーが連携されていません。" if not sids else "メッセージが空です。", ephemeral=True
        )
    for sid in sids:
        push(sid, type="chat", cat="server", text=FMT_SERVER.format(message=text))
    await interaction.response.send_message(
        FMT_SERVER.format(message=esc(text)), allowed_mentions=discord.AllowedMentions.none()
    )


# ---------------------------------------------------------------- HTTP
async def health(request):
    return web.Response(text="ok")


async def api_poll(request):
    key = request.headers.get("X-Api-Key", "")
    if not hmac.compare_digest(key.encode(), API_KEY.encode()):
        return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
    if not bot.is_ready():
        return web.json_response({"ok": False, "error": "starting"}, status=503)
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return web.json_response({"ok": False, "error": "bad json"}, status=400)
    sid = re.sub(r"[^A-Za-z0-9_-]", "", str(data.get("serverId", "")))[:40]
    if not sid:
        return web.json_response({"ok": False, "error": "no serverId"}, status=400)

    s = srv(sid)
    name = clean(data.get("serverName"), 24)
    if name and name != s["name"]:
        s["name"] = name
        store.mark()
    online[sid] = {str(p).lower() for p in (data.get("players") or [])[:200]}
    last_seen[sid] = time.time()

    for ev in (data.get("events") or [])[:100]:
        try:
            await handle_event(sid, ev)
        except Exception:  # noqa: BLE001
            log.exception("イベント処理失敗: %s", ev)

    out = []
    q = queues[sid]
    while q and len(out) < 100:
        out.append(q.popleft())

    snap = vc_snapshot(sid)
    prev = last_vc_sent.get(sid)
    now = time.time()
    if prev is None or prev[0] != snap or now - prev[1] > 30:
        out.append({"type": "vcstate", "states": snap})
        last_vc_sent[sid] = (snap, now)

    fast = any(v["sid"] == sid for v in vc_sessions.values())
    return web.json_response({"ok": True, "out": out, "fast": fast})


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
