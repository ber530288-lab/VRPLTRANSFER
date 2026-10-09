import asyncio
import functools
import glob
import io
import math
import random
import threading
import pathlib
import aiohttp
import os
import re
import shutil
import sqlite3
import datetime as dt
import time
import traceback
from zoneinfo import ZoneInfo
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
try:
    from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont
except ImportError:   # /table needs Pillow
    Image = None

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")
LEAGUE_NAME = os.getenv("LEAGUE_NAME", "VCL X NAPXR.GG | PitchX")
_env_short = os.getenv("LEAGUE_SHORT", "NAPXR").strip() or "NAPXR"
DEFAULT_LEAGUE_SHORT = "NAPXR" if _env_short.lower() == "pitchx" else _env_short   # old example default was PitchX
def _url(v):
    return v if v and v.startswith(("http://", "https://")) else None

LEAGUE_LOGO = _url(os.getenv("LEAGUE_LOGO_URL"))
PROFILE_URL = os.getenv("PROFILE_URL", "https://discord.com/users/{user_id}")
DASHBOARD_URL = _url(os.getenv("DASHBOARD_URL"))
REMINDER_DAYS = float(os.getenv("REMINDER_DAYS", "14"))
MATCHDAY_LEAD_HOURS = float(os.getenv("MATCHDAY_LEAD_HOURS", "24"))
OFFER_HOURS = 48
START_BUDGET = int(os.getenv("START_BUDGET", "100000000"))
MIN_FEE_PERCENT = float(os.getenv("MIN_FEE_PERCENT", "100"))      # transfer fee must be at least this % of market value
PLAYER_BASE_VALUE = int(os.getenv("PLAYER_BASE_VALUE", "500000"))
GOAL_VALUE = int(os.getenv("GOAL_VALUE", "600000"))
ASSIST_VALUE = int(os.getenv("ASSIST_VALUE", "350000"))
CLEANSHEET_VALUE = int(os.getenv("CLEANSHEET_VALUE", "400000"))
VALUE_SOFT_CAP = int(os.getenv("VALUE_SOFT_CAP", "6000000"))       # past this, extra output adds value at half rate
MAX_PLAYER_VALUE = int(os.getenv("MAX_PLAYER_VALUE", "20000000"))
SIGN_VALUE_BONUS = int(os.getenv("SIGN_VALUE_BONUS", "500000"))   # value added when a free agent signs for a team

PINK, ORANGE, GREEN, YELLOW, RED = 0xE91E63, 0xF57C00, 0x2ECC71, 0xF1C40F, 0xE74C3C
FORM = {"W": "🟢", "D": "🟡", "L": "🔴"}

# ---------------------------------------------------------------- database
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(os.path.abspath(__file__)), "pitchx.db"))
BACKUP_DIR = os.getenv("BACKUP_DIR", os.path.join(os.path.dirname(DB_PATH), "backups"))
BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "30"))
BACKUP_CHANNEL_NAME = "pitchx-backups"
AUTO_BACKUP_CHANNEL = os.getenv("AUTO_BACKUP_CHANNEL", "1") == "1"   # create a private #pitchx-backups channel if missing

def _count(path, table="teams"):
    """Row count of a table in a database file, read-only (0 if missing/unreadable). Never creates the file."""
    try:
        c = sqlite3.connect(pathlib.Path(path).resolve().as_uri() + "?mode=ro", uri=True)
        try:
            return c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            c.close()
    except Exception:
        return 0

def _latest_backup():
    for f in sorted(glob.glob(os.path.join(BACKUP_DIR, "pitchx-*.db")), reverse=True):
        if _count(f) > 0:
            return f
    return None

# If the live database has no teams (fresh folder / wiped host) but a backup has data, bring the data back.
if os.getenv("AUTO_RESTORE", "1") == "1" and _count(DB_PATH) == 0:
    _b = _latest_backup()
    if _b:
        for _ext in ("-wal", "-shm"):
            try:
                os.remove(DB_PATH + _ext)
            except OSError:
                pass
        shutil.copyfile(_b, DB_PATH)
        print(f"[db] Database was empty, restored your data from {_b}")

db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
try:
    db.execute('PRAGMA journal_mode=WAL'); db.execute('PRAGMA synchronous=NORMAL')
except sqlite3.Error:
    pass
def ensure_schema():
    """Create missing tables/columns only. Never deletes or resets data."""
    db.executescript("""
CREATE TABLE IF NOT EXISTS teams(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, logo TEXT, tier TEXT);
CREATE TABLE IF NOT EXISTS players(
  user_id INTEGER PRIMARY KEY, team_id INTEGER, signed_at TEXT);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, user_id INTEGER, team_id INTEGER);
CREATE TABLE IF NOT EXISTS fixtures(
  id INTEGER PRIMARY KEY, gw INTEGER, home_id INTEGER, away_id INTEGER, kickoff REAL, tier TEXT,
  competition TEXT, status TEXT DEFAULT 'scheduled', home_goals INTEGER, away_goals INTEGER,
  notes TEXT, brief_sent INTEGER DEFAULT 0, played_at REAL, forfeit INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS assets(name TEXT PRIMARY KEY, data BLOB);
CREATE TABLE IF NOT EXISTS divisions(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, logo BLOB, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS transfers(
  id INTEGER PRIMARY KEY, ts TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT, user_id INTEGER,
  from_team_id INTEGER, to_team_id INTEGER, fee INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS trophy_types(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE COLLATE NOCASE, logo BLOB, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS match_stats(
  fixture_id INTEGER, user_id INTEGER, team_id INTEGER,
  goals INTEGER DEFAULT 0, assists INTEGER DEFAULT 0, clean_sheets INTEGER DEFAULT 0,
  PRIMARY KEY(fixture_id, user_id));
CREATE TABLE IF NOT EXISTS trophies(
  id INTEGER PRIMARY KEY, user_id INTEGER, title TEXT, awarded_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS managers(
  team_id INTEGER, user_id INTEGER, PRIMARY KEY(team_id, user_id));
CREATE TABLE IF NOT EXISTS offers(
  id INTEGER PRIMARY KEY, user_id INTEGER, kind TEXT, team_id INTEGER, from_team_id INTEGER,
  staff_id INTEGER, guild_id INTEGER, created_at REAL, status TEXT DEFAULT 'pending');
""")

    for _stmt in (f"ALTER TABLE teams ADD COLUMN budget INTEGER DEFAULT {START_BUDGET}",
                  "ALTER TABLE offers ADD COLUMN fee INTEGER DEFAULT 0",
                  "ALTER TABLE trophies ADD COLUMN trophy_id INTEGER",
                  "ALTER TABLE teams ADD COLUMN logo_blob BLOB",
                  "ALTER TABLE players ADD COLUMN value_bonus INTEGER DEFAULT 0",
                  "ALTER TABLE offers ADD COLUMN channel_id INTEGER"):
        try:
            db.execute(_stmt); db.commit()
        except sqlite3.OperationalError:
            pass   # column already exists
    # every team's existing tier becomes a division (nothing is changed, just listed)
    db.execute("INSERT OR IGNORE INTO divisions(name) SELECT DISTINCT tier FROM teams WHERE tier IS NOT NULL"); db.commit()

ensure_schema()
print(f"[db] {DB_PATH}: {db.execute('SELECT COUNT(*) FROM teams').fetchone()[0]} teams, "
      f"{db.execute('SELECT COUNT(*) FROM players').fetchone()[0]} players, "
      f"{db.execute('SELECT COUNT(*) FROM fixtures').fetchone()[0]} fixtures loaded")
STARTED_EMPTY = db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0
if STARTED_EMPTY:
    print(f"[db] WARNING: empty database at {DB_PATH}. If you had teams before, this file was replaced or you started the bot from a new folder.")

def backup_db(tag="auto"):
    """Consistent snapshot of the live database into BACKUP_DIR; keeps the newest BACKUP_KEEP."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"pitchx-{stamp}-{tag}.db")
    dest = sqlite3.connect(path)
    try:
        db.backup(dest)
    finally:
        dest.close()
    for old in sorted(glob.glob(os.path.join(BACKUP_DIR, "pitchx-*.db")))[:-BACKUP_KEEP]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path

def get_team(name):
    return db.execute("SELECT * FROM teams WHERE name=?", (name,)).fetchone()

def team_by_id(tid):
    return db.execute("SELECT * FROM teams WHERE id=?", (tid,)).fetchone() if tid else None

def player_team(user_id):
    r = db.execute("SELECT team_id FROM players WHERE user_id=?", (user_id,)).fetchone()
    return team_by_id(r["team_id"]) if r else None

def get_setting(k):
    r = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else None

def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, v)); db.commit()

def league_short():
    return get_setting("league_short") or DEFAULT_LEAGUE_SHORT

# ---------------------------------------------------------------- helpers
intents = discord.Intents.default()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

async def team_ac(_, current: str):
    rows = db.execute("SELECT name FROM teams WHERE name LIKE ? LIMIT 25", (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

async def tier_ac(_, current: str):
    rows = db.execute("SELECT name FROM divisions WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

def profile_view(user_id):
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="View Your Profile", emoji="↗️",
                                 url=PROFILE_URL.format(user_id=user_id)))
    return v

def dashboard_view():
    if not DASHBOARD_URL:
        return None
    v = discord.ui.View()
    v.add_item(discord.ui.Button(label="Open Your Dashboard", emoji="🏟️", url=DASHBOARD_URL))
    return v

def money(n):
    return f"${int(n):,}"

def parse_money(text):
    """'5000000', '5,000,000', '$5m', '2.5m', '750k' -> int dollars (None if unreadable)."""
    t = str(text).strip().lower().replace("$", "").replace(",", "").replace(" ", "")
    mult = 1
    if t.endswith("k"):
        mult, t = 1_000, t[:-1]
    elif t.endswith("m"):
        mult, t = 1_000_000, t[:-1]
    elif t.endswith("b"):
        mult, t = 1_000_000_000, t[:-1]
    try:
        v = float(t) * mult
    except ValueError:
        return None
    if not (0 <= v < 1e15):
        return None
    return int(round(v))

def budget_of(team):
    return team["budget"] if team["budget"] is not None else START_BUDGET

def player_totals(uid):
    r = db.execute("""SELECT COALESCE(SUM(goals),0) g, COALESCE(SUM(assists),0) a,
                      COALESCE(SUM(clean_sheets),0) c FROM match_stats WHERE user_id=?""", (uid,)).fetchone()
    return r["g"], r["a"], r["c"]

def value_from(g, a, c):
    """Market value from output. Linear at first, then half-rate past the soft cap, hard-capped overall."""
    raw = g * GOAL_VALUE + a * ASSIST_VALUE + c * CLEANSHEET_VALUE
    if raw > VALUE_SOFT_CAP:
        raw = VALUE_SOFT_CAP + (raw - VALUE_SOFT_CAP) * 0.5
    return int(round(min(PLAYER_BASE_VALUE + raw, MAX_PLAYER_VALUE), -4))

def player_bonus(uid):
    r = db.execute("SELECT value_bonus FROM players WHERE user_id=?", (uid,)).fetchone()
    return int(r["value_bonus"] or 0) if r else 0

def player_value(uid):
    """Market value = what his goals/assists/clean sheets are worth + the premium from the price paid for him."""
    return value_from(*player_totals(uid)) + player_bonus(uid)

def rank_points(g, a, c):
    return (g + a + c) * 2

RANKS = ["Bronze I", "Bronze II", "Bronze III", "Silver I", "Silver II", "Silver III",
         "Gold I", "Gold II", "Gold III", "Platinum I", "Platinum II", "Platinum III",
         "Diamond I", "Diamond II", "Diamond III", "Elite"]

def rank_line(pts):
    idx = min(pts // 10, len(RANKS) - 1)
    if idx == len(RANKS) - 1:
        return f"{RANKS[idx]} {'▰' * 5} MAX"
    prog = pts % 10
    filled = prog // 2
    return f"{RANKS[idx]} {'▰' * filled}{'▱' * (5 - filled)} {prog}/10"

MENTION_RE = re.compile(r"<@!?(\d+)>\s*(?:[x×*]\s*)?(\d{1,2})?")

def parse_stat_list(text):
    """'@tung 2 @bob' -> {tung_id: 2, bob_id: 1}"""
    out = {}
    for m in MENTION_RE.finditer(text or ""):
        uid = int(m.group(1))
        out[uid] = out.get(uid, 0) + int(m.group(2) or 1)
    return out

def check_match_stats(fx, hg, ag, goals, assists, cs):
    """Returns an error string, or None if the stats are consistent with the score."""
    sides = {fx["home_id"]: (hg, ag), fx["away_id"]: (ag, hg)}   # team -> (scored, conceded)
    per = {tid: {"g": 0, "a": 0} for tid in sides}
    for kind, data in (("goals", goals), ("assists", assists), ("cs", cs)):
        for uid, n in data.items():
            t = player_team(uid)
            if not t or t["id"] not in sides:
                return f"<@{uid}> isn't on either team in this match (check /roster)."
            scored, conceded = sides[t["id"]]
            if kind == "goals":
                per[t["id"]]["g"] += n
            elif kind == "assists":
                per[t["id"]]["a"] += n
            else:
                if n > 1:
                    return f"A clean sheet counts once per match (<@{uid}>)."
                if conceded > 0:
                    return f"<@{uid}> can't have a clean sheet: {t['name']} conceded {conceded}."
    for tid, (scored, conceded) in sides.items():
        if per[tid]["g"] > scored:
            return f"You listed {per[tid]['g']} goals for {name_of(tid)} but they only scored {scored}."
        if per[tid]["a"] > scored:
            return f"You listed {per[tid]['a']} assists for {name_of(tid)} but they only scored {scored} goals."
    return None

def save_match_stats(fid, goals, assists, cs):
    for uid in set(goals) | set(assists) | set(cs):
        t = player_team(uid)
        db.execute("INSERT OR REPLACE INTO match_stats VALUES(?,?,?,?,?,?)",
                   (fid, uid, t["id"] if t else None, goals.get(uid, 0), assists.get(uid, 0), cs.get(uid, 0)))
    db.commit()

def logo_blob(team):
    try:
        return team["logo_blob"] if team else None
    except (IndexError, KeyError):
        return None

def logo(team):
    """What to put in an embed for a team's logo: its stored upload (attached automatically) or an old link."""
    if not team:
        return None
    if logo_blob(team):
        return f"attachment://logo_{team['id']}.png"
    u = team["logo"]
    return u if u and u.startswith(("http://", "https://")) else None

ASSET_RE = re.compile(r"attachment://(logo|division)_(\d+)\.png$")

def asset_bytes(kind, ident):
    if kind == "logo":
        r = db.execute("SELECT logo_blob FROM teams WHERE id=?", (int(ident),)).fetchone()
        return r["logo_blob"] if r else None
    r = db.execute("SELECT logo FROM divisions WHERE id=?", (int(ident),)).fetchone()
    return r["logo"] if r else None

def stored_assets(embeds, taken=()):
    """Stored team/division logos referenced by embeds via attachment://... -> [(filename, png bytes)]"""
    out, seen = [], set(taken)
    for e in embeds:
        d = e.to_dict()
        urls = [d.get("thumbnail", {}).get("url"), d.get("image", {}).get("url"),
                d.get("footer", {}).get("icon_url"), d.get("author", {}).get("icon_url")]
        for u in urls:
            m = ASSET_RE.match(u or "")
            if not m:
                continue
            name = f"{m.group(1)}_{m.group(2)}.png"
            if name in seen:
                continue
            data = asset_bytes(m.group(1), m.group(2))
            if data:
                out.append((name, data))
                seen.add(name)
    return out

def _with_assets(fn):
    """Makes every message that mentions a stored logo carry that image automatically."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        embeds = list(kwargs.get("embeds") or [])
        if kwargs.get("embed") is not None:
            embeds.append(kwargs["embed"])
        if embeds:
            files = list(kwargs.get("files") or [])
            if kwargs.get("file") is not None:
                files.append(kwargs.pop("file"))
            for name, data in stored_assets(embeds, {f.filename for f in files}):
                files.append(discord.File(io.BytesIO(data), filename=name))
            if files:
                kwargs["files"] = files
        return await fn(self, *args, **kwargs)
    return wrapper

discord.abc.Messageable.send = _with_assets(discord.abc.Messageable.send)
discord.Webhook.send = _with_assets(discord.Webhook.send)
discord.InteractionResponse.send_message = _with_assets(discord.InteractionResponse.send_message)

def footer_text(team):
    return f"{team['tier']} | {team['name']} • {league_short()}" if team else league_short()

def tx_embed(title, desc, quote, team, color=PINK):
    e = discord.Embed(title=title, description=f"{desc}\n\n{quote}", color=color,
                      timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} Transactions", icon_url=LEAGUE_LOGO)
    if logo(team):
        e.set_thumbnail(url=logo(team))
    return e

async def post_tx(guild, embed, user_id, content=None, files=None):
    ch_id = get_setting("tx_channel")
    ch = guild.get_channel(int(ch_id)) if guild and ch_id else None
    if ch:
        await ch.send(content=content, embed=embed, view=profile_view(user_id), **({"files": files} if files else {}))
    return ch

def is_staff_member(m):
    p = m.guild_permissions
    if p.administrator or p.manage_guild or m.id == m.guild.owner_id:
        return True
    rid = get_setting("staff_role")
    return bool(rid) and any(r.id == int(rid) for r in m.roles)

async def _staff_pred(i: discord.Interaction):
    if i.guild and isinstance(i.user, discord.Member) and is_staff_member(i.user):
        return True
    raise app_commands.CheckFailure("not staff")

# Runtime check (not hidden by Discord), so admins and the staff role can always use these.
staff = app_commands.check(_staff_pred)

async def _admin_pred(i: discord.Interaction):
    if i.guild and isinstance(i.user, discord.Member):
        p = i.user.guild_permissions
        if p.administrator or p.manage_guild or i.user.id == i.guild.owner_id:
            return True
    raise app_commands.CheckFailure("not admin")

# Stricter than `staff`: Administrator, Manage Server or the server owner (the staff role is not enough).
admin_only = app_commands.check(_admin_pred)

# ---------------------------------------------------------------- setup / teams
@bot.tree.command(description="Set the channel where transactions are posted")
@staff
async def setup(i: discord.Interaction, channel: discord.TextChannel):
    set_setting("tx_channel", str(channel.id)); set_setting("guild_id", str(i.guild_id))
    await i.response.send_message(f"✅ Transactions will post in {channel.mention}", ephemeral=True)

LOGO_HELP = ("Use a permanent **direct image link** (ends in .png or .jpg). Upload the image to imgur.com or "
             "postimages.org, then right-click it and choose *Copy image address*. "
             "Discord attachment links expire, so they can't be used.")

async def check_logo(url):
    """Returns (ok, reason). Makes sure the link is a real, reachable image."""
    if not url.startswith(("http://", "https://")):
        return False, "it must start with http:// or https://"
    if "cdn.discordapp.com" in url or "media.discordapp.net" in url:
        return False, "Discord attachment links expire after a while"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(url, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0 PitchXBot"}) as r:
                if r.status != 200:
                    return False, f"the link returned HTTP {r.status}"
                ctype = r.headers.get("Content-Type", "")
                if ctype.startswith("image/svg"):
                    return False, "SVG images aren't supported, use PNG or JPG"
                if not ctype.startswith("image/"):
                    return False, f"that's a web page ({ctype or 'unknown type'}), not a direct image link"
                return True, None
    except Exception as ex:
        return False, f"couldn't reach it ({type(ex).__name__})"

async def read_logo_input(image, link):
    """Returns (png_bytes or None, error or None). Uploaded image first, link as a fallback. Stored forever."""
    if not image and not link:
        return None, None
    if Image is None:
        return None, "Pillow isn't installed on the host, so I can't store images. Add `Pillow` to requirements.txt."
    if image:
        if image.content_type and not image.content_type.startswith("image/"):
            return None, "That file isn't an image. Upload a PNG or JPG."
        if image.size > 8_000_000:
            return None, "That image is too big (max 8 MB)."
        raw = await image.read()
    else:
        raw = await fetch_logo(link)
        if not raw:
            return None, "Couldn't download an image from that link. Upload the image file instead."
    png = await asyncio.to_thread(normalize_logo, raw)
    if not png:
        return None, "I couldn't read that image. Use a PNG or JPG."
    return png, None

async def team_logo_bytes(team):
    """Team logo as image bytes: our stored copy, or (old teams) downloaded from the saved link."""
    if not team:
        return None
    if logo_blob(team):
        return logo_blob(team)
    u = team["logo"]
    return await fetch_logo(u) if u and u.startswith(("http://", "https://")) else None

async def migrate_logos():
    """Old teams that only have a logo LINK: download it once and keep our own copy (links can expire)."""
    rows = db.execute("SELECT id, logo FROM teams WHERE logo_blob IS NULL AND logo LIKE 'http%'").fetchall()
    for r in rows:
        data = await fetch_logo(r["logo"])
        png = await asyncio.to_thread(normalize_logo, data) if data and Image is not None else None
        if png:
            db.execute("UPDATE teams SET logo_blob=? WHERE id=?", (png, r["id"])); db.commit()
            print(f"[logos] stored a permanent copy of team {r['id']}'s logo")
        else:
            print(f"[logos] couldn't download team {r['id']}'s old logo link. Re-upload it with /team_logo")

@bot.tree.command(description="Add a team to a division (upload its logo)")
@staff
@app_commands.describe(division="The division the team plays in", image="Upload the team's logo (PNG or JPG)",
                       logo_url="...or paste an image link instead (I'll save my own copy)")
@app_commands.autocomplete(division=tier_ac)
async def team_add(i: discord.Interaction, name: str, division: str,
                   image: discord.Attachment = None, logo_url: str = None):
    await i.response.defer(ephemeral=True)
    d = db.execute("SELECT name FROM divisions WHERE name=?", (division,)).fetchone()
    if not d:
        return await i.followup.send("That division doesn't exist. An admin can create it with /make_division.")
    png, err = await read_logo_input(image, logo_url)
    if err:
        return await i.followup.send(f"❌ {err}")
    name = name.strip()[:60]
    try:
        db.execute("INSERT INTO teams(name,tier,logo_blob) VALUES(?,?,?)", (name, d["name"], png)); db.commit()
    except sqlite3.IntegrityError:
        return await i.followup.send("That team already exists.")
    t = get_team(name)
    e = discord.Embed(title=f"✅ Added {t['name']}", color=GREEN,
                      description=f"{t['tier']} • Budget {money(START_BUDGET)}"
                      + ("" if png else "\nNo logo yet. Add one with `/team_logo` (upload an image)."))
    role = find_team_role(i.guild, t["name"])
    e.description += (f"\nRole: {role.mention}" if role else
                      f"\n⚠️ No role containing **{t['name']}** exists yet. Create one (e.g. `NAPXR | {t['name']}`) and players get it automatically.")
    if logo(t):
        e.set_thumbnail(url=logo(t))
    await i.followup.send(embed=e)

@bot.tree.command(description="Set or change a team's logo (upload an image)")
@staff
@app_commands.describe(image="Upload the team's logo (PNG or JPG)", logo_url="...or paste an image link instead")
@app_commands.autocomplete(team=team_ac)
async def team_logo(i: discord.Interaction, team: str, image: discord.Attachment = None, logo_url: str = None):
    await i.response.defer(ephemeral=True)
    t = get_team(team)
    if not t:
        return await i.followup.send("Team not found.")
    png, err = await read_logo_input(image, logo_url)
    if err:
        return await i.followup.send(f"❌ {err}")
    if not png:
        return await i.followup.send("Upload an image in the `image` box (or paste a `logo_url`).")
    db.execute("UPDATE teams SET logo_blob=?, logo=NULL WHERE id=?", (png, t["id"])); db.commit()
    t = team_by_id(t["id"])
    e = discord.Embed(title=f"✅ Logo updated for {t['name']}", color=GREEN,
                      description="Saved permanently. This is how it will look on embeds.")
    e.set_thumbnail(url=logo(t))
    await i.followup.send(embed=e)

@bot.tree.command(description="Remove a team")
@staff
@app_commands.autocomplete(team=team_ac)
async def team_remove(i: discord.Interaction, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("UPDATE players SET team_id=NULL WHERE team_id=?", (t["id"],))
    db.execute("DELETE FROM fixtures WHERE home_id=? OR away_id=?", (t["id"], t["id"]))
    db.execute("DELETE FROM managers WHERE team_id=?", (t["id"],))
    db.execute("DELETE FROM teams WHERE id=?", (t["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Removed **{team}**", ephemeral=True)

@bot.tree.command(description="List all teams")
async def teams(i: discord.Interaction):
    rows = db.execute("""SELECT t.name, t.tier, COUNT(p.user_id) n FROM teams t
                         LEFT JOIN players p ON p.team_id=t.id GROUP BY t.id ORDER BY t.name""").fetchall()
    if not rows:
        return await i.response.send_message("No teams yet.", ephemeral=True)
    e = discord.Embed(title=f"{LEAGUE_NAME} Teams", color=PINK,
                      description="\n".join(f"**{r['name']}** • {r['tier']} • {r['n']} players" for r in rows))
    await i.response.send_message(embed=e)

@bot.tree.command(description="Show a team's roster with player values")
@app_commands.autocomplete(team=team_ac)
async def roster(i: discord.Interaction, team: str):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    ps = sorted(((p["user_id"], player_value(p["user_id"])) for p in
                 db.execute("SELECT user_id FROM players WHERE team_id=?", (t["id"],)).fetchall()),
                key=lambda x: -x[1])
    e = discord.Embed(title=f"{t['name']} Roster", color=PINK,
                      description="\n".join(f"<@{u}> • {money(v)}" for u, v in ps) or "No players signed.")
    e.set_footer(text=f"Budget {money(budget_of(t))} • Squad value {money(sum(v for _, v in ps))}")
    if logo(t): e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

def signed_ts(uid):
    r = db.execute("SELECT signed_at FROM players WHERE user_id=?", (uid,)).fetchone()
    try:
        return dt.datetime.strptime(r["signed_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).timestamp()
    except (TypeError, ValueError):
        return None

def trophy_cabinet(uid):
    """A player's trophies grouped by type: [{'name', 'logo' (png bytes or None), 'n'}]"""
    rows = db.execute("""SELECT t.title, t.trophy_id, tt.name, tt.logo FROM trophies t
                         LEFT JOIN trophy_types tt ON tt.id=t.trophy_id
                         WHERE t.user_id=? ORDER BY t.id DESC""", (uid,)).fetchall()
    grouped = {}
    for r in rows:
        key = r["trophy_id"] or r["title"].lower()
        g = grouped.setdefault(key, {"name": r["name"] or r["title"], "logo": r["logo"], "n": 0})
        g["n"] += 1
    return list(grouped.values())

def profile_embed(member, cabinet):
    uid = member.id
    t = player_team(uid)
    g, a, c = player_totals(uid)
    pts = rank_points(g, a, c)
    e = discord.Embed(title=f"⚽ {member.display_name} · {league_short()} Player Card", color=PINK,
                      description=f"**{rank_line(pts)}**")
    e.add_field(name="Team", value=t["name"] if t else "Free Agent")
    e.add_field(name="Transfer value", value=money(player_value(uid)))
    e.add_field(name="Rank points", value=str(pts))
    e.add_field(name="⚽ Goals", value=str(g))
    e.add_field(name="🎯 Assists", value=str(a))
    e.add_field(name="🧤 Clean sheets", value=str(c))
    e.add_field(name="📈 Contributions", value=str(g + a))
    e.add_field(name="🏟️ Tier", value=t["tier"] if t else "—")
    since = signed_ts(uid) if t else None
    e.add_field(name="📝 With club since", value=tsf(since, "D") if since else "—")
    lines = [f"🏆 {x['name']}" + (f" ×{x['n']}" if x["n"] > 1 else "") for x in cabinet[:10]]
    e.add_field(name="🏆 Player trophy cabinet", inline=False, value="\n".join(lines) or "No trophies yet")
    e.set_footer(text=f"{league_short()} · Every stat moves you closer to the next rank")
    e.set_thumbnail(url=logo(t) or member.display_avatar.url)
    return e

def normalize_logo(data):
    """Any readable image -> PNG, max 256x256 (so it can be stored and never expires)."""
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((256, 256))
        buf = io.BytesIO()
        im.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None

def render_cabinet(items):
    """A shelf of the player's trophy logos with names (and ×N when won more than once)."""
    items = items[:10]
    per_row, SW, SH = 5, 150, 175
    rows_n = (len(items) + per_row - 1) // per_row
    W = SW * min(len(items), per_row)
    img = Image.new("RGB", (W, rows_n * SH + 10), (30, 31, 34))
    d = ImageDraw.Draw(img)
    f_name, f_badge, f_star = _font(20, True), _font(18, True), _font(54, True)
    for idx, it in enumerate(items):
        r, c = divmod(idx, per_row)
        x0, y0 = c * SW, r * SH + 5
        cx, drawn, box = x0 + SW // 2, False, 110
        if it["logo"]:
            try:
                im = Image.open(io.BytesIO(it["logo"])).convert("RGBA")
                im.thumbnail((box, box))
                img.paste(im, (cx - im.width // 2, y0 + 8 + (box - im.height) // 2), im)
                drawn = True
            except Exception:
                pass
        if not drawn:
            d.ellipse((cx - 45, y0 + 13, cx + 45, y0 + 103), fill=(250, 204, 21))
            d.polygon([(cx + (34 if k % 2 == 0 else 14) * math.cos(-math.pi / 2 + k * math.pi / 5),
                       y0 + 58 + (34 if k % 2 == 0 else 14) * math.sin(-math.pi / 2 + k * math.pi / 5)) for k in range(10)],
                      fill=(30, 31, 34))
        name = ascii_img(it["name"])
        if d.textlength(name, font=f_name) > SW - 10:
            while len(name) > 3 and d.textlength(name + "...", font=f_name) > SW - 10:
                name = name[:-1]
            name = name.rstrip() + "..."
        _put(d, (cx, y0 + 138), name, f_name, (255, 255, 255), "mm")
        if it["n"] > 1:
            bx, by = cx + 34, y0 + 8
            d.ellipse((bx, by, bx + 42, by + 42), fill=(88, 101, 242))
            _put(d, (bx + 21, by + 21), f"x{it['n']}", f_badge, (255, 255, 255), "mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

def trophy_file(tt, name="trophy.png"):
    return discord.File(io.BytesIO(tt["logo"]), filename=name) if tt["logo"] else None

async def trophy_ac(_, current: str):
    rows = db.execute("SELECT name FROM trophy_types WHERE name LIKE ? ORDER BY name LIMIT 25", (f"%{current}%",)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

@bot.tree.command(description="View a player card: team, value, goals, assists, clean sheets, trophies")
async def profile(i: discord.Interaction, player: discord.Member = None, private: bool = False):
    await i.response.defer(ephemeral=private)
    member = player or i.user
    cabinet = trophy_cabinet(member.id)
    e = profile_embed(member, cabinet)
    kw = {}
    if cabinet and Image is not None:
        png = await run_render(render_cabinet, cabinet)
        kw["file"] = discord.File(io.BytesIO(png), filename="cabinet.png")
        e.set_image(url="attachment://cabinet.png")     # shows right under the trophy cabinet
    await i.followup.send(embed=e, **kw)

@bot.tree.command(description="Create a trophy with a name and logo (admins only)")
@admin_only
@app_commands.describe(name="Trophy name, e.g. Ballon d'Or", image="Upload the trophy image",
                       logo_url="...or paste a direct image link instead")
async def make_trophy(i: discord.Interaction, name: str, image: discord.Attachment = None, logo_url: str = None):
    await i.response.defer(ephemeral=True)
    if Image is None:
        return await i.followup.send("❌ Pillow isn't installed on the host. Add `Pillow` to requirements.txt.")
    name = name.strip()[:60]
    if not name:
        return await i.followup.send("Give the trophy a name.")
    if db.execute("SELECT 1 FROM trophy_types WHERE name=?", (name,)).fetchone():
        return await i.followup.send(f"A trophy called **{name}** already exists.")
    if image:
        if image.content_type and not image.content_type.startswith("image/"):
            return await i.followup.send("That file isn't an image. Upload a PNG or JPG.")
        if image.size > 8_000_000:
            return await i.followup.send("That image is too big (max 8 MB).")
        raw = await image.read()
    elif logo_url:
        raw = await fetch_logo(logo_url)
        if not raw:
            return await i.followup.send("❌ Couldn't download an image from that link. Upload the image instead.")
    else:
        return await i.followup.send("Upload an image (the `image` box) or paste a `logo_url`.")
    png = await asyncio.to_thread(normalize_logo, raw)
    if not png:
        return await i.followup.send("❌ I couldn't read that image. Use a PNG or JPG.")
    cur = db.execute("INSERT INTO trophy_types(name, logo) VALUES(?,?)", (name, png))
    linked = db.execute("UPDATE trophies SET trophy_id=? WHERE trophy_id IS NULL AND lower(title)=lower(?)",
                        (cur.lastrowid, name)).rowcount
    db.commit()
    e = discord.Embed(title=f"🏆 Trophy created: {name}", color=YELLOW,
                      description="Award it with `/trophy_give`. Winners see it in their profile's trophy cabinet."
                      + (f"\nLinked **{linked}** existing award(s) with this name." if linked else ""))
    e.set_thumbnail(url="attachment://trophy.png")
    await i.followup.send(embed=e, file=discord.File(io.BytesIO(png), filename="trophy.png"))

@bot.tree.command(description="Award a trophy to a player")
@staff
@app_commands.autocomplete(trophy=trophy_ac)
async def trophy_give(i: discord.Interaction, player: discord.Member, trophy: str):
    await i.response.defer(ephemeral=True)
    tt = db.execute("SELECT * FROM trophy_types WHERE name=?", (trophy,)).fetchone()
    if not tt:
        return await i.followup.send("That trophy doesn't exist. An admin can create it with /make_trophy.")
    db.execute("INSERT INTO trophies(user_id,title,trophy_id) VALUES(?,?,?)", (player.id, tt["name"], tt["id"])); db.commit()
    n = db.execute("SELECT COUNT(*) FROM trophies WHERE user_id=? AND trophy_id=?", (player.id, tt["id"])).fetchone()[0]
    e = discord.Embed(title=f"🏆 {tt['name']}", color=YELLOW, timestamp=discord.utils.utcnow(),
                      description=f"{player.mention} has been awarded **{tt['name']}**!" + (f" (×{n})" if n > 1 else ""))
    e.set_author(name=f"{league_short()} Trophies", icon_url=LEAGUE_LOGO)
    if tt["logo"]:
        e.set_thumbnail(url="attachment://trophy.png")
    kw = {"file": trophy_file(tt)} if tt["logo"] else {}
    await i.followup.send(embed=e, **kw)
    guild = get_guild() or i.guild
    ch = chan(guild, "results_channel") or chan(guild, "matchday_channel")
    if ch:
        try:
            kw = {"file": trophy_file(tt)} if tt["logo"] else {}
            await ch.send(content=player.mention, embed=e, **kw)
        except Exception as ex:
            print(f"[trophy] announce failed: {ex!r}")

@bot.tree.command(description="Remove one trophy award from a player")
@staff
@app_commands.autocomplete(trophy=trophy_ac)
async def trophy_remove(i: discord.Interaction, player: discord.Member, trophy: str):
    r = db.execute("SELECT id FROM trophies WHERE user_id=? AND lower(title)=lower(?) ORDER BY id DESC LIMIT 1",
                   (player.id, trophy)).fetchone()
    if not r:
        return await i.response.send_message("That player doesn't have that trophy.", ephemeral=True)
    db.execute("DELETE FROM trophies WHERE id=?", (r["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Removed **{trophy}** from {player.mention}'s cabinet.", ephemeral=True)

@bot.tree.command(description="Delete a trophy (admins only). Awards already given stay as text")
@admin_only
@app_commands.autocomplete(trophy=trophy_ac)
async def trophy_delete(i: discord.Interaction, trophy: str):
    tt = db.execute("SELECT id FROM trophy_types WHERE name=?", (trophy,)).fetchone()
    if not tt:
        return await i.response.send_message("Trophy not found.", ephemeral=True)
    db.execute("UPDATE trophies SET trophy_id=NULL WHERE trophy_id=?", (tt["id"],))
    db.execute("DELETE FROM trophy_types WHERE id=?", (tt["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Deleted trophy **{trophy}**.", ephemeral=True)

@bot.tree.command(description="List the league's trophies")
async def trophies(i: discord.Interaction):
    rows = db.execute("""SELECT tt.*, (SELECT COUNT(*) FROM trophies t WHERE t.trophy_id=tt.id) n
                         FROM trophy_types tt ORDER BY name LIMIT 10""").fetchall()
    if not rows:
        return await i.response.send_message("No trophies yet. Admins can create one with /make_trophy.", ephemeral=True)
    embeds, files = [], []
    for r in rows:
        fn = f"trophy_{r['id']}.png"
        e = discord.Embed(title=f"🏆 {r['name']}", color=YELLOW, description=f"Awarded **{r['n']}** time(s)")
        if r["logo"]:
            e.set_thumbnail(url=f"attachment://{fn}")
            files.append(trophy_file(r, fn))
        embeds.append(e)
    await i.response.send_message(embeds=embeds, files=files)

def team_money(tid):
    q = "SELECT COALESCE(SUM(fee),0) FROM transfers WHERE {}=? AND kind IN ('transfer','loan')"
    return (db.execute(q.format("to_team_id"), (tid,)).fetchone()[0],
            db.execute(q.format("from_team_id"), (tid,)).fetchone()[0])

def deal_lines(tid, bought=True, limit=5):
    me_col, other_col = ("to_team_id", "from_team_id") if bought else ("from_team_id", "to_team_id")
    kinds = "('sign','transfer','loan')" if bought else "('transfer','loan')"
    rows = db.execute(f"SELECT * FROM transfers WHERE {me_col}=? AND kind IN {kinds} ORDER BY id DESC LIMIT ?",
                      (tid, limit)).fetchall()
    out = []
    for r in rows:
        if r["kind"] == "sign":
            out.append(f"• <@{r['user_id']}>: signed as a free agent")
            continue
        other = name_of(r[other_col]) if r[other_col] else "free agent"
        tag = " (loan)" if r["kind"] == "loan" else ""
        out.append(f"• <@{r['user_id']}>: **{money(r['fee'])}** {'from' if bought else 'to'} {other}{tag}")
    return "\n".join(out)

@bot.tree.command(description="Team budget, spending, who they bought and squad value (defaults to your team)")
@app_commands.autocomplete(team=team_ac)
async def budget(i: discord.Interaction, team: str = None):
    if team:
        t = get_team(team)
        if not t:
            return await i.response.send_message("Team not found.", ephemeral=True)
        rows = [t]
    else:
        mine = player_team(i.user.id)
        rows = [mine] if mine else db.execute("SELECT * FROM teams ORDER BY name").fetchall()
    if not rows:
        return await i.response.send_message("No teams yet.", ephemeral=True)
    squad_of = lambda t: sum(player_value(p["user_id"]) for p in
                             db.execute("SELECT user_id FROM players WHERE team_id=?", (t["id"],)).fetchall())
    if len(rows) > 1:
        lines = []
        for t in rows:
            lines.append(f"**{t['name']}**: 💰 {money(budget_of(t))} • spent {money(team_money(t['id'])[0])} • squad {money(squad_of(t))}")
        return await i.response.send_message(embed=discord.Embed(title="Team Budgets", color=PINK,
                                                                 description="\n".join(lines)[:4000]))
    t = rows[0]
    spent, got = team_money(t["id"])
    e = discord.Embed(title=f"{t['name']} • Budget", color=PINK)
    e.add_field(name="💰 Budget left", value=money(budget_of(t)))
    e.add_field(name="📉 Spent on transfers", value=money(spent))
    e.add_field(name="📈 Received from sales", value=money(got))
    e.add_field(name="👥 Squad value", value=money(squad_of(t)))
    e.add_field(name="🛒 Bought / signed (latest)", inline=False, value=deal_lines(t["id"], True) or "No signings yet")
    sold = deal_lines(t["id"], False)
    if sold:
        e.add_field(name="📤 Sold (latest)", inline=False, value=sold)
    if logo(t):
        e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

@bot.tree.command(description="Set a team's budget (e.g. 100m)")
@staff
@app_commands.autocomplete(team=team_ac)
async def budget_set(i: discord.Interaction, team: str, amount: str):
    t, v = get_team(team), parse_money(amount)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    if v is None:
        return await i.response.send_message("Couldn't read that amount. Try `100000000`, `100m` or `750k`.", ephemeral=True)
    db.execute("UPDATE teams SET budget=? WHERE id=?", (v, t["id"])); db.commit()
    await i.response.send_message(f"✅ **{t['name']}** budget set to {money(v)}.", ephemeral=True)

# ---------------------------------------------------------------- offers (player must accept)
def offer_embed(kind, team, from_team, fee=0):
    tail = f"\n\n⏳ This offer expires in {OFFER_HOURS} hours."
    fee_line = f"\n> 💰 **Fee:** {money(fee)}" if fee else ""
    if kind == "sign":
        title, desc = "📝 You've Got a Contract Offer!", f"**{team['name']}** wants to sign you."
        quote = f"> 🏟️ **Club:** {team['name']}\n> 📋 **Status:** Awaiting your answer"
        ask, logo_team, foot = "Do you want to sign?", team, team
    elif kind == "transfer":
        title, desc = "🔁 You've Got a Transfer Offer!", f"**{team['name']}** wants to bring you in."
        quote = f"> 🔁 **From:** {from_team['name']}\n> 🏟️ **To:** {team['name']}{fee_line}"
        ask, logo_team, foot = "Do you want to transfer?", team, team
    elif kind == "loan":
        title, desc = "🔄 You've Got a Loan Offer!", f"**{team['name']}** wants to take you on loan."
        quote = f"> 🔁 **From:** {from_team['name']}\n> 🏟️ **Loan Club:** {team['name']}{fee_line}"
        ask, logo_team, foot = "Do you want to go on loan?", team, team
    else:  # release
        title, desc = "📤 Release Request", f"**{from_team['name']}** wants to release you."
        quote = f"> 🏟️ **Club:** {from_team['name']}\n> 📋 **Status after:** Free Agent"
        ask, logo_team, foot = "Do you accept being released?", from_team, from_team
    e = discord.Embed(title=title, description=f"{desc}\n\n{quote}\n\n**{ask}**{tail}",
                      color=PINK, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} Transactions", icon_url=LEAGUE_LOGO)
    e.set_footer(text=footer_text(foot))
    if logo(logo_team):
        e.set_thumbnail(url=logo(logo_team))
    return e

class OfferButton(discord.ui.DynamicItem[discord.ui.Button],
                  template=r"pitchx:offer:(?P<action>accept|decline):(?P<id>[0-9]+)"):
    def __init__(self, action: str, offer_id: int):
        ok = action == "accept"
        super().__init__(discord.ui.Button(
            label="Accept" if ok else "Decline", emoji="✅" if ok else "❌",
            style=discord.ButtonStyle.success if ok else discord.ButtonStyle.danger,
            custom_id=f"pitchx:offer:{action}:{offer_id}"))
        self.action, self.offer_id = action, int(offer_id)

    @classmethod
    async def from_custom_id(cls, interaction, item, match, /):
        return cls(match["action"], int(match["id"]))

    async def callback(self, interaction: discord.Interaction):
        await handle_offer(interaction, self.offer_id, self.action == "accept")

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = f"⚠️ Something went wrong: {type(error).__name__}: {str(error)[:200]}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

def offer_view(offer_id):
    v = discord.ui.View(timeout=None)
    v.add_item(OfferButton("accept", offer_id))
    v.add_item(OfferButton("decline", offer_id))
    return v

def apply_offer(o):
    """Carry out an accepted offer. Returns (embed for the player, embed for the public channel)."""
    new, old = team_by_id(o["team_id"]), team_by_id(o["from_team_id"])
    uid, kind, fee = o["user_id"], o["kind"], int(o["fee"] or 0)
    mention = f"<@{uid}>"
    if kind == "release":
        db.execute("UPDATE players SET team_id=NULL, value_bonus=0 WHERE user_id=?", (uid,))
        embed = tx_embed("You've Been Released", f"You have been released from **{old['name']}**.",
                         f"> 🏟️ **Former Club:** {old['name']}\n> 📋 **Status:** Free Agent\n\n"
                         "You are now free to sign with any team.", old)
        public = tx_embed("Player Released", f"{mention} was released by **{old['name']}**.",
                          f"> 🏟️ **Former Club:** {old['name']}\n> 📋 **Status:** Free Agent", old)
        team_id = old["id"]
    else:
        if kind == "transfer":
            bonus = max(0, fee - value_from(*player_totals(uid)))   # the price paid becomes his market value
        elif kind == "loan":
            bonus = player_bonus(uid)
        else:
            bonus = SIGN_VALUE_BONUS            # signing for a club raises a free agent's value
        db.execute("INSERT OR REPLACE INTO players(user_id,team_id,signed_at,value_bonus) VALUES(?,?,datetime('now'),?)",
                   (uid, new["id"], bonus))
        team_id = new["id"]
        if kind in ("transfer", "loan") and fee:
            db.execute("UPDATE teams SET budget=budget-? WHERE id=?", (fee, new["id"]))   # buyer pays
            db.execute("UPDATE teams SET budget=budget+? WHERE id=?", (fee, old["id"]))   # seller is paid
        db.execute("INSERT INTO transfers(kind,user_id,from_team_id,to_team_id,fee) VALUES(?,?,?,?,?)",
                   (kind, uid, old["id"] if old else None, new["id"], fee))
        left = budget_of(team_by_id(new["id"]))
        fee_line = f"\n> 💰 **Fee:** {money(fee)}" if fee else ""
        if kind == "sign":
            embed = tx_embed("You've Been Signed!", f"You have signed a contract with **{new['name']}**!",
                             f"> 🏟️ **New Club:** {new['name']}\n\nWelcome to the team! ⚽", new)
            public = tx_embed("✍️ OFFICIAL: New Signing", f"**{new['name']}** signed {mention}.",
                              f"> 🏟️ **Club:** {new['name']}\n> 📋 **From:** Free Agent\n> 📈 **New market value:** {money(player_value(uid))}", new)
        elif kind == "transfer":
            embed = tx_embed("Transfer Complete", f"You have been transferred to **{new['name']}**!",
                             f"> 🔁 **From:** {old['name']}\n> 🏟️ **To:** {new['name']}{fee_line}", new, GREEN)
            public = tx_embed("🚨 HERE WE GO!", f"**{new['name']}** bought {mention} from **{old['name']}**.",
                              f"> 💰 **Fee:** {money(fee)}\n> 🏦 **{new['name']} budget left:** {money(left)}\n> 📈 **New market value:** {money(player_value(uid))}", new, GREEN)
        else:
            embed = tx_embed("Loan Complete", f"You are now on loan at **{new['name']}**!",
                             f"> 🔁 **From:** {old['name']}\n> 🏟️ **Loan Club:** {new['name']}{fee_line}", new, GREEN)
            public = tx_embed("🔄 Loan Complete", f"**{new['name']}** took {mention} on loan from **{old['name']}**.",
                              f"> 🔁 **From:** {old['name']}\n> 🏟️ **Loan Club:** {new['name']}{fee_line}", new, GREEN)
    db.execute("INSERT INTO events(kind,user_id,team_id) VALUES(?,?,?)", (kind, uid, team_id))
    db.execute("UPDATE offers SET status='accepted' WHERE id=?", (o["id"],))
    db.commit()
    return embed, public

_last_image_error = ""

async def announce_deal(guild, o, public, png):
    """Post a completed deal. Returns (channel or None, note for staff).
    Tries with the image first; if Discord refuses (usually a missing Attach Files permission) it posts the text version."""
    ch = chan(guild, "tx_channel")
    where_note = ""
    if not ch and guild and o["channel_id"]:
        ch = guild.get_channel(int(o["channel_id"]))
        where_note = "ℹ️ No transactions channel is set (/setchannel Transactions), so I posted where the offer was made."
    if not ch:
        return None, "⚠️ Nothing was posted publicly: no transactions channel is set. Use /setchannel Transactions."
    mention, view = f"<@{o['user_id']}>", profile_view(o["user_id"])
    img_note = ""
    if png:
        try:
            await asyncio.wait_for(ch.send(content=mention, embed=public, view=view,
                                           files=[discord.File(io.BytesIO(png), filename="herewego.jpg")]), timeout=25)
            return ch, where_note
        except Exception as ex:
            print(f"[deal] posting with the image failed: {ex!r}")
            public.set_image(url=None)
            img_note = f"⚠️ I posted without the image in {ch.mention} ({type(ex).__name__}). Give me the **Attach Files** permission there."
    try:
        await asyncio.wait_for(ch.send(content=mention, embed=public, view=view), timeout=20)
        return ch, (img_note or where_note)
    except Exception as ex:
        print(f"[deal] posting failed: {ex!r}")
        return None, (f"⚠️ I can't post in {ch.mention} ({type(ex).__name__}). Give me View Channel, Send Messages, "
                      "Embed Links and Attach Files there.")

async def handle_offer(i: discord.Interaction, offer_id: int, accept: bool):
    o = db.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
    if not o:
        return await i.response.send_message("This offer no longer exists.", ephemeral=True)
    if i.user.id != o["user_id"]:
        return await i.response.send_message("This offer isn't for you.", ephemeral=True)
    if o["status"] != "pending":
        return await i.response.send_message("This offer was already answered or cancelled.", ephemeral=True)

    emb = i.message.embeds[0].copy() if i.message.embeds else discord.Embed()
    if time.time() - o["created_at"] > OFFER_HOURS * 3600:
        db.execute("UPDATE offers SET status='expired' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text="⌛ This offer expired")
        return await i.response.edit_message(embed=emb, view=None)

    # make sure nothing changed since the offer was sent
    cur = player_team(o["user_id"])
    ok = True
    if o["kind"] == "sign":
        ok = cur is None and team_by_id(o["team_id"])
    elif o["kind"] in ("transfer", "loan"):
        ok = cur and cur["id"] == o["from_team_id"] and team_by_id(o["team_id"])
    else:
        ok = cur and cur["id"] == o["from_team_id"]
    why = "⚠️ This offer is no longer valid"
    if ok and o["kind"] in ("transfer", "loan") and (o["fee"] or 0) > 0:
        if budget_of(team_by_id(o["team_id"])) < o["fee"]:
            ok, why = False, "⚠️ The club can't afford this fee anymore"
    if accept and not ok:
        db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text=why)
        return await i.response.edit_message(embed=emb, view=None)

    guild = bot.get_guild(o["guild_id"])
    staff_user = None
    try:
        staff_user = await bot.fetch_user(o["staff_id"])
    except discord.HTTPException:
        pass

    if not accept:
        db.execute("UPDATE offers SET status='declined' WHERE id=?", (offer_id,)); db.commit()
        emb.color = discord.Color(RED); emb.set_footer(text="❌ You declined this offer")
        await i.response.edit_message(embed=emb, view=None)
        if staff_user:
            try: await staff_user.send(f"❌ <@{o['user_id']}> **declined** the {o['kind']} offer.")
            except discord.HTTPException: pass
        return

    result, public = apply_offer(o)
    emb.color = discord.Color(GREEN); emb.set_footer(text="✅ You accepted this offer")
    await i.response.edit_message(embed=emb, view=None)
    kind = o["kind"]
    new, old = team_by_id(o["team_id"]), team_by_id(o["from_team_id"])
    notes, png = [], None
    if kind in ("sign", "transfer", "loan"):
        png = await make_here_we_go(kind, i.user, new, old, int(o["fee"] or 0))
        if png:
            public.set_image(url="attachment://herewego.jpg")
        else:
            notes.append(f"⚠️ The image couldn't be drawn ({_last_image_error or 'Pillow missing?'}). The text post still went out.")
    try:
        role_note = await apply_team_roles(guild, o["user_id"], old, new)
    except Exception as ex:
        print(f"[roles] failed: {ex!r}")
        role_note = ""
    try:
        ch, deal_note = await announce_deal(guild, o, public, png)
    except Exception as ex:
        print(f"[deal] announce failed: {ex!r}")
        ch, deal_note = None, f"⚠️ The announcement failed ({type(ex).__name__})."
    notes += [n for n in (deal_note, role_note) if n]
    try:
        if not ch or i.channel_id != ch.id:
            await i.followup.send(embed=result, view=profile_view(o["user_id"]))
    except Exception as ex:
        print(f"[deal] player confirmation failed: {ex!r}")
    if staff_user:
        try: await staff_user.send(f"✅ <@{o['user_id']}> **accepted** the {kind} offer." + ("\n" + "\n".join(notes) if notes else ""))
        except discord.HTTPException: pass

async def send_offer(i: discord.Interaction, player: discord.Member, kind: str, team, from_team, fee=0):
    db.execute("UPDATE offers SET status='cancelled' WHERE user_id=? AND status='pending'", (player.id,))
    cur = db.execute("""INSERT INTO offers(user_id,kind,team_id,from_team_id,staff_id,guild_id,created_at,fee,channel_id)
                        VALUES(?,?,?,?,?,?,?,?,?)""",
                     (player.id, kind, team["id"] if team else None,
                      from_team["id"] if from_team else None, i.user.id, i.guild_id, time.time(), fee, i.channel_id))
    db.commit()
    oid = cur.lastrowid
    embed, view = offer_embed(kind, team, from_team, fee), offer_view(oid)
    tname = team["name"] if team else ""
    fname = from_team["name"] if from_team else ""
    fee_txt = f" for **{money(fee)}**" if fee else ""
    what = {"sign": f"sign with **{tname}**", "transfer": f"transfer to **{tname}**{fee_txt}",
            "loan": f"go on loan to **{tname}**{fee_txt}", "release": f"be released from **{fname}**"}[kind]

    try:
        await asyncio.wait_for(player.send(embed=embed, view=view), timeout=15)
        return await i.followup.send(f"📨 Offer sent to {player.mention} to {what}. Waiting for their answer.")
    except Exception as ex:
        print(f"[offer] DM to {player.id} failed: {ex!r}")

    reason = "no transactions channel is set (use /setup)"
    ch_id = get_setting("tx_channel")
    ch = i.guild.get_channel(int(ch_id)) if ch_id else None
    if ch:
        try:
            await asyncio.wait_for(ch.send(content=player.mention, embed=embed, view=view), timeout=15)
            return await i.followup.send(
                f"📨 Offer for {player.mention} to {what} (couldn't DM them, posted in {ch.mention})")
        except Exception as ex:
            print(f"[offer] channel post failed: {ex!r}")
            reason = f"I can't post in {ch.mention} ({type(ex).__name__}). Check my channel permissions"
    db.execute("UPDATE offers SET status='cancelled' WHERE id=?", (oid,)); db.commit()
    await i.followup.send(f"❌ Couldn't DM {player.mention} and {reason}.")

def managed_team_ids(user_id):
    return [r["team_id"] for r in db.execute("SELECT team_id FROM managers WHERE user_id=?", (user_id,)).fetchall()]

async def _staff_or_manager_pred(i: discord.Interaction):
    if i.guild and isinstance(i.user, discord.Member) and (is_staff_member(i.user) or managed_team_ids(i.user.id)):
        return True
    raise app_commands.CheckFailure("not staff or manager")

# Staff can act for any team; team managers can act for the team(s) they manage.
staff_or_manager = app_commands.check(_staff_or_manager_pred)

def resolve_team(i, name):
    """Returns (team, error). Staff may pick any team; managers only teams they manage."""
    staff_ok = is_staff_member(i.user)
    mine = managed_team_ids(i.user.id)
    if name:
        t = get_team(name)
        if not t:
            return None, "Team not found."
        if not staff_ok and t["id"] not in mine:
            return None, "You can only do this for a team you manage."
        return t, None
    if len(mine) == 1:
        return team_by_id(mine[0]), None
    if mine:
        return None, "You manage more than one team. Pick which one."
    return None, "Pick a team."

async def my_team_ac(i: discord.Interaction, current: str):
    like = f"%{current}%"
    if isinstance(i.user, discord.Member) and is_staff_member(i.user):
        rows = db.execute("SELECT name FROM teams WHERE name LIKE ? LIMIT 25", (like,)).fetchall()
    else:
        rows = db.execute("""SELECT t.name FROM teams t JOIN managers m ON m.team_id=t.id
                             WHERE m.user_id=? AND t.name LIKE ? LIMIT 25""", (i.user.id, like)).fetchall()
    return [app_commands.Choice(name=r["name"], value=r["name"]) for r in rows]

@bot.tree.command(description="Make someone a team's manager (they can sign/transfer/loan for it)")
@staff
@app_commands.autocomplete(team=team_ac)
async def manager_add(i: discord.Interaction, team: str, user: discord.Member):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("INSERT OR IGNORE INTO managers VALUES(?,?)", (t["id"], user.id)); db.commit()
    await i.response.send_message(f"✅ {user.mention} can now sign, transfer and loan players for **{t['name']}**. Tip: they can open `/dashboard`.", ephemeral=True)

@bot.tree.command(description="Remove a team manager")
@staff
@app_commands.autocomplete(team=team_ac)
async def manager_remove(i: discord.Interaction, team: str, user: discord.Member):
    t = get_team(team)
    if not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    db.execute("DELETE FROM managers WHERE team_id=? AND user_id=?", (t["id"], user.id)); db.commit()
    await i.response.send_message(f"✅ {user.mention} is no longer a manager of **{t['name']}**.", ephemeral=True)

@bot.tree.command(description="List team managers")
@app_commands.autocomplete(team=team_ac)
async def managers(i: discord.Interaction, team: str = None):
    t = get_team(team) if team else None
    if team and not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    q = "SELECT team_id, user_id FROM managers" + (" WHERE team_id=?" if t else "") + " ORDER BY team_id"
    rows = db.execute(q, (t["id"],) if t else ()).fetchall()
    desc = "\n".join(f"**{name_of(r['team_id'])}**: <@{r['user_id']}>" for r in rows) or "No managers set. Use /manager_add."
    await i.response.send_message(embed=discord.Embed(title="Team Managers", color=PINK, description=desc))

async def offer_sign(i, player, t):
    """Checks + sends a signing offer. `i` must already be deferred (it answers with followups)."""
    if player.bot:
        return await i.followup.send("You can't sign a bot.")
    if player_team(player.id):
        return await i.followup.send("That player already has a team. Use /transfer to bring them in.")
    await send_offer(i, player, "sign", t, None)

async def offer_transfer(i, player, new, fee):
    cur = player_team(player.id)
    if player.bot:
        return await i.followup.send("You can't transfer a bot.")
    if not cur:
        return await i.followup.send("That player is a free agent. Use /sign.")
    if cur["id"] == new["id"]:
        return await i.followup.send("They're already on that team.")
    amount = parse_money(fee)
    if amount is None:
        return await i.followup.send("Couldn't read that fee. Try `5000000`, `5m` or `750k`.")
    left = budget_of(new)
    if amount > left:
        return await i.followup.send(f"❌ **{new['name']}** only has {money(left)} left. You offered {money(amount)}.")
    value = player_value(player.id)
    minimum = int(value * MIN_FEE_PERCENT / 100)
    if amount < minimum:
        return await i.followup.send(f"❌ The minimum fee for {player.mention} is {money(minimum)} (their market value is {money(value)}).")
    await send_offer(i, player, "transfer", new, cur, amount)

async def offer_loan(i, player, new, fee):
    cur = player_team(player.id)
    if player.bot:
        return await i.followup.send("You can't loan a bot.")
    if not cur or cur["id"] == new["id"]:
        return await i.followup.send("Player needs a different current team to be loaned out.")
    amount = parse_money(fee or "0")
    if amount is None:
        return await i.followup.send("Couldn't read that fee. Try `500k` or leave it empty.")
    if amount > budget_of(new):
        return await i.followup.send(f"❌ **{new['name']}** only has {money(budget_of(new))} left.")
    await send_offer(i, player, "loan", new, cur, amount)

@bot.tree.command(description="Offer a free agent a contract for your team (they must accept)")
@staff_or_manager
@app_commands.autocomplete(team=my_team_ac)
async def sign(i: discord.Interaction, player: discord.Member, team: str = None):
    await i.response.defer(ephemeral=True)
    t, err = resolve_team(i, team)
    if err:
        return await i.followup.send(err)
    await offer_sign(i, player, t)

@bot.tree.command(description="Offer a player a paid transfer to your team (they must accept)")
@staff_or_manager
@app_commands.describe(fee="What you'll pay their club, e.g. 5000000, 5m or 750k",
                       to_team="Your team (optional if you manage just one)")
@app_commands.autocomplete(to_team=my_team_ac)
async def transfer(i: discord.Interaction, player: discord.Member, fee: str, to_team: str = None):
    await i.response.defer(ephemeral=True)
    new, err = resolve_team(i, to_team)
    if err:
        return await i.followup.send(err)
    await offer_transfer(i, player, new, fee)

@bot.tree.command(description="Offer a player a loan move to your team (they must accept)")
@staff_or_manager
@app_commands.describe(to_team="Your team (optional if you manage just one)",
                       fee="Optional loan fee paid to their club, e.g. 500k (default 0)")
@app_commands.autocomplete(to_team=my_team_ac)
async def loan(i: discord.Interaction, player: discord.Member, to_team: str = None, fee: str = "0"):
    await i.response.defer(ephemeral=True)
    new, err = resolve_team(i, to_team)
    if err:
        return await i.followup.send(err)
    await offer_loan(i, player, new, fee)

@bot.tree.command(description="Ask a player to be released (they must accept)")
@staff
async def release(i: discord.Interaction, player: discord.Member):
    await i.response.defer(ephemeral=True)
    cur = player_team(player.id)
    if not cur:
        return await i.followup.send("That player isn't on a team.")
    await send_offer(i, player, "release", None, cur)

# ---------------------------------------------------------------- time / lookup helpers
def league_tz():
    name = get_setting("timezone") or os.getenv("LEAGUE_TZ", "UTC")
    try:
        return ZoneInfo(name)
    except Exception:
        return dt.timezone.utc

def parse_dt(text):
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %I:%M %p", "%Y-%m-%d %I:%M%p"):
        try:
            return dt.datetime.strptime(text, fmt).replace(tzinfo=league_tz())
        except ValueError:
            continue
    return None

TIME_HELP = "Use `YYYY-MM-DD HH:MM` (24h) or `YYYY-MM-DD 8:00 PM`, in the league timezone (set with /timezone)."

def tsf(x, style="F"):
    return f"<t:{int(x)}:{style}>"

def ordinal(n):
    n = int(n)
    suf = "th" if 10 <= n % 100 <= 20 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"

def gw_label(gw):
    return f" — GW {gw}" if gw else ""

def get_guild():
    gid = GUILD_ID or get_setting("guild_id")
    if gid and bot.get_guild(int(gid)):
        return bot.get_guild(int(gid))
    return bot.guilds[0] if len(bot.guilds) == 1 else None

def chan(guild, key):
    cid = get_setting(key)
    return guild.get_channel(int(cid)) if guild and cid else None

def name_of(tid):
    t = team_by_id(tid)
    return t["name"] if t else "Deleted team"

def get_fx(val):
    try:
        n = int(str(val).strip().lstrip("#").split()[0])
    except (ValueError, IndexError):
        return None
    return db.execute("SELECT * FROM fixtures WHERE id=?", (n,)).fetchone()

def fixture_ac(status):
    async def ac(_, current: str):
        like = f"%{current}%"
        order = "ASC" if status == "scheduled" else "DESC"
        rows = db.execute(f"""SELECT f.id, f.gw, h.name hn, a.name an FROM fixtures f
            JOIN teams h ON h.id=f.home_id JOIN teams a ON a.id=f.away_id
            WHERE f.status=? AND (h.name LIKE ? OR a.name LIKE ? OR CAST(f.id AS TEXT)=?)
            ORDER BY f.kickoff {order} LIMIT 25""",
            (status, like, like, current.strip().lstrip("#"))).fetchall()
        return [app_commands.Choice(name=f"#{r['id']} GW{r['gw']} {r['hn']} vs {r['an']}"[:100],
                                    value=str(r["id"])) for r in rows]
    return ac

sched_ac = fixture_ac("scheduled")
played_ac = fixture_ac("played")

# ---------------------------------------------------------------- records (standings / form / h2h)
def standings(tier=None):
    if tier:
        ts_ = db.execute("SELECT * FROM teams WHERE tier=?", (tier,)).fetchall()
    else:
        ts_ = db.execute("SELECT * FROM teams").fetchall()
    tbl = {t["id"]: dict(team=t, p=0, w=0, d=0, l=0, gf=0, ga=0, pts=0) for t in ts_}
    for f in db.execute("SELECT * FROM fixtures WHERE status='played'").fetchall():
        hg, ag = f["home_goals"], f["away_goals"]
        for tid, gf, ga in ((f["home_id"], hg, ag), (f["away_id"], ag, hg)):
            r = tbl.get(tid)
            if not r:
                continue
            r["p"] += 1; r["gf"] += gf; r["ga"] += ga
            if gf > ga:
                r["w"] += 1; r["pts"] += 3
            elif gf == ga:
                r["d"] += 1; r["pts"] += 1
            else:
                r["l"] += 1
    rows = sorted(tbl.values(), key=lambda r: (-r["pts"], -(r["gf"] - r["ga"]), -r["gf"], r["team"]["name"].lower()))
    for n, r in enumerate(rows, 1):
        r["pos"] = n
    return rows

def pos_pts(team, tier):
    for r in standings(tier):
        if r["team"]["id"] == team["id"]:
            return ordinal(r["pos"]), r["pts"]
    return "-", 0

def form_str(team_id, n=5):
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND (home_id=? OR away_id=?)
                         ORDER BY played_at DESC, id DESC LIMIT ?""", (team_id, team_id, n)).fetchall()
    out = []
    for f in reversed(rows):
        gf, ga = (f["home_goals"], f["away_goals"]) if f["home_id"] == team_id else (f["away_goals"], f["home_goals"])
        out.append("W" if gf > ga else "D" if gf == ga else "L")
    return "".join(out)

def h2h_text(me_id, opp_id):
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND
                         ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
                         ORDER BY played_at DESC, id DESC""", (me_id, opp_id, opp_id, me_id)).fetchall()
    if not rows:
        return "First meeting this season"
    w = d = l = 0
    for f in rows:
        gf, ga = (f["home_goals"], f["away_goals"]) if f["home_id"] == me_id else (f["away_goals"], f["home_goals"])
        if gf > ga: w += 1
        elif gf == ga: d += 1
        else: l += 1
    last = rows[0]
    return (f"Played **{len(rows)}** • You: {w}W {d}D {l}L\n"
            f"Last: {name_of(last['home_id'])} {last['home_goals']}–{last['away_goals']} {name_of(last['away_id'])}")

def next_fixture(team_id):
    q = """SELECT * FROM fixtures WHERE status='scheduled' AND (home_id=? OR away_id=?) {} ORDER BY kickoff LIMIT 1"""
    f = db.execute(q.format("AND kickoff > ?"), (team_id, team_id, time.time() - 7200)).fetchone()
    return f or db.execute(q.format(""), (team_id, team_id)).fetchone()   # fall back to an overdue, unplayed fixture

def next_match_line(team, f):
    home = f["home_id"] == team["id"]
    opp = name_of(f["away_id"] if home else f["home_id"])
    line = f"**{opp}** ({'H' if home else 'A'}) • {tsf(f['kickoff'])} ({tsf(f['kickoff'], 'R')})"
    if f["kickoff"] < time.time() - 7200:
        line += "\n⚠️ **Overdue**: past its date with no result yet (staff: /schedule_shift or /result)."
    return line

def table_block(rows):
    lines = [f"{'#':>2}  {'Team':<14} {'P':>2} {'W':>2} {'D':>2} {'L':>2} {'GD':>3} {'Pts':>3}"]
    for r in rows[:18]:
        gd = r["gf"] - r["ga"]
        lines.append(f"{r['pos']:>2}  {r['team']['name'][:14]:<14} {r['p']:>2} {r['w']:>2} {r['d']:>2} {r['l']:>2} {gd:>+3} {r['pts']:>3}")
    return "```\n" + "\n".join(lines) + "\n```"

def _logo_img(data, box):
    try:
        im = Image.open(io.BytesIO(data)).convert("RGBA")
        im.thumbnail((box, box))
        return im
    except Exception:
        return None

def _draw_name(d, xy, text, max_w, anchor):
    """Team name that always fits: shrink, then wrap onto 2-3 lines, then cut with an ellipsis."""
    x, y = xy
    white = (255, 255, 255)
    for sz in (40, 36, 32, 28, 24):
        f = _font(sz, True)
        if d.textlength(text, font=f) <= max_w:
            return _put(d, (x, y), text, f, white, anchor)
    for sz, max_lines in ((24, 2), (20, 3)):
        f = _font(sz, True)
        lines, cur = [], ""
        for w in text.split():
            trial = (cur + " " + w).strip()
            if d.textlength(trial, font=f) <= max_w:
                cur = trial
            else:
                if cur:
                    lines.append(cur)
                cur = w
        lines.append(cur)
        if len(lines) <= max_lines and all(d.textlength(l, font=f) <= max_w for l in lines):
            step = sz * 1.25
            top = y - step * (len(lines) - 1) / 2
            for k, line in enumerate(lines):
                _put(d, (x, top + k * step), line, f, white, anchor)
            return
    f = _font(20, True)
    t = text
    while len(t) > 2 and d.textlength(t + "...", font=f) > max_w:
        t = t[:-1]
    _put(d, (x, y), t.rstrip() + "...", f, white, anchor)

def render_banner(h_name, a_name, center, h_logo, a_logo):
    """[LOGO] HOME  1 – 1  AWAY [LOGO]  (center is the score or 'VS')"""
    h_name, a_name, center = ascii_img(h_name), ascii_img(a_name), ascii_img(center)
    W, H, LB = 960, 220, 150
    img = Image.new("RGB", (W, H), (43, 45, 49))
    d = ImageDraw.Draw(img)
    for data, name, x in ((h_logo, h_name, 30), (a_logo, a_name, W - 30 - LB)):
        im = _logo_img(data, LB) if data else None
        if im:
            img.paste(im, (x + (LB - im.width) // 2, (H - im.height) // 2), im)
        else:
            d.ellipse((x, (H - LB) // 2, x + LB, (H + LB) // 2), fill=(64, 68, 75))
            _put(d, (x + LB // 2, H // 2), name[:1].upper(), _font(64, True), (255, 255, 255), "mm")
    f_c = _font(34, True)
    for sz in (84, 72, 60, 48, 40, 34):
        f_c = _font(sz, True)
        if d.textlength(center, font=f_c) <= 230:
            break
    cx, cw = W // 2, d.textlength(center, font=f_c)
    _put(d, (cx, H // 2), center, f_c, (255, 255, 255) if center != "VS" else (160, 163, 168), "mm")
    max_w = int(cx - cw / 2 - 14 - 198)
    _draw_name(d, (198, H // 2), h_name, max_w, "lm")
    _draw_name(d, (W - 198, H // 2), a_name, max_w, "rm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

async def make_banner(h, a, center="VS"):
    """PNG bytes of the logo/name banner, or None (no Pillow / error)."""
    if Image is None or not h or not a:
        return None
    try:
        hl, al = await asyncio.gather(team_logo_bytes(h), team_logo_bytes(a))
        return await run_render(render_banner, h["name"], a["name"], center, hl, al)
    except Exception as ex:
        print(f"[banner] failed: {ex!r}")
        return None

def with_banner(main, png):
    """Put the banner above `main` as its own embed (author line moves up with it). Returns (embeds, files)."""
    if not png:
        return [main], []
    m = main.copy()
    b = discord.Embed(color=m.color)
    if getattr(m.author, "name", None):
        b.set_author(name=m.author.name, icon_url=m.author.icon_url)
        m.remove_author()
    b.set_image(url="attachment://match.png")
    return [b, m], [discord.File(io.BytesIO(png), filename="match.png")]

def banner_kw(files):
    return {"files": files} if files else {}

# ---------------------------------------------------------------- matchday briefs (auto + manual)
def brief_text():
    return get_setting("brief_text") or "Goals ⚽ (2 pts), assists 🎯 (2 pts), clean sheets 🧤 (2 pts)."

def matchday_embed(fx, me, opp, is_home, banner=False):
    tier = fx["tier"] or me["tier"]
    mp, mpts = pos_pts(me, tier)
    op, opts = pos_pts(opp, tier)
    fm = lambda s: " ".join(FORM.get(c, "") for c in s)
    title = ("🏠 Home match" if is_home else "✈️ Away match") if banner else \
            f"{me['name']} vs {opp['name']} ({'H' if is_home else 'A'})"
    e = discord.Embed(title=title, color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} Matchday{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    e.description = (f"📍 **{fx['competition'] or 'League'}** • {tier}\n"
                     f"🕒 {tsf(fx['kickoff'])} ({tsf(fx['kickoff'], 'R')})")
    e.add_field(name="📊 League position", inline=False, value=
        f"> You: **{mp}** ({mpts} pts) {fm(form_str(me['id']))}\n> Them: **{op}** ({opts} pts) {fm(form_str(opp['id']))}")
    h2h = "\n".join("> " + x for x in h2h_text(me["id"], opp["id"]).split("\n"))
    e.add_field(name="⚔️ Head to head", inline=False, value=h2h)
    e.add_field(name="💡 Your matchday brief", inline=False, value=f"> {brief_text()}")
    e.set_footer(text=f"{me['name']} • {tier}", icon_url=logo(me))
    if logo(me) and not banner:
        e.set_thumbnail(url=logo(me))
    return e

def announce_embed(fx, h, a, banner=False):
    e = discord.Embed(title=None if banner else f"{h['name']} vs {a['name']}", color=ORANGE,
                      timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} Matchday{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    e.description = (f"📍 **{fx['competition'] or 'League'}** • {fx['tier'] or h['tier']}\n"
                     f"🕒 {tsf(fx['kickoff'])} ({tsf(fx['kickoff'], 'R')})")
    return e

async def send_matchday(fx):
    """DM every rostered player (with the logo banner); post a public card and ping anyone whose DMs are closed."""
    guild = get_guild()
    h, a = team_by_id(fx["home_id"]), team_by_id(fx["away_id"])
    png = await make_banner(h, a, "VS")
    sent, fallback, empty = 0, [], []
    for me, opp, is_home in ((h, a, True), (a, h, False)):
        e = matchday_embed(fx, me, opp, is_home, bool(png))
        players = db.execute("SELECT user_id FROM players WHERE team_id=?", (me["id"],)).fetchall()
        if not players:
            empty.append(me["name"])
        failed = []
        for p in players:
            try:
                u = (guild.get_member(p["user_id"]) if guild else None) or await bot.fetch_user(p["user_id"])
                embeds, files = with_banner(e, png)
                await asyncio.wait_for(u.send(embeds=embeds, view=profile_view(p["user_id"]), **banner_kw(files)), timeout=15)
                sent += 1
            except Exception as ex:
                print(f"[matchday] DM to {p['user_id']} failed: {ex!r}")
                failed.append(p["user_id"])
            await asyncio.sleep(0.3)
        if failed:
            fallback.append((e, failed))
    ch = chan(guild, "matchday_channel")
    if ch:
        try:
            embeds, files = with_banner(announce_embed(fx, h, a, bool(png)), png)
            await ch.send(embeds=embeds, **banner_kw(files))
            for e, ids in fallback:
                embeds, files = with_banner(e, png)
                await ch.send(content=" ".join(f"<@{u}>" for u in ids) + " (couldn't DM you, here's your brief)",
                              embeds=embeds, **banner_kw(files))
        except Exception as ex:
            print(f"[matchday] channel post failed: {ex!r}")
            ch = None
    return dict(sent=sent, failed=sum(len(ids) for _, ids in fallback), posted=ch, empty=empty)

@tasks.loop(minutes=1)
async def matchday_loop():
    now = time.time()
    due = db.execute("""SELECT * FROM fixtures WHERE status='scheduled' AND brief_sent=0
                        AND kickoff - ? <= ? AND kickoff > ?""",
                     (now, MATCHDAY_LEAD_HOURS * 3600, now - 3 * 3600)).fetchall()
    for fx in due:
        db.execute("UPDATE fixtures SET brief_sent=1 WHERE id=?", (fx["id"],)); db.commit()
        try:
            await send_matchday(fx)
        except Exception:
            traceback.print_exc()

@matchday_loop.before_loop
async def _wait_md():
    await bot.wait_until_ready()

@bot.tree.command(description="Send the matchday brief for a fixture right now")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def matchday(i: discord.Interaction, fixture: str):
    await i.response.defer(ephemeral=True)
    fx = get_fx(fixture)
    if not fx:
        return await i.followup.send("Fixture not found. Pick one from the list (add one with /schedule_add).")
    db.execute("UPDATE fixtures SET brief_sent=1 WHERE id=?", (fx["id"],)); db.commit()
    r = await send_matchday(fx)
    msg = f"📨 Matchday{gw_label(fx['gw'])}: **{r['sent']}** DMs sent, **{r['failed']}** couldn't be DMed."
    if r["posted"]:
        msg += f" Public post and pings in {r['posted'].mention}."
    elif r["failed"]:
        msg += " Set a channel with /setchannel so closed-DM players get pinged there."
    for n in r["empty"]:
        msg += f"\n⚠️ **{n}** has no players on its roster."
    await i.followup.send(msg)

# ---------------------------------------------------------------- schedule
def round_robin(ids):
    ids = list(ids)
    if len(ids) % 2:
        ids.append(None)
    n, rounds = len(ids), []
    for r in range(n - 1):
        pairs = []
        for k in range(n // 2):
            a, b = ids[k], ids[n - 1 - k]
            if a is not None and b is not None:
                pairs.append((a, b) if (r + k) % 2 == 0 else (b, a))
        rounds.append(pairs)
        ids = [ids[0]] + [ids[-1]] + ids[1:-1]
    return rounds

@bot.tree.command(description="Add one fixture to the schedule")
@staff
@app_commands.autocomplete(home=team_ac, away=team_ac)
async def schedule_add(i: discord.Interaction, home: str, away: str, when: str,
                       gameweek: int = 1, competition: str = "League"):
    h, a, d = get_team(home), get_team(away), parse_dt(when)
    if not h or not a:
        return await i.response.send_message("Team not found.", ephemeral=True)
    if h["id"] == a["id"]:
        return await i.response.send_message("A team can't play itself.", ephemeral=True)
    if not d:
        return await i.response.send_message(f"Couldn't read that time. {TIME_HELP}", ephemeral=True)
    cur = db.execute("INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition) VALUES(?,?,?,?,?,?)",
                     (gameweek, h["id"], a["id"], d.timestamp(), h["tier"], competition)); db.commit()
    await i.response.send_message(
        f"📅 Fixture **#{cur.lastrowid}**: **{h['name']}** vs **{a['name']}** • GW{gameweek} • {tsf(d.timestamp())}\n"
        f"Brief goes out automatically {int(MATCHDAY_LEAD_HOURS)}h before kickoff.", ephemeral=True)

@bot.tree.command(description="Auto-generate a full round-robin schedule for a tier")
@staff
@app_commands.autocomplete(tier=tier_ac)
async def schedule_generate(i: discord.Interaction, tier: str, start: str,
                            days_between: app_commands.Range[int, 1, 60] = 7,
                            legs: app_commands.Range[int, 1, 2] = 1,
                            competition: str = "League", replace: bool = False):
    await i.response.defer(ephemeral=True)
    base = parse_dt(start)
    if not base:
        return await i.followup.send(f"Couldn't read the start time. {TIME_HELP}")
    ids = [r["id"] for r in db.execute("SELECT id FROM teams WHERE tier=? ORDER BY id", (tier,)).fetchall()]
    if len(ids) < 2:
        return await i.followup.send(f"Tier **{tier}** needs at least 2 teams.")
    existing = db.execute("SELECT COUNT(*) FROM fixtures WHERE tier=? AND status='scheduled'", (tier,)).fetchone()[0]
    if existing and not replace:
        return await i.followup.send(f"**{tier}** already has {existing} scheduled fixtures. Re-run with `replace: True` to overwrite them.")
    if replace:
        db.execute("DELETE FROM fixtures WHERE tier=? AND status='scheduled'", (tier,))
    rounds = round_robin(ids)
    if legs == 2:
        rounds += [[(b, a) for a, b in r] for r in rounds]
    n = 0
    for gw, pairs in enumerate(rounds, 1):
        when = base + dt.timedelta(days=(gw - 1) * days_between)
        for h_id, a_id in pairs:
            db.execute("INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition) VALUES(?,?,?,?,?,?)",
                       (gw, h_id, a_id, when.timestamp(), tier, competition)); n += 1
    db.commit()
    await i.followup.send(f"📅 Created **{n}** fixtures over **{len(rounds)}** gameweeks for **{tier}** "
                          f"({len(ids)} teams), starting {tsf(base.timestamp())}. See them with /fixtures.")

@bot.tree.command(description="Remove a scheduled fixture")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def schedule_remove(i: discord.Interaction, fixture: str):
    fx = get_fx(fixture)
    if not fx or fx["status"] != "scheduled":
        return await i.response.send_message("Scheduled fixture not found.", ephemeral=True)
    db.execute("DELETE FROM fixtures WHERE id=?", (fx["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Removed fixture #{fx['id']}.", ephemeral=True)

@bot.tree.command(description="Move a fixture to a new date/time (brief is re-sent automatically)")
@staff
@app_commands.autocomplete(fixture=sched_ac)
async def schedule_move(i: discord.Interaction, fixture: str, when: str):
    fx, d = get_fx(fixture), parse_dt(when)
    if not fx or fx["status"] != "scheduled":
        return await i.response.send_message("Scheduled fixture not found.", ephemeral=True)
    if not d:
        return await i.response.send_message(f"Couldn't read that time. {TIME_HELP}", ephemeral=True)
    db.execute("UPDATE fixtures SET kickoff=?, brief_sent=0 WHERE id=?", (d.timestamp(), fx["id"])); db.commit()
    await i.response.send_message(f"📅 Fixture #{fx['id']} moved to {tsf(d.timestamp())}.", ephemeral=True)

@bot.tree.command(description="Delete all scheduled (unplayed) fixtures for a tier")
@staff
@app_commands.autocomplete(tier=tier_ac)
async def schedule_clear(i: discord.Interaction, tier: str):
    cur = db.execute("DELETE FROM fixtures WHERE tier=? AND status='scheduled'", (tier,)); db.commit()
    await i.response.send_message(f"🗑️ Deleted {cur.rowcount} scheduled fixtures from **{tier}**. Played results were kept.", ephemeral=True)

@bot.tree.command(name="fixtures", description="Upcoming fixtures")
@app_commands.autocomplete(team=team_ac, tier=tier_ac)
async def fixtures_cmd(i: discord.Interaction, team: str = None, tier: str = None,
                       limit: app_commands.Range[int, 1, 25] = 10):
    q, args = "SELECT * FROM fixtures WHERE status='scheduled'", []
    if team:
        t = get_team(team)
        if not t:
            return await i.response.send_message("Team not found.", ephemeral=True)
        q += " AND (home_id=? OR away_id=?)"; args += [t["id"], t["id"]]
    if tier:
        q += " AND tier=?"; args.append(tier)
    rows = db.execute(q + " ORDER BY kickoff LIMIT ?", args + [limit]).fetchall()
    if not rows:
        return await i.response.send_message("No upcoming fixtures.", ephemeral=True)
    lines = [("⚠️ " if f["kickoff"] < time.time() - 7200 else "") +
             f"`#{f['id']}` GW{f['gw']} • **{name_of(f['home_id'])}** vs **{name_of(f['away_id'])}** • {tsf(f['kickoff'])}" for f in rows]
    if any(f["kickoff"] < time.time() - 7200 for f in rows):
        lines.append("\n⚠️ = past its date with no result yet")
    await i.response.send_message(embed=discord.Embed(title=f"{league_short()} Fixtures", color=ORANGE, description="\n".join(lines)))

@bot.tree.command(description="Show a team's next match (defaults to your team)")
@app_commands.autocomplete(team=team_ac)
async def nextmatch(i: discord.Interaction, team: str = None):
    await i.response.defer()
    t = get_team(team) if team else player_team(i.user.id)
    if not t:
        return await i.followup.send("Pick a team (you aren't on one).", ephemeral=True)
    f = next_fixture(t["id"])
    if not f:
        return await i.followup.send(f"No upcoming match scheduled for **{t['name']}**.", ephemeral=True)
    h, a = team_by_id(f["home_id"]), team_by_id(f["away_id"])
    png = await make_banner(h, a, "VS")
    e = discord.Embed(title=None if png else f"{t['name']}: next match", color=ORANGE,
                      description=f"{next_match_line(t, f)}\n📍 {f['competition'] or 'League'} • {f['tier'] or t['tier']}{gw_label(f['gw'])}")
    e.set_author(name=f"{t['name']} • next match", icon_url=LEAGUE_LOGO)
    embeds, files = with_banner(e, png)
    await i.followup.send(embeds=embeds, **banner_kw(files))

# ---------------------------------------------------------------- match results
def save_result(fid, hg, ag, notes=None, forfeit=0):
    db.execute("""UPDATE fixtures SET status='played', home_goals=?, away_goals=?, notes=?, played_at=?, forfeit=?
                  WHERE id=?""", (hg, ag, notes, time.time(), forfeit, fid)); db.commit()

def result_embed(fx, changes=None, banner=False):
    h, a = team_by_id(fx["home_id"]), team_by_id(fx["away_id"])
    hg, ag = fx["home_goals"], fx["away_goals"]
    win = h if hg > ag else a if ag > hg else None
    line = f"🏆 **Winner:** {win['name']}" if win else "🤝 **Draw**"
    if fx["forfeit"]:
        line += " (forfeit)"
    e = discord.Embed(title=None if banner else f"{h['name']} {hg} – {ag} {a['name']}", description=line,
                      color=GREEN if win else YELLOW, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} Match Results{gw_label(fx['gw'])}", icon_url=LEAGUE_LOGO)
    tier = fx["tier"] or h["tier"]
    pos = {r["team"]["id"]: r for r in standings(tier)}
    lines = [f"> {t['name']}: **{ordinal(pos[t['id']]['pos'])}** ({pos[t['id']]['pts']} pts)" for t in (h, a) if t["id"] in pos]
    if lines:
        e.add_field(name="📊 League position", value="\n".join(lines), inline=False)
    st = db.execute("SELECT * FROM match_stats WHERE fixture_id=?", (fx["id"],)).fetchall()
    for col, emoji, label in (("goals", "⚽", "Goals"), ("assists", "🎯", "Assists"), ("clean_sheets", "🧤", "Clean sheets")):
        parts = [f"<@{r['user_id']}>" + (f" ×{r[col]}" if r[col] > 1 else "") for r in st if r[col] > 0]
        if parts:
            e.add_field(name=f"{emoji} {label}", value=", ".join(parts), inline=False)
    if changes:
        ups = [f"<@{u}>: {money(b)} → {money(n)} (+{money(n - b)})" for u, b, n in changes if n > b][:10]
        if ups:
            e.add_field(name="📈 Market value", value="\n".join(ups), inline=False)
    if fx["notes"] and not fx["forfeit"]:
        e.add_field(name="📝 Notes", value=f"> {fx['notes']}", inline=False)
    if win and logo(win) and not banner:
        e.set_thumbnail(url=logo(win))
    e.set_footer(text=f"{fx['competition'] or 'League'} • {tier}")
    return e

async def announce_result(fx, changes=None):
    guild = get_guild()
    ch = chan(guild, "results_channel") or chan(guild, "matchday_channel")
    if not ch:
        return "(No results channel set. Use /setchannel.)"
    try:
        h, a = team_by_id(fx["home_id"]), team_by_id(fx["away_id"])
        png = await make_banner(h, a, f"{fx['home_goals']} - {fx['away_goals']}")
        embeds, files = with_banner(result_embed(fx, changes, bool(png)), png)
        await asyncio.wait_for(ch.send(embeds=embeds, **banner_kw(files)), timeout=20)
        return f"Posted in {ch.mention}."
    except Exception as ex:
        print(f"[result] post failed: {ex!r}")
        return f"(Couldn't post in {ch.mention}: {type(ex).__name__}.)"

def read_stats(scorers, assists, clean_sheets):
    """Returns (goals, assists, cs, error)."""
    out = []
    for raw, label in ((scorers, "scorers"), (assists, "assists"), (clean_sheets, "clean_sheets")):
        parsed = parse_stat_list(raw)
        if raw and not parsed:
            return None, None, None, f"I couldn't find any @mentions in **{label}**. Type @ and pick each player from the list."
        out.append(parsed)
    return out[0], out[1], out[2], None

STAT_HELP = dict(scorers="Who scored ⚽ e.g. @tung 2 @bob (number after a name = goals, default 1)",
                 assists="Who assisted 🎯 e.g. @bob 1 @tung",
                 clean_sheets="Who kept a clean sheet 🧤 e.g. @keeper @defender")

@bot.tree.command(name="result", description="Enter a match result, with scorers, assists and clean sheets")
@staff
@app_commands.describe(**STAT_HELP, notes="Optional note shown on the result")
@app_commands.autocomplete(fixture=sched_ac)
async def result_cmd(i: discord.Interaction, fixture: str,
                     home_score: app_commands.Range[int, 0, 99], away_score: app_commands.Range[int, 0, 99],
                     scorers: str = None, assists: str = None, clean_sheets: str = None, notes: str = None):
    await i.response.defer(ephemeral=True)
    fx = get_fx(fixture)
    if not fx:
        return await i.followup.send("Fixture not found. Pick one from the list.")
    if fx["status"] == "played":
        return await i.followup.send("That fixture already has a result. Use /result_remove first.")
    goals, ast_, cs, err = read_stats(scorers, assists, clean_sheets)
    err = err or check_match_stats(fx, home_score, away_score, goals or {}, ast_ or {}, cs or {})
    if err:
        return await i.followup.send(f"❌ {err}\nNothing was saved.")
    ids = set(goals) | set(ast_) | set(cs)
    before = {u: player_value(u) for u in ids}
    save_result(fx["id"], home_score, away_score, notes)
    save_match_stats(fx["id"], goals, ast_, cs)
    changes = [(u, before[u], player_value(u)) for u in ids]
    fx = get_fx(fx["id"])
    where = await announce_result(fx, changes)
    await i.followup.send(f"✅ Saved: **{name_of(fx['home_id'])} {home_score}–{away_score} {name_of(fx['away_id'])}**. {where}")

@bot.tree.command(description="Record a result for a match that wasn't scheduled (friendly, make-up game)")
@staff
@app_commands.describe(**STAT_HELP)
@app_commands.autocomplete(home=team_ac, away=team_ac)
async def result_add(i: discord.Interaction, home: str, away: str,
                     home_score: app_commands.Range[int, 0, 99], away_score: app_commands.Range[int, 0, 99],
                     scorers: str = None, assists: str = None, clean_sheets: str = None,
                     gameweek: int = 0, notes: str = None):
    await i.response.defer(ephemeral=True)
    h, a = get_team(home), get_team(away)
    if not h or not a or h["id"] == a["id"]:
        return await i.followup.send("Pick two different, existing teams.")
    goals, ast_, cs, err = read_stats(scorers, assists, clean_sheets)
    err = err or check_match_stats({"home_id": h["id"], "away_id": a["id"]}, home_score, away_score,
                                   goals or {}, ast_ or {}, cs or {})
    if err:
        return await i.followup.send(f"❌ {err}\nNothing was saved.")
    ids = set(goals) | set(ast_) | set(cs)
    before = {u: player_value(u) for u in ids}
    cur = db.execute("""INSERT INTO fixtures(gw,home_id,away_id,kickoff,tier,competition,brief_sent)
                        VALUES(?,?,?,?,?,?,1)""", (gameweek, h["id"], a["id"], time.time(), h["tier"], "League"))
    save_result(cur.lastrowid, home_score, away_score, notes)
    save_match_stats(cur.lastrowid, goals, ast_, cs)
    changes = [(u, before[u], player_value(u)) for u in ids]
    where = await announce_result(get_fx(cur.lastrowid), changes)
    await i.followup.send(f"✅ Saved: **{h['name']} {home_score}–{away_score} {a['name']}**. {where}")

@bot.tree.command(description="Award a forfeit win (3-0) for a scheduled match")
@staff
@app_commands.autocomplete(fixture=sched_ac, winner=team_ac)
async def forfeit(i: discord.Interaction, fixture: str, winner: str):
    await i.response.defer(ephemeral=True)
    fx, w = get_fx(fixture), get_team(winner)
    if not fx or fx["status"] != "scheduled":
        return await i.followup.send("Scheduled fixture not found.")
    if not w or w["id"] not in (fx["home_id"], fx["away_id"]):
        return await i.followup.send("The winner must be one of the two teams in that fixture.")
    hg, ag = (3, 0) if w["id"] == fx["home_id"] else (0, 3)
    save_result(fx["id"], hg, ag, "Forfeit win", 1)
    where = await announce_result(get_fx(fx["id"]))
    await i.followup.send(f"✅ Forfeit win awarded to **{w['name']}** (3–0). {where}")

@bot.tree.command(description="Undo a result (puts the fixture back on the schedule)")
@staff
@app_commands.autocomplete(fixture=played_ac)
async def result_remove(i: discord.Interaction, fixture: str):
    fx = get_fx(fixture)
    if not fx or fx["status"] != "played":
        return await i.response.send_message("Played fixture not found.", ephemeral=True)
    db.execute("DELETE FROM match_stats WHERE fixture_id=?", (fx["id"],))
    db.execute("""UPDATE fixtures SET status='scheduled', home_goals=NULL, away_goals=NULL, notes=NULL,
                  played_at=NULL, forfeit=0 WHERE id=?""", (fx["id"],)); db.commit()
    await i.response.send_message(f"↩️ Result removed. Fixture #{fx['id']} is back on the schedule.", ephemeral=True)

@bot.tree.command(description="Recent match results")
@app_commands.autocomplete(team=team_ac)
async def results(i: discord.Interaction, team: str = None, limit: app_commands.Range[int, 1, 20] = 10):
    t = get_team(team) if team else None
    if team and not t:
        return await i.response.send_message("Team not found.", ephemeral=True)
    q, args = "SELECT * FROM fixtures WHERE status='played'", []
    if t:
        q += " AND (home_id=? OR away_id=?)"; args += [t["id"], t["id"]]
    rows = db.execute(q + " ORDER BY played_at DESC, id DESC LIMIT ?", args + [limit]).fetchall()
    if not rows:
        return await i.response.send_message("No results yet.", ephemeral=True)
    lines = [f"`#{f['id']}` **{name_of(f['home_id'])}** {f['home_goals']}–{f['away_goals']} **{name_of(f['away_id'])}**"
             + (f" • GW{f['gw']}" if f["gw"] else "") for f in rows]
    title = f"{t['name']} Results" if t else f"{league_short()} Results"
    await i.response.send_message(embed=discord.Embed(title=title, color=GREEN, description="\n".join(lines)))

@bot.tree.command(name="standings", description="League table (3 pts win, 1 draw)")
@app_commands.autocomplete(tier=tier_ac)
async def standings_cmd(i: discord.Interaction, tier: str = None):
    tiers = [tier] if tier else [r["tier"] for r in db.execute(
        "SELECT DISTINCT tier FROM teams WHERE tier IS NOT NULL ORDER BY tier").fetchall()]
    if not tiers:
        return await i.response.send_message("No teams yet.", ephemeral=True)
    e = discord.Embed(title=f"{league_short()} Standings", color=PINK, timestamp=discord.utils.utcnow())
    for tr in tiers[:25]:
        rows = standings(tr)
        if rows:
            e.add_field(name=tr, value=table_block(rows), inline=False)
    if not e.fields:
        return await i.response.send_message("No teams in that tier.", ephemeral=True)
    if tier:
        dv = db.execute("SELECT id, logo FROM divisions WHERE name=?", (tier,)).fetchone()
        if dv and dv["logo"]:
            e.set_thumbnail(url=f"attachment://division_{dv['id']}.png")
    await i.response.send_message(embed=e)

@bot.tree.command(description="A team's record, position and form (defaults to your team)")
@app_commands.autocomplete(team=team_ac)
async def record(i: discord.Interaction, team: str = None):
    t = get_team(team) if team else player_team(i.user.id)
    if not t:
        return await i.response.send_message("Pick a team (you aren't on one).", ephemeral=True)
    r = next((x for x in standings(t["tier"]) if x["team"]["id"] == t["id"]), None)
    fm = " ".join(FORM.get(c, "") for c in form_str(t["id"])) or "—"
    e = discord.Embed(title=f"{t['name']} Record", color=PINK)
    e.add_field(name="Position", value=f"{ordinal(r['pos'])} in {t['tier']}")
    e.add_field(name="Points", value=str(r["pts"]))
    e.add_field(name="W / D / L", value=f"{r['w']} / {r['d']} / {r['l']}")
    e.add_field(name="Goals", value=f"{r['gf']} for • {r['ga']} against ({r['gf'] - r['ga']:+d})")
    e.add_field(name="Form", value=fm)
    nf = next_fixture(t["id"])
    if nf:
        e.add_field(name="Next match", value=next_match_line(t, nf), inline=False)
    if logo(t):
        e.set_thumbnail(url=logo(t))
    await i.response.send_message(embed=e)

@bot.tree.command(description="Head-to-head record between two teams")
@app_commands.autocomplete(team_a=team_ac, team_b=team_ac)
async def h2h(i: discord.Interaction, team_a: str, team_b: str):
    await i.response.defer()
    a, b = get_team(team_a), get_team(team_b)
    if not a or not b or a["id"] == b["id"]:
        return await i.followup.send("Pick two different, existing teams.", ephemeral=True)
    rows = db.execute("""SELECT * FROM fixtures WHERE status='played' AND
                         ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
                         ORDER BY played_at DESC, id DESC LIMIT 5""", (a["id"], b["id"], b["id"], a["id"])).fetchall()
    png = await make_banner(a, b, "VS")
    e = discord.Embed(title=None if png else f"{a['name']} vs {b['name']}", color=ORANGE,
                      description=h2h_text(a["id"], b["id"]).replace("You:", f"{a['name']}:"))
    e.set_author(name=f"{league_short()} Head to Head", icon_url=LEAGUE_LOGO)
    if rows:
        e.add_field(name="Recent meetings", inline=False, value="\n".join(
            f"{name_of(f['home_id'])} {f['home_goals']}–{f['away_goals']} {name_of(f['away_id'])}" for f in rows))
    embeds, files = with_banner(e, png)
    await i.followup.send(embeds=embeds, **banner_kw(files))

class Pager(discord.ui.View):
    """◀ ▶ buttons over a list of embeds (only the person who ran the command can use them)."""
    def __init__(self, embeds, user_id):
        super().__init__(timeout=300)
        self.embeds, self.page, self.user_id = embeds, 0, user_id
        self._sync()

    def _sync(self):
        self.back.disabled = self.page == 0
        self.fwd.disabled = self.page >= len(self.embeds) - 1

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.user_id:
            await i.response.send_message("Only the person who ran the command can flip pages.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def back(self, i: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self._sync()
        await i.response.edit_message(embed=self.embeds[self.page], view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def fwd(self, i: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self._sync()
        await i.response.edit_message(embed=self.embeds[self.page], view=self)

@bot.tree.command(description="Everyone in the server who isn't on a team")
@app_commands.describe(role="Only show people who have this role (optional)")
async def freeagents(i: discord.Interaction, role: discord.Role = None):
    await i.response.defer()
    g = i.guild
    if not g.chunked:
        try:
            await g.chunk()
        except Exception:
            pass
    on_team = {r["user_id"] for r in db.execute("SELECT user_id FROM players WHERE team_id IS NOT NULL").fetchall()}
    people = [m for m in g.members if not m.bot and m.id not in on_team and (role is None or role in m.roles)]
    people.sort(key=lambda m: m.display_name.lower())
    if not people:
        return await i.followup.send("Nobody without a club right now.")
    per, pages = 20, []
    total_pages = math.ceil(len(people) / per)
    for k in range(0, len(people), per):
        desc = "\n".join(f"{m.mention} • {money(player_value(m.id))}" for m in people[k:k + per])
        e = discord.Embed(title=f"Free Agents ({len(people)})", description=desc, color=PINK)
        e.set_footer(text=f"Page {k // per + 1}/{total_pages} • value shown next to each name")
        pages.append(e)
    if len(pages) == 1:
        return await i.followup.send(embed=pages[0])
    await i.followup.send(embed=pages[0], view=Pager(pages, i.user.id))

# ---------------------------------------------------------------- /table (logo image, private)
_logo_cache = {}

async def fetch_logo(url):
    if not url:
        return None
    if url in _logo_cache:
        return _logo_cache[url]
    data = None
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as sess:
            async with sess.get(url, headers={"User-Agent": "Mozilla/5.0 PitchXBot"}) as r:
                if r.status == 200 and r.headers.get("Content-Type", "").startswith("image/"):
                    data = await r.read()
    except Exception as ex:
        print(f"[table] logo fetch failed: {ex!r}")
    if data and len(data) < 5_000_000:
        _logo_cache[url] = data
        return data
    return None

@functools.lru_cache(maxsize=64)
def _font(size, bold=False):
    names = ("DejaVuSans-Bold.ttf", "arialbd.ttf") if bold else ("DejaVuSans.ttf", "arial.ttf")
    for n in names:
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()

_IMG_MAP = str.maketrans({"•": "-", "·": "-", "–": "-", "—": "-", "…": "...", "×": "x", "★": "*",
                          "’": "'", "‘": "'", "“": '"', "”": '"', "→": ">", "\u00a0": " "})

def ascii_img(text):
    """Text for generated images: plain keyboard characters for punctuation, and nothing the host font can't draw."""
    out = "".join(c for c in str(text).translate(_IMG_MAP) if ord(c) < 0x250)
    return " ".join(out.split()) or "?"

def _put(d, xy, text, font, fill, anchor="lm", **kw):
    text = ascii_img(text)
    try:
        d.text(xy, text, font=font, fill=fill, anchor=anchor, **kw)
    except ValueError:
        d.text(xy, text, font=font, fill=fill, **kw)

def render_table(title, rows, logos, highlight_id=None, div_logo=None):
    title = ascii_img(title)
    W, ROW, HEAD = 940, 64, 104
    rows = rows[:30]
    img = Image.new("RGB", (W, HEAD + ROW * len(rows) + 24), (30, 31, 34))
    d = ImageDraw.Draw(img)
    f_title, f_head, f_name, f_num = _font(34, True), _font(18, True), _font(26, True), _font(24)
    grey = (150, 152, 157)
    cols = [("P", 590), ("W", 650), ("D", 710), ("L", 770), ("GD", 835), ("PTS", 900)]
    tx = 32
    if div_logo:
        dl = _logo_img(div_logo, 60)
        if dl:
            img.paste(dl, (28, 20 + (60 - dl.height) // 2), dl)
            tx = 100
    _put(d, (tx, 38), title, f_title, (255, 255, 255))
    _put(d, (36, HEAD - 14), "#", f_head, grey, "mm")
    _put(d, (130, HEAD - 14), "TEAM", f_head, grey, "lm")
    for label, x in cols:
        _put(d, (x, HEAD - 14), label, f_head, grey, "mm")
    for n, r in enumerate(rows):
        y0 = HEAD + n * ROW
        cy = y0 + ROW // 2
        t = r["team"]
        fill = (54, 57, 99) if t["id"] == highlight_id else (38, 40, 44) if n % 2 == 0 else (30, 31, 34)
        d.rectangle((0, y0, W, y0 + ROW - 1), fill=fill)
        accent = {0: (250, 204, 21), 1: (203, 213, 225), 2: (217, 119, 6)}.get(n)
        if accent:
            d.rectangle((0, y0, 5, y0 + ROW - 1), fill=accent)
        _put(d, (36, cy), str(r["pos"]), f_num, (255, 255, 255), "mm")
        box, lx = 48, 66
        ly, drawn = cy - box // 2, False
        if logos[n]:
            try:
                im = Image.open(io.BytesIO(logos[n])).convert("RGBA")
                im.thumbnail((box, box))
                img.paste(im, (lx + (box - im.width) // 2, ly + (box - im.height) // 2), im)
                drawn = True
            except Exception:
                pass
        if not drawn:
            d.ellipse((lx, ly, lx + box, ly + box), fill=(64, 68, 75))
            _put(d, (lx + box // 2, cy), ascii_img(t["name"])[:1].upper(), f_name, (255, 255, 255), "mm")
        name = ascii_img(t["name"])
        if d.textlength(name, font=f_name) > 400:
            while len(name) > 3 and d.textlength(name + "...", font=f_name) > 400:
                name = name[:-1]
            name = name.rstrip() + "..."
        _put(d, (130, cy), name, f_name, (255, 255, 255), "lm")
        vals = {"P": r["p"], "W": r["w"], "D": r["d"], "L": r["l"], "GD": f"{r['gf'] - r['ga']:+d}", "PTS": r["pts"]}
        for label, x in cols:
            _put(d, (x, cy), str(vals[label]), f_name if label == "PTS" else f_num, (255, 255, 255), "mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()

_table_cache = {}

def _table_key(title, rows, logos, highlight_id, div_logo):
    return hash((title, highlight_id, hash(div_logo) if div_logo else 0,
                 tuple((r["pos"], r["team"]["id"], r["team"]["name"], r["p"], r["w"], r["d"], r["l"], r["gf"], r["ga"], r["pts"])
                       for r in rows[:30]),
                 tuple(hash(x) if x else 0 for x in logos)))

@bot.tree.command(name="table", description="League table with every team's logo and position (only you see it)")
@app_commands.autocomplete(tier=tier_ac)
async def table_cmd(i: discord.Interaction, tier: str = None):
    await i.response.defer(ephemeral=True)
    if Image is None:
        return await i.followup.send("❌ Pillow isn't installed on the host, so I can't draw the table. Add `Pillow` to requirements.txt.")
    tiers = [tier] if tier else [r["name"] for r in db.execute("SELECT name FROM divisions ORDER BY name").fetchall()]
    mine = player_team(i.user.id)
    files = []
    for n, tr in enumerate(tiers[:10]):
        rows = standings(tr)
        if not rows:
            continue
        logos = await asyncio.gather(*(team_logo_bytes(r["team"]) for r in rows[:30]))   # stored copies: no downloads
        div = db.execute("SELECT logo FROM divisions WHERE name=?", (tr,)).fetchone()
        title, hl, dlogo = f"{league_short()} Table - {tr}", (mine["id"] if mine else None), (div["logo"] if div else None)
        key = _table_key(title, rows, logos, hl, dlogo)
        png = _table_cache.get(key)
        if png is None:                                  # only redraw when results/logos actually changed
            png = await run_render(render_table, title, rows, logos, hl, dlogo)
            if len(_table_cache) > 40:
                _table_cache.pop(next(iter(_table_cache)))
            _table_cache[key] = png
        files.append(discord.File(io.BytesIO(png), filename=f"table_{n + 1}.png"))
    if not files:
        return await i.followup.send("No teams yet.")
    await i.followup.send(files=files)

@bot.tree.command(description="Move ALL scheduled fixtures of a tier so the first one starts at a new date/time")
@staff
@app_commands.autocomplete(tier=tier_ac)
async def schedule_shift(i: discord.Interaction, tier: str, new_start: str):
    await i.response.defer(ephemeral=True)
    d = parse_dt(new_start)
    if not d:
        return await i.followup.send(f"Couldn't read that time. {TIME_HELP}")
    rows = db.execute("SELECT id, kickoff FROM fixtures WHERE tier=? AND status='scheduled' ORDER BY kickoff", (tier,)).fetchall()
    if not rows:
        return await i.followup.send(f"No scheduled fixtures in **{tier}**.")
    tz = league_tz()
    first = dt.datetime.fromtimestamp(rows[0]["kickoff"], tz)
    days = (d.date() - first.date()).days
    minutes = (d.hour * 60 + d.minute) - (first.hour * 60 + first.minute)
    for r in rows:
        new = dt.datetime.fromtimestamp(r["kickoff"], tz) + dt.timedelta(days=days, minutes=minutes)
        db.execute("UPDATE fixtures SET kickoff=?, brief_sent=0 WHERE id=?", (new.timestamp(), r["id"]))
    db.commit()
    await i.followup.send(f"📅 Moved **{len(rows)}** fixtures in **{tier}**. GW1 now starts {tsf(d.timestamp())}.")

# ---------------------------------------------------------------- settings
@bot.tree.command(description="Choose where the bot posts: transactions, matchdays or results")
@staff
@app_commands.choices(kind=[app_commands.Choice(name="Transactions", value="tx_channel"),
                            app_commands.Choice(name="Matchdays", value="matchday_channel"),
                            app_commands.Choice(name="Results", value="results_channel"),
                            app_commands.Choice(name="Bot logs (errors)", value="log_channel"),
                            app_commands.Choice(name="Backups (keep private!)", value="backup_channel")])
async def setchannel(i: discord.Interaction, kind: app_commands.Choice[str], channel: discord.TextChannel):
    set_setting(kind.value, str(channel.id)); set_setting("guild_id", str(i.guild_id))
    await i.response.send_message(f"✅ **{kind.name}** will post in {channel.mention}", ephemeral=True)

@bot.tree.command(description="Set the league timezone used when entering fixture times (e.g. America/New_York)")
@staff
async def timezone(i: discord.Interaction, name: str):
    try:
        ZoneInfo(name)
    except Exception:
        return await i.response.send_message("Unknown timezone. Use a name like `America/New_York`, `Europe/London` or `UTC`.", ephemeral=True)
    set_setting("timezone", name)
    await i.response.send_message(f"✅ Fixture times are now read as **{name}**. Everyone sees them in their own local time.", ephemeral=True)

@bot.tree.command(description="Set the text shown as the 'matchday brief' in matchday messages")
@staff
async def brief(i: discord.Interaction, text: str):
    set_setting("brief_text", text[:500])
    await i.response.send_message("✅ Matchday brief updated.", ephemeral=True)

@bot.tree.command(description="Check the bot's setup and find problems")
@staff
async def health(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    g, me = i.guild, i.guild.me
    lines = []
    for key, label in (("tx_channel", "Transactions"), ("matchday_channel", "Matchdays"),
                       ("results_channel", "Results"), ("log_channel", "Bot logs")):
        ch = chan(g, key)
        if not ch:
            lines.append(f"⚠️ **{label}** channel not set (/setchannel)")
            continue
        p = ch.permissions_for(me)
        missing = [n for n, v in (("View Channel", p.view_channel), ("Send Messages", p.send_messages),
                                  ("Embed Links", p.embed_links),
                                  ("Attach Files", p.attach_files)) if not v]
        lines.append(f"{'❌' if missing else '✅'} **{label}**: {ch.mention}"
                     + (f" (bot is missing: {', '.join(missing)})" if missing else ""))
    teams_ = db.execute("SELECT name, logo, logo_blob FROM teams ORDER BY name").fetchall()
    targets = [("League logo", LEAGUE_LOGO)] if LEAGUE_LOGO else []
    targets += [(f"{t['name']} logo", t["logo"]) for t in teams_ if t["logo"] and not t["logo_blob"]][:25]
    checked = await asyncio.gather(*(check_logo(u) for _, u in targets))
    for (label, _), (ok_, why) in zip(targets, checked):
        lines.append(f"{'✅' if ok_ else '❌'} {label}" + ("" if ok_ else f": {why}. Fix with /team_logo"))
    if not LEAGUE_LOGO:
        lines.append("⚠️ League logo not set or invalid (LEAGUE_LOGO_URL)")
    if Image is None:
        lines.append("❌ Pillow isn't installed, so /table can't draw. Add Pillow to requirements.txt.")
    if not me.guild_permissions.manage_roles:
        lines.append("❌ I don't have the **Manage Roles** permission, so team roles can't be given.")
    else:
        no_role = [t["name"] for t in teams_ if not find_team_role(g, t["name"])]
        lines.append(f"⚠️ No matching role for: {', '.join(no_role[:10])}" if no_role else "✅ Every team has a matching role")
    stored = sum(1 for t in teams_ if t["logo_blob"])
    if stored:
        lines.append(f"✅ {stored} team logo(s) stored permanently")
    nologo = [t["name"] for t in teams_ if not t["logo"] and not t["logo_blob"]]
    if nologo:
        lines.append(f"⚠️ No logo yet: {', '.join(nologo[:10])}")
    one = lambda q: db.execute(q).fetchone()[0]
    stats = (f"{one('SELECT COUNT(*) FROM teams')} teams • "
             f"{one('SELECT COUNT(*) FROM players WHERE team_id IS NOT NULL')} rostered players • "
             f"{one('SELECT COUNT(*) FROM managers')} managers • "
             f"{one('SELECT COUNT(*) FROM fixtures WHERE status=' + chr(39) + 'scheduled' + chr(39))} scheduled fixtures")
    nb = len(glob.glob(os.path.join(BACKUP_DIR, "pitchx-*.db")))
    last = float(get_setting("last_backup_post") or 0)
    bch = chan(g, "backup_channel")
    lines.append(f"💾 Data file: `{DB_PATH}` ({os.path.getsize(DB_PATH) // 1024} KB, changed {tsf(os.path.getmtime(DB_PATH), 'R')}) • {nb} local backups")
    lines.append((f"✅ Off-host backup in {bch.mention}, last posted {tsf(last, 'R')}" if bch and last else
                  f"⚠️ No off-host backup yet. I'll create **#{BACKUP_CHANNEL_NAME}** (needs Manage Channels), or make that channel yourself.")
                 + ("\n⚠️ This run STARTED WITH AN EMPTY DATABASE." if STARTED_EMPTY else ""))
    lines.append(f"📊 {stats}")
    lines.append(f"🕒 Timezone: {getattr(league_tz(), 'key', 'UTC')} • discord.py {discord.__version__}")
    if not get_setting("staff_role"):
        lines.append("ℹ️ No staff role set (/staffrole). Admins and Manage Server can still use staff commands.")
    await i.followup.send(embed=discord.Embed(title="PitchX health check", color=PINK,
                                              description="\n".join(lines)[:4000]))

# ---------------------------------------------------------------- league reminders (every 2 weeks)
REMINDER_KINDS = ["next", "rivals", "pulse"]

def week_stats():
    rows = db.execute("SELECT kind, COUNT(*) n FROM events WHERE ts >= datetime('now','-7 days') GROUP BY kind").fetchall()
    c = {r["kind"]: r["n"] for r in rows}
    active = db.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE ts >= datetime('now','-7 days')").fetchone()[0]
    total = db.execute("SELECT COUNT(*) FROM players").fetchone()[0]
    return dict(signings=c.get("sign", 0), transfers=c.get("transfer", 0), loans=c.get("loan", 0),
                moves=c.get("sign", 0) + c.get("transfer", 0) + c.get("loan", 0), active=active, total=total)

def reminder_embed(kind, team):
    st = week_stats()
    if kind == "next":
        e = discord.Embed(title="🧐 Do You Know Who You're Playing Next?", color=YELLOW, description=(
            "The best managers don't just pick a team — they **study the opponent first**.\n\n"
            "> 📊 **Opponent Analysis** — Break down any team's roster and strengths\n"
            "> 🧠 See their best players, weak positions, and recent form\n"
            "> 🎯 Build your game plan before kick-off\n\n"
            "⚠️ This feature is **only available in your manager dashboard**: run `/dashboard`.\n\n"
            "Managers who prepare win more. It's that simple."))
    elif kind == "rivals":
        e = discord.Embed(title="🔥 Your Rivals Are Making Moves", color=YELLOW, description=(
            f"**{st['moves']}** transfers and signings happened in **{league_short()}** this week.\n\n"
            f"> {st['active']} managers were active — were you one of them?\n\n"
            "Other teams are strengthening. Don't get left behind."))
    else:
        e = discord.Embed(title=f"📈 {league_short()} Weekly Pulse", color=GREEN, description=(
            f"This week in **{league_short()}**:\n\n"
            f"> 🏟️ **{st['active']}** of **{st['total']}** managers were active\n"
            f"> ✍️ **{st['signings']}** new signings\n"
            f"> 💰 **{st['transfers']}** transfers\n"
            f"> 🔄 **{st['loans']}** loans\n\n"
            "The active managers are pulling ahead. Join them."))
    if kind == "next" and team:
        nf = next_fixture(team["id"])
        if nf:
            e.add_field(name="🗓️ Your next match", value=next_match_line(team, nf), inline=False)
    e.set_author(name=f"{league_short()} • Manager HQ", icon_url=LEAGUE_LOGO)
    e.set_footer(text=footer_text(team))
    e.timestamp = discord.utils.utcnow()
    return e

async def send_reminders(kind):
    sent = failed = 0
    for p in db.execute("SELECT user_id, team_id FROM players WHERE team_id IS NOT NULL").fetchall():
        try:
            u = await bot.fetch_user(p["user_id"])
            await u.send(embed=reminder_embed(kind, team_by_id(p["team_id"])), view=dashboard_view())
            sent += 1
        except (discord.Forbidden, discord.HTTPException):
            failed += 1
        await asyncio.sleep(0.6)
    return sent, failed

@tasks.loop(hours=1)
async def reminder_loop():
    now, last = time.time(), get_setting("last_reminder")
    if last is None:                     # first run: start the 2-week clock
        return set_setting("last_reminder", str(now))
    if now - float(last) >= REMINDER_DAYS * 86400:
        idx = int(get_setting("reminder_idx") or 0)
        set_setting("last_reminder", str(now)); set_setting("reminder_idx", str(idx + 1))
        await send_reminders(REMINDER_KINDS[idx % len(REMINDER_KINDS)])

@reminder_loop.before_loop
async def _wait():
    await bot.wait_until_ready()

@bot.tree.command(description="Send a league reminder to all rostered players now (test)")
@staff
@app_commands.choices(kind=[app_commands.Choice(name="Who you're playing next", value="next"),
                            app_commands.Choice(name="Rivals making moves", value="rivals"),
                            app_commands.Choice(name="Weekly pulse", value="pulse")])
async def reminder_now(i: discord.Interaction, kind: app_commands.Choice[str]):
    await i.response.defer(ephemeral=True)
    sent, failed = await send_reminders(kind.value)
    await i.followup.send(f"📨 Reminder sent: {sent} DMs, {failed} failed.")

async def notify_owner(guild, text):
    try:
        owner = guild.owner or await guild.fetch_member(guild.owner_id)
        await owner.send(text)
    except Exception:
        pass
    ch = chan(guild, "log_channel")
    if ch:
        try:
            await ch.send(text)
        except Exception:
            pass

async def find_backup_channel(guild, create=False):
    """The private channel holding database backups: saved setting, BACKUP_CHANNEL_ID, a channel named #pitchx-backups, or a new one."""
    ch = chan(guild, "backup_channel")
    if not ch:
        cid = os.getenv("BACKUP_CHANNEL_ID", "")
        ch = guild.get_channel(int(cid)) if cid.isdigit() else None
    if not ch:
        ch = discord.utils.get(guild.text_channels, name=BACKUP_CHANNEL_NAME)
    if not ch and create and AUTO_BACKUP_CHANNEL and guild.me.guild_permissions.manage_channels:
        try:
            ow = {guild.default_role: discord.PermissionOverwrite(view_channel=False),
                  guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, attach_files=True,
                                                        read_message_history=True, manage_messages=True)}
            ch = await guild.create_text_channel(BACKUP_CHANNEL_NAME, overwrites=ow, reason="PitchX automatic database backups",
                                                 topic="Automatic PitchX database backups. Keep this channel private.")
        except Exception as ex:
            print(f"[backup] couldn't create the backup channel: {ex!r}")
            ch = None
    if ch and get_setting("backup_channel") != str(ch.id):
        set_setting("backup_channel", str(ch.id))
    return ch

async def post_backup(path, guild=None):
    """Copy the database file into the private backup channel (off the host). Keeps the newest 30."""
    guild = guild or get_guild()
    if not guild or os.path.getsize(path) >= 9_000_000:
        return False
    ch = await find_backup_channel(guild, create=True)
    if not ch:
        if time.time() - float(get_setting("backup_warned") or 0) > 86400:
            set_setting("backup_warned", str(time.time()))
            await notify_owner(guild, f"⚠️ I can't keep an off-host backup of your league data. Create a private channel named "
                                      f"**#{BACKUP_CHANNEL_NAME}** (or give me **Manage Channels**) so updates can never wipe your teams.")
        return False
    try:
        await ch.send(f"💾 {league_short()} database backup • {discord.utils.utcnow():%Y-%m-%d %H:%M} UTC",
                      file=discord.File(path, filename="pitchx.db"))
        set_setting("last_backup_post", str(time.time()))
    except Exception as ex:
        print(f"[backup] posting the backup failed: {ex!r}")
        return False
    try:
        mine = [m async for m in ch.history(limit=200) if m.author.id == bot.user.id and m.attachments]
        for m in mine[30:]:
            await m.delete()
    except Exception as ex:
        print(f"[backup] cleanup failed: {ex!r}")
    return True

async def restore_from_discord(guild):
    """If the database is empty, load the newest valid backup from the backup channel. Returns the number of teams restored (0 = none)."""
    ch = await find_backup_channel(guild, create=False)
    if not ch:
        return 0
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = os.path.join(BACKUP_DIR, "from-discord.tmp")
    try:
        async for m in ch.history(limit=100):
            if m.author.id != bot.user.id:
                continue
            for a in m.attachments:
                if a.filename != "pitchx.db" or a.size > 50_000_000:
                    continue
                try:
                    await a.save(tmp)
                    chk = sqlite3.connect(tmp)
                    try:
                        tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                        if not {"teams", "players", "settings"} <= tables:
                            continue
                        n = chk.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
                        if n == 0:
                            continue
                        chk.backup(db)
                    finally:
                        chk.close()
                    ensure_schema()
                    return n
                except sqlite3.DatabaseError:
                    continue
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    return 0

async def startup_data_check():
    """Runs once after login: if the bot woke up with NO data, bring it back from Discord (or say loudly that it can't)."""
    await asyncio.sleep(3)
    try:
        if db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] > 0:
            return
        if os.getenv("AUTO_RESTORE", "1") == "1":
            for guild in bot.guilds:
                n = await restore_from_discord(guild)
                if n:
                    print(f"[db] Database was empty: restored {n} teams from the Discord backup channel")
                    await notify_owner(guild, f"♻️ The bot started with an EMPTY database, so I restored **{n}** teams "
                                              f"(plus players, budgets, fixtures, results, trophies and settings) from the latest backup.")
                    return
        print(f"[db] ⚠️ EMPTY database at {DB_PATH} and no Discord backup found")
        for guild in bot.guilds:
            await notify_owner(guild, f"⚠️ The bot started with an **empty database** (`{DB_PATH}`) and found no backup to restore. "
                                      "If you had teams before, your host replaced or wiped the data file (or started the bot from a new folder). "
                                      "Upload your last backup with `/restore`, and set `DB_PATH` to a folder that survives updates.")
    except Exception:
        traceback.print_exc()

@tasks.loop(hours=6)
async def backup_loop():
    """Backs up at every start and every 6h: a local copy, plus a copy in the private Discord backup channel."""
    try:
        if db.execute("SELECT COUNT(*) FROM teams").fetchone()[0] == 0:
            return   # never rotate good backups out because of an empty database
        path = backup_db("auto")
    except Exception:
        traceback.print_exc()
        return
    await post_backup(path)

@backup_loop.before_loop
async def _wait_bk():
    await bot.wait_until_ready()

@bot.tree.command(description="Download a backup of ALL league data (admins only)")
@admin_only
async def backup(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    path = backup_db("manual")
    if os.path.getsize(path) >= 9_000_000:
        return await i.followup.send(f"💾 Backup saved on the host at `{path}` (too large to attach).")
    await i.followup.send("💾 Backup of teams, players, budgets, fixtures, results, stats, trophies and settings. "
                          "Keep this file safe. `/restore` loads it back.", file=discord.File(path, filename="pitchx.db"))

@bot.tree.command(description="Restore ALL league data from a backup file (admins only)")
@admin_only
@app_commands.describe(database="The pitchx.db file you got from /backup")
async def restore(i: discord.Interaction, database: discord.Attachment):
    await i.response.defer(ephemeral=True)
    if database.size > 50_000_000:
        return await i.followup.send("That file is too big to be a PitchX backup.")
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = os.path.join(BACKUP_DIR, "incoming.tmp")
    await database.save(tmp)
    try:
        chk = sqlite3.connect(tmp)
        tables = {r[0] for r in chk.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"teams", "players", "settings"} <= tables:
            chk.close()
            return await i.followup.send("❌ That file isn't a PitchX backup (missing tables). Nothing was changed.")
        n_teams = chk.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
        n_players = chk.execute("SELECT COUNT(*) FROM players").fetchone()[0]
        pre = backup_db("pre-restore")           # safety copy of what is live right now
        chk.backup(db)                           # replaces the live data with the uploaded copy
        chk.close()
        ensure_schema()                          # upgrade older backups to the current layout
    except sqlite3.DatabaseError as ex:
        return await i.followup.send(f"❌ That isn't a readable database ({ex}). Nothing was changed.")
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
    await i.followup.send(f"✅ Restored **{n_teams}** teams and **{n_players}** players (plus fixtures, budgets, stats, "
                          f"trophies, settings). What was live before is saved as `{os.path.basename(pre)}`.")

@bot.tree.command(description="Create a division (admins only). You can upload a logo for it")
@admin_only
@app_commands.describe(name="Division name, e.g. Premier Division",
                       image="Upload the division's logo (optional)", logo_url="...or paste an image link instead")
async def make_division(i: discord.Interaction, name: str, image: discord.Attachment = None, logo_url: str = None):
    await i.response.defer(ephemeral=True)
    name = name.strip()[:40]
    if not name:
        return await i.followup.send("Give the division a name.")
    if db.execute("SELECT 1 FROM divisions WHERE name=?", (name,)).fetchone():
        return await i.followup.send(f"A division called **{name}** already exists.")
    png, err = await read_logo_input(image, logo_url)
    if err:
        return await i.followup.send(f"❌ {err}")
    cur = db.execute("INSERT INTO divisions(name, logo) VALUES(?,?)", (name, png)); db.commit()
    e = discord.Embed(title=f"🏆 Division created: {name}", color=GREEN,
                      description="Add teams to it with `/team_add`, then build its schedule with `/schedule_generate`."
                      + ("" if png else "\nNo logo yet. Upload one any time with `/division_logo`."))
    if png:
        e.set_thumbnail(url=f"attachment://division_{cur.lastrowid}.png")
    await i.followup.send(embed=e)

@bot.tree.command(description="Set or change a division's logo (admins only, upload an image)")
@admin_only
@app_commands.describe(image="Upload the division's logo (PNG or JPG)", logo_url="...or paste an image link instead")
@app_commands.autocomplete(division=tier_ac)
async def division_logo(i: discord.Interaction, division: str, image: discord.Attachment = None, logo_url: str = None):
    await i.response.defer(ephemeral=True)
    d = db.execute("SELECT * FROM divisions WHERE name=?", (division,)).fetchone()
    if not d:
        return await i.followup.send("Division not found.")
    png, err = await read_logo_input(image, logo_url)
    if err:
        return await i.followup.send(f"❌ {err}")
    if not png:
        return await i.followup.send("Upload an image in the `image` box (or paste a `logo_url`).")
    db.execute("UPDATE divisions SET logo=? WHERE id=?", (png, d["id"])); db.commit()
    e = discord.Embed(title=f"✅ Logo updated for {d['name']}", color=GREEN)
    e.set_thumbnail(url=f"attachment://division_{d['id']}.png")
    await i.followup.send(embed=e)

@bot.tree.command(description="Delete an empty division (admins only)")
@admin_only
@app_commands.autocomplete(division=tier_ac)
async def division_delete(i: discord.Interaction, division: str):
    d = db.execute("SELECT * FROM divisions WHERE name=?", (division,)).fetchone()
    if not d:
        return await i.response.send_message("Division not found.", ephemeral=True)
    n = db.execute("SELECT COUNT(*) FROM teams WHERE tier=?", (d["name"],)).fetchone()[0]
    if n:
        return await i.response.send_message(f"❌ **{d['name']}** still has {n} team(s). Remove them first (/team_remove).", ephemeral=True)
    db.execute("DELETE FROM divisions WHERE id=?", (d["id"],)); db.commit()
    await i.response.send_message(f"🗑️ Deleted division **{d['name']}**.", ephemeral=True)

@bot.tree.command(description="List the league's divisions")
async def divisions(i: discord.Interaction):
    rows = db.execute("""SELECT d.id, d.name, d.logo IS NOT NULL has_logo,
                         (SELECT COUNT(*) FROM teams t WHERE t.tier=d.name) n FROM divisions d ORDER BY d.name LIMIT 10""").fetchall()
    if not rows:
        return await i.response.send_message("No divisions yet. Admins can create one with /make_division.", ephemeral=True)
    embeds = []
    for r in rows:
        e = discord.Embed(title=f"🏆 {r['name']}", color=PINK, description=f"**{r['n']}** team(s)")
        if r["has_logo"]:
            e.set_thumbnail(url=f"attachment://division_{r['id']}.png")
        embeds.append(e)
    await i.response.send_message(embeds=embeds)

@bot.tree.command(description="Set the league name shown on embeds, e.g. NAPXR (admins only)")
@admin_only
async def league_name(i: discord.Interaction, short: str):
    set_setting("league_short", short.strip()[:30])
    await i.response.send_message(f"✅ Embeds will now say **{short.strip()[:30]}** (Player Card, Transactions, Matchday, Results...).", ephemeral=True)

def backfill_money():
    """One-time: rebuild the transfer log and the 'price paid = value' from offers accepted before this update."""
    if get_setting("money_backfill_v1"):
        return
    if db.execute("SELECT COUNT(*) FROM transfers").fetchone()[0] == 0:
        db.execute("""INSERT INTO transfers(ts,kind,user_id,from_team_id,to_team_id,fee)
                      SELECT datetime(created_at,'unixepoch'), kind, user_id, from_team_id, team_id, COALESCE(fee,0)
                      FROM offers WHERE status='accepted' AND kind IN ('sign','transfer','loan') ORDER BY id""")
    for r in db.execute("""SELECT o.user_id, o.fee, o.team_id FROM offers o WHERE o.status='accepted' AND o.kind='transfer'
                           AND o.id=(SELECT MAX(id) FROM offers WHERE user_id=o.user_id AND status='accepted')""").fetchall():
        p = db.execute("SELECT team_id, value_bonus FROM players WHERE user_id=?", (r["user_id"],)).fetchone()
        if p and p["team_id"] == r["team_id"] and not p["value_bonus"] and (r["fee"] or 0) > 0:
            bonus = max(0, r["fee"] - value_from(*player_totals(r["user_id"])))
            db.execute("UPDATE players SET value_bonus=? WHERE user_id=?", (bonus, r["user_id"]))
    db.commit()
    set_setting("money_backfill_v1", "1")

# ---------------------------------------------------------------- "HERE WE GO" transfer image
_render_lock = threading.Lock()

def _locked(fn, *args):
    with _render_lock:
        return fn(*args)

async def run_render(fn, *args):
    """Run a Pillow drawing job off the event loop (one at a time, so shared fonts are never used concurrently)."""
    return await asyncio.to_thread(_locked, fn, *args)

@functools.lru_cache(maxsize=16)
def _serif_italic(size):
    for n in ("DejaVuSerif-Italic.ttf", "georgiai.ttf", "timesi.ttf", "Times New Roman Italic.ttf"):
        try:
            return ImageFont.truetype(n, size)
        except OSError:
            continue
    return _font(size, True)

def clean_text(text):
    """Keep only characters the image font can draw (fancy unicode names would show as boxes)."""
    out = "".join(c for c in (text or "") if ord(c) < 0x250 or c in "–—•·")
    return " ".join(out.split())

@functools.lru_cache(maxsize=2)
def _default_stadium(W, H):
    """A night stadium: floodlights, crowd, ad boards and a striped pitch (drawn once, then cached)."""
    rnd = random.Random(7)
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    horizon, top = int(H * 0.80), int(H * 0.20)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(4 + 16 * t), int(8 + 34 * t), int(22 + 66 * t)))
    for _ in range(30000):                                    # the crowd
        x, y = rnd.randrange(W), rnd.randrange(top, horizon)
        depth = (y - top) / (horizon - top)
        b = 28 + int(64 * depth)
        col = rnd.choice([(b, b + 20, b + 58), (b + 28, b + 40, b + 90), (b + 8, b, b + 28), (b + 60, b + 50, b + 42)])
        r = 1 + int(2.2 * depth)
        d.ellipse((x - r, y - r, x + r, y + r), fill=col)
    for k in range(1, 7):                                     # stand tiers
        y = int(top + (horizon - top) * k / 7)
        d.line([(0, y), (W, y)], fill=(6, 12, 32), width=3)
    d.rectangle((0, horizon - 44, W, horizon), fill=(8, 26, 100))   # advertising boards
    for x in range(0, W, 128):
        d.rectangle((x + 8, horizon - 36, x + 112, horizon - 8), fill=(22, 64, 178))
        d.ellipse((x + 52, horizon - 30, x + 68, horizon - 14), fill=(190, 215, 255))
    for y in range(horizon, H):                               # pitch
        t = (y - horizon) / (H - horizon)
        d.line([(0, y), (W, y)], fill=(int(16 + 22 * t), int(92 + 58 * t), int(26 + 22 * t)))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    od = ImageDraw.Draw(ov)
    for x in range(0, W, 110):
        if (x // 110) % 2:
            od.rectangle((x, horizon, x + 110, H), fill=(255, 255, 255, 16))
    od.line([(W // 2, horizon), (W // 2, H)], fill=(255, 255, 255, 180), width=5)
    lights = [(70, 50), (200, 92), (340, 64), (940, 64), (1080, 92), (1210, 50), (640, 36)]
    for cx, cy in lights:                                     # light beams
        od.polygon([(cx - 8, cy), (cx + 8, cy), (cx + 170, horizon - 40), (cx - 170, horizon - 40)], fill=(170, 200, 255, 14))
    img = Image.alpha_composite(img.convert("RGBA"), ov)
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for cx, cy in lights:
        gd.ellipse((cx - 46, cy - 46, cx + 46, cy + 46), fill=(255, 255, 255, 230))
    img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(26)))
    glow2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd2 = ImageDraw.Draw(glow2)
    for cx, cy in lights:
        gd2.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=(255, 255, 255, 255))
    img = Image.alpha_composite(img, glow2.filter(ImageFilter.GaussianBlur(3)))
    vign = Image.radial_gradient("L").resize((W, H)).point(lambda v: int(v * 0.85))
    img = Image.composite(Image.new("RGB", (W, H), (0, 0, 0)), img.convert("RGB"), vign)
    return img

def stadium_background(W, H, custom=None):
    if custom:
        try:
            im = Image.open(io.BytesIO(custom)).convert("RGB")
            k = max(W / im.width, H / im.height)
            im = im.resize((int(im.width * k) + 1, int(im.height * k) + 1))
            l, t = (im.width - W) // 2, (im.height - H) // 2
            return im.crop((l, t, l + W, t + H))
        except Exception:
            pass
    return _default_stadium(W, H).copy()

def dominant_color(png):
    """Main colour of a crest (used for the tifo cloth). Falls back to blue for grey/black/white logos."""
    try:
        im = Image.open(io.BytesIO(png)).convert("RGBA")
        im.thumbnail((48, 48))
        data, buckets = im.tobytes(), {}
        for k in range(0, len(data), 4):
            r, g, b, a = data[k], data[k + 1], data[k + 2], data[k + 3]
            if a < 200:
                continue
            acc = buckets.setdefault((r >> 5, g >> 5, b >> 5), [0, 0, 0, 0])
            acc[0] += r; acc[1] += g; acc[2] += b; acc[3] += 1
        best, score = None, 0
        for r, g, b, n in buckets.values():
            r, g, b = r // n, g // n, b // n
            mx, mn = max(r, g, b), min(r, g, b)
            sat = (mx - mn) / mx if mx else 0
            if mx < 50 or (mx > 235 and sat < 0.2) or sat < 0.25:
                continue
            if n * (0.3 + sat) > score:
                best, score = (r, g, b), n * (0.3 + sat)
        if best:
            f = min(1.0, 190 / max(best))
            return tuple(int(c * f) for c in best)
    except Exception:
        pass
    return (30, 70, 170)

def render_here_we_go(avatar, from_logo, to_logo, title, sub, from_label, bg=None, to_label=None):
    """Stadium + tifo (new club's crest on it, BEHIND the player's photo) + [old club] >>> [new club]."""
    title, from_label, to_label = ascii_img(title), (ascii_img(from_label) if from_label else None), (ascii_img(to_label) if to_label else None)
    sub = ascii_img(sub) if sub else ""
    W, H, S = 1280, 720, 250
    img = stadium_background(W, H, bg).convert("RGBA")
    d = ImageDraw.Draw(img)
    top_y, bot_y = 330, 540
    poly = [(330, top_y), (950, top_y), (1030, bot_y), (250, bot_y)]
    col = dominant_color(to_logo) if to_logo else (30, 70, 170)
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon([(x + 10, y + 14) for x, y in poly], fill=(0, 0, 0, 170))
    img.alpha_composite(shadow.filter(ImageFilter.GaussianBlur(14)))
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).polygon(poly, fill=255)
    cloth = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cloth.paste(col + (255,), mask=mask)
    folds = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    fd = ImageDraw.Draw(folds)
    for x in range(230, 1060, 6):                             # cloth folds
        s = math.sin(x / 19.0) * math.sin(x / 71.0 + 1)
        line = [(x, top_y - 6), (x + (x - 640) // 14, bot_y + 6)]
        fd.line(line, fill=(255, 255, 255, int(70 * s)) if s >= 0 else (0, 0, 0, int(90 * -s)), width=6)
    for k in range(50):                                       # sag shadow along the bottom
        fd.line([(0, bot_y - 50 + k), (W, bot_y - 50 + k)], fill=(0, 0, 0, int(k * 1.7)))
    folds.putalpha(ImageChops.multiply(folds.getchannel("A"), mask))
    img.alpha_composite(cloth)
    img.alpha_composite(folds)
    if to_logo:                                               # crest on the tifo: drawn BEFORE the photo, so it stays behind it
        lg = _logo_img(to_logo, 230)
        if lg:
            disc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            ImageDraw.Draw(disc).ellipse((455 - 112, 432 - 112, 455 + 112, 432 + 112), fill=(255, 255, 255, 38))
            img.alpha_composite(disc)
            img.alpha_composite(lg, (int(455 - lg.width / 2), int(432 - lg.height / 2)))
    ax0, ay0 = 640 - S // 2, 312
    right_edge = 950 + 80 * ((432 - top_y) / (bot_y - top_y)) - 24
    avail = right_edge - (ax0 + S + 16)
    for sz in range(64, 24, -2):
        f = _serif_italic(sz)
        if d.textlength("Welcome!", font=f) <= avail:
            break
    _put(d, (ax0 + S + 16 + avail / 2, 432), "Welcome!", f, (255, 255, 255), "mm")
    sh2 = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(sh2).rectangle((ax0 - 4, ay0 + 6, ax0 + S + 10, ay0 + S + 16), fill=(0, 0, 0, 190))
    img.alpha_composite(sh2.filter(ImageFilter.GaussianBlur(9)))
    d.rectangle((ax0 - 6, ay0 - 6, ax0 + S + 6, ay0 + S + 6), fill=(255, 255, 255))
    try:
        av = Image.open(io.BytesIO(avatar)).convert("RGBA").resize((S, S))
        img.alpha_composite(av, (ax0, ay0))
    except Exception:
        d.rectangle((ax0, ay0, ax0 + S, ay0 + S), fill=(55, 58, 64))
        _put(d, (640, ay0 + S // 2), "?", _font(110, True), (255, 255, 255), "mm")
    cy, LS = 634, 134                                         # bottom row: [old club] >>> [new club]
    def club(data, cx, label):
        lg = _logo_img(data, LS) if data else None
        if lg:
            img.alpha_composite(lg, (int(cx - lg.width / 2), int(cy - lg.height / 2)))
        else:
            d.ellipse((cx - 62, cy - 62, cx + 62, cy + 62), fill=(58, 62, 70), outline=(255, 255, 255), width=3)
            if label:
                _put(d, (cx, cy), label[:1].upper(), _font(60, True), (255, 255, 255), "mm")
            else:
                _put(d, (cx, cy - 15), "FREE", _font(28, True), (255, 255, 255), "mm")
                _put(d, (cx, cy + 15), "AGENT", _font(28, True), (255, 255, 255), "mm")
    club(from_logo, 430, from_label)
    club(to_logo, 850, to_label or "?")
    for k in range(3):
        x = 548 + k * 64
        pts = [(x, cy - 36), (x + 34, cy - 36), (x + 66, cy), (x + 34, cy + 36), (x, cy + 36), (x + 32, cy)]
        d.polygon(pts, fill=(8, 8, 8))
        d.line(pts + [pts[0]], fill=(255, 255, 255), width=3)
    _put(d, (640, 98), title, _font(112, True), (255, 255, 255), "mm", stroke_width=8, stroke_fill=(0, 0, 0))
    if sub:
        for sz in (42, 36, 30, 24):
            f = _font(sz, True)
            if d.textlength(sub, font=f) <= 1100:
                break
        _put(d, (640, 190), sub, f, (232, 238, 255), "mm", stroke_width=4, stroke_fill=(0, 0, 0))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()

async def make_here_we_go(kind, user, new, old, fee=0):
    """JPEG bytes of the transfer image, or None (no Pillow / any error: the text post still goes out)."""
    if Image is None or not new:
        return None
    try:
        avatar = None
        try:
            avatar = await user.display_avatar.replace(size=512, format="png").read()
        except Exception:
            pass
        to_logo, from_logo = await asyncio.gather(team_logo_bytes(new), team_logo_bytes(old))
        row = db.execute("SELECT data FROM assets WHERE name='transfer_bg'").fetchone()
        title = {"transfer": "HERE WE GO!", "sign": "OFFICIAL", "loan": "LOAN DEAL"}.get(kind, "OFFICIAL")
        name = clean_text(user.display_name) or clean_text(getattr(user, "name", ""))
        sub = " - ".join(x for x in (name, money(fee) if fee else "") if x)
        return await run_render(render_here_we_go, avatar, from_logo, to_logo, title, sub,
                                old["name"] if old else None, row["data"] if row else None, new["name"])
    except Exception as ex:
        global _last_image_error
        _last_image_error = f"{type(ex).__name__}: {str(ex)[:120]}"
        print(f"[herewego] image failed: {ex!r}")
        return None

def normalize_bg(raw):
    try:
        im = Image.open(io.BytesIO(raw)).convert("RGB")
        k = max(1280 / im.width, 720 / im.height)
        im = im.resize((int(im.width * k) + 1, int(im.height * k) + 1))
        l, t = (im.width - 1280) // 2, (im.height - 720) // 2
        buf = io.BytesIO()
        im.crop((l, t, l + 1280, t + 720)).save(buf, "JPEG", quality=90)
        return buf.getvalue()
    except Exception:
        return None

@bot.tree.command(description="Set the background photo for the 'Here we go' transfer images (admins only)")
@admin_only
@app_commands.describe(image="Upload a stadium photo (leave empty to go back to the default stadium)")
async def transfer_background(i: discord.Interaction, image: discord.Attachment = None):
    await i.response.defer(ephemeral=True)
    if not image:
        db.execute("DELETE FROM assets WHERE name='transfer_bg'"); db.commit()
        return await i.followup.send("✅ Back to the default stadium background.")
    if Image is None:
        return await i.followup.send("❌ Pillow isn't installed on the host.")
    if (image.content_type and not image.content_type.startswith("image/")) or image.size > 12_000_000:
        return await i.followup.send("That needs to be an image under 12 MB.")
    data = await asyncio.to_thread(normalize_bg, await image.read())
    if not data:
        return await i.followup.send("❌ I couldn't read that image. Use a PNG or JPG.")
    db.execute("INSERT OR REPLACE INTO assets(name, data) VALUES('transfer_bg', ?)", (data,)); db.commit()
    await i.followup.send("✅ New background saved. Use `/herewego_preview` to see it.")

@bot.tree.command(description="Preview the 'Here we go' image without making a transfer (staff)")
@staff
@app_commands.describe(to_team="The club they join", from_team="The club they leave (empty = free agent signing)",
                       fee="Optional fee to show, e.g. 5m",
                       post="Also post it in the transactions channel, to test that I'm allowed to")
@app_commands.autocomplete(to_team=team_ac, from_team=team_ac)
async def herewego_preview(i: discord.Interaction, player: discord.Member, to_team: str,
                           from_team: str = None, fee: str = "0", post: bool = False):
    await i.response.defer(ephemeral=True)
    new, old = get_team(to_team), (get_team(from_team) if from_team else None)
    if not new or (from_team and not old):
        return await i.followup.send("Team not found.")
    png = await make_here_we_go("transfer" if old else "sign", player, new, old, parse_money(fee) or 0)
    if not png:
        return await i.followup.send(f"❌ Couldn't draw the image: {_last_image_error or 'Pillow isn' + chr(39) + 't installed?'}")
    if not post:
        return await i.followup.send(file=discord.File(io.BytesIO(png), filename="herewego.jpg"))
    public = tx_embed("🧪 TEST: HERE WE GO!", f"{player.mention} → **{new['name']}** (only a test, nothing changed)", "", new, GREEN)
    public.set_image(url="attachment://herewego.jpg")
    ch, note = await announce_deal(i.guild, {"user_id": player.id, "channel_id": i.channel_id}, public, png)
    await i.followup.send((f"✅ Test posted in {ch.mention}." if ch else "❌ The test post failed.") + (f"\n{note}" if note else ""))

# ---------------------------------------------------------------- team roles (found by name, never created)
LEADER_WORDS = {"manager", "owner", "captain", "coach", "staff", "admin", "mod", "moderator", "assistant", "founder", "head"}

def _tokens(text):
    return [t for t in re.split(r"[\W_]+", (text or "").lower()) if t]

def role_score(role_name, team_name):
    """How well an existing role matches a team: 'NAPXR | RMA' scores high for RMA, 'RMA Manager' scores 0."""
    rt, tt = _tokens(role_name), _tokens(team_name)
    if not tt or not rt:
        return 0
    n = len(tt)
    pos = next((k for k in range(len(rt) - n + 1) if rt[k:k + n] == tt), None)
    if pos is None:
        return 0
    if any(w in LEADER_WORDS and w not in tt for w in rt[:pos] + rt[pos + n:]):
        return 0
    if rt == tt:
        return 5
    return 4 if pos + n == len(rt) else 3

def find_team_role(guild, team_name):
    """The existing role whose name contains the team's name (best match, shortest name wins ties)."""
    best = None
    for r in guild.roles:
        if r.is_default() or r.managed:
            continue
        sc = role_score(r.name, team_name)
        if sc and (best is None or (sc, -len(r.name)) > best[0]):
            best = ((sc, -len(r.name)), r)
    return best[1] if best else None

async def apply_team_roles(guild, uid, old, new):
    """Swap the player's old team role for the new team's role. Returns a short note (for the staff DM)."""
    if not guild:
        return ""
    if not guild.me.guild_permissions.manage_roles:
        return "⚠️ Roles not changed: I need the **Manage Roles** permission."
    try:
        member = guild.get_member(uid) or await guild.fetch_member(uid)
    except discord.HTTPException:
        return "⚠️ Roles not changed: that player isn't in the server."
    notes, remove, add = [], None, None
    if old:
        remove = find_team_role(guild, old["name"])
    if new:
        add = find_team_role(guild, new["name"])
        if not add:
            notes.append(f"⚠️ No role containing **{new['name']}** exists, so no team role was given.")
    try:
        if remove and remove != add and remove in member.roles:
            if remove.is_assignable():
                await member.remove_roles(remove, reason="PitchX: left the team")
                notes.append(f"➖ removed {remove.mention}")
            else:
                notes.append(f"⚠️ I can't remove {remove.mention}: move my bot role above it.")
        if add and add not in member.roles:
            if add.is_assignable():
                await member.add_roles(add, reason="PitchX: joined the team")
                notes.append(f"➕ gave {add.mention}")
            else:
                notes.append(f"⚠️ I can't give {add.mention}: move my bot role above it.")
    except discord.HTTPException as ex:
        notes.append(f"⚠️ Role change failed ({type(ex).__name__}).")
    return " • ".join(notes)

@bot.tree.command(description="Give every rostered player their team's role (admins only)")
@admin_only
async def sync_roles(i: discord.Interaction):
    await i.response.defer(ephemeral=True)
    g = i.guild
    if not g.me.guild_permissions.manage_roles:
        return await i.followup.send("❌ I need the **Manage Roles** permission.")
    given, already, no_role, blocked, gone = 0, 0, set(), set(), 0
    for p in db.execute("SELECT user_id, team_id FROM players WHERE team_id IS NOT NULL").fetchall():
        t = team_by_id(p["team_id"])
        member = g.get_member(p["user_id"])
        if not t or not member:
            gone += 1
            continue
        role = find_team_role(g, t["name"])
        if not role:
            no_role.add(t["name"])
        elif role in member.roles:
            already += 1
        elif not role.is_assignable():
            blocked.add(role.name)
        else:
            try:
                await member.add_roles(role, reason="PitchX: sync roles")
                given += 1
            except discord.HTTPException:
                blocked.add(role.name)
            await asyncio.sleep(0.4)
    lines = [f"✅ Gave **{given}** role(s). **{already}** player(s) already had theirs."]
    if no_role:
        lines.append(f"⚠️ No matching role for: {', '.join(sorted(no_role))}")
    if blocked:
        lines.append(f"⚠️ I can't assign (move my bot role higher): {', '.join(sorted(blocked))}")
    if gone:
        lines.append(f"ℹ️ {gone} rostered player(s) aren't in the server.")
    await i.followup.send("\n".join(lines))

def backfill_sign_bonus():
    """One-time: players already signed from free agency before this update get the signing value bonus."""
    if get_setting("sign_backfill_v1"):
        return
    for r in db.execute("""SELECT o.user_id, o.team_id FROM offers o WHERE o.status='accepted' AND o.kind='sign'
                           AND o.id=(SELECT MAX(id) FROM offers WHERE user_id=o.user_id AND status='accepted')""").fetchall():
        p = db.execute("SELECT team_id, value_bonus FROM players WHERE user_id=?", (r["user_id"],)).fetchone()
        if p and p["team_id"] == r["team_id"] and not p["value_bonus"]:
            db.execute("UPDATE players SET value_bonus=? WHERE user_id=?", (SIGN_VALUE_BONUS, r["user_id"]))
    db.commit()
    set_setting("sign_backfill_v1", "1")

# ---------------------------------------------------------------- manager dashboard (/dashboard)
def can_manage(member, team_id):
    return bool(member) and hasattr(member, "guild_permissions") and (is_staff_member(member) or team_id in managed_team_ids(member.id))

def squad_rows(tid):
    rows = []
    for p in db.execute("SELECT user_id FROM players WHERE team_id=?", (tid,)).fetchall():
        g, a, c = player_totals(p["user_id"])
        rows.append({"uid": p["user_id"], "g": g, "a": a, "c": c, "value": player_value(p["user_id"])})
    rows.sort(key=lambda r: (-r["value"], -r["g"]))
    return rows

def pending_offers(tid):
    return db.execute("""SELECT * FROM offers WHERE status='pending' AND created_at > ? AND (team_id=? OR from_team_id=?)
                         ORDER BY id DESC LIMIT 25""", (time.time() - OFFER_HOURS * 3600, tid, tid)).fetchall()

OFFER_ICON = {"sign": "✍️", "transfer": "🔁", "loan": "🤝", "release": "📤"}

def standing_row(t):
    return next((r for r in standings(t["tier"]) if r["team"]["id"] == t["id"]), None)

def fx_line(t, f):
    home = f["home_id"] == t["id"]
    opp = name_of(f["away_id"] if home else f["home_id"])
    if f["status"] == "played":
        gf, ga = (f["home_goals"], f["away_goals"]) if home else (f["away_goals"], f["home_goals"])
        res = "W" if gf > ga else "D" if gf == ga else "L"
        return f"{FORM[res]} **{gf}–{ga}** vs {opp} ({'H' if home else 'A'})" + (f" • GW{f['gw']}" if f["gw"] else "")
    return f"{tsf(f['kickoff'])} • vs **{opp}** ({'H' if home else 'A'})" + (f" • GW{f['gw']}" if f["gw"] else "")

def dash_base(t, title):
    e = discord.Embed(title=f"{t['name']} • {title}", color=PINK, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} • Manager Dashboard", icon_url=LEAGUE_LOGO)
    if logo(t):
        e.set_thumbnail(url=logo(t))
    return e

def dash_overview(t):
    e = dash_base(t, "Overview")
    r = standing_row(t)
    form = " ".join(FORM.get(c, "") for c in form_str(t["id"])) or "no games yet"
    if r:
        e.add_field(name="📊 League", value=f"**{ordinal(r['pos'])}** in {t['tier']}\n{r['pts']} pts • {r['w']}W {r['d']}D {r['l']}L")
        e.add_field(name="⚽ Goals", value=f"{r['gf']} for • {r['ga']} against ({r['gf'] - r['ga']:+d})")
    e.add_field(name="📈 Form", value=form)
    squad = squad_rows(t["id"])
    e.add_field(name="💰 Budget", value=money(budget_of(t)))
    e.add_field(name="👥 Squad", value=f"{len(squad)} players • {money(sum(x['value'] for x in squad))}")
    e.add_field(name="📨 Pending offers", value=str(len(pending_offers(t["id"]))))
    nf = next_fixture(t["id"])
    e.add_field(name="🗓️ Next match", inline=False, value=next_match_line(t, nf) if nf else "Nothing scheduled")
    last = db.execute("""SELECT * FROM fixtures WHERE status='played' AND (home_id=? OR away_id=?)
                         ORDER BY played_at DESC, id DESC LIMIT 1""", (t["id"], t["id"])).fetchone()
    if last:
        e.add_field(name="🏁 Last result", value=fx_line(t, last), inline=False)
    best = [x for x in squad if x["g"] or x["a"] or x["c"]]
    if best:
        top_g = max(best, key=lambda x: x["g"])
        top_a = max(best, key=lambda x: x["a"])
        bits = []
        if top_g["g"]:
            bits.append(f"⚽ <@{top_g['uid']}> ({top_g['g']})")
        if top_a["a"]:
            bits.append(f"🎯 <@{top_a['uid']}> ({top_a['a']})")
        if bits:
            e.add_field(name="⭐ Top performers", value=" • ".join(bits), inline=False)
    return e

def dash_squad(t):
    e = dash_base(t, "Squad")
    squad = squad_rows(t["id"])
    if not squad:
        e.description = "No players yet. Press **Sign** to make an offer."
        return e
    e.description = "\n".join(f"<@{x['uid']}> • **{money(x['value'])}** • ⚽{x['g']} 🎯{x['a']} 🧤{x['c']}" for x in squad[:20])
    e.set_footer(text=f"{len(squad)} players • squad value {money(sum(x['value'] for x in squad))}")
    return e

def dash_fixtures(t):
    e = dash_base(t, "Fixtures")
    up = db.execute("""SELECT * FROM fixtures WHERE status='scheduled' AND (home_id=? OR away_id=?)
                       ORDER BY kickoff LIMIT 5""", (t["id"], t["id"])).fetchall()
    done = db.execute("""SELECT * FROM fixtures WHERE status='played' AND (home_id=? OR away_id=?)
                         ORDER BY played_at DESC, id DESC LIMIT 5""", (t["id"], t["id"])).fetchall()
    e.add_field(name="🗓️ Upcoming", inline=False, value="\n".join(fx_line(t, f) for f in up) or "Nothing scheduled")
    e.add_field(name="🏁 Recent results", inline=False, value="\n".join(fx_line(t, f) for f in done) or "No games played yet")
    return e

def dash_money(t):
    e = dash_base(t, "Transfers & budget")
    spent, got = team_money(t["id"])
    e.add_field(name="💰 Budget left", value=money(budget_of(t)))
    e.add_field(name="📉 Spent", value=money(spent))
    e.add_field(name="📈 Received", value=money(got))
    e.add_field(name="🛒 Bought / signed", inline=False, value=deal_lines(t["id"], True, 8) or "No signings yet")
    sold = deal_lines(t["id"], False, 8)
    if sold:
        e.add_field(name="📤 Sold", inline=False, value=sold)
    return e

def dash_offers(t):
    e = dash_base(t, "Offers")
    pend = pending_offers(t["id"])
    lines = []
    for o in pend:
        exp = tsf(o["created_at"] + OFFER_HOURS * 3600, "R")
        fee = f" • {money(o['fee'])}" if o["fee"] else ""
        lines.append(f"{OFFER_ICON.get(o['kind'], '•')} {o['kind']} <@{o['user_id']}>{fee} • expires {exp}")
    e.add_field(name="⏳ Waiting for the player's answer", inline=False, value="\n".join(lines) or "No pending offers")
    done = db.execute("""SELECT * FROM offers WHERE status IN ('accepted','declined','expired') AND (team_id=? OR from_team_id=?)
                         ORDER BY id DESC LIMIT 6""", (t["id"], t["id"])).fetchall()
    mark = {"accepted": "✅", "declined": "❌", "expired": "⌛"}
    e.add_field(name="🕘 Recent answers", inline=False,
                value="\n".join(f"{mark[o['status']]} {o['kind']} <@{o['user_id']}>" + (f" • {money(o['fee'])}" if o["fee"] else "") for o in done) or "Nothing yet")
    return e

def rank_of(rows, tid, key, reverse):
    ordered = sorted(rows, key=key, reverse=reverse)
    return next((k for k, r in enumerate(ordered, 1) if r["team"]["id"] == tid), len(ordered)), len(ordered)

def dash_scout(t, opp):
    if not opp:
        e = dash_base(t, "Scouting")
        e.description = "No upcoming match found. Pick any team below to scout it."
        return e
    e = discord.Embed(title=f"🎯 Scouting: {opp['name']}", color=ORANGE, timestamp=discord.utils.utcnow())
    e.set_author(name=f"{league_short()} • Manager Dashboard", icon_url=LEAGUE_LOGO)
    if logo(opp):
        e.set_thumbnail(url=logo(opp))
    rows = standings(opp["tier"])
    r = next((x for x in rows if x["team"]["id"] == opp["id"]), None)
    form = " ".join(FORM.get(c, "") for c in form_str(opp["id"])) or "no games yet"
    if r and r["p"]:
        n = r["p"]
        a_rank, total = rank_of(rows, opp["id"], lambda x: x["gf"] / max(x["p"], 1), True)
        d_rank, _ = rank_of(rows, opp["id"], lambda x: x["ga"] / max(x["p"], 1), False)
        e.add_field(name="📊 Record", value=f"**{ordinal(r['pos'])}** • {r['pts']} pts\n{r['w']}W {r['d']}D {r['l']}L", inline=True)
        e.add_field(name="📈 Form", value=form, inline=True)
        e.add_field(name="⚔️ Style", inline=False, value=
            f"🔥 Attack: **{r['gf'] / n:.1f}** goals/game (#{a_rank} of {total})\n"
            f"🛡️ Defence: **{r['ga'] / n:.1f}** conceded/game (#{d_rank} of {total})")
        weak = "a leaky defence: attack early and shoot often" if d_rank / total > a_rank / total else \
               "a blunt attack: stay compact and hit them on the break"
        e.add_field(name="💡 Game plan", value=f"Their weak spot is {weak}.", inline=False)
    else:
        e.add_field(name="📊 Record", value="No matches played yet", inline=True)
        e.add_field(name="📈 Form", value=form, inline=True)
    squad = squad_rows(opp["id"])
    if squad:
        e.add_field(name="👥 Squad (best first)", inline=False, value="\n".join(
            f"<@{x['uid']}> • {money(x['value'])} • ⚽{x['g']} 🎯{x['a']} 🧤{x['c']}" for x in squad[:6]))
        tags = []
        scorer = max(squad, key=lambda x: x["g"])
        maker = max(squad, key=lambda x: x["a"])
        wall = max(squad, key=lambda x: x["c"])
        if scorer["g"]:
            tags.append(f"⚽ Danger man: <@{scorer['uid']}> ({scorer['g']})")
        if maker["a"]:
            tags.append(f"🎯 Playmaker: <@{maker['uid']}> ({maker['a']})")
        if wall["c"]:
            tags.append(f"🧤 Clean sheets: <@{wall['uid']}> ({wall['c']})")
        if tags:
            e.add_field(name="⭐ Players to watch", value="\n".join(tags), inline=False)
    else:
        e.add_field(name="👥 Squad", value="No players signed yet", inline=False)
    h2h = "\n".join("> " + x for x in h2h_text(t["id"], opp["id"]).split("\n"))
    e.add_field(name="⚔️ Head to head", value=h2h, inline=False)
    nxt = db.execute("""SELECT * FROM fixtures WHERE status='scheduled' AND ((home_id=? AND away_id=?) OR (home_id=? AND away_id=?))
                        ORDER BY kickoff LIMIT 1""", (t["id"], opp["id"], opp["id"], t["id"])).fetchone()
    if nxt:
        e.add_field(name="🗓️ Next meeting", value=f"{tsf(nxt['kickoff'])} ({tsf(nxt['kickoff'], 'R')})", inline=False)
    return e

class FeeModal(discord.ui.Modal):
    def __init__(self, kind, player, team_id, hint):
        super().__init__(title=("Transfer offer" if kind == "transfer" else "Loan offer"))
        self.kind, self.player_id, self.team_id = kind, player.id, team_id
        self.fee = discord.ui.TextInput(label="Fee", placeholder=hint[:100], required=(kind == "transfer"), max_length=20)
        self.add_item(self.fee)

    async def on_submit(self, i: discord.Interaction):
        await i.response.defer(ephemeral=True)
        t = team_by_id(self.team_id)
        if not t or not can_manage(i.user, self.team_id):
            return await i.followup.send("You can't make offers for this team.")
        try:
            player = i.guild.get_member(self.player_id) or await i.guild.fetch_member(self.player_id)
        except discord.HTTPException:
            return await i.followup.send("That player isn't in the server.")
        fee = str(self.fee.value or "").strip()
        if self.kind == "transfer":
            await offer_transfer(i, player, t, fee)
        else:
            await offer_loan(i, player, t, fee or "0")

class Dashboard(discord.ui.View):
    TABS = [("overview", "🏠 Overview"), ("squad", "👥 Squad"), ("fixtures", "🗓️ Fixtures"),
            ("money", "💸 Transfers & budget"), ("offers", "📨 Offers"), ("scout", "🎯 Scouting")]

    def __init__(self, user_id, team_id, staff_user=False, tab="overview"):
        super().__init__(timeout=900)
        self.user_id, self.team_id, self.staff_user = user_id, team_id, staff_user
        self.tab, self.scout_id = tab, None
        self._build()

    def team(self):
        return team_by_id(self.team_id)

    def embed(self):
        t = self.team()
        if not t:
            return discord.Embed(title="This team no longer exists", color=RED)
        if self.tab == "squad":
            return dash_squad(t)
        if self.tab == "fixtures":
            return dash_fixtures(t)
        if self.tab == "money":
            return dash_money(t)
        if self.tab == "offers":
            return dash_offers(t)
        if self.tab == "scout":
            opp = team_by_id(self.scout_id) if self.scout_id else None
            if not opp:
                nf = next_fixture(t["id"])
                opp = team_by_id(nf["away_id"] if nf["home_id"] == t["id"] else nf["home_id"]) if nf else None
            return dash_scout(t, opp)
        return dash_overview(t)

    def _build(self):
        self.clear_items()
        tabs = discord.ui.Select(placeholder="Dashboard section", row=0, options=[
            discord.SelectOption(label=n, value=k, default=(k == self.tab)) for k, n in self.TABS])
        tabs.callback = self.on_tab
        self.add_item(tabs)
        t = self.team()
        if self.tab == "scout" and t:
            others = db.execute("SELECT id, name FROM teams WHERE id<>? ORDER BY (tier=?) DESC, name LIMIT 25",
                                (self.team_id, t["tier"])).fetchall()
            if others:
                sel = discord.ui.Select(placeholder="Scout any team (default: your next opponent)", row=1, options=[
                    discord.SelectOption(label=o["name"][:100], value=str(o["id"]), default=(self.scout_id == o["id"])) for o in others])
                sel.callback = self.on_scout
                self.add_item(sel)
        elif self.tab == "offers":
            pend = pending_offers(self.team_id)
            if pend:
                sel = discord.ui.Select(placeholder="Cancel a pending offer", row=1, options=[
                    discord.SelectOption(label=f"{o['kind'].title()}: {member_name(o['user_id'])}"[:100], value=str(o["id"]),
                                         emoji=OFFER_ICON.get(o["kind"])) for o in pend])
                sel.callback = self.on_cancel
                self.add_item(sel)
        buttons = [("Sign", "✍️", discord.ButtonStyle.success, self.on_sign),
                   ("Transfer", "🔁", discord.ButtonStyle.primary, self.on_transfer),
                   ("Loan", "🤝", discord.ButtonStyle.primary, self.on_loan)]
        if self.staff_user:
            buttons.append(("Release", "📤", discord.ButtonStyle.danger, self.on_release))
        buttons.append(("Refresh", "🔃", discord.ButtonStyle.secondary, self.on_refresh))
        for label, emoji, style, cb in buttons:
            b = discord.ui.Button(label=label, emoji=emoji, style=style, row=2)
            b.callback = cb
            self.add_item(b)

    async def interaction_check(self, i: discord.Interaction):
        if i.user.id != self.user_id:
            await i.response.send_message("This is someone else's dashboard. Use /dashboard to open yours.", ephemeral=True)
            return False
        if not can_manage(i.user, self.team_id):
            await i.response.send_message("You no longer manage this team.", ephemeral=True)
            return False
        self.staff_user = is_staff_member(i.user)
        return True

    async def on_error(self, i: discord.Interaction, error: Exception, item):
        traceback.print_exception(type(error), error, error.__traceback__)
        msg = f"⚠️ Something went wrong: {type(error).__name__}: {str(error)[:200]}"
        try:
            if i.response.is_done():
                await i.followup.send(msg, ephemeral=True)
            else:
                await i.response.send_message(msg, ephemeral=True)
        except discord.HTTPException:
            pass

    async def redraw(self, i: discord.Interaction):
        self._build()
        await i.response.edit_message(embed=self.embed(), view=self)

    async def on_tab(self, i: discord.Interaction):
        self.tab = i.data["values"][0]
        await self.redraw(i)

    async def on_scout(self, i: discord.Interaction):
        self.scout_id = int(i.data["values"][0])
        await self.redraw(i)

    async def on_cancel(self, i: discord.Interaction):
        oid = int(i.data["values"][0])
        db.execute("UPDATE offers SET status='cancelled' WHERE id=? AND status='pending' AND (team_id=? OR from_team_id=?)",
                   (oid, self.team_id, self.team_id)); db.commit()
        await self.redraw(i)

    async def on_refresh(self, i: discord.Interaction):
        await self.redraw(i)

    async def _pick(self, i, prompt, action):
        v = discord.ui.View(timeout=180)
        us = discord.ui.UserSelect(placeholder="Pick the player")
        async def picked(i2: discord.Interaction):
            if i2.user.id != self.user_id or not can_manage(i2.user, self.team_id):
                return await i2.response.send_message("Not allowed.", ephemeral=True)
            await action(i2, us.values[0])
        us.callback = picked
        v.add_item(us)
        await i.response.send_message(prompt, view=v, ephemeral=True)

    async def on_sign(self, i: discord.Interaction):
        async def action(i2, player):
            await i2.response.defer(ephemeral=True)
            await offer_sign(i2, player, self.team())
        await self._pick(i, "**Who do you want to sign?** They must be a free agent.", action)

    def _fee_action(self, kind):
        async def action(i2, player):
            t = self.team()
            if kind == "transfer":
                minimum = int(player_value(player.id) * MIN_FEE_PERCENT / 100)
                hint = f"Minimum {money(minimum)} • your budget {money(budget_of(t))}"
            else:
                hint = f"Optional, e.g. 500k • your budget {money(budget_of(t))}"
            await i2.response.send_modal(FeeModal(kind, player, self.team_id, hint))
        return action

    async def on_transfer(self, i: discord.Interaction):
        await self._pick(i, "**Which player do you want to buy?** They must be on another team.", self._fee_action("transfer"))

    async def on_loan(self, i: discord.Interaction):
        await self._pick(i, "**Which player do you want on loan?** They must be on another team.", self._fee_action("loan"))

    async def on_release(self, i: discord.Interaction):
        squad = squad_rows(self.team_id)
        if not squad:
            return await i.response.send_message("Nobody to release.", ephemeral=True)
        v = discord.ui.View(timeout=180)
        sel = discord.ui.Select(placeholder="Who should be released?", options=[
            discord.SelectOption(label=member_name(x["uid"])[:100], value=str(x["uid"])) for x in squad[:25]])
        async def picked(i2: discord.Interaction):
            if i2.user.id != self.user_id or not can_manage(i2.user, self.team_id) or not is_staff_member(i2.user):
                return await i2.response.send_message("Only staff can release players.", ephemeral=True)
            await i2.response.defer(ephemeral=True)
            uid = int(i2.data["values"][0])
            cur = player_team(uid)
            if not cur or cur["id"] != self.team_id:
                return await i2.followup.send("That player isn't on this team any more.")
            player = i2.guild.get_member(uid) or await i2.guild.fetch_member(uid)
            await send_offer(i2, player, "release", None, cur)
        sel.callback = picked
        v.add_item(sel)
        await i.response.send_message("**Who should be released?** They get an Accept / Decline request.", view=v, ephemeral=True)

def member_name(uid):
    g = get_guild()
    m = g.get_member(uid) if g else None
    return m.display_name if m else str(uid)

@bot.tree.command(description="Your manager dashboard: squad, finances, fixtures, offers and scouting")
@staff_or_manager
@app_commands.describe(team="Your team (optional if you manage just one)")
@app_commands.autocomplete(team=my_team_ac)
async def dashboard(i: discord.Interaction, team: str = None):
    t, err = resolve_team(i, team)
    if err:
        return await i.response.send_message(err, ephemeral=True)
    view = Dashboard(i.user.id, t["id"], is_staff_member(i.user))
    await i.response.send_message(embed=view.embed(), view=view, ephemeral=True)

@bot.tree.command(description="Set the role that can use staff commands (admins always can)")
@staff
async def staffrole(i: discord.Interaction, role: discord.Role):
    if not i.user.guild_permissions.manage_guild:
        return await i.response.send_message("Only admins can change the staff role.", ephemeral=True)
    set_setting("staff_role", str(role.id))
    await i.response.send_message(f"✅ Members with {role.mention} can now use staff commands.", ephemeral=True)

async def log_error(where, err):
    ch = chan(get_guild(), "log_channel")
    if ch:
        try:
            await ch.send(f"⚠️ `/{where}` failed: `{type(err).__name__}: {str(err)[:600]}`")
        except Exception:
            pass

@bot.tree.error
async def on_app_error(i: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.CheckFailure):
        if str(error) == "not admin":
            msg = "🚫 Admins only (Administrator or Manage Server)."
        elif str(error) == "not staff or manager":
            msg = "🚫 Only staff and team managers can use this. Ask staff to add you with /manager_add."
        else:
            msg = "🚫 Staff only. You need Administrator, Manage Server, or the staff role (/staffrole)."
    else:
        orig = getattr(error, "original", error)
        traceback.print_exception(type(orig), orig, orig.__traceback__)
        await log_error(i.command.name if i.command else "command", orig)
        msg = f"⚠️ Something went wrong: {type(orig).__name__}: {str(orig)[:200]}"
    try:
        if i.response.is_done():
            await i.followup.send(msg, ephemeral=True)
        else:
            await i.response.send_message(msg, ephemeral=True)
    except discord.HTTPException:
        pass

@bot.event
async def setup_hook():
    bot.add_dynamic_items(OfferButton)
    reminder_loop.start()
    backup_loop.start()
    matchday_loop.start()
    backfill_money()
    backfill_sign_bonus()
    asyncio.create_task(migrate_logos())
    if GUILD_ID:
        # server-only commands (appear instantly); wipe the global copies that cause duplicates
        g = discord.Object(int(GUILD_ID))
        bot.tree.copy_global_to(guild=g)
        await bot.tree.sync(guild=g)
        bot.tree.clear_commands(guild=None)
        await bot.tree.sync()
    else:
        await bot.tree.sync()

_cleaned = False
_startup_checked = False

@bot.event
async def on_ready():
    global _cleaned, _startup_checked
    print(f"Logged in as {bot.user}")
    if not _startup_checked:
        _startup_checked = True
        asyncio.create_task(startup_data_check())
    if not GUILD_ID and not _cleaned:   # remove old per-server copies that cause duplicates
        _cleaned = True
        for g in bot.guilds:
            bot.tree.clear_commands(guild=g)
            try:
                await bot.tree.sync(guild=g)
            except discord.HTTPException:
                pass

if not TOKEN:
    raise SystemExit("DISCORD_TOKEN is not set. Add it in your host's environment/variables panel or in a .env file.")
bot.run(TOKEN)
