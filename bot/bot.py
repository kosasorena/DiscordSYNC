#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Discord Bridge for Minecraft Bedrock  -  bot.py  (v1.0.0)

Render の Web Service として動かす想定 (discord.py + aiohttp, 単一プロセス)。

  Minecraft ──(A) HTTP  : BDS の @minecraft/server-net が POST /api/sync        ──┐
  Minecraft ──(B) WS    : ゲーム内 /connect wss://<host>/ws/<BRIDGE_SECRET>     ──┼─> この bot ─> Discord
                          (BDS 以外 = ローカル/LAN ワールドでも動く公式プロトコル)   ──┘

環境変数
  DISCORD_TOKEN   (必須) Bot トークン
  BRIDGE_SECRET   (必須) 共有シークレット。HTTP の Bearer / WS の URL / 認証レシート署名に使う
  PORT            Render が自動で入れる (既定 10000)
  DATA_DIR        任意。 /connect フォールバックモードの設定保存先 (既定 ./data)
  ALLOWED_GUILDS  任意。カンマ区切りの guild id。指定するとそのサーバーでしか動かない
  RELAY_BOTS      1 にすると他の Bot の発言も Minecraft に流す (既定 0)
  VC_SELF_DEAF    1 にすると Bot は VC で自分をスピーカーミュートにする (既定 0)
  KEEPALIVE       0 にすると Render 無料枠のスリープ防止 self-ping を止める
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
import discord
from discord import app_commands

VERSION = "1.0.0"
log = logging.getLogger("dcbridge")

TOKEN = os.getenv("DISCORD_TOKEN", "")
SECRET = os.getenv("BRIDGE_SECRET", "")
PORT = int(os.getenv("PORT", "10000"))
DATA_DIR = os.getenv("DATA_DIR", "./data")
ALLOWED_GUILDS = {int(x) for x in re.findall(r"\d+", os.getenv("ALLOWED_GUILDS", ""))}
RELAY_BOTS = os.getenv("RELAY_BOTS", "0") == "1"
VC_SELF_DEAF = os.getenv("VC_SELF_DEAF", "0") == "1"

MAX_MSG = 300
WID_RE = re.compile(r"^[A-Za-z0-9_\-:]{6,64}$")
NO_MENTIONS = discord.AllowedMentions.none()
BOT_PERMS = discord.Permissions(
    view_channel=True, send_messages=True, read_message_history=True, connect=True
)

client: Any = None          # discord.Client (テストでは差し替え可能)
tree: Any = None
worlds: Dict[str, "World"] = {}
vcsessions: Dict[int, "VcSession"] = {}
pending_verify: Dict[Tuple[str, int, int], Dict[str, Any]] = {}
pending_link: Dict[str, Dict[str, Any]] = {}
raw_store: Dict[str, Dict[str, Any]] = {}
_bg: set = set()


# ───────────────────────── 小道具 ─────────────────────────
def spawn(coro) -> asyncio.Task:
    t = asyncio.ensure_future(coro)
    _bg.add(t)
    t.add_done_callback(_bg.discard)
    t.add_done_callback(lambda f: f.cancelled() or f.exception() and log.error("task error: %r", f.exception()))
    return t


def rcpt(wid: str, gid: int, cid: int) -> str:
    """認証レシート (HMAC)。Bot が成功判定した (world, guild, channel) だけが持てる。"""
    return hmac.new(SECRET.encode(), f"{wid}|{gid}|{cid}".encode(), hashlib.sha256).hexdigest()[:24]


def sha(s: str) -> str:
    return hashlib.sha256(("dcb-room:" + s).encode()).hexdigest()


def gen_code(n: int = 6) -> str:
    return "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(n))


def clean_mc(text: Any, limit: int = MAX_MSG) -> str:
    s = str(text or "")
    s = re.sub(r"§[0-9a-fk-orA-FK-OR]", "", s).replace("§", "")  # 色コードは記号ごと除去 (色の注入防止)
    s = "".join(ch for ch in s if ch >= " " or ch == "\n")
    s = re.sub(r"\s*\n\s*", " ⏎ ", s).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def norm(s: Optional[str]) -> str:
    return re.sub(r"[\s_\-]+", "", (s or "")).casefold()


def esc_cmd(js: str) -> str:
    """scriptevent / コマンド行に載せても壊れない JSON にする ('@' はセレクタ展開、'%' は書式展開対策)。"""
    return js.replace("@", "\\u0040").replace("%", "\\u0025")


def jdump(o: Any) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))


def is_voice(ch: Any) -> bool:
    return getattr(ch, "type", None) in (discord.ChannelType.voice, discord.ChannelType.stage_voice)


def invite_url() -> str:
    try:
        return discord.utils.oauth_url(client.user.id, permissions=BOT_PERMS, scopes=("bot", "applications.commands"))
    except Exception:
        return "(Developer Portal > OAuth2 > URL Generator で bot + applications.commands を選んで招待してください)"


# ───────────────────────── フォーマット ─────────────────────────
def fmt_discord(chname: str, nick: str, user: str, text: str) -> str:
    return f"[§aDiscord§r][#{chname}] {nick}(@{user}) ≫ {text}"


def fmt_vc(nick: str, user: str, text: str) -> str:
    return f"[§aDiscord§r][VC] {nick}(@{user}) ≫ {text}"


def fmt_server_mc(text: str, name: Optional[str] = None) -> str:
    return "§e[SERVER]§r" + (f"[{name}]" if name else "") + text


def fmt_room_mc(name: str, player: str, text: str) -> str:
    return f"[Minecraft][{name}] {player} ≫ {text}"


def d_mc(world: "World", player: str, text: str, multi: bool) -> str:
    return f"[Minecraft][{world.display_name}] {player} ≫ {text}" if multi else f"[Minecraft]{player} ≫ {text}"


def d_server(world: "World", text: str, multi: bool) -> str:
    return f"[SERVER][{world.display_name}]{text}" if multi else f"[SERVER]{text}"


def msg_ev(kind: str, text: str, to: Optional[List[str]] = None) -> Dict[str, Any]:
    ev: Dict[str, Any] = {"t": "msg", "k": kind, "text": text}
    if to is not None:
        ev["to"] = list(to)
    return ev


# ───────────────────────── Discord への送信キュー ─────────────────────────
class Sender:
    """チャンネルごとの順序保証付き送信キュー (HTTP 応答をブロックしない)。"""

    def __init__(self) -> None:
        self.queues: Dict[int, asyncio.Queue] = {}

    def send(self, channel: Any, content: str) -> None:
        q = self.queues.get(channel.id)
        if q is None:
            q = self.queues[channel.id] = asyncio.Queue(maxsize=200)
            spawn(self._run(q))
        try:
            q.put_nowait((channel, content[:1990]))
        except asyncio.QueueFull:
            log.warning("send queue full: %s", channel.id)

    async def _run(self, q: asyncio.Queue) -> None:
        while True:
            ch, content = await q.get()
            try:
                await ch.send(content, allowed_mentions=NO_MENTIONS)
            except Exception as e:  # noqa
                log.warning("send failed (%s): %r", getattr(ch, "id", "?"), e)


sender = Sender()


# ───────────────────────── World (= 接続中の Minecraft ワールド) ─────────────────────────
DEFAULT_FLAGS = {"m2d": True, "d2m": True, "srv": True, "room": True}


class RateLimit:
    def __init__(self, n: int = 8, per: float = 6.0) -> None:
        self.n, self.per, self.t = n, per, deque()

    def allow(self) -> bool:
        now = time.monotonic()
        while self.t and now - self.t[0] > self.per:
            self.t.popleft()
        if len(self.t) >= self.n:
            return False
        self.t.append(now)
        return True


class World:
    def __init__(self, wid: str, conn: Any = None, raw: bool = False) -> None:
        self.wid = wid
        self.conn = conn
        self.raw = raw
        self.mode = "raw" if raw else "?"
        self.name = ""
        self.paused = False
        self.channels: Dict[Tuple[int, int], Dict[str, Any]] = {}
        self.room_id: Optional[str] = None
        self.room_hash: Optional[str] = None
        self.room_ok = False
        self.vc: Optional[Tuple[int, int]] = None
        self.links: Dict[str, int] = {}
        self.flags = dict(DEFAULT_FLAGS)
        self.players: set = set()
        self.pset: Dict[str, Dict[str, Any]] = {}
        self.chathook = False
        self.outq: deque = deque()
        self.wake = asyncio.Event()
        self.last_seen = time.time()
        self.rate = RateLimit()
        self.last_vc_json = ""
        self.hello_done = False

    @property
    def display_name(self) -> str:
        return self.name or self.wid[-6:]

    def push(self, ev: Dict[str, Any]) -> None:
        self.outq.append(ev)
        self.wake.set()

    def drain(self, max_bytes: Optional[int] = None, max_events: int = 200) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        size = 2
        while self.outq and len(out) < max_events:
            n = len(jdump(self.outq[0])) + 1
            if max_bytes is not None and out and size + n > max_bytes:
                break
            out.append(self.outq.popleft())
            size += n
        if not self.outq:
            self.wake.clear()
        return out

    def active_channel(self, gid: int, cid: int) -> bool:
        st = self.channels.get((gid, cid))
        return bool(st) and not st.get("paused") and not self.paused

    def can_send(self, player: str) -> bool:
        return bool(self.pset.get(player, {}).get("send", True))

    # ---- 設定の読み込み / 書き出し ----
    def load_cfg(self, cfg: Dict[str, Any], trusted: bool = False) -> List[Tuple[int, int]]:
        """Core (アドオン) が持つ設定を取り込む。チャンネルはレシートが正しいものだけ有効。返り値は削除されたチャンネル。"""
        old = set(self.channels)
        self.name = clean_mc(cfg.get("name", ""), 24)
        self.paused = bool(cfg.get("paused"))
        new: Dict[Tuple[int, int], Dict[str, Any]] = {}
        for ch in cfg.get("channels", []):
            try:
                g, c = int(ch["g"]), int(ch["c"])
            except Exception:
                continue
            if trusted or hmac.compare_digest(str(ch.get("r", "")), rcpt(self.wid, g, c)):
                new[(g, c)] = {"paused": bool(ch.get("p")), "cn": str(ch.get("n", ""))[:60]}
        self.channels = new
        room = cfg.get("room") or {}
        self.room_id = str(room["id"]) if room.get("id") else None
        self.room_hash = sha(str(room.get("pw", ""))) if self.room_id else None
        vc = cfg.get("vc") or {}
        try:
            self.vc = (int(vc["g"]), int(vc["c"])) if vc.get("g") and vc.get("c") else None
        except Exception:
            self.vc = None
        self.links = {}
        for k, v in (cfg.get("links") or {}).items():
            try:
                self.links[str(k)] = int(v)
            except Exception:
                pass
        fl = cfg.get("flags") or {}
        self.flags = {k: bool(fl.get(k, DEFAULT_FLAGS[k])) for k in DEFAULT_FLAGS}
        return sorted(old - set(self.channels))

    def dump_cfg(self) -> Dict[str, Any]:  # raw ワールドの保存用 (信頼済み)
        return {
            "name": self.name, "paused": self.paused,
            "channels": [{"g": str(g), "c": str(c), "p": s.get("paused", False), "n": s.get("cn", "")} for (g, c), s in self.channels.items()],
            "room": {"id": self.room_id, "pw": self._room_pw} if self.room_id else None,
            "vc": {"g": str(self.vc[0]), "c": str(self.vc[1])} if self.vc else None,
            "links": {k: str(v) for k, v in self.links.items()},
            "flags": self.flags,
        }

    _room_pw = ""


def get_world(wid: str) -> Optional[World]:
    return worlds.get(wid)


def room_members(room_id: Optional[str], me: Optional[World] = None) -> List[World]:
    if not room_id:
        return []
    return [w for w in worlds.values() if w is not me and w.room_id == room_id and w.room_ok]


def worlds_on_channel(gid: int, cid: int) -> List[World]:
    return [w for w in worlds.values() if w.active_channel(gid, cid)]


def find_channel(gid: int, cid: int) -> Any:
    g = client.get_guild(gid)
    if not g:
        return None
    return g.get_channel(cid) or (g.get_thread(cid) if hasattr(g, "get_thread") else None)


# ───────────────────────── 生ワールド (アドオン無し /connect フォールバック) ─────────────────────────
def raw_load() -> None:
    try:
        with open(os.path.join(DATA_DIR, "raw_worlds.json"), encoding="utf-8") as f:
            raw_store.update(json.load(f))
    except Exception:
        pass


def raw_save(w: World) -> None:
    try:
        raw_store[w.wid] = w.dump_cfg()
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = os.path.join(DATA_DIR, "raw_worlds.json.tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(raw_store, f, ensure_ascii=False)
        os.replace(tmp, os.path.join(DATA_DIR, "raw_worlds.json"))
    except Exception as e:  # noqa
        log.warning("raw save failed: %r", e)


def make_raw_world(name: str, conn: Any) -> World:
    wid = "ws:" + re.sub(r"[^A-Za-z0-9_\-]", "", name)[:40]
    w = worlds.get(wid) or World(wid, conn=conn, raw=True)
    w.conn = conn
    cfg = raw_store.get(wid)
    if cfg:
        w.load_cfg(cfg, trusted=True)
        w._room_pw = (cfg.get("room") or {}).get("pw", "")
    w.name = w.name or name
    worlds[wid] = w
    register_room(w)
    return w


def apply_cfgop(w: World, op: Dict[str, Any]) -> None:
    """raw ワールド用: Core がやる設定変更を Bot 側で行う。"""
    k = op.get("op")
    if k == "add_channel":
        w.channels[(int(op["gid"]), int(op["cid"]))] = {"paused": False, "cn": op.get("name", "")}
    elif k == "del_channel":
        w.channels.pop((int(op["gid"]), int(op["cid"])), None)
    elif k == "pause":
        w.paused = bool(op.get("v"))
    elif k == "room":
        w.room_id, w._room_pw = str(op["id"]), str(op.get("pw", ""))
        w.room_hash = sha(w._room_pw)
        register_room(w)
    elif k == "room_leave":
        w.room_id = None
        w.room_ok = False
    elif k == "server_name":
        w.name = clean_mc(op.get("name", ""), 24)
    elif k == "vc":
        w.vc = (int(op["gid"]), int(op["cid"])) if op.get("gid") else None
    elif k == "link":
        w.links[str(op["p"])] = int(op["did"])
    if w.raw:
        raw_save(w)


# ───────────────────────── ルーム ─────────────────────────
def register_room(w: World) -> None:
    if not w.room_id:
        w.room_ok = False
        return
    others = [o for o in worlds.values() if o is not w and o.room_id == w.room_id and o.room_hash]
    if others and any(o.room_hash != w.room_hash for o in others):
        w.room_ok = False
        w.push(msg_ev("sys", "§c[ルーム] パスワードが他のワールドと一致しないため、ルーム同期は無効です (/server room join で入り直してください)"))
    else:
        w.room_ok = True


# ───────────────────────── Discord ↔ MC プレイヤー対応 ─────────────────────────
def match_player(w: World, member: Any) -> Optional[str]:
    for p, did in w.links.items():
        if did == member.id and p in w.players:
            return p
    names = {norm(getattr(member, "display_name", "")), norm(getattr(member, "name", "")), norm(getattr(member, "global_name", None))}
    names.discard("")
    for p in w.players:
        if norm(p) in names:
            return p
    return None


def member_for_player(w: World, player: str, channel: Any) -> Optional[Any]:
    for m in getattr(channel, "members", []):
        if getattr(m, "bot", False):
            continue
        if w.links.get(player) == m.id or norm(player) in {norm(m.display_name), norm(m.name), norm(getattr(m, "global_name", None))}:
            return m
    return None


# ───────────────────────── VC (話してる/ミュート/スピーカーミュート) ─────────────────────────
try:
    from discord.voice_state import VoiceConnectionState
except Exception:  # pragma: no cover
    VoiceConnectionState = None  # type: ignore


class SpeakingVoiceClient(discord.VoiceClient):
    """Voice WebSocket の op 5 (SPEAKING) を拾う VoiceClient。"""

    on_speaking = None

    def create_connection_state(self):  # discord.py 2.4+
        return VoiceConnectionState(self, hook=self._ws_hook)

    async def _ws_hook(self, ws: Any, msg: Dict[str, Any]) -> None:
        try:
            if msg.get("op") == 5 and self.on_speaking:
                d = msg.get("d") or {}
                if d.get("user_id") is not None:
                    self.on_speaking(int(d["user_id"]), bool(d.get("speaking")))
        except Exception as e:  # noqa
            log.debug("speaking hook: %r", e)


class VcSession:
    def __init__(self, gid: int, cid: int, vc: Any) -> None:
        self.gid, self.cid, self.vc = gid, cid, vc
        self.speaking: Dict[int, bool] = {}


vc_dirty: set = set()


def vc_touch(gid: int) -> None:
    vc_dirty.add(gid)


def member_state(sess: VcSession, m: Any) -> str:
    v = m.voice
    if v and (v.self_deaf or v.deaf):
        return "deaf"
    if v and (v.self_mute or v.mute):
        return "muted"
    return "speaking" if sess.speaking.get(m.id) else "idle"


def push_vc_state(gid: int) -> None:
    sess = vcsessions.get(gid)
    for w in list(worlds.values()):
        if not w.vc or w.vc[0] != gid:
            continue
        if not sess or sess.cid != w.vc[1]:
            payload: Dict[str, Any] = {"t": "vc", "on": False}
        else:
            ch = find_channel(gid, sess.cid)
            members = [m for m in getattr(ch, "members", []) if not getattr(m, "bot", False)]
            lst = []
            for m in members:
                p = match_player(w, m)
                lst.append({"p": p, "s": member_state(sess, m), "dn": m.display_name})
            payload = {"t": "vc", "on": True, "name": getattr(ch, "name", "VC"), "n": len(members), "members": [x for x in lst if x["p"]], "others": [x["dn"] for x in lst if not x["p"]][:8]}
        j = jdump(payload)
        if j != w.last_vc_json:
            w.last_vc_json = j
            w.push(payload)


async def vc_flush_loop() -> None:
    while True:
        await asyncio.sleep(0.3)
        if vc_dirty:
            ids = list(vc_dirty)
            vc_dirty.clear()
            for gid in ids:
                try:
                    push_vc_state(gid)
                except Exception as e:  # noqa
                    log.warning("vc push: %r", e)


async def join_vc(gid: int, cid: int) -> Tuple[bool, str]:
    guild = client.get_guild(gid)
    if not guild:
        return False, "BotがそのDiscordサーバーに入っていません。招待してください:\n" + invite_url()
    ch = guild.get_channel(cid)
    if not ch or not is_voice(ch):
        return False, "ボイスチャンネルが見つかりません (ID違い / Botから見えない)。"
    perms = ch.permissions_for(guild.me)
    miss = [n for n, ok in (("チャンネルを見る", perms.view_channel), ("接続", perms.connect)) if not ok]
    if miss:
        return False, "BotにこのVCでの権限がありません: " + "、".join(miss)
    cur = vcsessions.get(gid)
    try:
        if cur and cur.cid == cid:
            pass
        elif cur:
            users = [w for w in worlds.values() if w.vc == (gid, cur.cid)]
            if users:
                return False, "BotはこのDiscordサーバーの別のVCに接続中で、他のワールドが使っています。"
            await cur.vc.move_to(ch)
            cur.cid = cid
            cur.speaking.clear()
        else:
            vc = await ch.connect(cls=SpeakingVoiceClient, timeout=25.0, reconnect=True, self_deaf=VC_SELF_DEAF)
            sess = VcSession(gid, cid, vc)
            vc.on_speaking = lambda uid, sp, s=sess: (s.speaking.__setitem__(uid, sp), vc_touch(s.gid))
            vcsessions[gid] = sess
    except Exception as e:  # noqa
        log.warning("vc connect failed: %r", e)
        return False, f"VCに接続できませんでした: {type(e).__name__} (PyNaCl / davey の導入と Connect 権限を確認)"
    vc_touch(gid)
    return True, f"VC「{ch.name}」に参加しました"


async def leave_vc(w: World) -> None:
    if not w.vc:
        return
    gid, cid = w.vc
    w.vc = None
    w.last_vc_json = ""
    w.push({"t": "vc", "on": False})
    if not any(o.vc == (gid, cid) for o in worlds.values()):
        s = vcsessions.pop(gid, None)
        if s:
            try:
                await s.vc.disconnect(force=True)
            except Exception:
                pass


async def ensure_vc(w: World) -> None:
    if w.vc and w.vc[0] not in vcsessions and client and client.is_ready():
        ok, msg = await join_vc(*w.vc)
        w.push(msg_ev("sys", ("§a[VC] " if ok else "§c[VC] ") + msg, None))
    if w.vc:
        vc_touch(w.vc[0])


# ───────────────────────── MC → Discord / 他ワールド ─────────────────────────
async def on_mc_chat(w: World, player: str, text: str) -> None:
    text = clean_mc(text)
    if not text or not player:
        return
    if w.raw and text.startswith("!"):
        await raw_command(w, player, text)
        return
    if not w.can_send(player):
        return
    if not w.rate.allow():
        return
    if w.flags["m2d"] and not w.paused:
        for (g, c), st in list(w.channels.items()):
            if st.get("paused"):
                continue
            ch = find_channel(g, c)
            if ch:
                sender.send(ch, d_mc(w, player, text, len(worlds_on_channel(g, c)) > 1))
    if w.flags["room"] and w.room_ok:
        for o in room_members(w.room_id, w):
            o.push(msg_ev("room", fmt_room_mc(w.display_name, player, text)))


async def on_mc_server(w: World, text: str) -> None:
    text = clean_mc(text)
    if not text or not w.flags["srv"]:
        return
    if not w.paused:
        for (g, c), st in list(w.channels.items()):
            if st.get("paused"):
                continue
            ch = find_channel(g, c)
            if ch:
                sender.send(ch, d_server(w, text, len(worlds_on_channel(g, c)) > 1))
    if w.flags["room"] and w.room_ok:
        for o in room_members(w.room_id, w):
            o.push(msg_ev("server", fmt_server_mc(text, w.display_name)))


# ───────────────────────── コマンド処理 (Core の cmd / 生ワールドの !コマンド 共通) ─────────────────────────
Res = Tuple[bool, str, Optional[Dict[str, Any]]]


def _ids(a: List[str], n: int = 2) -> Optional[List[int]]:
    if len(a) < n or not all(re.fullmatch(r"\d{15,22}", str(x)) for x in a[:n]):
        return None
    return [int(x) for x in a[:n]]


async def cmd_add(w: World, p: str, a: List[str]) -> Res:
    ids = _ids(a)
    if not ids:
        return False, "使い方: /discord add channel <ギルドID> <チャンネルID>  (IDは Discord の開発者モードで右クリック→IDをコピー)", None
    gid, cid = ids
    if ALLOWED_GUILDS and gid not in ALLOWED_GUILDS:
        return False, "このDiscordサーバーはBotの許可リストにありません。", None
    guild = client.get_guild(gid)
    if not guild:
        return False, "BotがそのDiscordサーバーに入っていません。次のURLからBotを招待してから、もう一度実行してください:\n" + invite_url(), None
    ch = find_channel(gid, cid)
    if not ch:
        return False, "チャンネルが見つかりません。IDが違うか、Botにそのチャンネルを見る権限がありません。", None
    if not hasattr(ch, "send"):
        return False, "そのチャンネルにはメッセージを送れません。", None
    perms = ch.permissions_for(guild.me)
    miss = [n for n, ok in (("チャンネルを見る", perms.view_channel), ("メッセージを送信", perms.send_messages), ("メッセージ履歴を読む", perms.read_message_history)) if not ok]
    if miss:
        return False, f"Botにこのチャンネルでの権限がありません: {'、'.join(miss)}\nチャンネルの権限設定でBotに許可してください (招待し直す必要はありません)。", None
    code = gen_code()
    pending_verify[(w.wid, gid, cid)] = {"code": code, "exp": time.time() + 600, "p": p}
    try:
        await ch.send(
            f"🔐 **Minecraft連携の認証コード: `{code}`**\nMinecraft で `/discord verify {code}` を実行してください。(10分有効 / 依頼者: {p})",
            allowed_mentions=NO_MENTIONS,
        )
    except Exception as e:  # noqa
        return False, f"認証コードを送信できませんでした ({type(e).__name__})。Botの送信権限を確認してください。", None
    return True, f"#{ch.name} に認証コードを送信しました。Minecraftで /discord verify <コード> を実行してください。", None


async def cmd_verify(w: World, p: str, a: List[str]) -> Res:
    code = (a[0] if a else "").strip().upper()
    now = time.time()
    for k in [k for k, v in pending_verify.items() if v["exp"] < now]:
        pending_verify.pop(k, None)
    for (wid, g, c), v in list(pending_verify.items()):
        if wid == w.wid and v["code"] == code:
            pending_verify.pop((wid, g, c), None)
            ch = find_channel(g, c)
            cn = getattr(ch, "name", "")
            if ch:
                sender.send(ch, f"✅ Minecraftワールド「{w.display_name}」と連携しました。ここでのチャットが同期されます。")
            op = {"op": "add_channel", "gid": str(g), "cid": str(c), "rcpt": rcpt(w.wid, g, c), "name": cn}
            if w.raw:
                apply_cfgop(w, op)
            else:
                w.channels[(g, c)] = {"paused": False, "cn": cn}
            return True, f"§a認証成功! #{cn} とのチャット同期を開始しました。", op
    return False, "コードが違うか期限切れです。/discord add channel からやり直してください。", None


async def cmd_del(w: World, p: str, a: List[str]) -> Res:  # 生ワールド専用 (アドオン世界は Core が処理)
    ids = _ids(a)
    if not ids or (ids[0], ids[1]) not in w.channels:
        return False, "そのチャンネルは登録されていません。", None
    apply_cfgop(w, {"op": "del_channel", "gid": ids[0], "cid": ids[1]})
    ch = find_channel(*ids)
    if ch:
        sender.send(ch, "🔌 Minecraftとの連携を解除しました。")
    return True, "連携を削除しました。", None


async def cmd_stop(w: World, p: str, a: List[str], v: bool = True) -> Res:
    if a and (ids := _ids(a, 1)) and any(c == ids[0] for (_, c) in w.channels):
        for k in w.channels:
            if k[1] == ids[0]:
                w.channels[k]["paused"] = v
        if w.raw:
            raw_save(w)
    else:
        apply_cfgop(w, {"op": "pause", "v": v})
    return True, "同期を一時停止しました。再開は /discord start" if v else "同期を再開しました。", None


async def cmd_vc_join(w: World, p: str, a: List[str]) -> Res:
    ids = _ids(a)
    if not ids:
        return False, "使い方: /discord vc join <ギルドID> <VCのID>", None
    gid, cid = ids
    if ALLOWED_GUILDS and gid not in ALLOWED_GUILDS:
        return False, "このDiscordサーバーはBotの許可リストにありません。", None
    ok, msg = await join_vc(gid, cid)
    if not ok:
        return False, msg, None
    op = {"op": "vc", "gid": str(gid), "cid": str(cid)}
    w.vc = (gid, cid)
    if w.raw:
        apply_cfgop(w, op)
    ensure_names(w)
    vc_touch(gid)
    return True, "§a" + msg + "。同じVCにいる人の状態が表示されます。(見えない場合は /discord link)", op


async def cmd_vc_leave(w: World, p: str, a: List[str]) -> Res:
    await leave_vc(w)
    op = {"op": "vc", "gid": "", "cid": ""}
    if w.raw:
        w.vc = None
        raw_save(w)
    return True, "VCから退出しました。", op


def ensure_names(w: World) -> None:
    w.last_vc_json = ""


async def cmd_vc_say(w: World, p: str, a: List[str]) -> Res:
    text = clean_mc(" ".join(a))
    if not text:
        return False, "使い方: /vc <メッセージ>  (長い文は \"...\" で囲む)", None
    if not w.vc:
        return False, "このワールドはVCに参加していません。OPが /discord vc join <ギルドID> <VCのID> を実行してください。", None
    gid, cid = w.vc
    ch = find_channel(gid, cid)
    if not ch:
        return False, "VCが見つかりません。", None
    if not w.rate.allow():
        return False, "送信が速すぎます。", None
    me = member_for_player(w, p, ch)
    if not me or not getattr(me, "voice", None) or me.voice.channel.id != cid:
        return False, "あなたは対象VCにいません (またはDiscordと未連携です)。/discord link で連携してください。", None
    try:
        sender.send(ch, f"[Minecraft]{p} ≫ {text}")
    except Exception as e:  # noqa
        return False, f"VCチャットに送れませんでした: {e!r}", None
    line = f"[Minecraft][VC] {p} ≫ {text}"
    for o in [x for x in worlds.values() if x.vc == (gid, cid)]:
        to = [mp for m in getattr(ch, "members", []) if (mp := match_player(o, m))]
        if to:
            o.push(msg_ev("vc", line, to))
    return True, "", None


async def cmd_room_join(w: World, p: str, a: List[str]) -> Res:
    if len(a) < 2:
        return False, "使い方: /server room join <ルームID> <パスワード>", None
    rid, pw = str(a[0])[:32], str(a[1])[:64]
    h = sha(pw)
    others = [o for o in worlds.values() if o is not w and o.room_id == rid and o.room_hash]
    if others and any(o.room_hash != h for o in others):
        return False, "パスワードが違います。", None
    w.room_id, w.room_hash, w._room_pw, w.room_ok = rid, h, pw, True
    n = len(room_members(rid, w))
    return True, f"§aルーム「{rid}」に参加しました (現在 他に {n} ワールド)。名前は /server room name <名前> で設定できます。", {"op": "room", "id": rid, "pw": pw}


async def cmd_room_leave(w: World, p: str, a: List[str]) -> Res:
    apply_cfgop(w, {"op": "room_leave"})
    return True, "ルームから退出しました。", None


async def cmd_room_name(w: World, p: str, a: List[str]) -> Res:
    name = clean_mc(" ".join(a), 24)
    if not name:
        return False, "使い方: /server room name <サーバー名>", None
    apply_cfgop(w, {"op": "server_name", "name": name})
    return True, f"サーバー名を「{name}」にしました。", None


async def cmd_link(w: World, p: str, a: List[str]) -> Res:
    now = time.time()
    for k in [k for k, v in pending_link.items() if v["exp"] < now]:
        pending_link.pop(k, None)
    code = gen_code(5)
    pending_link[code] = {"wid": w.wid, "p": p, "exp": now + 600}
    return True, f"Discordで `/mclink code:{code}` を実行してください (10分有効)。これで {p} とDiscordアカウントが紐づきます。", None


async def cmd_status(w: World, p: str, a: List[str]) -> Res:
    lines = [f"§bBot v{VERSION} / 接続: {w.mode} / 稼働ワールド {len(worlds)}"]
    for (g, c), st in w.channels.items():
        ch = find_channel(g, c)
        lines.append(f" #{getattr(ch, 'name', c)} {'(一時停止)' if st.get('paused') else '(同期中)'}  同チャンネルのワールド数: {len(worlds_on_channel(g, c))}")
    if not w.channels:
        lines.append(" チャンネル未設定 (/discord add channel ...)")
    if w.room_id:
        lines.append(f" ルーム {w.room_id} ({'OK' if w.room_ok else '無効'}) 参加ワールド {len(room_members(w.room_id, w)) + 1}")
    if w.vc:
        lines.append(f" VC: {'接続中' if w.vc[0] in vcsessions else '未接続'}")
    return True, "\n".join(lines), None


CMDS = {
    "add": cmd_add, "verify": cmd_verify, "vc_join": cmd_vc_join, "vc_leave": cmd_vc_leave, "vc_say": cmd_vc_say,
    "room_join": cmd_room_join, "link": cmd_link, "status": cmd_status,
    "del": cmd_del, "stop": cmd_stop, "room_leave": cmd_room_leave, "room_name": cmd_room_name,
}
CORE_ONLY = {"del", "stop", "room_leave", "room_name"}  # アドオン世界では Core が自前で処理する


async def run_cmd(w: World, p: str, c: str, a: List[str]) -> Res:
    if c == "start":
        return await cmd_stop(w, p, a, False)
    fn = CMDS.get(c)
    if not fn:
        return False, f"不明な操作: {c}", None
    return await fn(w, p, a)


async def on_cmd(w: World, ev: Dict[str, Any]) -> None:
    p, c, a = str(ev.get("p", "")), str(ev.get("c", "")), [str(x) for x in ev.get("a", [])]
    try:
        ok, msg, op = await run_cmd(w, p, c, a)
    except Exception as e:  # noqa
        log.exception("cmd %s failed", c)
        ok, msg, op = False, f"Bot内部エラー: {type(e).__name__}", None
    ack: Dict[str, Any] = {"t": "ack", "id": ev.get("id"), "p": p, "ok": ok, "msg": msg}
    if op:
        ack["cfgop"] = op
    w.push(ack)


# ───────────────────────── 生ワールド (!discord ...) ─────────────────────────
def parse_bang(text: str) -> Optional[Tuple[str, List[str]]]:
    t = text[1:].split()
    if len(t) < 1:
        return None
    if t[0] == "discord" and len(t) >= 2:
        s, rest = t[1], t[2:]
        if s == "add" and rest[:1] == ["channel"]:
            return "add", rest[1:]
        if s == "vc" and rest[:1] == ["join"]:
            return "vc_join", rest[1:]
        if s == "vc" and rest[:1] == ["leave"]:
            return "vc_leave", []
        if s in ("verify", "del", "stop", "start", "link", "status"):
            return s, rest
    if t[0] == "server" and len(t) >= 3 and t[1] == "room":
        s, rest = t[2], t[3:]
        if s == "join":
            return "room_join", rest
        if s in ("leave", "name"):
            return "room_" + s, rest
    if t[0] == "vc":
        return "vc_say", t[1:]
    return None


async def raw_command(w: World, player: str, text: str) -> None:
    pc = parse_bang(text)
    if not pc:
        w.push(msg_ev("sys", "§7[Discord] 使えるコマンド: !discord add channel <G> <C> / verify <コード> / del <G> <C> / stop / start / link / status、!server room join <ID> <PW> / name <名>", [player]))
        return
    ok, msg, _ = await run_cmd(w, player, *pc)
    if msg:
        w.push(msg_ev("sys", ("§a" if ok else "§c") + "[Discord] " + msg, [player]))


def raw_deliver(ev: Dict[str, Any]) -> Optional[str]:
    """msg イベントを tellraw コマンドにする (アドオン無し用)。"""
    if ev.get("t") != "msg":
        return None
    to = ev.get("to")
    sel = "@a" if to is None else None
    if to is not None:
        if not to:
            return None
        sel = '@a[name="%s"]' % str(to[0]).replace('"', "").replace("\\", "") if len(to) == 1 else "@a"
    return "tellraw " + sel + " " + json.dumps({"rawtext": [{"text": ev["text"]}]}, ensure_ascii=False, separators=(",", ":"))


# ───────────────────────── 上りイベント (Core / ChatHook → Bot) ─────────────────────────
async def on_hello(w: World, ev: Dict[str, Any], first: bool) -> None:
    cfg = ev.get("cfg") or {}
    old_vc = w.vc
    removed = w.load_cfg(cfg)
    if old_vc and old_vc != w.vc:  # /discord vc leave (アドオン側で設定から消えた)
        w.last_vc_json = ""
        spawn(release_vc(old_vc))
    if str(ev.get("mode")) in ("http", "ws"):
        w.mode = str(ev.get("mode"))
    w.chathook = bool(ev.get("chathook"))
    if "players" in ev:
        w.players = {str(x["n"]) for x in ev["players"] if x.get("n")}
        w.pset = {str(x["n"]): x.get("ps") or {} for x in ev["players"] if x.get("n")}
    register_room(w)
    for g, c in removed:
        ch = find_channel(g, c)
        if ch and w.hello_done:
            sender.send(ch, f"🔌 Minecraftワールド「{w.display_name}」との連携が解除されました。")
    if w.vc and w.vc[0] not in vcsessions:
        spawn(ensure_vc(w))
    elif w.vc:
        vc_touch(w.vc[0])
    if ev.get("t") == "hello":
        w.push({"t": "hello_ack", "v": VERSION, "invite": invite_url() if client and client.user else ""})
    w.hello_done = True


async def handle_up(w: World, ev: Dict[str, Any]) -> None:
    t = ev.get("t")
    if t in ("hello", "cfg"):
        await on_hello(w, ev, first=not w.hello_done)
    elif not w.hello_done and t != "chat":
        w.push({"t": "need_hello"})
    elif t == "chat":
        await on_mc_chat(w, str(ev.get("p", "")), str(ev.get("m", "")))
    elif t == "srv":
        await on_mc_server(w, str(ev.get("m", "")))
    elif t == "players":
        w.players = {str(x) for x in ev.get("list", [])}
        if w.vc:
            vc_touch(w.vc[0])
    elif t == "pset":
        w.pset[str(ev.get("p", ""))] = ev.get("ps") if isinstance(ev.get("ps"), dict) else {}
    elif t == "cmd":
        await on_cmd(w, ev)


# ───────────────────────── HTTP: POST /api/sync (BDS + server-net) ─────────────────────────
def key_ok(k: str) -> bool:
    return bool(SECRET) and hmac.compare_digest(str(k).encode(), SECRET.encode())


async def api_sync(request: web.Request) -> web.Response:
    auth = request.headers.get("Authorization", "")
    if not key_ok(auth[7:] if auth.startswith("Bearer ") else auth):
        return web.json_response({"ok": False, "err": "unauthorized"}, status=401)
    try:
        body = await request.json()
        wid = str(body.get("wid", ""))
        if not WID_RE.match(wid):
            raise ValueError("wid")
        evs = body.get("ev", [])
        if not isinstance(evs, list):
            raise ValueError("ev")
    except Exception:
        return web.json_response({"ok": False, "err": "bad request"}, status=400)
    w = worlds.get(wid)
    if w is None:
        w = worlds[wid] = World(wid, raw=False)
        w.mode = "http"
        if not any(e.get("t") == "hello" for e in evs if isinstance(e, dict)):
            w.push({"t": "need_hello"})
    w.last_seen = time.time()
    for e in evs[:200]:
        if isinstance(e, dict):
            try:
                await handle_up(w, e)
            except Exception:
                log.exception("handle_up")
    return web.json_response({"ok": True, "ev": w.drain(max_events=100)})


# ───────────────────────── WebSocket: /connect wss://host/ws/<key>[/<name>] ─────────────────────────
class WsConn:
    def __init__(self, ws: web.WebSocketResponse, name: str) -> None:
        self.ws, self.name = ws, name
        self.id = secrets.token_hex(3)
        self.world: Optional[World] = None
        self.rawworld: Optional[World] = None
        self.pending: Dict[str, asyncio.Future] = {}
        self.ack = 0
        self.alive = True
        self.addon_ok: Optional[bool] = None
        self.fails = 0
        self.last_probe = 0.0
        self.boot = ""

    async def command(self, line: str, timeout: float = 6.0) -> Dict[str, Any]:
        rid = str(uuid.uuid4())
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        await self.ws.send_json({
            "header": {"version": 1, "requestId": rid, "messagePurpose": "commandRequest", "messageType": "commandRequest"},
            "body": {"origin": {"type": "player"}, "commandLine": line, "version": 1},
        })
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self.pending.pop(rid, None)

    async def subscribe(self, event: str) -> None:
        await self.ws.send_json({
            "header": {"version": 1, "requestId": str(uuid.uuid4()), "messagePurpose": "subscribe", "messageType": "commandRequest"},
            "body": {"eventName": event},
        })

    def target(self) -> Optional[World]:
        return self.world or self.rawworld

    async def on_player_chat(self, sender_name: str, message: str) -> None:
        w = self.world
        if w is not None:
            if w.chathook:
                return  # ChatHook(BDS) が拾うので二重にしない
            await on_mc_chat(w, sender_name, message)
            return
        if self.addon_ok:  # アドオンは居るが hello 前
            return
        if self.rawworld is None:
            self.rawworld = make_raw_world(self.name, self)
        await on_mc_chat(self.rawworld, sender_name, message)

    async def flush_down(self) -> None:
        w = self.target()
        if not w:
            return
        for _ in range(10):
            evs = w.drain(max_bytes=1400)
            if not evs:
                break
            if w.raw:
                for ev in evs:
                    cmd = raw_deliver(ev)
                    if cmd:
                        await self.command(cmd, 4.0)
            else:
                r = await self.command("scriptevent dcb:rx " + esc_cmd(jdump(evs)), 5.0)
                if r.get("statusCode", 0) != 0:
                    log.debug("scriptevent failed: %s", r)

    async def poll(self) -> None:
        r = await self.command(f"dcb:poll {self.ack} {self.boot or '-'}", 6.0)
        msg = str(r.get("statusMessage", "") or "")
        i = msg.find("DCB1")
        if i >= 0:
            self.fails, self.addon_ok = 0, True
            try:
                data = json.loads(msg[i + 4:])
            except Exception:
                log.warning("poll json: %r", msg[:200])
                return
            if data.get("b") != self.boot:  # アドオンが再起動した → seq が巻き戻る
                self.boot, self.ack = data.get("b"), 0
            for e in data.get("e", []):
                s = int(e.get("s", 0) or 0)
                if s and s <= self.ack:
                    continue
                if e.get("t") == "hello":
                    wid = str(e.get("wid", ""))
                    if not WID_RE.match(wid):
                        continue
                    w = worlds.get(wid) or World(wid, conn=self)
                    w.conn, w.mode = self, "ws"
                    worlds[wid] = w
                    self.world = w
                if self.world:
                    self.world.last_seen = time.time()
                    await handle_up(self.world, e)
                if s:
                    self.ack = max(self.ack, s)
        elif r.get("statusCode", 0) == 0:
            self.fails, self.addon_ok = 0, True if self.world else self.addon_ok
            if self.world:
                self.world.last_seen = time.time()
        else:
            self.fails += 1
            if self.fails >= 3 and self.addon_ok is not True:
                self.addon_ok = False
        self.last_probe = time.time()

    async def pump(self) -> None:
        await asyncio.sleep(0.4)
        try:  # 新しい接続では必ず hello を取り直す (Bot再起動・再 /connect 対策)
            await self.command("scriptevent dcb:rx " + esc_cmd(jdump([{"t": "need_hello"}])), 4.0)
        except Exception:
            pass
        while self.alive:
            try:
                await self.flush_down()
                if self.addon_ok is not False or time.time() - self.last_probe > 15:
                    await self.poll()
            except asyncio.TimeoutError:
                self.fails += 1
            except (ConnectionError, RuntimeError):
                break
            except Exception:
                log.exception("ws pump")
            w = self.target()
            try:
                if w:
                    await asyncio.wait_for(w.wake.wait(), 1.0)
                else:
                    await asyncio.sleep(1.0)
            except asyncio.TimeoutError:
                pass


async def ws_handler(request: web.Request) -> web.StreamResponse:
    key = request.match_info.get("key") or request.query.get("key", "")
    if not key_ok(key):
        return web.Response(status=401, text="unauthorized")
    name = request.match_info.get("name") or request.query.get("name") or "default"
    ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)  # heartbeat は付けない: Minecraft が ping に返事しない場合の誤切断を避ける (poll が常時通信になる)
    await ws.prepare(request)
    conn = WsConn(ws, name)
    log.info("WS connected: %s (%s)", conn.id, name)
    await conn.subscribe("PlayerMessage")
    pump = asyncio.ensure_future(conn.pump())
    try:
        async for m in ws:
            if m.type != aiohttp.WSMsgType.TEXT:
                continue
            try:
                d = json.loads(m.data)
            except Exception:
                continue
            h = d.get("header", {})
            purpose = h.get("messagePurpose")
            if purpose == "event" and h.get("eventName") == "PlayerMessage":
                b = d.get("body", {})
                if b.get("type") == "chat" and b.get("sender"):
                    spawn(conn.on_player_chat(str(b["sender"]), str(b.get("message", ""))))
            elif purpose == "commandResponse":
                fut = conn.pending.get(h.get("requestId"))
                if fut and not fut.done():
                    fut.set_result(d.get("body", {}))
    finally:
        conn.alive = False
        pump.cancel()
        for f in conn.pending.values():
            if not f.done():
                f.cancel()
        for w in list(worlds.values()):
            if w.conn is conn and not w.raw:
                worlds.pop(w.wid, None)
                spawn(leave_vc_if_orphan(w))
        if conn.rawworld:
            worlds.pop(conn.rawworld.wid, None)
        log.info("WS closed: %s", conn.id)
    return ws


async def release_vc(vc: Optional[Tuple[int, int]]) -> None:
    """どのワールドも使わなくなった VC から Bot を退出させる。"""
    if vc and not any(o.vc == vc for o in worlds.values()):
        s = vcsessions.pop(vc[0], None)
        if s:
            try:
                await s.vc.disconnect(force=True)
            except Exception:
                pass


async def leave_vc_if_orphan(w: World) -> None:
    await release_vc(w.vc)


async def health(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "bot": VERSION, "worlds": len(worlds), "discord_ready": bool(client and client.is_ready())})


async def gc_loop() -> None:
    while True:
        await asyncio.sleep(15)
        now = time.time()
        for wid, w in list(worlds.items()):
            if w.conn is None and not w.raw and now - w.last_seen > 60:  # HTTP ワールドが黙った
                worlds.pop(wid, None)
                await leave_vc_if_orphan(w)


async def keepalive_loop() -> None:
    url = os.getenv("RENDER_EXTERNAL_URL")
    if not url or os.getenv("KEEPALIVE", "1") == "0":
        return
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                await s.get(url.rstrip("/") + "/healthz", timeout=aiohttp.ClientTimeout(total=15))
            except Exception:
                pass


def make_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", health), web.get("/healthz", health),
        web.post("/api/sync", api_sync),
        web.get("/ws", ws_handler), web.get("/ws/{key}", ws_handler), web.get("/ws/{key}/{name}", ws_handler),
    ])
    return app


# ───────────────────────── Discord → Minecraft ─────────────────────────
def build_discord_text(message: Any) -> str:
    text = (message.clean_content or "").replace("§", "")
    text = re.sub(r"<a?:(\w+):\d+>", r":\1:", text)
    text = re.sub(r"\s*\n\s*", " ⏎ ", text.strip())
    extras: List[str] = []
    for a in message.attachments:
        extras.append("[画像]" if (a.content_type or "").startswith("image/") else f"[添付:{a.filename}]")
    for s in getattr(message, "stickers", []):
        extras.append(f"[スタンプ:{s.name}]")
    if getattr(message, "embeds", None) and not text and not extras:
        extras.append("[埋め込み]")
    ref = ""
    r = getattr(message, "reference", None)
    if r is not None and isinstance(getattr(r, "resolved", None), discord.Message):
        ref = f"↪{r.resolved.author.display_name} "
    out = (ref + text + " " + " ".join(extras)).strip()
    return out if len(out) <= MAX_MSG else out[: MAX_MSG - 1] + "…"


async def handle_discord_message(message: Any) -> None:
    if not message.guild or not message.content and not message.attachments and not getattr(message, "stickers", None):
        return
    if message.author.id == client.user.id or (message.author.bot and not RELAY_BOTS):
        return
    if ALLOWED_GUILDS and message.guild.id not in ALLOWED_GUILDS:
        return
    gid, cid = message.guild.id, message.channel.id
    text = build_discord_text(message)
    if not text:
        return
    nick, user = message.author.display_name.replace("§", ""), message.author.name
    vc_done: set = set()
    if is_voice(message.channel):
        line = fmt_vc(nick, user, text)
        for w in list(worlds.values()):
            if w.vc == (gid, cid) and w.flags["d2m"]:
                vc_done.add(w.wid)
                to = [p for m in getattr(message.channel, "members", []) if (p := match_player(w, m))]
                if to:
                    w.push(msg_ev("vc", line, to))
    for w in worlds_on_channel(gid, cid):
        if w.flags["d2m"] and w.wid not in vc_done:
            w.push(msg_ev("discord", fmt_discord(message.channel.name, nick, user, text)))


# ───────────────────────── Discord クライアント構築 ─────────────────────────
def build_client() -> Any:
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True
    intents.voice_states = True

    class Bridge(discord.Client):
        async def setup_hook(self) -> None:
            await self.tree.sync()
            spawn(vc_flush_loop())
            spawn(gc_loop())
            spawn(keepalive_loop())

        async def on_ready(self) -> None:
            log.info("Discord ready: %s (%d guilds)", self.user, len(self.guilds))
            await self.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="Minecraft ↔ Discord"))
            for w in list(worlds.values()):
                spawn(ensure_vc(w))

        async def on_message(self, message: discord.Message) -> None:
            try:
                await handle_discord_message(message)
            except Exception:
                log.exception("on_message")

        async def on_voice_state_update(self, member, before, after) -> None:
            for ch in (before.channel, after.channel):
                if ch is not None and vcsessions.get(ch.guild.id) and vcsessions[ch.guild.id].cid == ch.id:
                    sess = vcsessions[ch.guild.id]
                    if before.channel != after.channel:
                        sess.speaking.pop(member.id, None)
                    vc_touch(ch.guild.id)
            if member.id == self.user.id and after.channel is None and member.guild.id in vcsessions:
                vcsessions.pop(member.guild.id, None)  # 手動で蹴られた等
                for w in worlds.values():
                    if w.vc and w.vc[0] == member.guild.id:
                        w.last_vc_json = ""
                        w.push({"t": "vc", "on": False})
                        w.push(msg_ev("sys", "§e[VC] BotがVCから切断されました。再接続は /discord vc join <ギルドID> <VCのID>"))

        async def on_guild_remove(self, guild) -> None:
            vcsessions.pop(guild.id, None)

    c = Bridge(intents=intents)
    t = app_commands.CommandTree(c)
    c.tree = t

    @t.command(name="minecraft", description="連携中のMinecraftに [SERVER] メッセージを送ります")
    @app_commands.describe(message="送るメッセージ (例: 30秒で鯖落とします)")
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def minecraft(interaction: discord.Interaction, message: str):
        if not interaction.permissions.manage_guild:
            return await interaction.response.send_message("サーバー管理権限が必要です。", ephemeral=True)
        text = clean_mc(message, 400)
        targets = worlds_on_channel(interaction.guild_id, interaction.channel_id)
        if not targets:
            return await interaction.response.send_message("このチャンネルに連携中のワールドがありません。", ephemeral=True)
        for w in targets:
            w.push(msg_ev("server", fmt_server_mc(text)))
        await interaction.response.send_message(f"[SERVER]{text}", allowed_mentions=NO_MENTIONS)

    @t.command(name="mcstatus", description="このチャンネルに連携中のMinecraftワールドを表示します")
    @app_commands.guild_only()
    async def mcstatus(interaction: discord.Interaction):
        ws = worlds_on_channel(interaction.guild_id, interaction.channel_id)
        if not ws:
            return await interaction.response.send_message("連携中のワールドはありません。", ephemeral=True)
        lines = [f"**{w.display_name}** ({w.mode}) — {len(w.players)}人: {', '.join(sorted(w.players)) or '-'}" for w in ws]
        await interaction.response.send_message("\n".join(lines), allowed_mentions=NO_MENTIONS)

    @t.command(name="mclink", description="Minecraftのゲーマータグとあなたを紐づけます (VC表示用)")
    @app_commands.describe(code="Minecraft の /discord link で出たコード")
    @app_commands.guild_only()
    async def mclink(interaction: discord.Interaction, code: str):
        v = pending_link.pop(code.strip().upper(), None)
        w = worlds.get(v["wid"]) if v and v["exp"] > time.time() else None
        if not w:
            return await interaction.response.send_message("コードが違うか期限切れです。", ephemeral=True)
        op = {"op": "link", "p": v["p"], "did": str(interaction.user.id)}
        if w.raw:
            apply_cfgop(w, op)
        else:
            w.links[v["p"]] = interaction.user.id
        w.push({"t": "cfgop", "cfgop": op})
        w.push(msg_ev("sys", f"§a[Discord] {v['p']} を {interaction.user.display_name} (@{interaction.user.name}) と紐づけました", [v["p"]]))
        if w.vc:
            vc_touch(w.vc[0])
        await interaction.response.send_message(f"✅ {v['p']} と連携しました。", ephemeral=True)

    return c


# ───────────────────────── エントリポイント ─────────────────────────
async def main() -> None:
    global client, tree, SECRET
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN が未設定です")
    if not SECRET:
        SECRET = secrets.token_urlsafe(24)
        log.warning("BRIDGE_SECRET が未設定のため一時的に生成しました: %s  (再起動で変わるので Render の環境変数に設定してください)", SECRET)
    raw_load()
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("HTTP/WS server on :%d", PORT)
    client = build_client()
    tree = client.tree
    async with client:
        await client.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
