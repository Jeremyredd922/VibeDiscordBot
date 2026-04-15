import os
import json
import time
import random
import asyncio
import discord
import yt_dlp
from collections import defaultdict
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
from keep_alive import keep_alive

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
TOKEN = os.getenv("DISCORD_TOKEN")

# Persistent storage file
DATA_FILE = "locations.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─── Limits ────────────────────────────────────────────────────────────────────

MAX_NAME_LEN   = 64
MAX_NOTES_LEN  = 200
MAX_COORD      = 30_000_000   # beyond Minecraft's far lands / world border
MAX_LOCS_PER_GUILD = 500

# Per-user rate limit: max 5 write commands per 60 seconds
RATE_LIMIT_CALLS    = 5
RATE_LIMIT_WINDOW   = 60  # seconds

_user_timestamps: dict[int, list[float]] = defaultdict(list)

def is_rate_limited(user_id: int) -> bool:
    now = time.monotonic()
    timestamps = _user_timestamps[user_id]
    # Drop timestamps outside the window
    _user_timestamps[user_id] = [t for t in timestamps if now - t < RATE_LIMIT_WINDOW]
    if len(_user_timestamps[user_id]) >= RATE_LIMIT_CALLS:
        return True
    _user_timestamps[user_id].append(now)
    return False

def sanitize(text: str, max_len: int) -> str:
    """Strip leading/trailing whitespace and enforce a length cap."""
    return text.strip()[:max_len]

# ───────────────────────────────────────────────────────────────────────────────

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# ─── Events ────────────────────────────────────────────────────────────────────

MY_GUILD = discord.Object(id=int(os.getenv("GUILD_ID")))

@bot.event
async def on_ready():
    bot.tree.copy_global_to(guild=MY_GUILD)
    await bot.tree.sync(guild=MY_GUILD)
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

# ─── Slash Commands ─────────────────────────────────────────────────────────────

@bot.tree.command(name="addlocation", description="Save a Minecraft location or structure")
@app_commands.describe(
    name="Name for this location (e.g. 'Desert Temple', 'Base')",
    x="X coordinate",
    y="Y coordinate",
    z="Z coordinate",
    dimension="Dimension: overworld, nether, or end",
    notes="Optional notes about this location"
)
async def add_location(
    interaction: discord.Interaction,
    name: str,
    x: int,
    y: int,
    z: int,
    dimension: str = "overworld",
    notes: str = ""
):
    if is_rate_limited(interaction.user.id):
        await interaction.response.send_message(
            "You're sending commands too quickly. Try again in a moment.", ephemeral=True
        )
        return

    name  = sanitize(name,  MAX_NAME_LEN)
    notes = sanitize(notes, MAX_NOTES_LEN)

    if not name:
        await interaction.response.send_message("Location name cannot be empty.", ephemeral=True)
        return

    dimension = dimension.lower().strip()
    if dimension not in ("overworld", "nether", "end"):
        await interaction.response.send_message(
            "Dimension must be `overworld`, `nether`, or `end`.", ephemeral=True
        )
        return

    if not (-MAX_COORD <= x <= MAX_COORD and -MAX_COORD <= y <= MAX_COORD and -MAX_COORD <= z <= MAX_COORD):
        await interaction.response.send_message(
            f"Coordinates must be between -{MAX_COORD:,} and {MAX_COORD:,}.", ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    data = load_data()
    data.setdefault(guild_id, {})

    if len(data[guild_id]) >= MAX_LOCS_PER_GUILD and name.lower() not in data[guild_id]:
        await interaction.response.send_message(
            f"This server has reached the {MAX_LOCS_PER_GUILD}-location limit.", ephemeral=True
        )
        return

    key = name.lower()
    data[guild_id][key] = {
        "name": name,
        "x": x,
        "y": y,
        "z": z,
        "dimension": dimension,
        "notes": notes,
        "added_by": str(interaction.user)
    }
    save_data(data)

    embed = discord.Embed(title="Location Saved", color=0x2ecc71)
    embed.add_field(name="Name", value=name, inline=True)
    embed.add_field(name="Dimension", value=dimension.capitalize(), inline=True)
    embed.add_field(name="Coordinates", value=f"`{x}, {y}, {z}`", inline=False)
    if notes:
        embed.add_field(name="Notes", value=notes, inline=False)
    embed.set_footer(text=f"Added by {interaction.user}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="getlocation", description="Look up a saved Minecraft location")
@app_commands.describe(name="Name of the location to look up")
async def get_location(interaction: discord.Interaction, name: str):
    guild_id = str(interaction.guild_id)
    data = load_data()
    loc = data.get(guild_id, {}).get(name.lower())

    if not loc:
        await interaction.response.send_message(
            f"No location found for `{name}`. Use `/listlocations` to see all saved spots.",
            ephemeral=True
        )
        return

    embed = discord.Embed(title=loc["name"], color=0x3498db)
    embed.add_field(name="Dimension", value=loc["dimension"].capitalize(), inline=True)
    embed.add_field(name="Coordinates", value=f"`{loc['x']}, {loc['y']}, {loc['z']}`", inline=True)
    if loc.get("notes"):
        embed.add_field(name="Notes", value=loc["notes"], inline=False)
    embed.set_footer(text=f"Added by {loc['added_by']}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="listlocations", description="List all saved Minecraft locations")
@app_commands.describe(dimension="Filter by dimension (optional)")
async def list_locations(interaction: discord.Interaction, dimension: str = ""):
    guild_id = str(interaction.guild_id)
    data = load_data()
    locations = data.get(guild_id, {})

    if dimension:
        dimension = dimension.lower()
        locations = {k: v for k, v in locations.items() if v["dimension"] == dimension}

    if not locations:
        msg = "No locations saved yet." if not dimension else f"No locations saved for `{dimension}`."
        await interaction.response.send_message(msg, ephemeral=True)
        return

    embed = discord.Embed(
        title="Saved Minecraft Locations",
        color=0xe67e22,
        description=f"Filtered by: `{dimension}`" if dimension else "All dimensions"
    )

    for loc in locations.values():
        value = f"**Dim:** {loc['dimension'].capitalize()} | **Coords:** `{loc['x']}, {loc['y']}, {loc['z']}`"
        if loc.get("notes"):
            value += f"\n{loc['notes']}"
        embed.add_field(name=loc["name"], value=value, inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="deletelocation", description="Delete a saved Minecraft location")
@app_commands.describe(name="Name of the location to delete")
async def delete_location(interaction: discord.Interaction, name: str):
    if is_rate_limited(interaction.user.id):
        await interaction.response.send_message(
            "You're sending commands too quickly. Try again in a moment.", ephemeral=True
        )
        return

    guild_id = str(interaction.guild_id)
    data = load_data()
    key = sanitize(name, MAX_NAME_LEN).lower()

    if key not in data.get(guild_id, {}):
        await interaction.response.send_message(
            f"No location named `{name}` found.", ephemeral=True
        )
        return

    deleted = data[guild_id].pop(key)
    save_data(data)
    await interaction.response.send_message(
        f"Deleted location **{deleted['name']}** (`{deleted['x']}, {deleted['y']}, {deleted['z']}`)."
    )


@bot.tree.command(name="mchelp", description="Show all Minecraft bot commands")
async def mc_help(interaction: discord.Interaction):
    embed = discord.Embed(title="Minecraft Bot Commands", color=0x9b59b6)
    commands_list = [
        ("/addlocation",  "Save coords for a structure or POI"),
        ("/getlocation",  "Look up a saved location by name"),
        ("/listlocations","List all saved locations (filter by dimension)"),
        ("/deletelocation","Remove a saved location"),
        ("/mchelp",       "Show this help message"),
        ("", ""),
        ("PremiumOnlyFans", ""),
        ("/play",         "Search YouTube and play audio"),
        ("/skip",         "Skip the current track"),
        ("/pause",        "Pause playback"),
        ("/resume",       "Resume playback"),
        ("/stop",         "Stop and disconnect the bot"),
        ("/queue",        "Show the current music queue"),
        ("/nowplaying",   "Show what's currently playing"),
    ]
    for name, desc in commands_list:
        embed.add_field(name=name, value=desc, inline=False)
    await interaction.response.send_message(embed=embed)


# ─── Valorant Data ─────────────────────────────────────────────────────────────

VALO_SIDEARMS  = ["Classic", "Shorty", "Frenzy", "Ghost", "Sheriff"]
VALO_SMGS      = ["Stinger", "Spectre"]
VALO_SHOTGUNS  = ["Bucky", "Judge"]
VALO_RIFLES    = ["Bulldog", "Guardian", "Phantom", "Vandal"]
VALO_SNIPERS   = ["Marshal", "Outlaw", "Operator"]
VALO_HEAVIES   = ["Ares", "Odin"]
VALO_PRIMARIES = VALO_SMGS + VALO_SHOTGUNS + VALO_RIFLES + VALO_SNIPERS + VALO_HEAVIES

VALO_AGENTS = [
    "Brimstone", "Viper", "Omen", "Killjoy", "Cypher", "Sova", "Sage",
    "Phoenix", "Jett", "Reyna", "Raze", "Breach", "Skye", "Yoru", "Astra",
    "KAY/O", "Chamber", "Neon", "Fade", "Harbor", "Gekko", "Deadlock",
    "Iso", "Clove", "Vyse", "Tejo", "Waylay"
]

VALO_AGENT_ROLES = {
    "Duelist":    ["Phoenix", "Jett", "Reyna", "Raze", "Yoru", "Neon", "Iso", "Waylay"],
    "Initiator":  ["Sova", "Breach", "Skye", "KAY/O", "Fade", "Gekko", "Tejo"],
    "Controller": ["Brimstone", "Viper", "Omen", "Astra", "Harbor", "Clove"],
    "Sentinel":   ["Killjoy", "Cypher", "Sage", "Chamber", "Deadlock", "Vyse"],
}

VALO_CHALLENGES = [
    "Pistols only all round — no primary weapons allowed.",
    "Every player must buy the same weapon this round.",
    "Knife rush — run straight for the site, no shooting until you're on site.",
    "No scope only — if you have a sniper, no ADS allowed.",
    "Buy the most expensive loadout you can afford, no saving.",
    "One-tap challenge — headshots only, body shots = sit out next round.",
    "Sheriff or bust — sidearm must be the Sheriff, no primaries.",
    "Eco warriors — spend no more than 1,000 credits this round.",
    "Classic challenge — everyone uses only the Classic this round.",
    "Operator round — whoever has the most credits must buy the Operator.",
    "Shotgun frenzy — only Bucky or Judge allowed as primary.",
    "Run it down — no stopping, push the site as fast as possible.",
    "Spray battle — only Ares or Odin allowed this round.",
    "Ghost round — everyone buys the Ghost, no other weapons.",
    "Judge and pray — everyone buys the Judge, rush the closest site.",
]


# ─── Valorant Commands ──────────────────────────────────────────────────────────

@bot.tree.command(name="loadout", description="Generate a random Valorant weapon loadout")
async def loadout(interaction: discord.Interaction):
    primary  = random.choice(VALO_PRIMARIES)
    sidearm  = random.choice(VALO_SIDEARMS)

    if primary in VALO_SMGS:
        category = "SMG"
    elif primary in VALO_SHOTGUNS:
        category = "Shotgun"
    elif primary in VALO_RIFLES:
        category = "Rifle"
    elif primary in VALO_SNIPERS:
        category = "Sniper"
    else:
        category = "Heavy"

    embed = discord.Embed(title="Your Valorant Loadout", color=0xff4655)
    embed.add_field(name=f"Primary ({category})", value=f"**{primary}**", inline=True)
    embed.add_field(name="Sidearm", value=f"**{sidearm}**", inline=True)
    embed.set_footer(text=f"Rolled by {interaction.user}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="agentpick", description="Spin for a random Valorant agent")
@app_commands.describe(role="Filter by role: Duelist, Initiator, Controller, Sentinel (optional)")
async def agent_pick(interaction: discord.Interaction, role: str = ""):
    if role:
        role_title = role.strip().title()
        pool = VALO_AGENT_ROLES.get(role_title)
        if pool is None:
            await interaction.response.send_message(
                "Invalid role. Choose from: `Duelist`, `Initiator`, `Controller`, `Sentinel`.",
                ephemeral=True
            )
            return
        agent = random.choice(pool)
        label = f"{role_title} Agent"
    else:
        agent = random.choice(VALO_AGENTS)
        label = next((r for r, agents in VALO_AGENT_ROLES.items() if agent in agents), "Agent")

    embed = discord.Embed(title="Agent Spin", color=0xff4655)
    embed.add_field(name=label, value=f"**{agent}**", inline=False)
    embed.set_footer(text=f"Spun by {interaction.user}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="valochallenge", description="Get a random challenge rule for the next Valorant round")
async def valo_challenge(interaction: discord.Interaction):
    challenge = random.choice(VALO_CHALLENGES)
    embed = discord.Embed(title="Round Challenge", color=0xff4655)
    embed.description = f"**{challenge}**"
    embed.set_footer(text=f"Challenged by {interaction.user}")
    await interaction.response.send_message(embed=embed)


# ─── Music (PremiumOnlyFans) ───────────────────────────────────────────────────

PREMIUM_ROLE = "PremiumOnlyFans"

def premium_check():
    """App command check: user must have the PremiumOnlyFans role."""
    async def predicate(interaction: discord.Interaction) -> bool:
        if discord.utils.get(interaction.user.roles, name=PREMIUM_ROLE) is None:
            await interaction.response.send_message(
                f"This command requires the **{PREMIUM_ROLE}** role.", ephemeral=True
            )
            return False
        return True
    return app_commands.check(predicate)

# yt-dlp options — stream best audio, no download
_YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch1",
    "source_address": "0.0.0.0",
}

# FFmpeg reconnect flags keep streams alive through brief network blips
_FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

# Per-guild state: { guild_id: { "queue": [...], "current": {...}|None, "text_channel": channel } }
_music: dict[int, dict] = {}

def _guild_state(guild_id: int) -> dict:
    if guild_id not in _music:
        _music[guild_id] = {"queue": [], "current": None, "text_channel": None}
    return _music[guild_id]

async def _fetch_track(query: str) -> dict:
    """Resolve a search query or URL to a streamable track dict."""
    loop = asyncio.get_event_loop()
    with yt_dlp.YoutubeDL(_YDL_OPTS) as ydl:
        info = await loop.run_in_executor(
            None, lambda: ydl.extract_info(query, download=False)
        )
    if "entries" in info:
        info = info["entries"][0]
    return {
        "url":         info["url"],
        "title":       info.get("title", "Unknown"),
        "duration":    info.get("duration", 0),
        "webpage_url": info.get("webpage_url", ""),
        "thumbnail":   info.get("thumbnail", ""),
        "uploader":    info.get("uploader", "Unknown"),
    }

def _fmt_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

def _play_next(guild_id: int, voice_client: discord.VoiceClient):
    """Play the next track in queue, or mark idle if empty."""
    state = _guild_state(guild_id)
    if not state["queue"]:
        state["current"] = None
        return

    track = state["queue"].pop(0)
    state["current"] = track

    source = discord.FFmpegPCMAudio(track["url"], **_FFMPEG_OPTS)
    voice_client.play(
        discord.PCMVolumeTransformer(source, volume=0.5),
        after=lambda _: _play_next(guild_id, voice_client),
    )

    channel = state["text_channel"]
    if channel:
        embed = _now_playing_embed(track)
        asyncio.run_coroutine_threadsafe(channel.send(embed=embed), bot.loop)

def _now_playing_embed(track: dict) -> discord.Embed:
    embed = discord.Embed(
        title="Now Playing",
        description=f"[{track['title']}]({track['webpage_url']})",
        color=0xff0000,
    )
    embed.add_field(name="Duration", value=_fmt_duration(track["duration"]), inline=True)
    embed.add_field(name="Uploader", value=track["uploader"], inline=True)
    if track.get("thumbnail"):
        embed.set_thumbnail(url=track["thumbnail"])
    return embed


@bot.tree.command(name="play", description="Search YouTube and play audio in your voice channel")
@app_commands.describe(query="Song name or YouTube URL")
@premium_check()
async def music_play(interaction: discord.Interaction, query: str):
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "You need to be in a voice channel first.", ephemeral=True
        )
        return

    await interaction.response.defer()

    voice_channel = interaction.user.voice.channel
    guild_id = interaction.guild_id
    state = _guild_state(guild_id)
    state["text_channel"] = interaction.channel

    vc: discord.VoiceClient = interaction.guild.voice_client
    if vc is None:
        vc = await voice_channel.connect()
    elif vc.channel != voice_channel:
        await vc.move_to(voice_channel)

    try:
        track = await _fetch_track(query)
    except Exception as exc:
        await interaction.followup.send(f"Could not find that track: `{exc}`", ephemeral=True)
        return

    if vc.is_playing() or vc.is_paused():
        state["queue"].append(track)
        embed = discord.Embed(
            title="Added to Queue",
            description=f"[{track['title']}]({track['webpage_url']})",
            color=0x3498db,
        )
        embed.add_field(name="Position", value=str(len(state["queue"])), inline=True)
        embed.add_field(name="Duration", value=_fmt_duration(track["duration"]), inline=True)
        if track.get("thumbnail"):
            embed.set_thumbnail(url=track["thumbnail"])
        await interaction.followup.send(embed=embed)
    else:
        state["queue"].append(track)
        _play_next(guild_id, vc)
        await interaction.followup.send(embed=_now_playing_embed(track))


@bot.tree.command(name="skip", description="Skip the current track")
@premium_check()
async def music_skip(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client
    if vc is None or not vc.is_playing():
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    vc.stop()
    await interaction.response.send_message("Skipped.")


@bot.tree.command(name="pause", description="Pause the current track")
@premium_check()
async def music_pause(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume a paused track")
@premium_check()
async def music_resume(interaction: discord.Interaction):
    vc: discord.VoiceClient = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop playback and disconnect the bot")
@premium_check()
async def music_stop(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    state = _guild_state(guild_id)
    state["queue"].clear()
    state["current"] = None

    vc: discord.VoiceClient = interaction.guild.voice_client
    if vc:
        await vc.disconnect()
        await interaction.response.send_message("Stopped and disconnected.")
    else:
        await interaction.response.send_message("Not connected to a voice channel.", ephemeral=True)


@bot.tree.command(name="queue", description="Show the current music queue")
@premium_check()
async def music_queue(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    state = _guild_state(guild_id)
    current = state["current"]
    queue   = state["queue"]

    if not current and not queue:
        await interaction.response.send_message("The queue is empty.", ephemeral=True)
        return

    embed = discord.Embed(title="Music Queue", color=0x9b59b6)

    if current:
        embed.add_field(
            name="Now Playing",
            value=f"[{current['title']}]({current['webpage_url']}) `{_fmt_duration(current['duration'])}`",
            inline=False,
        )

    if queue:
        lines = []
        for i, track in enumerate(queue[:10], 1):
            lines.append(f"`{i}.` [{track['title']}]({track['webpage_url']}) `{_fmt_duration(track['duration'])}`")
        if len(queue) > 10:
            lines.append(f"...and {len(queue) - 10} more")
        embed.add_field(name="Up Next", value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="nowplaying", description="Show what's currently playing")
@premium_check()
async def music_nowplaying(interaction: discord.Interaction):
    state = _guild_state(interaction.guild_id)
    current = state["current"]
    if not current:
        await interaction.response.send_message("Nothing is playing right now.", ephemeral=True)
        return
    await interaction.response.send_message(embed=_now_playing_embed(current))


# ─── Run ───────────────────────────────────────────────────────────────────────

keep_alive()
bot.run(TOKEN)
