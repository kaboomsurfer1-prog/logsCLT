from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

BOT_VERSION = "1.0.4-ignore-position-move-strict"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "1505903653079351357"))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "1518950724753424404"))
DB_PATH = os.getenv("DB_PATH", "/data/legacy_logs.db")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Bucharest")
STORE_MESSAGE_CONTENT = os.getenv("STORE_MESSAGE_CONTENT", "true").lower() in {"1", "true", "yes", "on"}
MAX_STORED_MESSAGES = int(os.getenv("MAX_STORED_MESSAGES", "50000"))
LOG_MESSAGE_EDITS = os.getenv("LOG_MESSAGE_EDITS", "true").lower() in {"1", "true", "yes", "on"}
LOG_MESSAGE_DELETES = os.getenv("LOG_MESSAGE_DELETES", "true").lower() in {"1", "true", "yes", "on"}
LOG_VOICE = os.getenv("LOG_VOICE", "true").lower() in {"1", "true", "yes", "on"}
LOG_ONLINE_READY = os.getenv("LOG_ONLINE_READY", "true").lower() in {"1", "true", "yes", "on"}

TZ = ZoneInfo(TIMEZONE_NAME)

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("legacy_logs")

intents = discord.Intents.default()
for attr in (
    "guilds",
    "members",
    "moderation",
    "emojis_and_stickers",
    "integrations",
    "webhooks",
    "invites",
    "voice_states",
    "messages",
    "reactions",
    "message_content",
):
    if hasattr(intents, attr):
        setattr(intents, attr, True)

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)
TARGET_GUILD = discord.Object(id=GUILD_ID)
invite_cache: Dict[int, Dict[str, Dict[str, Any]]] = {}
ready_once = False


def ensure_db_parent() -> None:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)


def db() -> sqlite3.Connection:
    ensure_db_parent()
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def init_db() -> None:
    with db() as con:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS invites (
                guild_id INTEGER NOT NULL,
                code TEXT NOT NULL,
                inviter_id INTEGER,
                inviter_name TEXT,
                channel_id INTEGER,
                uses INTEGER DEFAULT 0,
                max_uses INTEGER,
                max_age INTEGER,
                temporary INTEGER,
                created_at TEXT,
                last_seen_at TEXT,
                deleted_at TEXT,
                PRIMARY KEY (guild_id, code)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS joins (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                joined_at TEXT NOT NULL,
                account_created_at TEXT,
                invite_code TEXT,
                inviter_id INTEGER,
                inviter_name TEXT,
                invite_channel_id INTEGER,
                invite_uses INTEGER,
                status TEXT,
                PRIMARY KEY (guild_id, user_id, joined_at)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS latest_join (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                invite_code TEXT,
                inviter_id INTEGER,
                inviter_name TEXT,
                invite_channel_id INTEGER,
                invite_uses INTEGER,
                status TEXT,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER PRIMARY KEY,
                author_id INTEGER,
                author_name TEXT,
                content TEXT,
                attachments TEXT,
                created_at TEXT,
                edited_at TEXT
            )
            """
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS log_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                actor_id INTEGER,
                target_id INTEGER,
                channel_id INTEGER,
                description TEXT,
                created_at TEXT NOT NULL
            )
            """
        )
        con.execute("CREATE INDEX IF NOT EXISTS idx_joins_inviter ON joins(guild_id, inviter_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(guild_id, channel_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_log_events_type ON log_events(guild_id, event_type)")


def cleanup_messages() -> None:
    if MAX_STORED_MESSAGES <= 0:
        return
    with db() as con:
        count = con.execute(
            "SELECT COUNT(*) FROM messages WHERE guild_id = ?",
            (GUILD_ID,),
        ).fetchone()[0]
        extra = max(int(count) - MAX_STORED_MESSAGES, 0)
        if extra <= 0:
            return
        rows = con.execute(
            "SELECT message_id FROM messages WHERE guild_id = ? ORDER BY created_at ASC LIMIT ?",
            (GUILD_ID, extra),
        ).fetchall()
        ids = [int(r[0]) for r in rows]
        if ids:
            placeholders = ",".join("?" for _ in ids)
            con.execute(f"DELETE FROM messages WHERE message_id IN ({placeholders})", ids)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: Optional[datetime] = None) -> str:
    return (dt or utcnow()).astimezone(timezone.utc).isoformat()


def local_dt(dt: Optional[datetime] = None) -> datetime:
    return (dt or utcnow()).astimezone(TZ)


def fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "Necunoscut"
    return dt.astimezone(TZ).strftime("%d/%m/%Y %H:%M")


def fmt_iso(value: Optional[str]) -> str:
    if not value:
        return "Necunoscut"
    try:
        return fmt_dt(datetime.fromisoformat(value))
    except Exception:
        return "Necunoscut"


def fmt_delta(start_iso: Optional[str], end: Optional[datetime] = None) -> str:
    if not start_iso:
        return "Necunoscut"
    try:
        start = datetime.fromisoformat(start_iso)
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        end_dt = end or utcnow()
        seconds = max(int((end_dt.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds()), 0)
        days, rem = divmod(seconds, 86400)
        hours, rem = divmod(rem, 3600)
        minutes, _ = divmod(rem, 60)
        parts = []
        if days:
            parts.append(f"{days} zile")
        if hours:
            parts.append(f"{hours} ore")
        if minutes or not parts:
            parts.append(f"{minutes} minute")
        return ", ".join(parts)
    except Exception:
        return "Necunoscut"


def truncate(text: Optional[str], limit: int = 1024) -> str:
    if text is None or text == "":
        return "—"
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def user_line(user: Optional[discord.abc.User]) -> str:
    if user is None:
        return "Necunoscut"
    return f"{user.mention}\n`{user}` • `{user.id}`"


def member_line(member: Optional[discord.Member]) -> str:
    if member is None:
        return "Necunoscut"
    return f"{member.mention}\n`{member}` • `{member.id}`"


def channel_line(channel: Optional[discord.abc.GuildChannel]) -> str:
    if channel is None:
        return "Necunoscut"
    mention = getattr(channel, "mention", f"#{getattr(channel, 'name', 'canal')}")
    return f"{mention}\n`{getattr(channel, 'name', 'necunoscut')}` • `{channel.id}`"


def role_line(role: Optional[discord.Role]) -> str:
    if role is None:
        return "Necunoscut"
    return f"{role.mention}\n`{role.name}` • `{role.id}`"


def bool_ro(value: Any) -> str:
    return "Da" if bool(value) else "Nu"


def color_blue() -> int:
    return 0x3498DB


def color_green() -> int:
    return 0x2ECC71


def color_red() -> int:
    return 0xE74C3C


def color_orange() -> int:
    return 0xF39C12


def color_purple() -> int:
    return 0x9B59B6


def color_gray() -> int:
    return 0x95A5A6


def add_event(event_type: str, description: str, *, actor_id: Optional[int] = None, target_id: Optional[int] = None, channel_id: Optional[int] = None) -> None:
    with db() as con:
        con.execute(
            "INSERT INTO log_events(guild_id, event_type, actor_id, target_id, channel_id, description, created_at) VALUES(?,?,?,?,?,?,?)",
            (GUILD_ID, event_type, actor_id, target_id, channel_id, description, iso()),
        )


async def get_log_channel() -> Optional[discord.TextChannel]:
    channel = bot.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        try:
            fetched = await bot.fetch_channel(LOG_CHANNEL_ID)
            channel = fetched if isinstance(fetched, discord.TextChannel) else None
        except Exception as exc:
            logger.warning("Nu pot găsi canalul de log: %s", exc)
            return None
    if not isinstance(channel, discord.TextChannel):
        return None
    return channel


async def send_log(
    title: str,
    description: str,
    *,
    color: int = 0x2B2D31,
    fields: Optional[List[Tuple[str, str, bool]]] = None,
    footer: Optional[str] = None,
    thumbnail_url: Optional[str] = None,
) -> None:
    channel = await get_log_channel()
    if not channel:
        return

    embed = discord.Embed(
        title=title,
        description=truncate(description, 4096),
        color=color,
        timestamp=utcnow(),
    )
    if thumbnail_url:
        embed.set_thumbnail(url=thumbnail_url)
    for name, value, inline in fields or []:
        embed.add_field(name=truncate(name, 256), value=truncate(value, 1024), inline=inline)
    embed.set_footer(text=footer or f"Legacy Logs • {BOT_VERSION}")

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except discord.Forbidden:
        logger.warning("Nu am permisiune să trimit loguri în canalul %s", LOG_CHANNEL_ID)
    except Exception as exc:
        logger.exception("Eroare la trimitere log: %s", exc)


def has_target_guild(obj: Any) -> bool:
    guild = getattr(obj, "guild", None)
    return bool(guild and guild.id == GUILD_ID)


def attachment_json(message: discord.Message) -> str:
    attachments = []
    for a in message.attachments:
        attachments.append({"filename": a.filename, "url": a.url, "size": a.size, "content_type": getattr(a, "content_type", None)})
    return json.dumps(attachments, ensure_ascii=False)


def save_message(message: discord.Message) -> None:
    if not message.guild or message.guild.id != GUILD_ID:
        return
    if not STORE_MESSAGE_CONTENT:
        content = "[STORE_MESSAGE_CONTENT=false]"
    else:
        content = message.content or ""
    with db() as con:
        con.execute(
            """
            INSERT OR REPLACE INTO messages(message_id, guild_id, channel_id, author_id, author_name, content, attachments, created_at, edited_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                message.id,
                message.guild.id,
                message.channel.id,
                message.author.id if message.author else None,
                str(message.author) if message.author else None,
                content,
                attachment_json(message),
                iso(message.created_at) if message.created_at else iso(),
                iso(message.edited_at) if message.edited_at else None,
            ),
        )
    cleanup_messages()


def get_saved_message(message_id: int) -> Optional[sqlite3.Row]:
    with db() as con:
        return con.execute("SELECT * FROM messages WHERE message_id = ?", (message_id,)).fetchone()


def save_invite(invite: discord.Invite, *, deleted_at: Optional[str] = None) -> None:
    guild_id = invite.guild.id if invite.guild else GUILD_ID
    inviter = invite.inviter
    channel = invite.channel
    created_at = None
    if getattr(invite, "created_at", None):
        created_at = iso(invite.created_at)
    with db() as con:
        con.execute(
            """
            INSERT INTO invites(guild_id, code, inviter_id, inviter_name, channel_id, uses, max_uses, max_age, temporary, created_at, last_seen_at, deleted_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(guild_id, code) DO UPDATE SET
                inviter_id=excluded.inviter_id,
                inviter_name=excluded.inviter_name,
                channel_id=excluded.channel_id,
                uses=excluded.uses,
                max_uses=excluded.max_uses,
                max_age=excluded.max_age,
                temporary=excluded.temporary,
                created_at=COALESCE(invites.created_at, excluded.created_at),
                last_seen_at=excluded.last_seen_at,
                deleted_at=excluded.deleted_at
            """,
            (
                guild_id,
                invite.code,
                inviter.id if inviter else None,
                str(inviter) if inviter else None,
                channel.id if channel else None,
                invite.uses or 0,
                invite.max_uses,
                invite.max_age,
                1 if invite.temporary else 0,
                created_at,
                iso(),
                deleted_at,
            ),
        )


def invite_to_cache(invite: discord.Invite) -> Dict[str, Any]:
    return {
        "code": invite.code,
        "uses": invite.uses or 0,
        "inviter_id": invite.inviter.id if invite.inviter else None,
        "inviter_name": str(invite.inviter) if invite.inviter else None,
        "channel_id": invite.channel.id if invite.channel else None,
        "max_uses": invite.max_uses,
        "max_age": invite.max_age,
        "temporary": invite.temporary,
        "created_at": iso(invite.created_at) if getattr(invite, "created_at", None) else None,
    }


async def refresh_invites(guild: discord.Guild) -> Dict[str, Dict[str, Any]]:
    if guild.id != GUILD_ID:
        return {}
    data: Dict[str, Dict[str, Any]] = {}
    try:
        invites = await guild.invites()
        for inv in invites:
            data[inv.code] = invite_to_cache(inv)
            save_invite(inv)
        invite_cache[guild.id] = data
        logger.info("Invite cache actualizat: %s invitații", len(data))
        return data
    except discord.Forbidden:
        logger.warning("Lipsește permisiunea Manage Server pentru a citi invitațiile.")
        invite_cache[guild.id] = {}
        return {}
    except Exception as exc:
        logger.exception("Eroare la refresh invite cache: %s", exc)
        return invite_cache.get(guild.id, {})


async def detect_used_invite(guild: discord.Guild) -> Tuple[Optional[Dict[str, Any]], str]:
    old = invite_cache.get(guild.id, {})
    try:
        invites = await guild.invites()
    except discord.Forbidden:
        return None, "Nu pot verifica invitația: lipsește permisiunea Manage Server."
    except Exception as exc:
        return None, f"Nu pot verifica invitația: {exc}"

    current = {inv.code: invite_to_cache(inv) for inv in invites}
    used: Optional[Dict[str, Any]] = None
    best_delta = 0

    for inv in invites:
        old_uses = int(old.get(inv.code, {}).get("uses", 0))
        new_uses = inv.uses or 0
        delta = new_uses - old_uses
        if delta > best_delta:
            best_delta = delta
            used = invite_to_cache(inv)

    if used is None:
        for inv in invites:
            if inv.code not in old and (inv.uses or 0) > 0:
                used = invite_to_cache(inv)
                break

    for inv in invites:
        save_invite(inv)
    invite_cache[guild.id] = current

    if used:
        return used, "Invitație detectată"
    return None, "Invitație necunoscută / vanity URL / intrare în timp ce botul era offline"


async def audit_executor(
    guild: discord.Guild,
    action: Optional[discord.AuditLogAction],
    *,
    target_id: Optional[int] = None,
    reason_required: bool = False,
    delay: float = 1.0,
) -> Tuple[Optional[discord.User], Optional[str]]:
    if action is None:
        return None, None
    try:
        await asyncio.sleep(delay)
        me = guild.me or guild.get_member(bot.user.id) if bot.user else None
        if me and not me.guild_permissions.view_audit_log:
            return None, "Botul nu are permisiunea View Audit Log."
        async for entry in guild.audit_logs(limit=8, action=action):
            if entry.created_at and (utcnow() - entry.created_at).total_seconds() > 25:
                continue
            if target_id is not None:
                target = getattr(entry, "target", None)
                if not target or getattr(target, "id", None) != target_id:
                    continue
            reason = entry.reason if getattr(entry, "reason", None) else None
            if reason_required and not reason:
                continue
            return entry.user, reason
    except discord.Forbidden:
        return None, "Botul nu are permisiunea View Audit Log."
    except Exception as exc:
        logger.debug("Audit log error: %s", exc)
    return None, None


def audit_name(action_name: str) -> Optional[discord.AuditLogAction]:
    return getattr(discord.AuditLogAction, action_name, None)


@bot.event
async def on_ready() -> None:
    global ready_once
    init_db()
    guild = bot.get_guild(GUILD_ID)
    logger.info("Bot online ca %s | Versiune bot: %s", bot.user, BOT_VERSION)
    print(f"Versiune bot: {BOT_VERSION}")

    if guild:
        await refresh_invites(guild)
        try:
            bot.tree.copy_global_to(guild=TARGET_GUILD)
            synced = await bot.tree.sync(guild=TARGET_GUILD)
            logger.info("Comenzi slash sincronizate: %s", len(synced))
        except Exception as exc:
            logger.warning("Nu am putut sincroniza comenzile slash: %s", exc)
    else:
        logger.warning("Botul nu vede serverul configurat GUILD_ID=%s", GUILD_ID)

    if LOG_ONLINE_READY and not ready_once:
        ready_once = True
        await send_log(
            "✅ Legacy Logs online",
            "Sistemul avansat de loguri a pornit cu succes.",
            color=color_green(),
            fields=[
                ("Server ID", f"`{GUILD_ID}`", True),
                ("Canal log ID", f"`{LOG_CHANNEL_ID}`", True),
                ("Versiune", f"`{BOT_VERSION}`", True),
            ],
        )


@bot.event
async def on_invite_create(invite: discord.Invite) -> None:
    if not invite.guild or invite.guild.id != GUILD_ID:
        return
    save_invite(invite)
    await refresh_invites(invite.guild)
    add_event("invite_create", f"Invitație creată: {invite.code}", actor_id=invite.inviter.id if invite.inviter else None, channel_id=invite.channel.id if invite.channel else None)
    await send_log(
        "🔗 Invitație creată",
        "A fost creată o invitație nouă pe server.",
        color=color_blue(),
        fields=[
            ("Cod", f"`{invite.code}`", True),
            ("Creată de", user_line(invite.inviter), True),
            ("Canal", channel_line(invite.channel), True),
            ("Folosiri", f"`{invite.uses or 0}` / `{invite.max_uses or '∞'}`", True),
            ("Expiră după", f"`{invite.max_age or 'Niciodată'} secunde`", True),
            ("Temporară", bool_ro(invite.temporary), True),
        ],
    )


@bot.event
async def on_invite_delete(invite: discord.Invite) -> None:
    if not invite.guild or invite.guild.id != GUILD_ID:
        return
    with db() as con:
        con.execute("UPDATE invites SET deleted_at = ? WHERE guild_id = ? AND code = ?", (iso(), invite.guild.id, invite.code))
    await refresh_invites(invite.guild)
    executor, reason = await audit_executor(invite.guild, audit_name("invite_delete"), target_id=None)
    add_event("invite_delete", f"Invitație ștearsă: {invite.code}", actor_id=executor.id if executor else None, channel_id=invite.channel.id if invite.channel else None)
    await send_log(
        "🗑️ Invitație ștearsă",
        "O invitație a fost ștearsă de pe server.",
        color=color_orange(),
        fields=[
            ("Cod", f"`{invite.code}`", True),
            ("Canal", channel_line(invite.channel), True),
            ("Executor", user_line(executor), True),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_member_join(member: discord.Member) -> None:
    if member.guild.id != GUILD_ID:
        return

    used, status = await detect_used_invite(member.guild)
    inviter_id = used.get("inviter_id") if used else None
    inviter_name = used.get("inviter_name") if used else None
    invite_code = used.get("code") if used else None
    invite_channel_id = used.get("channel_id") if used else None
    invite_uses = used.get("uses") if used else None

    with db() as con:
        con.execute(
            """
            INSERT INTO joins(guild_id, user_id, user_name, joined_at, account_created_at, invite_code, inviter_id, inviter_name, invite_channel_id, invite_uses, status)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                member.guild.id,
                member.id,
                str(member),
                iso(member.joined_at or utcnow()),
                iso(member.created_at),
                invite_code,
                inviter_id,
                inviter_name,
                invite_channel_id,
                invite_uses,
                status,
            ),
        )
        con.execute(
            """
            INSERT OR REPLACE INTO latest_join(guild_id, user_id, joined_at, invite_code, inviter_id, inviter_name, invite_channel_id, invite_uses, status)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (member.guild.id, member.id, iso(member.joined_at or utcnow()), invite_code, inviter_id, inviter_name, invite_channel_id, invite_uses, status),
        )

    account_age = fmt_delta(iso(member.created_at))
    invite_channel = member.guild.get_channel(invite_channel_id) if invite_channel_id else None
    inviter_display = "Necunoscut"
    if inviter_id:
        inviter_member = member.guild.get_member(int(inviter_id))
        inviter_display = member_line(inviter_member) if inviter_member else f"`{inviter_name}` • `{inviter_id}`"

    add_event("member_join", f"{member} a intrat pe server. Invitat de: {inviter_name or 'Necunoscut'}", target_id=member.id, actor_id=inviter_id)
    await send_log(
        "📥 Membru nou intrat",
        f"{member.mention} a intrat pe server.",
        color=color_green(),
        thumbnail_url=member.display_avatar.url,
        fields=[
            ("Membru", member_line(member), True),
            ("Cont creat", fmt_dt(member.created_at), True),
            ("Vârsta contului", account_age, True),
            ("Invitat de", inviter_display, True),
            ("Cod invitație", f"`{invite_code}`" if invite_code else "Necunoscut", True),
            ("Folosiri invitație", f"`{invite_uses}`" if invite_uses is not None else "Necunoscut", True),
            ("Canal invitație", channel_line(invite_channel), True),
            ("Status detectare", status, False),
        ],
    )


@bot.event
async def on_member_remove(member: discord.Member) -> None:
    if member.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(member.guild, audit_name("kick"), target_id=member.id)
    event_title = "👢 Membru dat afară" if executor else "📤 Membru ieșit"
    event_color = color_orange() if executor else color_red()
    event_type = "member_kick" if executor else "member_leave"

    row = None
    with db() as con:
        row = con.execute("SELECT * FROM latest_join WHERE guild_id = ? AND user_id = ?", (member.guild.id, member.id)).fetchone()

    invite_info = "Necunoscut"
    if row:
        inviter_id = row["inviter_id"]
        inviter_name = row["inviter_name"]
        invite_code = row["invite_code"]
        if inviter_id:
            inv_member = member.guild.get_member(int(inviter_id))
            invite_info = f"{inv_member.mention if inv_member else inviter_name} (`{inviter_id}`) • cod `{invite_code or 'necunoscut'}`"
        elif invite_code:
            invite_info = f"Cod `{invite_code}`"

    add_event(event_type, f"{member} a ieșit / a fost scos.", actor_id=executor.id if executor else None, target_id=member.id)
    await send_log(
        event_title,
        f"`{member}` nu mai este pe server.",
        color=event_color,
        thumbnail_url=member.display_avatar.url,
        fields=[
            ("Membru", f"`{member}` • `{member.id}`", True),
            ("Intrat pe server", fmt_iso(row["joined_at"] if row else None), True),
            ("Timp pe server", fmt_delta(row["joined_at"] if row else None), True),
            ("Invitație folosită", invite_info, False),
            ("Executor", user_line(executor), True),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_member_ban(guild: discord.Guild, user: discord.User) -> None:
    if guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(guild, audit_name("ban"), target_id=user.id)
    add_event("member_ban", f"{user} a primit ban.", actor_id=executor.id if executor else None, target_id=user.id)
    await send_log(
        "🔨 Membru banat",
        "Un membru a primit ban pe server.",
        color=color_red(),
        thumbnail_url=user.display_avatar.url,
        fields=[
            ("User", user_line(user), True),
            ("Executor", user_line(executor), True),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_member_unban(guild: discord.Guild, user: discord.User) -> None:
    if guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(guild, audit_name("unban"), target_id=user.id)
    add_event("member_unban", f"{user} a primit unban.", actor_id=executor.id if executor else None, target_id=user.id)
    await send_log(
        "✅ Membru debanat",
        "Un membru a primit unban pe server.",
        color=color_green(),
        thumbnail_url=user.display_avatar.url,
        fields=[
            ("User", user_line(user), True),
            ("Executor", user_line(executor), True),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_member_update(before: discord.Member, after: discord.Member) -> None:
    if after.guild.id != GUILD_ID:
        return

    fields: List[Tuple[str, str, bool]] = []
    event_name = "👤 Membru actualizat"
    color = color_blue()
    action_name = "member_update"

    if before.nick != after.nick:
        fields.append(("Nickname vechi", before.nick or "—", True))
        fields.append(("Nickname nou", after.nick or "—", True))

    before_roles = set(before.roles)
    after_roles = set(after.roles)
    added = [r for r in after_roles - before_roles if r.name != "@everyone"]
    removed = [r for r in before_roles - after_roles if r.name != "@everyone"]
    if added:
        fields.append(("Roluri adăugate", "\n".join(r.mention for r in sorted(added, key=lambda r: r.position, reverse=True)), False))
        event_name = "➕ Roluri adăugate"
        action_name = "member_role_update"
        color = color_green()
    if removed:
        fields.append(("Roluri eliminate", "\n".join(r.mention for r in sorted(removed, key=lambda r: r.position, reverse=True)), False))
        event_name = "➖ Roluri eliminate" if not added else "🔁 Roluri modificate"
        action_name = "member_role_update"
        color = color_orange()

    if before.timed_out_until != after.timed_out_until:
        fields.append(("Timeout vechi", fmt_dt(before.timed_out_until), True))
        fields.append(("Timeout nou", fmt_dt(after.timed_out_until), True))
        event_name = "⏳ Timeout modificat"
        color = color_orange() if after.timed_out_until else color_green()
        action_name = "member_update"

    if before.pending != after.pending:
        fields.append(("Pending vechi", bool_ro(before.pending), True))
        fields.append(("Pending nou", bool_ro(after.pending), True))

    if getattr(before, "guild_avatar", None) != getattr(after, "guild_avatar", None):
        fields.append(("Avatar server", "A fost modificat.", False))

    if before.premium_since != after.premium_since:
        fields.append(("Boost vechi", fmt_dt(before.premium_since), True))
        fields.append(("Boost nou", fmt_dt(after.premium_since), True))
        event_name = "💎 Boost server modificat"
        color = color_purple()

    if not fields:
        return

    executor, reason = await audit_executor(after.guild, audit_name(action_name), target_id=after.id)
    fields.insert(0, ("Membru", member_line(after), True))
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))

    add_event("member_update", f"{after} a fost actualizat.", actor_id=executor.id if executor else None, target_id=after.id)
    await send_log(
        event_name,
        "A fost detectată o modificare la un membru.",
        color=color,
        thumbnail_url=after.display_avatar.url,
        fields=fields,
    )


@bot.event
async def on_user_update(before: discord.User, after: discord.User) -> None:
    guild = bot.get_guild(GUILD_ID)
    if not guild or not guild.get_member(after.id):
        return
    changes = []
    if before.name != after.name:
        changes.append(("Username vechi", before.name, True))
        changes.append(("Username nou", after.name, True))
    if before.discriminator != after.discriminator:
        changes.append(("Discriminator vechi", before.discriminator, True))
        changes.append(("Discriminator nou", after.discriminator, True))
    if before.avatar != after.avatar:
        changes.append(("Avatar", "A fost modificat.", False))
    if not changes:
        return
    changes.insert(0, ("User", user_line(after), True))
    add_event("user_update", f"{after} și-a modificat profilul.", target_id=after.id)
    await send_log("🪪 Profil utilizator modificat", "Un membru și-a schimbat informațiile contului Discord.", color=color_blue(), fields=changes, thumbnail_url=after.display_avatar.url)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.guild and message.guild.id == GUILD_ID:
        save_message(message)
    await bot.process_commands(message)


@bot.event
async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
    if not after.guild or after.guild.id != GUILD_ID or not LOG_MESSAGE_EDITS:
        return
    if before.content == after.content:
        save_message(after)
        return
    save_message(after)
    add_event("message_edit", f"Mesaj editat în #{getattr(after.channel, 'name', 'necunoscut')}", actor_id=after.author.id if after.author else None, channel_id=after.channel.id)
    await send_log(
        "✏️ Mesaj editat",
        "Un mesaj a fost modificat.",
        color=color_blue(),
        fields=[
            ("Autor", user_line(after.author), True),
            ("Canal", channel_line(after.channel), True),
            ("Link mesaj", f"[Deschide mesajul]({after.jump_url})", True),
            ("Înainte", truncate(before.content, 1000), False),
            ("După", truncate(after.content, 1000), False),
        ],
    )


@bot.event
async def on_message_delete(message: discord.Message) -> None:
    if not message.guild or message.guild.id != GUILD_ID or not LOG_MESSAGE_DELETES:
        return
    row = get_saved_message(message.id)
    content = message.content or (row["content"] if row else "")
    attachments = []
    if message.attachments:
        attachments = [f"[{a.filename}]({a.url})" for a in message.attachments]
    elif row and row["attachments"]:
        try:
            attachments = [f"[{a.get('filename')}]({a.get('url')})" for a in json.loads(row["attachments"])]
        except Exception:
            attachments = []

    executor, reason = await audit_executor(message.guild, audit_name("message_delete"), target_id=message.author.id if message.author else None)
    add_event("message_delete", f"Mesaj șters în #{getattr(message.channel, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, target_id=message.author.id if message.author else None, channel_id=message.channel.id)
    await send_log(
        "🗑️ Mesaj șters",
        "Un mesaj a fost șters.",
        color=color_orange(),
        fields=[
            ("Autor mesaj", user_line(message.author), True),
            ("Canal", channel_line(message.channel), True),
            ("Șters de", user_line(executor), True),
            ("Conținut", truncate(content, 1000), False),
            ("Atașamente", "\n".join(attachments) if attachments else "—", False),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent) -> None:
    if payload.guild_id != GUILD_ID or not LOG_MESSAGE_DELETES:
        return
    if payload.cached_message:
        return
    row = get_saved_message(payload.message_id)
    if not row:
        return
    channel = bot.get_channel(payload.channel_id)
    author = bot.get_user(row["author_id"]) if row["author_id"] else None
    add_event("raw_message_delete", "Mesaj șters necache-uit.", target_id=row["author_id"], channel_id=payload.channel_id)
    await send_log(
        "🗑️ Mesaj șters",
        "Un mesaj a fost șters. Informația a fost recuperată din baza de date.",
        color=color_orange(),
        fields=[
            ("Autor", user_line(author), True),
            ("Canal", channel_line(channel), True),
            ("Conținut", truncate(row["content"], 1000), False),
        ],
    )


@bot.event
async def on_bulk_message_delete(messages: List[discord.Message]) -> None:
    if not messages:
        return
    guild = messages[0].guild
    if not guild or guild.id != GUILD_ID:
        return
    channel = messages[0].channel
    executor, reason = await audit_executor(guild, audit_name("message_bulk_delete"), target_id=channel.id if hasattr(channel, "id") else None)
    add_event("bulk_message_delete", f"{len(messages)} mesaje șterse în #{getattr(channel, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, channel_id=channel.id)
    await send_log(
        "🧹 Mesaje șterse în masă",
        f"Au fost șterse `{len(messages)}` mesaje.",
        color=color_red(),
        fields=[
            ("Canal", channel_line(channel), True),
            ("Executor", user_line(executor), True),
            ("Motiv audit", reason or "—", False),
        ],
    )


@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.abc.User) -> None:
    if not reaction.message.guild or reaction.message.guild.id != GUILD_ID or user.bot:
        return
    if str(reaction.emoji) in {"✅", "❌", "⚠️", "📌", "⭐"}:
        add_event("reaction_add", f"Reacție {reaction.emoji} adăugată.", actor_id=user.id, channel_id=reaction.message.channel.id)
        await send_log(
            "➕ Reacție importantă adăugată",
            "A fost adăugată o reacție urmărită.",
            color=color_blue(),
            fields=[
                ("User", user_line(user), True),
                ("Canal", channel_line(reaction.message.channel), True),
                ("Emoji", str(reaction.emoji), True),
                ("Mesaj", f"[Deschide mesajul]({reaction.message.jump_url})", True),
            ],
        )


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.guild.id != GUILD_ID or not LOG_VOICE:
        return
    fields: List[Tuple[str, str, bool]] = [("Membru", member_line(member), True)]
    title = "🎙️ Voice actualizat"
    color = color_blue()
    changed = False

    if before.channel != after.channel:
        changed = True
        if before.channel is None and after.channel is not None:
            title = "🔊 Membru conectat voice"
            color = color_green()
        elif before.channel is not None and after.channel is None:
            title = "🔇 Membru deconectat voice"
            color = color_orange()
        else:
            title = "🔁 Membru mutat voice"
            color = color_blue()
        fields.append(("Canal vechi", channel_line(before.channel), True))
        fields.append(("Canal nou", channel_line(after.channel), True))

    voice_checks = [
        ("server_mute", before.mute, after.mute, "Mute server"),
        ("server_deaf", before.deaf, after.deaf, "Deaf server"),
        ("self_mute", before.self_mute, after.self_mute, "Self mute"),
        ("self_deaf", before.self_deaf, after.self_deaf, "Self deaf"),
        ("self_video", before.self_video, after.self_video, "Cameră"),
        ("self_stream", before.self_stream, after.self_stream, "Stream"),
        ("suppress", before.suppress, after.suppress, "Suppress"),
    ]
    for _, old, new, label in voice_checks:
        if old != new:
            changed = True
            fields.append((label, f"`{bool_ro(old)}` → `{bool_ro(new)}`", True))

    if not changed:
        return

    executor = None
    reason = None
    if before.mute != after.mute or before.deaf != after.deaf:
        executor, reason = await audit_executor(member.guild, audit_name("member_update"), target_id=member.id)
        fields.append(("Executor", user_line(executor), True))
        if reason:
            fields.append(("Motiv audit", reason, False))

    add_event("voice_state_update", title, actor_id=executor.id if executor else member.id, target_id=member.id, channel_id=(after.channel or before.channel).id if (after.channel or before.channel) else None)
    await send_log(title, "A fost detectată o modificare în voice.", color=color, fields=fields)


def role_diff(before: discord.Role, after: discord.Role) -> List[Tuple[str, str, bool]]:
    fields: List[Tuple[str, str, bool]] = []
    checks = [
        ("Nume", before.name, after.name),
        ("Culoare", str(before.color), str(after.color)),
        ("Afișat separat", bool_ro(before.hoist), bool_ro(after.hoist)),
        ("Menționabil", bool_ro(before.mentionable), bool_ro(after.mentionable)),
    ]
    for label, old, new in checks:
        if old != new:
            fields.append((label, f"`{old}` → `{new}`", False))
    if before.permissions.value != after.permissions.value:
        added = [name for name, value in after.permissions if value and not getattr(before.permissions, name)]
        removed = [name for name, value in before.permissions if value and not getattr(after.permissions, name)]
        if added:
            fields.append(("Permisiuni adăugate", ", ".join(f"`{p}`" for p in added), False))
        if removed:
            fields.append(("Permisiuni eliminate", ", ".join(f"`{p}`" for p in removed), False))
    return fields


@bot.event
async def on_guild_role_create(role: discord.Role) -> None:
    if role.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(role.guild, audit_name("role_create"), target_id=role.id)
    add_event("role_create", f"Rol creat: {role.name}", actor_id=executor.id if executor else None, target_id=role.id)
    await send_log("🆕 Rol creat", "A fost creat un rol nou.", color=color_green(), fields=[("Rol", role_line(role), True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_guild_role_delete(role: discord.Role) -> None:
    if role.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(role.guild, audit_name("role_delete"), target_id=role.id)
    add_event("role_delete", f"Rol șters: {role.name}", actor_id=executor.id if executor else None, target_id=role.id)
    await send_log("🗑️ Rol șters", "Un rol a fost șters.", color=color_red(), fields=[("Rol", f"`{role.name}` • `{role.id}`", True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


def is_only_role_position_change(before: discord.Role, after: discord.Role) -> bool:
    return (
        before.position != after.position
        and before.name == after.name
        and before.color == after.color
        and before.hoist == after.hoist
        and before.mentionable == after.mentionable
        and before.permissions.value == after.permissions.value
    )


@bot.event
async def on_guild_role_update(before: discord.Role, after: discord.Role) -> None:
    if after.guild.id != GUILD_ID:
        return
    if is_only_role_position_change(before, after):
        return
    fields = role_diff(before, after)
    if not fields:
        return
    executor, reason = await audit_executor(after.guild, audit_name("role_update"), target_id=after.id)
    fields.insert(0, ("Rol", role_line(after), True))
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))
    add_event("role_update", f"Rol actualizat: {after.name}", actor_id=executor.id if executor else None, target_id=after.id)
    await send_log("🛡️ Rol modificat", "Un rol a fost modificat.", color=color_blue(), fields=fields)


def channel_diff(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> List[Tuple[str, str, bool]]:
    fields: List[Tuple[str, str, bool]] = []
    attrs = [
        ("Nume", "name"),
        ("Categorie", "category"),
        ("Topic", "topic"),
        ("Slowmode", "slowmode_delay"),
        ("NSFW", "nsfw"),
        ("Bitrate", "bitrate"),
        ("Limită useri", "user_limit"),
    ]
    for label, attr in attrs:
        if hasattr(before, attr) and hasattr(after, attr):
            old = getattr(before, attr)
            new = getattr(after, attr)
            if old != new:
                fields.append((label, f"`{old}` → `{new}`", False))
    if getattr(before, "overwrites", None) != getattr(after, "overwrites", None):
        fields.append(("Permisiuni canal", "Overwrite-urile canalului au fost modificate.", False))
    return fields


@bot.event
async def on_guild_channel_create(channel: discord.abc.GuildChannel) -> None:
    if channel.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(channel.guild, audit_name("channel_create"), target_id=channel.id)
    add_event("channel_create", f"Canal creat: {getattr(channel, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, target_id=channel.id, channel_id=channel.id)
    await send_log("🆕 Canal creat", "A fost creat un canal nou.", color=color_green(), fields=[("Canal", channel_line(channel), True), ("Tip", channel.__class__.__name__, True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_guild_channel_delete(channel: discord.abc.GuildChannel) -> None:
    if channel.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(channel.guild, audit_name("channel_delete"), target_id=channel.id)
    add_event("channel_delete", f"Canal șters: {getattr(channel, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, target_id=channel.id, channel_id=channel.id)
    await send_log("🗑️ Canal șters", "Un canal a fost șters.", color=color_red(), fields=[("Canal", f"`{getattr(channel, 'name', 'necunoscut')}` • `{channel.id}`", True), ("Tip", channel.__class__.__name__, True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


def is_only_channel_position_change(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> bool:
    comparable_attrs = [
        "name", "category", "topic", "slowmode_delay", "nsfw",
        "bitrate", "user_limit", "overwrites",
    ]
    if getattr(before, "position", None) == getattr(after, "position", None):
        return False
    for attr in comparable_attrs:
        if hasattr(before, attr) and hasattr(after, attr):
            if getattr(before, attr) != getattr(after, attr):
                return False
    return True


@bot.event
async def on_guild_channel_update(before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
    if after.guild.id != GUILD_ID:
        return
    if is_only_channel_position_change(before, after):
        return
    fields = channel_diff(before, after)
    if not fields:
        return
    executor, reason = await audit_executor(after.guild, audit_name("channel_update"), target_id=after.id)
    fields.insert(0, ("Canal", channel_line(after), True))
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))
    add_event("channel_update", f"Canal modificat: {getattr(after, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, target_id=after.id, channel_id=after.id)
    await send_log("⚙️ Canal modificat", "Un canal a fost modificat.", color=color_blue(), fields=fields)


@bot.event
async def on_thread_create(thread: discord.Thread) -> None:
    if not thread.guild or thread.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(thread.guild, audit_name("thread_create"), target_id=thread.id)
    add_event("thread_create", f"Thread creat: {thread.name}", actor_id=executor.id if executor else None, target_id=thread.id, channel_id=thread.parent_id)
    await send_log("🧵 Thread creat", "A fost creat un thread nou.", color=color_green(), fields=[("Thread", channel_line(thread), True), ("Canal părinte", channel_line(thread.parent), True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_thread_delete(thread: discord.Thread) -> None:
    if not thread.guild or thread.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(thread.guild, audit_name("thread_delete"), target_id=thread.id)
    add_event("thread_delete", f"Thread șters: {thread.name}", actor_id=executor.id if executor else None, target_id=thread.id, channel_id=thread.parent_id)
    await send_log("🧵 Thread șters", "Un thread a fost șters.", color=color_red(), fields=[("Thread", f"`{thread.name}` • `{thread.id}`", True), ("Canal părinte", channel_line(thread.parent), True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_thread_update(before: discord.Thread, after: discord.Thread) -> None:
    if not after.guild or after.guild.id != GUILD_ID:
        return
    fields = channel_diff(before, after)
    if before.archived != after.archived:
        fields.append(("Arhivat", f"`{bool_ro(before.archived)}` → `{bool_ro(after.archived)}`", True))
    if before.locked != after.locked:
        fields.append(("Blocat", f"`{bool_ro(before.locked)}` → `{bool_ro(after.locked)}`", True))
    if not fields:
        return
    executor, reason = await audit_executor(after.guild, audit_name("thread_update"), target_id=after.id)
    fields.insert(0, ("Thread", channel_line(after), True))
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))
    add_event("thread_update", f"Thread modificat: {after.name}", actor_id=executor.id if executor else None, target_id=after.id, channel_id=after.parent_id)
    await send_log("🧵 Thread modificat", "Un thread a fost modificat.", color=color_blue(), fields=fields)


@bot.event
async def on_guild_emojis_update(guild: discord.Guild, before: List[discord.Emoji], after: List[discord.Emoji]) -> None:
    if guild.id != GUILD_ID:
        return
    before_ids = {e.id: e for e in before}
    after_ids = {e.id: e for e in after}
    added = [e for eid, e in after_ids.items() if eid not in before_ids]
    removed = [e for eid, e in before_ids.items() if eid not in after_ids]
    changed = [after_ids[eid] for eid in before_ids.keys() & after_ids.keys() if before_ids[eid].name != after_ids[eid].name]
    if not added and not removed and not changed:
        return
    executor, reason = await audit_executor(guild, audit_name("emoji_update"), target_id=None)
    fields = [
        ("Adăugate", "\n".join(f"{e} `{e.name}` • `{e.id}`" for e in added) or "—", False),
        ("Șterse", "\n".join(f"`{e.name}` • `{e.id}`" for e in removed) or "—", False),
        ("Redenumite", "\n".join(f"`{before_ids[e.id].name}` → `{e.name}` • `{e.id}`" for e in changed) or "—", False),
        ("Executor", user_line(executor), True),
        ("Motiv audit", reason or "—", False),
    ]
    add_event("emoji_update", "Emoji actualizate.", actor_id=executor.id if executor else None)
    await send_log("😀 Emoji actualizate", "Au fost detectate modificări la emoji-urile serverului.", color=color_purple(), fields=fields)


@bot.event
async def on_guild_stickers_update(guild: discord.Guild, before: List[discord.GuildSticker], after: List[discord.GuildSticker]) -> None:
    if guild.id != GUILD_ID:
        return
    before_ids = {s.id: s for s in before}
    after_ids = {s.id: s for s in after}
    added = [s for sid, s in after_ids.items() if sid not in before_ids]
    removed = [s for sid, s in before_ids.items() if sid not in after_ids]
    changed = [after_ids[sid] for sid in before_ids.keys() & after_ids.keys() if before_ids[sid].name != after_ids[sid].name]
    if not added and not removed and not changed:
        return
    executor, reason = await audit_executor(guild, audit_name("sticker_update"), target_id=None)
    fields = [
        ("Adăugate", "\n".join(f"`{s.name}` • `{s.id}`" for s in added) or "—", False),
        ("Șterse", "\n".join(f"`{s.name}` • `{s.id}`" for s in removed) or "—", False),
        ("Redenumite", "\n".join(f"`{before_ids[s.id].name}` → `{s.name}` • `{s.id}`" for s in changed) or "—", False),
        ("Executor", user_line(executor), True),
        ("Motiv audit", reason or "—", False),
    ]
    add_event("sticker_update", "Stickere actualizate.", actor_id=executor.id if executor else None)
    await send_log("🏷️ Stickere actualizate", "Au fost detectate modificări la stickerele serverului.", color=color_purple(), fields=fields)


@bot.event
async def on_guild_update(before: discord.Guild, after: discord.Guild) -> None:
    if after.id != GUILD_ID:
        return
    fields: List[Tuple[str, str, bool]] = []
    attrs = [
        ("Nume server", "name"),
        ("Descriere", "description"),
        ("Nivel verificare", "verification_level"),
        ("Notificări default", "default_notifications"),
        ("Filtru conținut explicit", "explicit_content_filter"),
        ("Canal AFK", "afk_channel"),
        ("Canal sistem", "system_channel"),
        ("Canal reguli", "rules_channel"),
        ("Canal updates comunitate", "public_updates_channel"),
    ]
    for label, attr in attrs:
        old = getattr(before, attr, None)
        new = getattr(after, attr, None)
        if old != new:
            fields.append((label, f"`{old}` → `{new}`", False))
    if before.icon != after.icon:
        fields.append(("Icon server", "A fost modificat.", False))
    if before.banner != after.banner:
        fields.append(("Banner server", "A fost modificat.", False))
    if not fields:
        return
    executor, reason = await audit_executor(after, audit_name("guild_update"), target_id=after.id)
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))
    add_event("guild_update", "Setările serverului au fost modificate.", actor_id=executor.id if executor else None)
    await send_log("⚙️ Server modificat", "Au fost detectate modificări la setările serverului.", color=color_orange(), fields=fields, thumbnail_url=after.icon.url if after.icon else None)


@bot.event
async def on_webhooks_update(channel: discord.abc.GuildChannel) -> None:
    if not channel.guild or channel.guild.id != GUILD_ID:
        return
    executor, reason = await audit_executor(channel.guild, audit_name("webhook_create"), target_id=None, delay=1.5)
    if executor is None:
        executor, reason = await audit_executor(channel.guild, audit_name("webhook_update"), target_id=None, delay=0.2)
    if executor is None:
        executor, reason = await audit_executor(channel.guild, audit_name("webhook_delete"), target_id=None, delay=0.2)
    add_event("webhooks_update", f"Webhook modificat în {getattr(channel, 'name', 'necunoscut')}", actor_id=executor.id if executor else None, channel_id=channel.id)
    await send_log("🪝 Webhook actualizat", "Au fost detectate modificări la webhook-urile unui canal.", color=color_orange(), fields=[("Canal", channel_line(channel), True), ("Executor posibil", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_scheduled_event_create(event: discord.ScheduledEvent) -> None:
    if event.guild and event.guild.id == GUILD_ID:
        executor, reason = await audit_executor(event.guild, audit_name("guild_scheduled_event_create"), target_id=event.id)
        add_event("scheduled_event_create", f"Eveniment creat: {event.name}", actor_id=executor.id if executor else None, target_id=event.id)
        await send_log("📅 Eveniment creat", "A fost creat un eveniment programat.", color=color_green(), fields=[("Eveniment", f"`{event.name}` • `{event.id}`", True), ("Începe", fmt_dt(event.start_time), True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_scheduled_event_delete(event: discord.ScheduledEvent) -> None:
    if event.guild and event.guild.id == GUILD_ID:
        executor, reason = await audit_executor(event.guild, audit_name("guild_scheduled_event_delete"), target_id=event.id)
        add_event("scheduled_event_delete", f"Eveniment șters: {event.name}", actor_id=executor.id if executor else None, target_id=event.id)
        await send_log("📅 Eveniment șters", "Un eveniment programat a fost șters.", color=color_red(), fields=[("Eveniment", f"`{event.name}` • `{event.id}`", True), ("Executor", user_line(executor), True), ("Motiv audit", reason or "—", False)])


@bot.event
async def on_scheduled_event_update(before: discord.ScheduledEvent, after: discord.ScheduledEvent) -> None:
    if not after.guild or after.guild.id != GUILD_ID:
        return
    fields = []
    for label, attr in [("Nume", "name"), ("Descriere", "description"), ("Start", "start_time"), ("End", "end_time"), ("Status", "status")]:
        old = getattr(before, attr, None)
        new = getattr(after, attr, None)
        if old != new:
            if isinstance(old, datetime) or isinstance(new, datetime):
                old_s = fmt_dt(old) if old else "—"
                new_s = fmt_dt(new) if new else "—"
            else:
                old_s = str(old)
                new_s = str(new)
            fields.append((label, f"`{old_s}` → `{new_s}`", False))
    if not fields:
        return
    executor, reason = await audit_executor(after.guild, audit_name("guild_scheduled_event_update"), target_id=after.id)
    fields.insert(0, ("Eveniment", f"`{after.name}` • `{after.id}`", True))
    fields.append(("Executor", user_line(executor), True))
    if reason:
        fields.append(("Motiv audit", reason, False))
    add_event("scheduled_event_update", f"Eveniment modificat: {after.name}", actor_id=executor.id if executor else None, target_id=after.id)
    await send_log("📅 Eveniment modificat", "Un eveniment programat a fost modificat.", color=color_blue(), fields=fields)


@bot.tree.command(name="logstatus", description="Verifică statusul sistemului de loguri.")
@app_commands.default_permissions(manage_guild=True)
async def logstatus(interaction: discord.Interaction) -> None:
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("❌ Această comandă este configurată doar pentru serverul principal.", ephemeral=True)
        return
    guild = interaction.guild
    me = guild.me if guild else None
    perms = me.guild_permissions if me else None
    invite_count = len(invite_cache.get(GUILD_ID, {}))
    with db() as con:
        joins_count = con.execute("SELECT COUNT(*) FROM latest_join WHERE guild_id = ?", (GUILD_ID,)).fetchone()[0]
        msg_count = con.execute("SELECT COUNT(*) FROM messages WHERE guild_id = ?", (GUILD_ID,)).fetchone()[0]
    embed = discord.Embed(title="📊 Legacy Logs Status", color=color_blue(), timestamp=utcnow())
    embed.add_field(name="Versiune", value=f"`{BOT_VERSION}`", inline=True)
    embed.add_field(name="Invite cache", value=f"`{invite_count}` invitații", inline=True)
    embed.add_field(name="Join-uri salvate", value=f"`{joins_count}` membri", inline=True)
    embed.add_field(name="Mesaje în cache DB", value=f"`{msg_count}`", inline=True)
    if perms:
        embed.add_field(name="Manage Server", value=bool_ro(perms.manage_guild), inline=True)
        embed.add_field(name="View Audit Log", value=bool_ro(perms.view_audit_log), inline=True)
        embed.add_field(name="Read Message History", value=bool_ro(perms.read_message_history), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="syncinvites", description="Reîncarcă manual cache-ul de invitații.")
@app_commands.default_permissions(manage_guild=True)
async def syncinvites(interaction: discord.Interaction) -> None:
    if interaction.guild_id != GUILD_ID or not interaction.guild:
        await interaction.response.send_message("❌ Comandă disponibilă doar pe serverul configurat.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    data = await refresh_invites(interaction.guild)
    await interaction.followup.send(f"✅ Cache invitații actualizat. Invitații active: `{len(data)}`", ephemeral=True)


@bot.tree.command(name="whoinvited", description="Arată cine a invitat un membru pe server.")
@app_commands.describe(membru="Membrul verificat")
@app_commands.default_permissions(manage_guild=True)
async def whoinvited(interaction: discord.Interaction, membru: discord.Member) -> None:
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("❌ Comandă disponibilă doar pe serverul configurat.", ephemeral=True)
        return
    with db() as con:
        row = con.execute("SELECT * FROM latest_join WHERE guild_id = ? AND user_id = ?", (GUILD_ID, membru.id)).fetchone()
    if not row:
        await interaction.response.send_message(f"⚠️ Nu am găsit informații despre invitația folosită de {membru.mention}.", ephemeral=True)
        return
    inviter_text = "Necunoscut"
    if row["inviter_id"]:
        inviter = interaction.guild.get_member(int(row["inviter_id"])) if interaction.guild else None
        inviter_text = member_line(inviter) if inviter else f"`{row['inviter_name']}` • `{row['inviter_id']}`"
    embed = discord.Embed(title="🔎 Cine a invitat membrul?", color=color_blue(), timestamp=utcnow())
    embed.add_field(name="Membru", value=member_line(membru), inline=True)
    embed.add_field(name="Invitat de", value=inviter_text, inline=True)
    embed.add_field(name="Cod invitație", value=f"`{row['invite_code'] or 'Necunoscut'}`", inline=True)
    embed.add_field(name="Canal invitație", value=f"<#{row['invite_channel_id']}>" if row["invite_channel_id"] else "Necunoscut", inline=True)
    embed.add_field(name="Intrat la", value=fmt_iso(row["joined_at"]), inline=True)
    embed.add_field(name="Status", value=row["status"] or "—", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="invites", description="Arată câți membri a invitat un user detectat de bot.")
@app_commands.describe(membru="Userul verificat")
@app_commands.default_permissions(manage_guild=True)
async def invites_cmd(interaction: discord.Interaction, membru: discord.Member) -> None:
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("❌ Comandă disponibilă doar pe serverul configurat.", ephemeral=True)
        return
    with db() as con:
        total = con.execute("SELECT COUNT(*) FROM latest_join WHERE guild_id = ? AND inviter_id = ?", (GUILD_ID, membru.id)).fetchone()[0]
        rows = con.execute("SELECT user_id, user_name, joined_at, invite_code FROM latest_join WHERE guild_id = ? AND inviter_id = ? ORDER BY joined_at DESC LIMIT 10", (GUILD_ID, membru.id)).fetchall()
    lines = []
    for r in rows:
        user = interaction.guild.get_member(int(r["user_id"])) if interaction.guild else None
        lines.append(f"• {user.mention if user else r['user_name']} (`{r['user_id']}`) • `{r['invite_code'] or 'necunoscut'}` • {fmt_iso(r['joined_at'])}")
    embed = discord.Embed(title="🔗 Invitații detectate", description=f"{membru.mention} are `{total}` membri invitați detectați de bot.", color=color_green(), timestamp=utcnow())
    embed.add_field(name="Ultimele invitații", value="\n".join(lines) if lines else "—", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="topinvites", description="Top membri după invitații detectate.")
@app_commands.default_permissions(manage_guild=True)
async def topinvites(interaction: discord.Interaction) -> None:
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("❌ Comandă disponibilă doar pe serverul configurat.", ephemeral=True)
        return
    with db() as con:
        rows = con.execute("""
            SELECT inviter_id, inviter_name, COUNT(*) AS total
            FROM latest_join
            WHERE guild_id = ? AND inviter_id IS NOT NULL
            GROUP BY inviter_id, inviter_name
            ORDER BY total DESC
            LIMIT 10
        """, (GUILD_ID,)).fetchall()
    lines = []
    for i, r in enumerate(rows, start=1):
        member = interaction.guild.get_member(int(r["inviter_id"])) if interaction.guild else None
        name = member.mention if member else (r["inviter_name"] or "Necunoscut")
        lines.append(f"`#{i}` {name} (`{r['inviter_id']}`) — `{r['total']}` invitații")
    embed = discord.Embed(title="🏆 Top invitații", description="\n".join(lines) if lines else "Nu există date salvate.", color=color_purple(), timestamp=utcnow())
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="logsearch", description="Caută ultimele evenimente salvate după tip.")
@app_commands.describe(tip="Ex: member_join, message_delete, role_update", limita="Număr rezultate, maxim 20")
@app_commands.default_permissions(manage_guild=True)
async def logsearch(interaction: discord.Interaction, tip: str, limita: int = 10) -> None:
    if interaction.guild_id != GUILD_ID:
        await interaction.response.send_message("❌ Comandă disponibilă doar pe serverul configurat.", ephemeral=True)
        return
    limita = max(1, min(limita, 20))
    with db() as con:
        rows = con.execute("SELECT * FROM log_events WHERE guild_id = ? AND event_type = ? ORDER BY id DESC LIMIT ?", (GUILD_ID, tip, limita)).fetchall()
    if not rows:
        await interaction.response.send_message(f"Nu am găsit evenimente de tip `{tip}`.", ephemeral=True)
        return
    lines = []
    for r in rows:
        lines.append(f"`{fmt_iso(r['created_at'])}` • {truncate(r['description'], 120)}")
    embed = discord.Embed(title=f"🔍 Log search: {tip}", description="\n".join(lines), color=color_gray(), timestamp=utcnow())
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def main() -> None:
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN lipsește. Adaugă tokenul în Railway Variables.")
    init_db()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
