"""Shared helpers: config loading and a one-shot bot session."""

import logging
import os
import sys
from pathlib import Path

import discord
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent

# Channel and role names often contain emoji; keep Windows consoles from crashing on them.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# What the manager needs to create channels/roles, set permissions and post welcome messages.
REQUIRED_PERMISSIONS = discord.Permissions(
    view_channel=True,
    manage_channels=True,
    manage_roles=True,
    send_messages=True,
    embed_links=True,
    attach_files=True,
    read_message_history=True,
)


class ConfigError(Exception):
    """A setup problem the user has to fix (bad token, wrong server ID, ...)."""


def load_config():
    load_dotenv(ROOT / ".env")
    token = os.getenv("DISCORD_BOT_TOKEN", "").strip()
    guild_id = os.getenv("DISCORD_GUILD_ID", "").strip()
    if not token:
        raise ConfigError("DISCORD_BOT_TOKEN is empty. Paste your bot token into .env.")
    if not guild_id.isdigit():
        raise ConfigError("DISCORD_GUILD_ID must be your server's numeric ID. Set it in .env.")
    return token, int(guild_id)


def invite_url(client_id, guild_id=None):
    return discord.utils.oauth_url(
        client_id,
        permissions=REQUIRED_PERMISSIONS,
        scopes=("bot",),
        guild=discord.Object(guild_id) if guild_id else discord.utils.MISSING,
    )


def run_with_guild(task):
    """Log in, run `await task(client, guild)`, then log out and return its result."""
    token, guild_id = load_config()
    client = discord.Client(intents=discord.Intents.default())
    outcome = {}

    @client.event
    async def on_ready():
        try:
            guild = client.get_guild(guild_id)
            if guild is None:
                joined = ", ".join(f"{g.name} ({g.id})" for g in client.guilds) or "none"
                outcome["error"] = ConfigError(
                    f"The bot is not in server {guild_id}. Servers it is in: {joined}.\n"
                    f"Invite it with:\n  {invite_url(client.user.id, guild_id)}"
                )
            else:
                outcome["result"] = await task(client, guild)
        except BaseException as exc:
            outcome["error"] = exc
        finally:
            await client.close()

    try:
        client.run(token, log_level=logging.WARNING)
    except discord.LoginFailure:
        raise ConfigError(
            "Discord rejected the bot token. Reset it in the Developer Portal (Bot -> Reset Token) "
            "and paste the new one into .env."
        ) from None
    if "error" in outcome:
        raise outcome["error"]
    return outcome.get("result")


def cli_main(fn):
    """Run fn(), turning setup problems into a readable message and exit code 1."""
    try:
        return fn()
    except ConfigError as exc:
        print(f"Setup problem: {exc}", file=sys.stderr)
        sys.exit(1)
