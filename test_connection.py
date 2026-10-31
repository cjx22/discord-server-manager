"""Read-only connection test: logs in and lists the server's channels and roles."""

import discord

from common import REQUIRED_PERMISSIONS, cli_main, invite_url, run_with_guild

TYPE_LABELS = {
    discord.ChannelType.text: "text",
    discord.ChannelType.news: "announcement",
    discord.ChannelType.voice: "voice",
    discord.ChannelType.stage_voice: "stage",
    discord.ChannelType.forum: "forum",
}


async def report(client, guild):
    me = guild.me
    perms = me.guild_permissions
    print(f"Logged in as {client.user} (id {client.user.id})")
    print(f"Connected to {guild.name} (id {guild.id}), {guild.member_count} members\n")

    if perms.administrator:
        print("Bot permissions: Administrator (everything allowed)")
    else:
        missing = [name for name, needed in REQUIRED_PERMISSIONS if needed and not getattr(perms, name)]
        if missing:
            print(f"Bot is missing permissions needed for server management: {', '.join(missing)}")
            print(f"Re-invite with the right permissions:\n  {invite_url(client.user.id, guild.id)}")
        else:
            print("Bot permissions: everything the manager needs")
    print(f"Bot's highest role: {me.top_role.name} (position {me.top_role.position}). "
          "It can only edit roles below this.\n")

    print("Channels")
    for category, channels in guild.by_category():
        print(f"  {category.name if category else '(no category)'}")
        for ch in channels:
            print(f"    [{TYPE_LABELS.get(ch.type, str(ch.type))}] {ch.name}")

    print("\nRoles (highest first)")
    for role in reversed(guild.roles):
        notes = []
        if role.managed:
            notes.append("managed by an integration")
        elif role == me.top_role:
            notes.append("bot's role")
        elif role > me.top_role:
            notes.append("above the bot, can't be edited by it")
        if role.permissions.administrator:
            notes.append("administrator")
        color = f"#{role.colour.value:06x}" if role.colour.value else "no color"
        suffix = f"  ({'; '.join(notes)})" if notes else ""
        print(f"  {role.position:>3}  {role.name}  [{color}]{suffix}")

    print(f"\n{len(guild.categories)} categories, {len(guild.channels) - len(guild.categories)} channels, "
          f"{len(guild.roles)} roles. No changes were made.")


if __name__ == "__main__":
    cli_main(lambda: run_with_guild(report))
