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
TOKEN = "MTUxNTgxODI0MjIzNTQzNzIwNw.GjBhgl.EIWSUotAMCmmGr71-19ol6A7TkSeaNE7Efj2Bw"
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


def random_code(length: int = 6) -> str:
    chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(random.choice(chars) for _ in range(length))


def generate_captcha_image(text: str) -> BytesIO:
    w, h = 300, 100
    img = Image.new("RGB", (w, h), (240, 240, 240))
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.truetype("arial.ttf", 48)
    except Exception:
        font = ImageFont.load_default()

    # noise lines
    for _ in range(6):
        x1, y1 = random.randint(0, w), random.randint(0, h)
        x2, y2 = random.randint(0, w), random.randint(0, h)
        color = tuple(random.randint(50, 180) for _ in range(3))
        draw.line((x1, y1, x2, y2), fill=color, width=2)

    # helper for char size using textbbox (Pillow 10+)
    def char_size(ch: str):
        bbox = draw.textbbox((0, 0), ch, font=font)
        return bbox[2] - bbox[0], bbox[3] - bbox[1]

    total_width = sum(char_size(c)[0] for c in text)
    start_x = (w - total_width) // 2
    x = start_x
    for ch in text:
        ch_w, ch_h = char_size(ch)
        char_img = Image.new("RGBA", (ch_w + 10, h), (0, 0, 0, 0))
        char_draw = ImageDraw.Draw(char_img)
        color = (27, 94, 32)
        char_draw.text((5, (h - ch_h) // 2), ch, font=font, fill=color)
        angle = random.uniform(-30, 30)
        char_img = char_img.rotate(angle, resample=Image.BILINEAR, expand=1)
        img.paste(char_img, (x, 0), char_img)
        x += ch_w

    # dots
    for _ in range(60):
        rx = random.randint(0, w)
        ry = random.randint(0, h)
        draw.ellipse((rx, ry, rx + 1, ry + 1), fill=(0, 0, 0))

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
        # Only the original user can answer
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
        code = random_code(6)
        captcha_store[user_id] = code

        # expire after 2 minutes
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

    filtered_items = []
    for uid_str, score in scores.items():
        if guild is None:
            filtered_items.append((uid_str, score))
            continue
        member = guild.get_member(int(uid_str))
        if member and any(r.id == VERIFIED_ROLE_ID for r in member.roles):
            filtered_items.append((uid_str, score))

    sorted_items = sorted(filtered_items, key=lambda kv: (-kv[1], int(kv[0])))

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

    if guild is not None:
        new_scores = {uid: s for uid, s in filtered_items}
        leaderboard["scores"] = new_scores
        save_leaderboard(leaderboard)

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

    # sync slash commands
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

    # clear bot messages in the two channels on startup
    await clear_bot_messages(ANNOUNCE_CHANNEL_ID)
    await clear_bot_messages(LEADERBOARD_CHANNEL_ID)

    # send the guide message on startup
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

    # ensure leaderboard message exists (new one after clear)
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
    # Check if user is already verified
    guild = interaction.guild
    if guild is not None:
        member = guild.get_member(interaction.user.id)
        if member and any(r.id == VERIFIED_ROLE_ID for r in member.roles):
            await interaction.response.send_message(
                "You have already been verified you cant verify again",
                ephemeral=True,
            )
            return

    # Not verified → show guide + Verify button
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

    if not any(r.id == VERIFIED_ROLE_ID for r in person.roles):
        await interaction.response.send_message(
            f"{person.mention} does not have the Verified role, so they cannot be on the leaderboard.",
            ephemeral=True,
        )
        return

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
    bot.run(TOKEN)
