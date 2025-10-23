import discord
from discord import app_commands
from discord.ext import tasks
import json
from datetime import datetime, timedelta, timezone
from typing import List
import os
import re
import asyncio

# ---------- Configuration ----------
TOKEN = 'INSERT TOKEN KEY' # Replace with your App token
GUILD_ID = "INSERT GUILD ID"  # Replace with your server ID
TOD_CHANNEL_NAME = "INSERT CHANNEL" # Replace with your channel name
REFRESH_INTERVAL = 60 # seconds

ROLE_USER = ["ROLE1", "ROLE2", "ADMIN1"]
ROLE_ELEVATED = ["ADMIN1"]

MOB_DATA_FILE = "mob_data.json"
TIMERS_FILE = "timers.json"



intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True  # may be required depending on usage

bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

timers = {}
live_message_id = None
live_channel_id = None

# ---------- Utilities ----------
def has_role(member: discord.Member, role_names):
    if isinstance(role_names, str):
        role_names = [role_names]
    # member may be None in some contexts; handle gracefully
    if not member or not hasattr(member, "roles"):
        return False
    return any(role.name in role_names for role in member.roles)

def check_role(user: discord.Member, role_names):
    if not has_role(user, role_names):
        raise app_commands.CheckFailure(f"You need one of these roles: {', '.join(role_names)}")

def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_json(filename, data):
    with open(filename, "w") as f:
        json.dump(data, f, indent=4, sort_keys=True, default=str)

def save_timers():
    save_json(TIMERS_FILE, timers)

def parse_offset(offset_str: str) -> timedelta:
    """Parses strings like '2d3h15m' into a timedelta. Raises ValueError if invalid."""
    if not offset_str or not isinstance(offset_str, str):
        raise ValueError("Offset must be a non-empty string.")

    pattern = r"(\d+)([dhm])"
    matches = re.findall(pattern, offset_str.lower())

    if not matches:
        raise ValueError("Use formats like '2h', '1d3h', '45m'.")

    combined = ''.join(f"{value}{unit}" for value, unit in matches)
    if combined != offset_str.replace(" ", "").lower():
        raise ValueError("Only use digits with d, h, or m (e.g., '2h15m').")

    days = hours = minutes = 0
    for value, unit in matches:
        value = int(value)
        if unit == 'd': days += value
        elif unit == 'h': hours += value
        elif unit == 'm': minutes += value

    return timedelta(days=days, hours=hours, minutes=minutes)

def format_duration(minutes: int) -> str:
    d = minutes // 1440
    h = (minutes % 1440) // 60
    m = minutes % 60
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m: parts.append(f"{m}m")
    return " ".join(parts) if parts else "0m"

# ---------- Autocomplete ----------
async def get_mob_autocomplete(interaction: discord.Interaction, current: str) -> List[app_commands.Choice[str]]:
    mob_data = load_json(MOB_DATA_FILE)
    return [
        app_commands.Choice(name=name, value=name)
        for name in mob_data.keys()
        if current.lower() in name.lower()
    ][:25]

# ---------- Helper: Clear previous bot messages in channel ----------
async def clear_previous_bot_messages(channel: discord.TextChannel):
    try:
        async for msg in channel.history(limit=200):
            # Delete messages authored by this bot or embeds titled "Mob Timers"
            if msg.author == bot.user or (msg.author.bot and msg.content and "Mob Timers" in msg.content):
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception as e:
        print(f"Failed to clear previous messages: {e}")

# ---------- Bot Events ----------
@bot.event
async def on_ready():
    global timers
    await tree.sync()
    timers.update(load_json(TIMERS_FILE))
    print(f"✅ Logged in as {bot.user}")

    # Clear previous messages in the TOD channel on startup
    # (best-effort; requires bot to have manage_messages / read message history permissions)
    for guild in bot.guilds:
        for c in guild.text_channels:
            if c.name == TOD_CHANNEL_NAME:
                await clear_previous_bot_messages(c)
                break

    update_timer_display.start()

# ---------- /addmob ----------
@tree.command(name="addmob", description="Add a new mob to the tracker")
@app_commands.describe(
    mob="Name of the mob",
    respawn_min="Base respawn time in minutes (e.g. 4320 = 3 days)",
    variance_time="Variance time in minutes (e.g. 432 = 7h12m)",
    variance_mode="plus, minus, plusminus, or none"
)
async def addmob(interaction: discord.Interaction, mob: str, respawn_min: int, variance_time: int = 0, variance_mode: str = "none"):
    check_role(interaction.user, ROLE_ELEVATED)
    
    if variance_mode not in ["none", "plus", "minus", "plusminus"]:
        await interaction.response.send_message("❌ Invalid variance_mode. Use: none, plus, minus, plusminus", ephemeral=True)
        return
    
    # Compute min/max variance (in minutes) based on mode and variance_time
    if variance_mode == "plusminus":
        min_var = -abs(int(variance_time))
        max_var = abs(int(variance_time))
    elif variance_mode == "plus":
        min_var = 0
        max_var = abs(int(variance_time))
    elif variance_mode == "minus":
        min_var = -abs(int(variance_time))
        max_var = 0
    else:  # none
        min_var = 0
        max_var = 0

    mob_data = load_json(MOB_DATA_FILE)
    mob_data[mob] = {
        "respawn_min": respawn_min,
        "min_variance": min_var,
        "max_variance": max_var,
        "variance_mode": variance_mode
    }
    save_json(MOB_DATA_FILE, mob_data)
    await interaction.response.send_message(f"Mob `{mob}` added with variance [{min_var}, {max_var}] (mode: {variance_mode}).", ephemeral=True)

# ---------- /tod ----------
@tree.command(name="tod", description="Set time of death for a mob")
@app_commands.describe(
    mob="Name of the mob",
    offset="How long ago the mob died (e.g. '2h15m', '1d')"
)
@app_commands.autocomplete(mob=get_mob_autocomplete)
async def tod(interaction: discord.Interaction, mob: str, offset: str = "0m"):
    check_role(interaction.user, ROLE_USER)

    mob_data = load_json(MOB_DATA_FILE)
    if mob not in mob_data:
        await interaction.response.send_message(f"❌ Mob `{mob}` not found.", ephemeral=True)
        return

    try:
        offset_delta = parse_offset(offset)
    except ValueError as e:
        await interaction.response.send_message(f"❌ Invalid offset: {str(e)}", ephemeral=True)
        return

    now = datetime.now(timezone.utc)
    tod_time = now - offset_delta
    timers[mob] = {"tod": tod_time.isoformat()}
    save_timers()

    await interaction.response.send_message(
        f"ToD for `{mob}` set to <t:{int(tod_time.timestamp())}:f>.",
        ephemeral=True
    )

    await update_live_message()

# ---------- Live Timer Message ----------
async def update_live_message():
    global live_message_id, live_channel_id, timers

    # Locate the Discord channel
    channel = None
    for guild in bot.guilds:
        for c in guild.text_channels:
            if c.name == TOD_CHANNEL_NAME:
                channel = c
                break
        if channel:
            break

    if not channel:
        print("⚠️ {TOD_CHANNEL_NAME} channel not found.")
        return

    embed = discord.Embed(title="Mob Timers", color=discord.Color.blue())
    now = datetime.now(timezone.utc)
    mob_data = load_json(MOB_DATA_FILE)
    
    expired = []

    # Build fields
    for mob, data in list(timers.items()):
        if mob not in mob_data:
            continue

        try:
            tod_time = datetime.fromisoformat(data["tod"])
        except Exception:
            # Skip invalid entries
            continue

        mob_info = mob_data[mob]
        base_respawn = tod_time + timedelta(minutes=mob_info.get("respawn_min", 0))

        # Backwards-compatibility: support older keys
        if "min_variance" in mob_info and "max_variance" in mob_info:
            min_var = int(mob_info.get("min_variance", 0))
            max_var = int(mob_info.get("max_variance", 0))
        else:
            # older format: use variance_time + variance_mode if present
            variance = int(mob_info.get("variance_time", 0))
            mode = mob_info.get("variance_mode", "none")
            if mode == "plusminus":
                min_var = -abs(variance)
                max_var = abs(variance)
            elif mode == "plus":
                min_var = 0
                max_var = abs(variance)
            elif mode == "minus":
                min_var = -abs(variance)
                max_var = 0
            else:
                min_var = 0
                max_var = 0

        min_time = base_respawn + timedelta(minutes=min_var)
        max_time = base_respawn + timedelta(minutes=max_var)

        # Remove if expired (window has fully passed)
        if max_time < now:
            expired.append(mob)
            continue

        respawn_str = format_duration(mob_info.get("respawn_min", 0))
        if (min_var != 0 and max_var != 0):
            variance_str = f"±{format_duration(max_var)}"
        elif (min_var == 0 and max_var !=0):
            variance_str = f"+{format_duration(max_var)}"
        else: 
            variance_str = ""

        # Always show full window
        if min_time != max_time:
            time_str = f"<t:{int(min_time.timestamp())}:f> – <t:{int(max_time.timestamp())}:f>"
        else:
            time_str = f"<t:{int(min_time.timestamp())}:f>"
        
        field_value = f"{time_str}"
        embed.add_field(name=f"{mob} - Respawn: {respawn_str} {variance_str}\n", value=field_value, inline=False)

    # Delete expired timers now (after building the embed)
    if expired:
        for m in expired:
            if m in timers:
                del timers[m]
        save_timers()

    content = "**Current Mob Timers**"

    # Create or update message (do not rely on pinning)
    if live_message_id and live_channel_id == channel.id:
        try:
            msg = await channel.fetch_message(live_message_id)
            await msg.edit(content=content, embed=embed)
            return
        except discord.NotFound:
            pass  # Message was deleted or not found

    # Send new message
    try:
        msg = await channel.send(content=content, embed=embed)
        live_message_id = msg.id
        live_channel_id = channel.id
    except Exception as e:
        print(f"Failed to send live message: {e}")

# ----------------- BACKGROUND TASK -----------------
@tasks.loop(seconds=REFRESH_INTERVAL)
async def update_timer_display():
    await update_live_message()

# ----------------- RUN -----------------
if __name__ == '__main__':
    # Ensure timers file exists
    if not os.path.exists(TIMERS_FILE):
        save_json(TIMERS_FILE, {})
    bot.run(TOKEN)
