import asyncio
import logging
import os
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone, date, time
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

# =========================
# CONFIGURARE
# =========================

def env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw.strip())


def env_ids(name: str, default: str) -> set[int]:
    raw = os.getenv(name, default)
    ids: set[int] = set()
    for part in re.split(r"[,;\s]+", raw.strip()):
        if part and part.isdigit():
            ids.add(int(part))
    return ids


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

MAIN_GUILD_ID = env_int("MAIN_GUILD_ID", 1505903653079351357)
EMS_GUILD_ID = env_int("EMS_GUILD_ID", 0)

DEMISIE_CHANNEL_ID = env_int("DEMISIE_CHANNEL_ID", 0)
EMS_LOG_CHANNEL_ID = env_int("EMS_LOG_CHANNEL_ID", 0)
MAIN_LOG_CHANNEL_ID = env_int("MAIN_LOG_CHANNEL_ID", 0)

STAFF_ROLE_IDS = env_ids(
    "STAFF_ROLE_IDS",
    "",
)

BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
DB_PATH = os.getenv("DB_PATH", "/data/legacy_ems.db")
TIMEZONE_NAME = os.getenv("TIMEZONE", "Europe/Bucharest")
DELETE_TRIGGER_MESSAGE = os.getenv("DELETE_TRIGGER_MESSAGE", "false").lower() in {"1", "true", "yes", "da"}
BOT_VERSION = "1.0.0-legacy-ems"

try:
    LOCAL_TZ = ZoneInfo(TIMEZONE_NAME)
except ZoneInfoNotFoundError:
    LOCAL_TZ = timezone.utc

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("legacy-ems-bot")


# =========================
# BAZA DE DATE
# =========================

class Database:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._migrate()

    def _migrate(self) -> None:
        with self.conn:
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS join_dates (
                    user_id TEXT PRIMARY KEY,
                    join_date TEXT NOT NULL,
                    set_by TEXT NOT NULL,
                    set_at TEXT NOT NULL
                )
                """
            )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS resignations (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    channel_id TEXT NOT NULL,
                    message_id TEXT UNIQUE NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    reason TEXT,
                    request_reason TEXT,
                    request_name TEXT,
                    request_hours TEXT,
                    join_date TEXT,
                    days INTEGER
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_resignations_user_status ON resignations(user_id, status)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_resignations_message ON resignations(message_id)")

            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(resignations)").fetchall()}
            if "request_reason" not in columns:
                self.conn.execute("ALTER TABLE resignations ADD COLUMN request_reason TEXT")
            if "request_name" not in columns:
                self.conn.execute("ALTER TABLE resignations ADD COLUMN request_name TEXT")
            if "request_hours" not in columns:
                self.conn.execute("ALTER TABLE resignations ADD COLUMN request_hours TEXT")

    async def set_join_date(self, user_id: int, join_dt: datetime, set_by: int) -> None:
        async with self.lock:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO join_dates(user_id, join_date, set_by, set_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id) DO UPDATE SET
                        join_date=excluded.join_date,
                        set_by=excluded.set_by,
                        set_at=excluded.set_at
                    """,
                    (str(user_id), join_dt.astimezone(LOCAL_TZ).isoformat(), str(set_by), now_iso()),
                )

    async def get_join_date(self, user_id: int) -> Optional[str]:
        async with self.lock:
            row = self.conn.execute(
                "SELECT join_date FROM join_dates WHERE user_id = ?",
                (str(user_id),),
            ).fetchone()
            return row["join_date"] if row else None

    async def create_resignation(
        self,
        request_id: str,
        user_id: int,
        channel_id: int,
        message_id: int,
        join_date_iso: Optional[str],
        days: Optional[int],
        request_reason: str,
        request_name: str,
        request_hours: str,
    ) -> None:
        async with self.lock:
            with self.conn:
                self.conn.execute(
                    """
                    INSERT INTO resignations(
                        id, user_id, channel_id, message_id, status, created_at, join_date, days, request_reason, request_name, request_hours
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        request_id,
                        str(user_id),
                        str(channel_id),
                        str(message_id),
                        "PENDING",
                        now_iso(),
                        join_date_iso,
                        days,
                        request_reason,
                        request_name,
                        request_hours,
                    ),
                )

    async def get_pending_for_user(self, user_id: int) -> Optional[dict]:
        async with self.lock:
            row = self.conn.execute(
                """
                SELECT * FROM resignations
                WHERE user_id = ? AND status = 'PENDING'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (str(user_id),),
            ).fetchone()
            return dict(row) if row else None

    async def get_by_message_id(self, message_id: int) -> Optional[dict]:
        async with self.lock:
            row = self.conn.execute(
                "SELECT * FROM resignations WHERE message_id = ? LIMIT 1",
                (str(message_id),),
            ).fetchone()
            return dict(row) if row else None

    async def decide(
        self,
        message_id: int,
        status: str,
        decided_by: int,
        reason: Optional[str],
        join_date_iso: Optional[str],
        days: Optional[int],
    ) -> Optional[dict]:
        async with self.lock:
            with self.conn:
                row = self.conn.execute(
                    "SELECT * FROM resignations WHERE message_id = ? LIMIT 1",
                    (str(message_id),),
                ).fetchone()
                if not row:
                    return None
                if row["status"] != "PENDING":
                    return dict(row)

                self.conn.execute(
                    """
                    UPDATE resignations
                    SET status = ?, decided_at = ?, decided_by = ?, reason = ?, join_date = ?, days = ?
                    WHERE message_id = ?
                    """,
                    (
                        status,
                        now_iso(),
                        str(decided_by),
                        reason,
                        join_date_iso,
                        days,
                        str(message_id),
                    ),
                )
                updated = self.conn.execute(
                    "SELECT * FROM resignations WHERE message_id = ? LIMIT 1",
                    (str(message_id),),
                ).fetchone()
                return dict(updated) if updated else None

    async def get_recent_resignations(self, limit: int = 10) -> list[dict]:
        async with self.lock:
            rows = self.conn.execute(
                """
                SELECT * FROM resignations
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [dict(row) for row in rows]


db = Database(DB_PATH)


# =========================
# FUNCTII UTILE
# =========================

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def unix_from_iso(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def parse_join_date(value: str) -> date:
    value = value.strip()
    formats = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise ValueError("Format dată invalid. Folosește YYYY-MM-DD sau DD/MM/YYYY.")


def parse_join_time(value: str) -> time:
    value = value.strip().replace(".", ":")
    formats = ("%H:%M", "%H")
    for fmt in formats:
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            pass
    raise ValueError("Format oră invalid. Folosește HH:MM, exemplu 20:30.")


def parse_join_datetime(data: str, ora: str) -> datetime:
    d = parse_join_date(data)
    t = parse_join_time(ora)
    join_dt = datetime.combine(d, t, tzinfo=LOCAL_TZ)
    now_local = datetime.now(LOCAL_TZ)
    if join_dt > now_local:
        raise ValueError("Data și ora intrării nu pot fi în viitor.")
    return join_dt


def parse_stored_join_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        try:
            d = date.fromisoformat(value)
            dt = datetime.combine(d, time(0, 0), tzinfo=LOCAL_TZ)
        except ValueError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def calculate_duration_seconds(join_date_iso: Optional[str]) -> Optional[int]:
    join_dt = parse_stored_join_datetime(join_date_iso)
    if not join_dt:
        return None
    seconds = int((datetime.now(LOCAL_TZ) - join_dt).total_seconds())
    return max(0, seconds)


def calculate_days(join_date_iso: Optional[str]) -> Optional[int]:
    seconds = calculate_duration_seconds(join_date_iso)
    if seconds is None:
        return None
    return seconds // 86400


def format_days(days: Optional[int]) -> str:
    if days is None:
        return "Necunoscut"
    if days == 1:
        return "1 zi"
    return f"{days} zile"


def format_duration_from_join(join_date_iso: Optional[str]) -> str:
    seconds = calculate_duration_seconds(join_date_iso)
    if seconds is None:
        return "Necunoscut"

    total_minutes = seconds // 60
    days = total_minutes // (24 * 60)
    hours = (total_minutes % (24 * 60)) // 60
    minutes = total_minutes % 60

    parts: list[str] = []
    if days == 1:
        parts.append("1 zi")
    elif days > 1:
        parts.append(f"{days} zile")

    if hours == 1:
        parts.append("1 oră")
    elif hours > 1:
        parts.append(f"{hours} ore")

    if minutes == 1:
        parts.append("1 minut")
    elif minutes > 1:
        parts.append(f"{minutes} minute")

    if not parts:
        return "Sub 1 minut"
    return ", ".join(parts)


def format_join_date(join_date_iso: Optional[str]) -> str:
    join_dt = parse_stored_join_datetime(join_date_iso)
    if not join_dt:
        return "Nesetată" if not join_date_iso else "Invalidă"
    return join_dt.strftime("%d/%m/%Y %H:%M")


def is_valid_join_date(join_date_iso: Optional[str]) -> bool:
    return parse_stored_join_datetime(join_date_iso) is not None


def is_staff(member: discord.abc.User) -> bool:
    if not isinstance(member, discord.Member):
        return False
    return any(role.id in STAFF_ROLE_IDS for role in member.roles)


def user_mention(user_id: str | int) -> str:
    return f"<@{user_id}>"


def status_ro(status: str) -> str:
    return {
        "PENDING": "În așteptare",
        "ACCEPTED": "Acceptată",
        "REFUSED": "Refuzată",
    }.get(status, status)


async def send_to_channel(bot: commands.Bot, channel_id: int, *, content: Optional[str] = None, embed: Optional[discord.Embed] = None) -> None:
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            await channel.send(content=content, embed=embed)
        else:
            log.warning("Canalul %s nu este TextChannel/Thread.", channel_id)
    except discord.Forbidden:
        log.exception("Botul nu are permisiune să trimită mesaje în canalul %s.", channel_id)
    except discord.NotFound:
        log.exception("Canalul %s nu a fost găsit.", channel_id)
    except discord.HTTPException:
        log.exception("Eroare Discord la trimiterea mesajului în canalul %s.", channel_id)


def parse_demisie_template(content: str) -> Optional[tuple[str, str, str]]:
    """Parsează modelul:
    Nume:
    Ore:
    Motiv:

    Acceptă și varianta cu `demisia` / `demisie` pe primul rând.
    """
    text = content.strip()
    text = re.sub(r"^\s*(demisia|demisie)\s*[:\-]?\s*", "", text, flags=re.IGNORECASE)

    match = re.search(
        r"(?is)^\s*nume\s*:\s*(?P<nume>.+?)\s+ore\s*:\s*(?P<ore>.+?)\s+motiv\s*:\s*(?P<motiv>.+?)\s*$",
        text,
    )
    if not match:
        return None

    nume = re.sub(r"\s+", " ", match.group("nume")).strip()
    ore = re.sub(r"\s+", " ", match.group("ore")).strip()
    motiv = match.group("motiv").strip()
    return nume, ore, motiv


def demisie_format_message() -> str:
    return (
        "⚠️ Trebuie să folosești modelul corect pentru demisie.\n"
        "```\n"
        "Nume: numele tău\n"
        "Ore: numărul de ore\n"
        "Motiv: motivul demisiei\n"
        "```\n"
        "Exemplu:\n"
        "```\n"
        "Nume: Jmarok\n"
        "Ore: 120\n"
        "Motiv: Nu mai am timp să activez în departament.\n"
        "```"
    )


# =========================
# EMBED-URI
# =========================

def build_pending_embed(member: discord.Member, request_id: str, join_date_iso: Optional[str], days: Optional[int], request_reason: str, request_name: str, request_hours: str) -> discord.Embed:
    embed = discord.Embed(
        title="📋 Cerere de Demisie",
        description=(
            f"{member.mention} a depus o cerere de demisie din **Legacy EMS**.\n\n"
            "Conducerea trebuie să aleagă o acțiune folosind butoanele de mai jos."
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👤 Membru", value=f"{member.mention}\n`{member.id}`", inline=True)
    embed.add_field(name="🪪 Nume", value=request_name[:256], inline=True)
    embed.add_field(name="⏱️ Ore", value=request_hours[:256], inline=True)
    embed.add_field(name="📅 Data intrării", value=format_join_date(join_date_iso), inline=True)
    embed.add_field(name="⏳ Timp în departament", value=format_duration_from_join(join_date_iso), inline=True)
    embed.add_field(name="📝 Motiv", value=request_reason[:1024], inline=False)
    embed.add_field(name="📌 Status", value="🟡 În așteptare", inline=False)
    embed.set_footer(text=f"ID cerere: {request_id}")
    return embed


def build_decision_embed(row: dict) -> discord.Embed:
    status = row["status"]
    if status == "ACCEPTED":
        title = "✅ Demisie Acceptată"
        color = discord.Color.green()
        status_line = "🟢 Acceptată"
    elif status == "REFUSED":
        title = "❌ Demisie Refuzată"
        color = discord.Color.red()
        status_line = "🔴 Refuzată"
    else:
        title = "📋 Cerere de Demisie"
        color = discord.Color.orange()
        status_line = "🟡 În așteptare"

    embed = discord.Embed(
        title=title,
        description=f"Cererea de demisie pentru {user_mention(row['user_id'])} a fost actualizată.",
        color=color,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👤 Membru", value=f"{user_mention(row['user_id'])}\n`{row['user_id']}`", inline=True)
    if row.get("request_name"):
        embed.add_field(name="🪪 Nume", value=row["request_name"][:256], inline=True)
    if row.get("request_hours"):
        embed.add_field(name="⏱️ Ore", value=row["request_hours"][:256], inline=True)
    embed.add_field(name="📅 Data intrării", value=format_join_date(row.get("join_date")), inline=True)
    embed.add_field(name="⏳ Timp în departament", value=format_duration_from_join(row.get("join_date")), inline=True)
    embed.add_field(name="📌 Status", value=status_line, inline=False)
    if row.get("request_reason"):
        embed.add_field(name="📝 Motiv", value=row["request_reason"][:1024], inline=False)

    if row.get("decided_by"):
        embed.add_field(name="👮 Decizie luată de", value=user_mention(row["decided_by"]), inline=True)
    decided_ts = unix_from_iso(row.get("decided_at"))
    if decided_ts:
        embed.add_field(name="🕒 Data deciziei", value=f"<t:{decided_ts}:F>", inline=True)
    if status == "REFUSED" and row.get("reason"):
        embed.add_field(name="📝 Motivul refuzului", value=row["reason"][:1024], inline=False)

    embed.add_field(name="⚠️ Roluri", value="Rolurile se elimină manual de către conducere.", inline=False)
    embed.set_footer(text=f"ID cerere: {row['id']}")
    return embed


def build_main_accepted_embed(row: dict) -> discord.Embed:
    embed = discord.Embed(
        title="📢 Demisie Acceptată",
        description=(
            f"{user_mention(row['user_id'])} a părăsit departamentul **Legacy EMS** "
            f"după **{format_duration_from_join(row.get('join_date')).lower()}**."
        ),
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👮 Acceptată de", value=user_mention(row.get("decided_by", "0")), inline=True)
    if row.get("request_name"):
        embed.add_field(name="🪪 Nume", value=row["request_name"][:256], inline=True)
    if row.get("request_hours"):
        embed.add_field(name="⏱️ Ore", value=row["request_hours"][:256], inline=True)
    embed.add_field(name="📅 Data intrării", value=format_join_date(row.get("join_date")), inline=True)
    if row.get("request_reason"):
        embed.add_field(name="📝 Motiv", value=row["request_reason"][:1024], inline=False)
    embed.set_footer(text="Legacy EMS • Departamentul Medical")
    return embed


def build_refused_public_embed(row: dict) -> discord.Embed:
    embed = discord.Embed(
        title="❌ Demisie Refuzată",
        description=f"{user_mention(row['user_id'])}, demisia ta a fost refuzată de către conducere.",
        color=discord.Color.red(),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👮 Refuzată de", value=user_mention(row.get("decided_by", "0")), inline=True)
    if row.get("request_name"):
        embed.add_field(name="🪪 Nume", value=row["request_name"][:256], inline=True)
    if row.get("request_hours"):
        embed.add_field(name="⏱️ Ore", value=row["request_hours"][:256], inline=True)
    if row.get("request_reason"):
        embed.add_field(name="📝 Motiv", value=row["request_reason"][:1024], inline=False)
    embed.add_field(name="📝 Motivul refuzului", value=(row.get("reason") or "Nespecificat")[:1024], inline=False)
    embed.set_footer(text="Legacy EMS • Departamentul Medical")
    return embed


# =========================
# VIEW + MODAL
# =========================

class DemisieDecisionView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @classmethod
    def disabled(cls, bot: commands.Bot) -> "DemisieDecisionView":
        view = cls(bot)
        for item in view.children:
            item.disabled = True
        return view

    async def _get_pending_request_or_reply(self, interaction: discord.Interaction) -> Optional[dict]:
        if not interaction.message:
            await interaction.response.send_message("❌ Nu am putut identifica mesajul cererii.", ephemeral=True)
            return None

        row = await db.get_by_message_id(interaction.message.id)
        if not row:
            await interaction.response.send_message("❌ Cererea nu există în baza de date.", ephemeral=True)
            return None

        if row["status"] != "PENDING":
            await interaction.response.send_message(
                f"⚠️ Această cerere este deja **{status_ro(row['status']).lower()}**.",
                ephemeral=True,
            )
            return None

        return row

    @discord.ui.button(
        label="Acceptă Demisia",
        style=discord.ButtonStyle.success,
        custom_id="legacy_ems_demisie_accept",
        emoji="✅",
    )
    async def accept_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != EMS_GUILD_ID:
            await interaction.response.send_message("❌ Acest buton funcționează doar pe serverul EMS.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Nu ai permisiune să accepți demisii.", ephemeral=True)
            return

        row = await self._get_pending_request_or_reply(interaction)
        if not row:
            return

        await interaction.response.defer(ephemeral=True)

        join_date_iso = await db.get_join_date(int(row["user_id"])) or row.get("join_date")
        days = calculate_days(join_date_iso)
        if not is_valid_join_date(join_date_iso) or days is None:
            await interaction.followup.send(
                "❌ Nu pot accepta demisia deoarece data intrării membrului nu este setată corect.\n"
                "Folosește mai întâi: `/setintrare @membru DD/MM/YYYY HH:MM` și apoi apasă din nou pe `Acceptă Demisia`.",
                ephemeral=True,
            )
            return

        updated = await db.decide(
            int(row["message_id"]),
            status="ACCEPTED",
            decided_by=interaction.user.id,
            reason=None,
            join_date_iso=join_date_iso,
            days=days,
        )
        if not updated or updated["status"] != "ACCEPTED":
            await interaction.followup.send("⚠️ Cererea nu mai este în așteptare.", ephemeral=True)
            return

        try:
            await interaction.message.edit(embed=build_decision_embed(updated), view=DemisieDecisionView.disabled(self.bot))
        except discord.HTTPException:
            log.exception("Nu am putut edita mesajul cererii acceptate.")

        await send_to_channel(self.bot, EMS_LOG_CHANNEL_ID, embed=build_decision_embed(updated))
        await send_to_channel(self.bot, MAIN_LOG_CHANNEL_ID, embed=build_main_accepted_embed(updated))

        # DM optional către membru. Dacă are DM închis, se ignoră.
        try:
            user = self.bot.get_user(int(updated["user_id"])) or await self.bot.fetch_user(int(updated["user_id"]))
            await user.send(embed=build_decision_embed(updated))
        except discord.HTTPException:
            pass

        await interaction.followup.send("✅ Demisia a fost acceptată. Logurile au fost trimise.", ephemeral=True)

    @discord.ui.button(
        label="Refuză Demisia",
        style=discord.ButtonStyle.danger,
        custom_id="legacy_ems_demisie_refuse",
        emoji="❌",
    )
    async def refuse_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.guild_id != EMS_GUILD_ID:
            await interaction.response.send_message("❌ Acest buton funcționează doar pe serverul EMS.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Nu ai permisiune să refuzi demisii.", ephemeral=True)
            return

        row = await self._get_pending_request_or_reply(interaction)
        if not row:
            return

        await interaction.response.send_modal(RefuzDemisieModal(self.bot, int(row["message_id"])))


class RefuzDemisieModal(discord.ui.Modal, title="Refuz Demisie"):
    motiv = discord.ui.TextInput(
        label="Motivul refuzului",
        placeholder="Scrie motivul complet pentru care demisia este refuzată...",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=1000,
        required=True,
    )

    def __init__(self, bot: commands.Bot, message_id: int):
        super().__init__(timeout=300)
        self.bot = bot
        self.message_id = message_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_staff(interaction.user):
            await interaction.response.send_message("❌ Nu ai permisiune să refuzi demisii.", ephemeral=True)
            return

        row = await db.get_by_message_id(self.message_id)
        if not row:
            await interaction.response.send_message("❌ Cererea nu există în baza de date.", ephemeral=True)
            return
        if row["status"] != "PENDING":
            await interaction.response.send_message(
                f"⚠️ Această cerere este deja **{status_ro(row['status']).lower()}**.",
                ephemeral=True,
            )
            return

        join_date_iso = await db.get_join_date(int(row["user_id"])) or row.get("join_date")
        days = calculate_days(join_date_iso)
        updated = await db.decide(
            self.message_id,
            status="REFUSED",
            decided_by=interaction.user.id,
            reason=str(self.motiv.value).strip(),
            join_date_iso=join_date_iso,
            days=days,
        )

        if not updated or updated["status"] != "REFUSED":
            await interaction.response.send_message("⚠️ Cererea nu mai este în așteptare.", ephemeral=True)
            return

        await interaction.response.send_message("❌ Demisia a fost refuzată. Mesajele au fost trimise.", ephemeral=True)

        try:
            channel = interaction.channel
            if not isinstance(channel, discord.TextChannel):
                channel = self.bot.get_channel(int(row["channel_id"])) or await self.bot.fetch_channel(int(row["channel_id"]))
            original_message = await channel.fetch_message(self.message_id)
            await original_message.edit(embed=build_decision_embed(updated), view=DemisieDecisionView.disabled(self.bot))
            await channel.send(embed=build_refused_public_embed(updated))
        except discord.HTTPException:
            log.exception("Nu am putut edita/trimitere mesaj pentru demisia refuzată.")

        await send_to_channel(self.bot, EMS_LOG_CHANNEL_ID, embed=build_decision_embed(updated))

        # DM optional către membru. Dacă are DM închis, se ignoră.
        try:
            user = self.bot.get_user(int(updated["user_id"])) or await self.bot.fetch_user(int(updated["user_id"]))
            await user.send(embed=build_refused_public_embed(updated))
        except discord.HTTPException:
            pass


# =========================
# BOT
# =========================

intents = discord.Intents.default()
intents.guilds = True
intents.messages = True
intents.message_content = True
intents.members = True

REQUIRED_IDS = {
    "MAIN_GUILD_ID": MAIN_GUILD_ID,
    "EMS_GUILD_ID": EMS_GUILD_ID,
    "DEMISIE_CHANNEL_ID": DEMISIE_CHANNEL_ID,
    "EMS_LOG_CHANNEL_ID": EMS_LOG_CHANNEL_ID,
    "MAIN_LOG_CHANNEL_ID": MAIN_LOG_CHANNEL_ID,
}


def validate_config_startup() -> None:
    missing = [name for name, value in REQUIRED_IDS.items() if not value]
    if missing:
        raise RuntimeError(
            "Lipsesc ID-uri obligatorii în Railway Variables / .env: " + ", ".join(missing)
        )
    if not STAFF_ROLE_IDS:
        raise RuntimeError("Lipsește STAFF_ROLE_IDS. Adaugă ID-urile rolurilor staff, separate prin virgulă.")


class LegacyEMSBot(commands.Bot):
    async def setup_hook(self) -> None:
        self.add_view(DemisieDecisionView(self))
        ems_guild = discord.Object(id=EMS_GUILD_ID)
        synced = await self.tree.sync(guild=ems_guild)
        log.info("Slash commands sincronizate pe serverul EMS: %s", len(synced))


bot = LegacyEMSBot(command_prefix=BOT_PREFIX, intents=intents)


@bot.event
async def on_ready() -> None:
    log.info("Bot online ca %s | Servere: %s", bot.user, len(bot.guilds))
    log.info("Versiune bot: %s", BOT_VERSION)
    log.info("DB_PATH: %s", DB_PATH)


@bot.event
async def on_message(message: discord.Message) -> None:
    if message.author.bot:
        return

    await bot.process_commands(message)

    if not message.guild or message.guild.id != EMS_GUILD_ID:
        return
    if message.channel.id != DEMISIE_CHANNEL_ID:
        return

    looks_like_demisie = bool(
        re.match(r"^\s*(demisia|demisie)\b", message.content, flags=re.IGNORECASE)
        or re.search(r"(?im)^\s*nume\s*:", message.content)
    )
    if not looks_like_demisie:
        return

    parsed_template = parse_demisie_template(message.content)
    if not parsed_template:
        await message.reply(demisie_format_message(), mention_author=True)
        return

    request_name, request_hours, request_reason = parsed_template

    if len(request_name) < 2:
        await message.reply("⚠️ Câmpul `Nume` trebuie completat corect.", mention_author=True)
        return
    if len(request_name) > 100:
        await message.reply("⚠️ Câmpul `Nume` este prea lung. Maxim 100 de caractere.", mention_author=True)
        return
    if len(request_hours) < 1:
        await message.reply("⚠️ Câmpul `Ore` trebuie completat.", mention_author=True)
        return
    if len(request_hours) > 30:
        await message.reply("⚠️ Câmpul `Ore` este prea lung. Maxim 30 de caractere.", mention_author=True)
        return
    if len(request_reason) < 3:
        await message.reply("⚠️ Câmpul `Motiv` trebuie completat corect.", mention_author=True)
        return
    if len(request_reason) > 1000:
        await message.reply("⚠️ Câmpul `Motiv` este prea lung. Maxim 1000 de caractere.", mention_author=True)
        return

    if not isinstance(message.author, discord.Member):
        return

    existing = await db.get_pending_for_user(message.author.id)
    if existing:
        await message.reply(
            f"⚠️ Ai deja o cerere de demisie în așteptare: https://discord.com/channels/{EMS_GUILD_ID}/{existing['channel_id']}/{existing['message_id']}",
            mention_author=True,
        )
        return

    join_date_iso = await db.get_join_date(message.author.id)
    days = calculate_days(join_date_iso)
    if not is_valid_join_date(join_date_iso) or days is None:
        await message.reply(
            "❌ Nu poți depune demisia deoarece **data intrării tale nu este setată corect**.\n"
            "Roagă conducerea să folosească mai întâi comanda:\n"
            "`/setintrare @membru DD/MM/YYYY HH:MM`\n\n"
            "După ce data este setată, trimite din nou demisia.",
            mention_author=True,
        )
        return

    request_id = f"DMS-{message.author.id}-{int(datetime.now(timezone.utc).timestamp())}"

    embed = build_pending_embed(message.author, request_id, join_date_iso, days, request_reason, request_name, request_hours)
    sent = await message.channel.send(embed=embed, view=DemisieDecisionView(bot))

    await db.create_resignation(
        request_id=request_id,
        user_id=message.author.id,
        channel_id=message.channel.id,
        message_id=sent.id,
        join_date_iso=join_date_iso,
        days=days,
        request_reason=request_reason,
        request_name=request_name,
        request_hours=request_hours,
    )

    if DELETE_TRIGGER_MESSAGE:
        try:
            await message.delete()
        except discord.HTTPException:
            pass


# =========================
# SLASH COMMANDS STAFF
# =========================

ems_guild_obj = discord.Object(id=EMS_GUILD_ID)


@bot.tree.command(name="setintrare", description="Setează data și ora intrării unui membru în Legacy EMS.", guild=ems_guild_obj)
@app_commands.describe(
    membru="Membrul pentru care setezi data intrării.",
    data="Data intrării: YYYY-MM-DD sau DD/MM/YYYY.",
    ora="Ora intrării: HH:MM, exemplu 20:30.",
)
async def setintrare(interaction: discord.Interaction, membru: discord.Member, data: str, ora: str):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Nu ai permisiune să folosești această comandă.", ephemeral=True)
        return
    try:
        parsed = parse_join_datetime(data, ora)
    except ValueError as exc:
        await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        return

    await db.set_join_date(membru.id, parsed, interaction.user.id)
    await interaction.response.send_message(
        f"✅ Data și ora intrării pentru {membru.mention} au fost setate la **{parsed.strftime('%d/%m/%Y %H:%M')}**.\n"
        f"⏳ Timp în departament acum: **{format_duration_from_join(parsed.isoformat())}**.",
        ephemeral=True,
    )


@bot.tree.command(name="intrare", description="Verifică data intrării unui membru în Legacy EMS.", guild=ems_guild_obj)
@app_commands.describe(membru="Membrul verificat.")
async def intrare(interaction: discord.Interaction, membru: discord.Member):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Nu ai permisiune să folosești această comandă.", ephemeral=True)
        return

    join_date_iso = await db.get_join_date(membru.id)
    await interaction.response.send_message(
        f"👤 {membru.mention}\n📅 Data intrării: **{format_join_date(join_date_iso)}**\n⏳ Timp în departament: **{format_duration_from_join(join_date_iso)}**",
        ephemeral=True,
    )


@bot.tree.command(name="demisii", description="Afișează ultimele cereri de demisie.", guild=ems_guild_obj)
@app_commands.describe(limit="Număr de cereri afișate, maxim 10.")
async def demisii(interaction: discord.Interaction, limit: Optional[int] = 10):
    if not is_staff(interaction.user):
        await interaction.response.send_message("❌ Nu ai permisiune să folosești această comandă.", ephemeral=True)
        return

    limit = max(1, min(limit or 10, 10))
    rows = await db.get_recent_resignations(limit)
    if not rows:
        await interaction.response.send_message("Nu există cereri de demisie înregistrate.", ephemeral=True)
        return

    lines = []
    for row in rows:
        created_ts = unix_from_iso(row.get("created_at"))
        created_text = f"<t:{created_ts}:R>" if created_ts else "dată necunoscută"
        lines.append(
            f"• {user_mention(row['user_id'])} — **{status_ro(row['status'])}** — {created_text}"
        )

    await interaction.response.send_message("\n".join(lines), ephemeral=True)


if __name__ == "__main__":
    if not DISCORD_TOKEN:
        raise RuntimeError("Lipsește DISCORD_TOKEN. Adaugă tokenul în Railway Variables sau în .env local.")
    validate_config_startup()
    bot.run(DISCORD_TOKEN)
