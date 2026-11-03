"""
NV BOT  -  Discord + Web control for a Minecraft (Forge 1.20.1) bot
- Discord slash commands
- Live control panel  (/panel)
- Mods download page  (/mods)
- Session replays     (/replay)
"""
import asyncio
import json
import logging
import os
import secrets
import time
import zipfile
from pathlib import Path

import discord
from aiohttp import ClientSession, ClientTimeout, web
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()
log = logging.getLogger("nvbot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ROOT = Path(__file__).parent
MODS_DIR = ROOT / "mods"
REC_DIR = ROOT / "recordings"
WEB_DIR = ROOT / "web"
ZIP_PATH = ROOT / "mods_all.zip"
for d in (MODS_DIR, REC_DIR):
    d.mkdir(exist_ok=True)

E = os.environ.get
DISCORD_TOKEN = E("DISCORD_TOKEN", "")
GUILD_ID = int(E("GUILD_ID") or 0)
ADMIN_IDS = {int(x) for x in (E("ADMIN_IDS") or "").replace(" ", "").split(",") if x.isdigit()}
NOTIFY_CHANNEL_ID = int(E("NOTIFY_CHANNEL_ID") or 0)
WEB_PORT = int(E("PORT") or E("WEB_PORT", "8080"))
PUBLIC_URL = (E("PUBLIC_URL") or f"http://localhost:{WEB_PORT}").rstrip("/")
VIEWER_URL = (E("VIEWER_URL") or "http://localhost:3007").rstrip("/")
PANEL_TOKEN = E("PANEL_TOKEN") or secrets.token_urlsafe(24)
BRIDGE_PORT = int(E("BRIDGE_PORT", "3100"))
BRIDGE_SECRET = E("BRIDGE_SECRET") or secrets.token_urlsafe(24)

PANEL_ACTIONS = {"join", "leave", "status", "watch", "unwatch", "rotate", "stealth", "afk", "say", "move", "look", "skip"}


# ───────────────────────── Bridge (Node process) ─────────────────────────
class Bridge:
    def __init__(self):
        self.proc = None
        self.on_event = None
        self.http: ClientSession | None = None

    async def run_forever(self):
        self.http = ClientSession(timeout=ClientTimeout(total=10))
        env = {**os.environ, "BRIDGE_PORT": str(BRIDGE_PORT), "BRIDGE_SECRET": BRIDGE_SECRET}
        while True:
            self.proc = await asyncio.create_subprocess_exec(
                "node", str(ROOT / "bridge" / "bot.js"), env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
            log.info("bridge started (pid %s)", self.proc.pid)
            async for raw in self.proc.stdout:
                line = raw.decode(errors="replace").strip()
                try:
                    ev = json.loads(line)
                except ValueError:
                    log.info("[node] %s", line)
                    continue
                log.info("[mc] %s", line[:300])
                if self.on_event:
                    asyncio.create_task(self.on_event(ev))
            await self.proc.wait()
            log.warning("bridge exited, restarting in 5s")
            await asyncio.sleep(5)

    async def call(self, action: str, **kw) -> dict:
        try:
            async with self.http.post(
                f"http://127.0.0.1:{BRIDGE_PORT}/api",
                json={"action": action, **kw}, headers={"x-secret": BRIDGE_SECRET},
            ) as r:
                return await r.json()
        except Exception as e:  # bridge not up yet
            return {"ok": False, "error": f"الـ bridge مش شغال: {e}"}


bridge = Bridge()


# ───────────────────────────── Web server ────────────────────────────────
def build_zip():
    """يعمل zip لكل المودات (بدون ضغط عشان السرعة) لو في مود اتغير."""
    files = sorted(p for p in MODS_DIR.iterdir() if p.is_file() and not p.name.startswith("."))
    if not files:
        return None
    newest = max(p.stat().st_mtime for p in files)
    if ZIP_PATH.exists() and ZIP_PATH.stat().st_mtime >= newest:
        return ZIP_PATH
    tmp = ZIP_PATH.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_STORED, allowZip64=True) as z:
        for p in files:
            z.write(p, arcname=f"mods/{p.name}")
    tmp.replace(ZIP_PATH)
    return ZIP_PATH


def need_token(request: web.Request):
    tok = request.query.get("token") or request.headers.get("x-token", "")
    if not secrets.compare_digest(tok, PANEL_TOKEN):
        raise web.HTTPForbidden(text="forbidden")


def safe_file(base: Path, name: str) -> Path:
    p = base / Path(name).name
    if not p.is_file():
        raise web.HTTPNotFound()
    return p


def page(name):
    async def h(request):
        return web.FileResponse(WEB_DIR / name)
    return h


async def panel_page(request):
    need_token(request)
    return web.FileResponse(WEB_DIR / "panel.html")


async def replay_page(request):
    need_token(request)
    return web.FileResponse(WEB_DIR / "replay.html")


async def api_config(request):
    need_token(request)
    return web.json_response({"viewer": VIEWER_URL, "mods": f"{PUBLIC_URL}/mods"})


async def api_control(request):
    need_token(request)
    data = await request.json()
    action = data.pop("action", "")
    if action not in PANEL_ACTIONS:
        raise web.HTTPBadRequest()
    return web.json_response(await bridge.call(action, **data))


async def api_mods(request):
    files = sorted((p for p in MODS_DIR.iterdir() if p.is_file() and not p.name.startswith(".")), key=lambda p: p.name.lower())
    return web.json_response([{"name": p.name, "size": p.stat().st_size} for p in files])


async def mods_file(request):
    p = safe_file(MODS_DIR, request.match_info["name"])
    return web.FileResponse(p, headers={"Content-Disposition": f'attachment; filename="{p.name}"'})


async def mods_zip(request):
    z = await asyncio.get_running_loop().run_in_executor(None, build_zip)
    if not z:
        raise web.HTTPNotFound(text="no mods")
    return web.FileResponse(z, headers={"Content-Disposition": 'attachment; filename="NV_mods.zip"'})


def list_replays():
    out = []
    for p in sorted(REC_DIR.glob("*.jsonl"), reverse=True):
        out.append({"name": p.name, "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)})
    return out


async def api_replays(request):
    need_token(request)
    return web.json_response(list_replays())


async def api_replay(request):
    need_token(request)
    p = safe_file(REC_DIR, request.match_info["name"])
    return web.FileResponse(p, headers={"content-type": "application/x-ndjson"})


async def root(request):
    raise web.HTTPFound("/mods")


def make_app():
    app = web.Application(client_max_size=1024 ** 2)
    app.add_routes([
        web.get("/", root),
        web.get("/mods", page("mods.html")),
        web.get("/api/mods", api_mods),
        web.get("/mods/all.zip", mods_zip),
        web.get("/mods/file/{name}", mods_file),
        web.get("/panel", panel_page),
        web.get("/replay", replay_page),
        web.get("/api/config", api_config),
        web.post("/api/control", api_control),
        web.get("/api/replays", api_replays),
        web.get("/api/replay/{name}", api_replay),
    ])
    return app


# ───────────────────────────── Discord bot ───────────────────────────────
class NVBot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.default())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        if GUILD_ID:
            g = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=g)
            await self.tree.sync(guild=g)
        else:
            await self.tree.sync()

    async def notify(self, text: str):
        if not NOTIFY_CHANNEL_ID:
            return
        ch = self.get_channel(NOTIFY_CHANNEL_ID)
        if ch:
            await ch.send(text)


client = NVBot()
tree = client.tree


def panel_link(path="/panel"):
    return f"{PUBLIC_URL}{path}?token={PANEL_TOKEN}"


def allowed(i: discord.Interaction) -> bool:
    if ADMIN_IDS:
        return i.user.id in ADMIN_IDS
    return bool(i.guild and i.user.guild_permissions.administrator)


def admin_only():
    async def pred(i: discord.Interaction):
        if allowed(i):
            return True
        await i.response.send_message("❌ مش مسموحلك تستخدم الأمر ده.", ephemeral=True)
        return False
    return app_commands.check(pred)


async def reply_result(i: discord.Interaction, res: dict, ok_msg: str):
    if res.get("ok", True) is False:
        await i.followup.send(f"⚠️ {res.get('error', 'حصل خطأ')}", ephemeral=True)
    else:
        await i.followup.send(ok_msg, ephemeral=True)


async def player_autocomplete(i: discord.Interaction, current: str):
    res = await bridge.call("players")
    names = res.get("players", [])
    return [app_commands.Choice(name=n, value=n) for n in names if current.lower() in n.lower()][:25]


@tree.command(name="join", description="خلّي NV_BOT يدخل السيرفر ويبعتلك رابط التحكم")
@admin_only()
async def cmd_join(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    res = await bridge.call("join")
    if res.get("ok") is False and "بالفعل" not in res.get("error", ""):
        return await i.followup.send(f"⚠️ {res.get('error')}", ephemeral=True)
    await i.followup.send(f"✅ البوت بيدخل السيرفر...\n🎮 **لوحة التحكم:** {panel_link()}", ephemeral=True)


@tree.command(name="panel", description="رابط لوحة التحكم والمشاهدة الحية")
@admin_only()
async def cmd_panel(i: discord.Interaction):
    await i.response.send_message(f"🎮 {panel_link()}", ephemeral=True)


@tree.command(name="mods", description="رابط تحميل المودات (للناس الي هتلعب)")
async def cmd_mods(i: discord.Interaction):
    await i.response.send_message(f"📦 **مودات السيرفر (Forge 1.20.1):** {PUBLIC_URL}/mods")


@tree.command(name="leave", description="اطلّع البوت من السيرفر")
@admin_only()
async def cmd_leave(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("leave"), "👋 البوت خرج من السيرفر.")


@tree.command(name="watch", description="راقب لاعب معين")
@app_commands.describe(player="اسم اللاعب")
@app_commands.autocomplete(player=player_autocomplete)
@admin_only()
async def cmd_watch(i: discord.Interaction, player: str):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("watch", name=player), f"👀 بيراقب **{player}** دلوقتي (والتسجيل شغال).")


@tree.command(name="unwatch", description="وقّف المراقبة")
@admin_only()
async def cmd_unwatch(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("unwatch"), "⏹️ وقفت المراقبة.")


@tree.command(name="rotate", description="يراقب كل لاعب فترة وبعدين ينقل للي بعده")
@app_commands.describe(mode="شغّل ولا وقّف", minutes="كام دقيقة لكل لاعب (الافتراضي 5)")
@app_commands.choices(mode=[app_commands.Choice(name="تشغيل", value="on"), app_commands.Choice(name="إيقاف", value="off")])
@admin_only()
async def cmd_rotate(i: discord.Interaction, mode: str, minutes: app_commands.Range[int, 1, 120] = 5):
    await i.response.defer(ephemeral=True)
    res = await bridge.call("rotate", on=(mode == "on"), minutes=minutes)
    await reply_result(i, res, f"🔄 المراقبة الدورية شغالة: **{minutes} دقائق** لكل لاعب." if mode == "on" else "⏹️ وقفت المراقبة الدورية.")


@tree.command(name="skip", description="استثني لاعب من المراقبة الدورية (أو رجّعه)")
@app_commands.autocomplete(player=player_autocomplete)
@app_commands.choices(mode=[app_commands.Choice(name="استثناء", value="on"), app_commands.Choice(name="إرجاع", value="off")])
@admin_only()
async def cmd_skip(i: discord.Interaction, player: str, mode: str = "on"):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("skip", name=player, on=(mode == "on")), "✅ تمام.")


@tree.command(name="stealth", description="إخفاء البوت (محتاج أوب)")
@app_commands.choices(mode=[
    app_commands.Choice(name="سبيكتيتور", value="spectator"),
    app_commands.Choice(name="كريتف", value="creative"),
    app_commands.Choice(name="شفاف (Invisibility)", value="invisible"),
    app_commands.Choice(name="إلغاء الإخفاء", value="off"),
])
@admin_only()
async def cmd_stealth(i: discord.Interaction, mode: str):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("stealth", mode=mode), f"🫥 الوضع: **{mode}** (لازم NV_BOT يبقى أوب عشان يشتغل).")


@tree.command(name="afk", description="تشغيل/إيقاف الحركة العشوائية عشان البوت ميتطردش")
@app_commands.choices(mode=[app_commands.Choice(name="تشغيل", value="on"), app_commands.Choice(name="إيقاف", value="off")])
@admin_only()
async def cmd_afk(i: discord.Interaction, mode: str):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("afk", on=(mode == "on")), f"🏃 Anti-AFK: **{mode}**")


@tree.command(name="say", description="خلّي البوت يكتب في الشات")
@admin_only()
async def cmd_say(i: discord.Interaction, message: str):
    await i.response.defer(ephemeral=True)
    await reply_result(i, await bridge.call("say", msg=message), "💬 اتبعت.")


@tree.command(name="players", description="اللاعبين الأونلاين")
async def cmd_players(i: discord.Interaction):
    res = await bridge.call("players")
    names = res.get("players", [])
    await i.response.send_message("👥 " + (", ".join(f"`{n}`" for n in names) if names else "مفيش حد (أو البوت مش داخل)."), ephemeral=True)


@tree.command(name="status", description="حالة البوت")
async def cmd_status(i: discord.Interaction):
    s = await bridge.call("status")
    if s.get("online") is None:
        return await i.response.send_message(f"⚠️ {s.get('error')}", ephemeral=True)
    w = s["watch"]
    emb = discord.Embed(title="NV BOT", color=0x3F6FE0 if s["online"] else 0x888888)
    emb.add_field(name="الحالة", value="🟢 داخل" if s["online"] else ("🟡 بيدخل..." if s["connecting"] else "🔴 مش داخل"))
    emb.add_field(name="السيرفر", value=s["server"])
    if s["online"]:
        emb.add_field(name="الصحة / الأكل", value=f"{s['health']} / {s['food']}")
        emb.add_field(name="المكان", value=" ".join(map(str, s["pos"])))
    emb.add_field(name="المراقبة", value=(w["target"] or "—") + (f" (دورية {w['minutes']}د)" if w["rotate"] else ""))
    emb.add_field(name="Anti-AFK", value="شغال" if s["afk"] else "واقف")
    emb.add_field(name="الإخفاء", value=s["stealth"])
    emb.add_field(name="التسجيل", value=s["recording"] or "—", inline=False)
    await i.response.send_message(embed=emb, ephemeral=True)


@tree.command(name="replays", description="رابط التسجيلات (إيه الي حصل وهو بيراقب)")
@admin_only()
async def cmd_replays(i: discord.Interaction):
    files = list_replays()[:5]
    txt = "\n".join(f"• `{f['name']}`" for f in files) or "مفيش تسجيلات لسه."
    await i.response.send_message(f"🎞️ آخر التسجيلات:\n{txt}\n\n▶️ {panel_link('/replay')}", ephemeral=True)


EVENT_MSG = {
    "spawned": "🟢 NV_BOT دخل السيرفر.",
    "kicked": "🔴 NV_BOT اتطرد: {reason}",
    "ended": "⚪ NV_BOT خرج من السيرفر ({reason}).",
    "watch": "👀 بيراقب دلوقتي: **{target}**",
    "forge_error": "⚠️ مشكلة في Forge handshake: {error}",
    "mc_error": "⚠️ {error}",
}


async def on_bridge_event(ev: dict):
    tmpl = EVENT_MSG.get(ev.get("ev"))
    if tmpl:
        try:
            await client.notify(tmpl.format(**{"reason": "", "target": "", "error": "", **ev})[:1900])
        except Exception:
            log.exception("notify failed")


# ─────────────────────────────── main ────────────────────────────────────
async def main():
    if not DISCORD_TOKEN:
        raise SystemExit("حط DISCORD_TOKEN في ملف .env")
    bridge.on_event = on_bridge_event
    runner = web.AppRunner(make_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", WEB_PORT).start()
    log.info("web on :%s | panel: %s", WEB_PORT, panel_link())
    asyncio.create_task(bridge.run_forever())
    async with client:
        await client.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
