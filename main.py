# main.py
import random
import json
import os
import asyncio
from io import BytesIO

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button, Modal, TextInput
from PIL import Image, ImageDraw, ImageFont

# ---- CONFIG ----
GUILD_ID = 1495226742124843048  # your server ID
ANNOUNCE_CHANNEL_ID = 1512630051869425694  # how-to-get-linked channel
LEADERBOARD_CHANNEL_ID = 1510834005518585957  # leaderboard channel
VERIFIED_ROLE_ID = 1515845200096919593  # Verified role ID
LEADERBOARD_FILE = "leaderboard.json"  # {"scores": {user_id: score}, "msg_id": message_id}
# ----------------

intents = discord.Intents.default()
intents.messages = True
intents.guilds = True
intents.dm_messages = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# user_id -> expected code
captcha_store: dict[int, str] = {}


def load_leaderboard():
    if os.path.exists(LEADERBOARD_FILE):
        with open(LEADERBOARD_FILE, "r") as f:
            return json.load(f)
    return {"scores": {}, "msg_id": None}


def save_leaderboard(data):
    with open(LEADERBOARD_FILE, "w") as f:
        json.dump(data, f)


leaderboard = load_leaderboard()


# 4‑digit numeric code, easy but not too weak
def random_code(length: int = 4) -> str:
    chars = "23456789"
    return "".join(random.choice(chars) for _ in range(length))


# HARDER (but no rotation): more noise, jittered chars
def generate_captcha_image(text: str) -> BytesIO:
    w, h = 260, 80
    img = Image.new("RGB", (w, h), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 42)
    except Exception:
        font = ImageFont.load_default()

    # grid background
    for y in range(0, h, 16):
        draw.line((0, y, w, y), fill=(220, 220, 220), width=1)
    for x in range(0, w, 26):
        draw.line((x, 0, x, h), fill=(220, 220, 220), width=1)

    # random thicker lines
    for _ in range(4):
        x1, y1 = random.randint(0, w), random.randint(0, h)
        x2, y2 = random.randint(0, w), random.randint(0, h)
        color = (
            random.randint(120, 180),
            random.randint(120, 180),
            random.randint(120, 180),
        )
        draw.line((x1, y1, x2, y2), fill=color, width=2)

    # measure full text
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = draw.textsize(text, font=font)

    base_x = (w - text_w) // 2
    base_y = (h - text_h) // 2

    # draw each char with slight vertical/spacing jitter
    x = base_x
    for ch in text:
        try:
            cbox = draw.textbbox((0, 0), ch, font=font)
            ch_w = cbox[2] - cbox[0]
        except Exception:
            ch_w, _ = draw.textsize(ch, font=font)

        offset_y = random.randint(-4, 4)
        spacing = random.randint(0, 4)
        color = (
            random.randint(0, 40),
            random.randint(0, 40),
            random.randint(0, 40),
        )
        draw.text((x, base_y + offset_y), ch, font=font, fill=color)
        x += ch_w + spacing

    # more random dots
    for _ in range(40):
        rx = random.randint(0, w)
        ry = random.randint(0, h)
        draw.ellipse((rx, ry, rx + 1, ry + 1), fill=(100, 100, 100))

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


class AnswerModal(Modal, title="Captcha verification"):
    answer = TextInput(label="Enter the text shown in the image:", max_length=10)

    def __init__(self, user_id: int):
        super().__init__()
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction):
        expected = captcha_store.pop(self.user_id, None)
        if expected is None:
            await interaction.response.send_message(
                "No active captcha found or it expired. Please run /link again.",
                ephemeral=True,
            )
            return

        if self.answer.value.strip().upper() != expected.upper():
            await interaction.response.send_message(
                "Incorrect captcha. Please run /link again.", ephemeral=True
            )
            return

        # 1) Correct answer → DM first
        try:
            await interaction.user.send("You have been linked!")
        except Exception:
            await interaction.response.send_message(
                "I could not DM you (your DMs might be closed). "
                "Please enable DMs and try again.",
                ephemeral=True,
            )
            return

        # 2) DM succeeded → give Verified role
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "Something went wrong (no guild found). Please try again in the server.",
                ephemeral=True,
            )
            return

        role = guild.get_role(VERIFIED_ROLE_ID)
        if role is None:
            await interaction.response.send_message(
                "Verified role is not configured correctly. Please contact an admin.",
                ephemeral=True,
            )
            return

        member = guild.get_member(self.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(self.user_id)
            except Exception:
                member = None

        if member is None:
            await interaction.response.send_message(
                "Could not find you in the server. Please rejoin and try again.",
                ephemeral=True,
            )
            return

        try:
            await member.add_roles(
                role,
                reason="Captcha passed and DM sent: You have been linked!",
            )
        except Exception:
            await interaction.response.send_message(
                "I could not give you the Verified role (check my role position/permissions).",
                ephemeral=True,
            )
            return

        # 3) Role added → now add to leaderboard and confirm
        uid_str = str(self.user_id)
        if uid_str not in leaderboard["scores"]:
            leaderboard["scores"][uid_str] = 0
            save_leaderboard(leaderboard)

        await interaction.response.send_message(
            "Verification successful — check your DMs.", ephemeral=True
        )
        await update_leaderboard_message()


class AnswerView(View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(
        label="Answer",
        style=discord.ButtonStyle.primary,
        custom_id="answer_button",
    )
    async def answer_button(
        self, interaction: discord.Interaction, button: Button
    ):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message(
                "This button is not for you.", ephemeral=True
            )
            return

        modal = AnswerModal(user_id=self.user_id)
        await interaction.response.send_modal(modal)


class InitialView(View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Verify",
        style=discord.ButtonStyle.success,
        custom_id="verify_button",
    )
    async def verify_button(
        self, interaction: discord.Interaction, button: Button
    ):
        user_id = interaction.user.id
        code = random_code()
        captcha_store[user_id] = code

        async def expire(uid):
            await asyncio.sleep(120)
            captcha_store.pop(uid, None)

        asyncio.create_task(expire(user_id))

        buf = generate_captcha_image(code)
        file = discord.File(fp=buf, filename="captcha.png")
        view = AnswerView(user_id=user_id)
        await interaction.response.send_message(
            content="Please solve this captcha and click Answer to submit:",
            file=file,
            view=view,
            ephemeral=True,
        )


async def build_leaderboard_embed():
    scores = leaderboard.get("scores", {})
    guild = bot.get_guild(GUILD_ID) if GUILD_ID else None
    embed = discord.Embed(
        title="AGT challenge leaderboard",
        color=0xFF0000,
    )

    display_items = []
    for uid_str, score in scores.items():
        if guild is None:
            display_items.append((uid_str, score))
            continue
        member = guild.get_member(int(uid_str))
        if member and any(r.id == VERIFIED_ROLE_ID for r in member.roles):
            display_items.append((uid_str, score))

    sorted_items = sorted(display_items, key=lambda kv: (-kv[1], int(kv[0])))

    if not sorted_items:
        embed.description = "No players yet."
    else:
        lines = []
        for i, (uid_str, score) in enumerate(sorted_items, start=1):
            try:
                user = await bot.fetch_user(int(uid_str))
                mention = user.mention
            except Exception:
                mention = f"<@{uid_str}>"
            lines.append(f"{i}. {mention} — {score}")
        embed.description = "\n".join(lines)

    return embed


async def update_leaderboard_message():
    try:
        channel = bot.get_channel(LEADERBOARD_CHANNEL_ID) or await bot.fetch_channel(
            LEADERBOARD_CHANNEL_ID
        )
        old_msg_id = leaderboard.get("msg_id")
        if old_msg_id:
            try:
                old_msg = await channel.fetch_message(old_msg_id)
                await old_msg.delete()
            except Exception:
                pass

        embed = await build_leaderboard_embed()
        sent = await channel.send(embed=embed)
        leaderboard["msg_id"] = sent.id
        save_leaderboard(leaderboard)
    except Exception as e:
        print("Failed to update leaderboard:", e)


async def clear_bot_messages(channel_id: int, limit: int = 1000):
    try:
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        async for msg in channel.history(limit=limit):
            if msg.author.id == bot.user.id:
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception as e:
        print(f"Failed to clear bot messages in {channel_id}:", e)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
        else:
            await bot.tree.sync()
        print("Slash commands synced.")
    except Exception as e:
        print("Failed to sync commands:", e)

    await clear_bot_messages(ANNOUNCE_CHANNEL_ID)
    await clear_bot_messages(LEADERBOARD_CHANNEL_ID)

    try:
        guide = (
            "## AGT linking guide\n\n"
            "To link first start off by running the command /link, when you do that "
            "the bot will make you verify to make sure your not a bot then you will "
            "be linked and put on the leaderboard if you have any sorts of confusion "
            "or problems please make a ticket"
        )
        channel = bot.get_channel(ANNOUNCE_CHANNEL_ID) or await bot.fetch_channel(
            ANNOUNCE_CHANNEL_ID
        )
        await channel.send(guide)
    except Exception as e:
        print("Failed to send guide:", e)

    try:
        await update_leaderboard_message()
    except Exception as e:
        print("Failed to ensure leaderboard on ready:", e)


@bot.tree.command(
    name="link",
    description="Start verification captcha",
    guild=discord.Object(id=GUILD_ID) if GUILD_ID else None,
)
async def link_command(interaction: discord.Interaction):
    guild = interaction.guild
    if guild is not None:
        member = guild.get_member(interaction.user.id)
        if member and any(r.id == VERIFIED_ROLE_ID for r in member.roles):
            await interaction.response.send_message(
                "You have already been verified you cant verify again",
                ephemeral=True,
            )
            return

    guide = (
        "## AGT linking guide\n\n"
        "To link first start off by running the command /link, when you do that "
        "the bot will make you verify to make sure your not a bot then you will be "
        "linked and put on the leaderboard if you have any sorts of confusion or "
        "problems please make a ticket"
    )
    view = InitialView()
    await interaction.response.send_message(
        content=guide,
        view=view,
        ephemeral=True,
    )


@bot.tree.command(
    name="data",
    description="Show current leaderboard data and raw JSON (admins only)",
    guild=discord.Object(id=GUILD_ID) if GUILD_ID else None,
)
async def data_command(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You do not have permission to use this command.",
            ephemeral=True,
        )
        return

    scores = leaderboard.get("scores", {})
    msg_id = leaderboard.get("msg_id")
    entries_count = len(scores)

    lines = []
    for i, (uid_str, score) in enumerate(scores.items(), start=1):
        if i > 10:
            lines.append(f"... and {entries_count - 10} more entries")
            break
        lines.append(f"{uid_str}: {score}")
    summary = "\n".join(lines) if lines else "No scores stored."

    if os.path.exists(LEADERBOARD_FILE):
        try:
            with open(LEADERBOARD_FILE, "r") as f:
                raw_json = f.read()
        except Exception as e:
            raw_json = f"Error reading file: {e}"
    else:
        raw_json = "File does not exist."

    if len(raw_json) > 1900:
        raw_json = raw_json[:1900] + "\n... (truncated)"

    text = (
        f"**In-memory leaderboard data:**\n"
        f"- Entries: `{entries_count}`\n"
        f"- Stored leaderboard message ID: `{msg_id}`\n\n"
        f"**Scores preview (user_id: score):**\n"
        f"```txt\n{summary}\n```\n"
        f"**Raw {LEADERBOARD_FILE}:**\n"
        f"```json\n{raw_json}\n```"
    )

    await interaction.response.send_message(text, ephemeral=True)


@bot.tree.command(
    name="submit-score",
    description="Set a player's leaderboard score (admins only)",
    guild=discord.Object(id=GUILD_ID) if GUILD_ID else None,
)
@app_commands.describe(
    person="The member to set the score for",
    points="The score value (0 or higher)",
)
async def submit_score(
    interaction: discord.Interaction, person: discord.Member, points: int
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message(
            "You do not have permission to use this command.", ephemeral=True
        )
        return

    # TEMP: Verified check disabled so you can confirm storage works
    # Re-enable later if you want to restrict:
    # if not any(r.id == VERIFIED_ROLE_ID for r in person.roles):
    #     await interaction.response.send_message(
    #         f"{person.mention} does not have the Verified role, so they cannot be on the leaderboard.",
    #         ephemeral=True,
    #     )
    #     return

    if points < 0:
        await interaction.response.send_message(
            "Points must be 0 or greater.", ephemeral=True
        )
        return

    uid_str = str(person.id)
    leaderboard.setdefault("scores", {})
    leaderboard["scores"][uid_str] = points
    save_leaderboard(leaderboard)

    await update_leaderboard_message()
    await interaction.response.send_message(
        f"Set {person.mention}'s score to {points}.", ephemeral=True
    )


if __name__ == "__main__":
    bot.run(os.getenv("TOKEN"))
