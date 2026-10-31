"""Plan and apply a server layout described in a YAML file.

  python manage.py export [-o layouts/current.yaml]    snapshot the server as a layout
  python manage.py plan layouts/my_layout.yaml         preview changes (read-only)
  python manage.py apply layouts/my_layout.yaml        preview, then ask before applying
  python manage.py apply layouts/my_layout.yaml --confirm <hash>   apply the exact plan you reviewed

Only things listed in the layout are created or updated, and only the fields the
layout sets are managed. Nothing is ever deleted: channels, roles, messages and
permission overwrites that aren't in the layout are left alone.
"""

import argparse
import asyncio
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path

import discord
import yaml

from common import ROOT, cli_main, run_with_guild

STATE_PATH = ROOT / "data" / "state.json"
REASON = "Server Manager layout"
HISTORY_LIMIT = 50

CHANNEL_TYPES = {
    "text": discord.ChannelType.text,
    "announcement": discord.ChannelType.news,
    "voice": discord.ChannelType.voice,
    "stage": discord.ChannelType.stage_voice,
    "forum": discord.ChannelType.forum,
}
TYPE_NAMES = {v: k for k, v in CHANNEL_TYPES.items()}
COMMUNITY_TYPES = {"announcement", "stage"}
# Channel settings the layout may manage, per type (discord.py attribute names).
CHANNEL_SETTINGS = {
    "text": ("topic", "nsfw", "slowmode_delay"),
    "announcement": ("topic", "nsfw"),
    "forum": ("topic", "nsfw", "slowmode_delay"),
    "voice": ("nsfw", "user_limit"),
    "stage": (),
}
ROLE_FIELDS = ("color", "hoist", "mentionable", "permissions")
EMBED_FIELDS = ("title", "description", "color", "footer", "image", "thumbnail")
VALID_PERMS = discord.Permissions.VALID_FLAGS


# ---------------------------------------------------------------- layout file

class LayoutError(Exception):
    pass


def parse_color(value):
    match = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(value).strip())
    if not match:
        raise ValueError(f"color must look like #5865f2, got {value!r}")
    return discord.Colour(int(match.group(1), 16))


def load_layout(path):
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    for key in ("roles", "categories", "channels"):
        data[key] = data.get(key) or []
    # `overwrites` may be a list of blocks, merged permission by permission.
    for item in data["categories"] + data["channels"] + [
            ch for cat in data["categories"] for ch in cat.get("channels") or []]:
        if isinstance(item.get("overwrites"), list):
            merged = {}
            for block in item["overwrites"]:
                merged = merge_overwrites(merged, block or {})
            item["overwrites"] = merged
    errors = []

    def check_perms(where, names):
        for name in names or []:
            if name not in VALID_PERMS:
                errors.append(f"{where}: unknown permission '{name}'")

    def check_overwrites(where, overwrites):
        for target, spec in (overwrites or {}).items():
            spec = spec or {}
            check_perms(f"{where}, overwrite for {target}", spec.get("allow"))
            check_perms(f"{where}, overwrite for {target}", spec.get("deny"))
            if both := set(spec.get("allow") or []) & set(spec.get("deny") or []):
                errors.append(f"{where}: {target} both allows and denies {', '.join(sorted(both))}")

    def check_color(where, value):
        try:
            parse_color(value)
        except ValueError as exc:
            errors.append(f"{where}: {exc}")

    for role in data["roles"]:
        where = f"role {role.get('name')!r}"
        if not role.get("name"):
            errors.append("a role is missing its name")
        if extra := set(role) - {"name", *ROLE_FIELDS}:
            errors.append(f"{where}: unknown keys {sorted(extra)}")
        check_perms(where, role.get("permissions"))
        if "color" in role:
            check_color(where, role["color"])

    seen_messages = set()

    def check_channel(cat_name, ch):
        where = f"channel {ch.get('name')!r}"
        ctype = ch.setdefault("type", "text")
        if not ch.get("name"):
            errors.append(f"a channel in {cat_name or 'no category'} is missing its name")
        if ctype not in CHANNEL_TYPES:
            errors.append(f"{where}: type must be one of {', '.join(CHANNEL_TYPES)}")
            return
        if extra := set(ch) - {"name", "type", "overwrites", "messages", *CHANNEL_SETTINGS[ctype]}:
            errors.append(f"{where}: keys not supported for {ctype} channels: {sorted(extra)}")
        check_overwrites(where, ch.get("overwrites"))
        if ch.get("messages") and ctype not in ("text", "announcement"):
            errors.append(f"{where}: messages can only be posted in text or announcement channels")
        for msg in ch.get("messages") or []:
            key = (cat_name, ch.get("name"), msg.get("key"))
            if not msg.get("key"):
                errors.append(f"{where}: every message needs a unique 'key'")
            elif key in seen_messages:
                errors.append(f"{where}: duplicate message key {msg['key']!r}")
            seen_messages.add(key)
            if not msg.get("content") and not msg.get("embed"):
                errors.append(f"{where}, message {msg.get('key')!r}: needs content or embed")
            if extra := set(msg.get("embed") or {}) - set(EMBED_FIELDS):
                errors.append(f"{where}, message {msg.get('key')!r}: unknown embed keys {sorted(extra)}")
            if (msg.get("embed") or {}).get("color") is not None:
                check_color(f"{where}, message {msg.get('key')!r}", msg["embed"]["color"])

    for cat in data["categories"]:
        if not cat.get("name"):
            errors.append("a category is missing its name")
        check_overwrites(f"category {cat.get('name')!r}", cat.get("overwrites"))
        for ch in cat.get("channels") or []:
            check_channel(cat.get("name"), ch)
    for ch in data["channels"]:
        check_channel(None, ch)
    if data.get("bots_role") and data["bots_role"] not in {r.get("name") for r in data["roles"]}:
        errors.append(f"bots_role {data['bots_role']!r} must be one of the layout's roles")

    if errors:
        raise LayoutError("\n".join(f"  - {e}" for e in errors))

    for cat in data["categories"]:
        for ch in cat.get("channels") or []:
            if ch.get("overwrites"):
                ch["overwrites"] = merge_overwrites(cat.get("overwrites"), ch["overwrites"])
    if data.get("bold_names"):
        for ch in data["channels"] + [ch for cat in data["categories"] for ch in cat.get("channels") or []]:
            ch["name"] = bold_text(ch["name"])
    return data


def merge_overwrites(base, extra):
    """Layer a channel's overwrites on its category's, permission by permission.

    A channel with its own overwrites stops inheriting from its category, so the
    category's entries are copied in and the channel's entries win where they clash.
    """
    merged = {target: dict(spec or {}) for target, spec in (base or {}).items()}
    for target, spec in extra.items():
        spec = spec or {}
        allow, deny = set(spec.get("allow") or []), set(spec.get("deny") or [])
        current = merged.get(target, {})
        merged[target] = {
            "allow": sorted((set(current.get("allow") or []) - deny) | allow),
            "deny": sorted((set(current.get("deny") or []) - allow) | deny),
        }
    return merged


def bold_text(text):
    """Render ASCII letters and digits in Unicode mathematical bold serif (𝐋𝐢𝐤𝐞 𝐭𝐡𝐢𝐬)."""
    out = []
    for c in text:
        if "A" <= c <= "Z":
            c = chr(0x1D400 + ord(c) - ord("A"))
        elif "a" <= c <= "z":
            c = chr(0x1D41A + ord(c) - ord("a"))
        elif "0" <= c <= "9":
            c = chr(0x1D7CE + ord(c) - ord("0"))
        out.append(c)
    return "".join(out)


def norm_channel_name(name, ctype):
    """Discord lowercases text-like channel names and turns spaces into hyphens."""
    name = name.strip().lower()
    return re.sub(r"\s+", "-", name) if ctype in ("text", "announcement", "forum") else name


def role_kwargs(spec):
    kwargs = {}
    if "color" in spec:
        kwargs["colour"] = parse_color(spec["color"])
    for key in ("hoist", "mentionable"):
        if key in spec:
            kwargs[key] = bool(spec[key])
    if "permissions" in spec:
        kwargs["permissions"] = discord.Permissions(**{p: True for p in spec["permissions"] or []})
    return kwargs


def build_overwrite(spec):
    spec = spec or {}
    overwrite = discord.PermissionOverwrite()
    for name in spec.get("allow") or []:
        setattr(overwrite, name, True)
    for name in spec.get("deny") or []:
        setattr(overwrite, name, False)
    return overwrite


def render_message(spec):
    content = spec.get("content") or None
    embed = None
    if e := spec.get("embed"):
        embed = discord.Embed(
            title=e.get("title"),
            description=(e.get("description") or "").strip() or None,  # Discord strips it anyway
            colour=parse_color(e["color"]) if e.get("color") is not None else None,
        )
        if e.get("footer"):
            embed.set_footer(text=e["footer"])
        if e.get("image"):
            embed.set_image(url=e["image"])
        if e.get("thumbnail"):
            embed.set_thumbnail(url=e["thumbnail"])
    return content, embed


def embed_signature(embed):
    if embed is None:
        return None
    return (embed.title, embed.description, embed.colour.value if embed.colour else None,
            embed.footer.text, embed.image.url, embed.thumbnail.url)


def perm_names(perms):
    # discord.py iterates view_channel under its legacy name read_messages.
    return ["view_channel" if name == "read_messages" else name for name, value in perms if value]


def short(text, limit=60):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- state file

def load_state(guild_id):
    everything = json.loads(STATE_PATH.read_text(encoding="utf-8")) if STATE_PATH.exists() else {}
    state = everything.setdefault(str(guild_id), {})
    for kind in ("roles", "categories", "channels", "messages"):
        state.setdefault(kind, {})
    return everything, state


def save_state(everything):
    STATE_PATH.parent.mkdir(exist_ok=True)
    STATE_PATH.write_text(json.dumps(everything, indent=2), encoding="utf-8")


# ---------------------------------------------------------------- planning

@dataclass
class Action:
    symbol: str  # "+" create, "~" update
    text: str
    run: object  # zero-argument coroutine function


@dataclass
class Plan:
    layout_digest: str
    actions: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    unchanged: int = 0
    untouched: list = field(default_factory=list)

    def add(self, symbol, text, run):
        self.actions.append(Action(symbol, text, run))

    @property
    def hash(self):
        payload = json.dumps([self.layout_digest] + [[a.symbol, a.text] for a in self.actions])
        return hashlib.sha256(payload.encode()).hexdigest()[:10]


class Planner:
    def __init__(self, guild, layout, layout_bytes, state):
        self.guild = guild
        self.me = guild.me
        self.layout = layout
        self.state = state
        self.plan = Plan(hashlib.sha256(layout_bytes).hexdigest())
        self.layout_roles = {r["name"] for r in layout["roles"]}
        # Name/key -> live object. Filled while planning (existing) and applying (newly created).
        self.roles = {}
        self.categories = {}
        self.channels = {}

    async def build(self):
        for spec in self.layout["roles"]:
            self.plan_role(spec)
        for cat in self.layout["categories"]:
            self.plan_category(cat)
            for ch in cat.get("channels") or []:
                await self.plan_channel(cat["name"], ch)
        for ch in self.layout["channels"]:
            await self.plan_channel(None, ch)
        if bots_role := self.layout.get("bots_role"):
            await self.plan_bots_role(bots_role)
        self.note_untouched()
        return self.plan

    def warn(self, text):
        self.plan.warnings.append(text)

    def by_state(self, kind, key, candidates):
        obj_id = self.state[kind].get(key)
        return next((c for c in candidates if c.id == obj_id), None) if obj_id else None

    def check_grantable(self, where, names):
        perms = self.me.guild_permissions
        if perms.administrator:
            return
        if missing := [n for n in names if not getattr(perms, n)]:
            self.warn(f"{where}: the bot doesn't have {', '.join(missing)} itself, so Discord "
                      "will reject this unless you give the bot that permission (or Administrator)")

    # --- roles

    def plan_role(self, spec):
        name = spec["name"]
        role = self.by_state("roles", name, self.guild.roles)
        if role is None:
            found = [r for r in self.guild.roles if r.name == name]
            if len(found) > 1:
                self.warn(f"role '{name}': {len(found)} roles share this name; skipped. "
                          "Rename one so the manager knows which to use.")
                return
            role = found[0] if found else None
        desired = role_kwargs(spec)

        if role is None:
            if "permissions" in desired:
                self.check_grantable(f"role '{name}'", perm_names(desired["permissions"]))
            details = describe_role(desired)
            self.plan.add("+", f"create role '{name}'{details}", partial(self.create_role, name, desired))
            return

        self.roles[name] = role
        self.state["roles"][name] = role.id
        changes = {k: v for k, v in desired.items() if getattr(role, k) != v}
        if not changes:
            self.plan.unchanged += 1
            return
        if role.managed or role >= self.me.top_role:
            self.warn(f"role '{name}' needs changes but is managed by an integration or isn't below "
                      "the bot's highest role, so the bot can't edit it; skipped")
            return
        if "permissions" in changes:
            added = set(perm_names(changes["permissions"])) - set(perm_names(role.permissions))
            self.check_grantable(f"role '{name}'", sorted(added))
        self.plan.add("~", f"update role '{name}': {describe_role_changes(role, changes)}",
                      partial(self.edit, role, changes))

    async def create_role(self, name, kwargs):
        role = await self.guild.create_role(name=name, reason=REASON, **kwargs)
        self.roles[name] = role
        self.state["roles"][name] = role.id

    # --- permission overwrites

    def overwrite_target(self, name):
        if name == "@everyone":
            return self.guild.default_role
        return self.roles.get(name) or discord.utils.get(self.guild.roles, name=name)

    def resolve_overwrites(self, specs):
        return {self.overwrite_target(name): build_overwrite(spec) for name, spec in (specs or {}).items()}

    def valid_overwrite_targets(self, where, specs):
        valid = {}
        for name, spec in (specs or {}).items():
            if name == "@everyone" or name in self.layout_roles or self.overwrite_target(name):
                valid[name] = spec
            else:
                self.warn(f"{where}: overwrite for unknown role '{name}' skipped "
                          "(add the role to the layout or create it first)")
        return valid

    def check_overwrites_grantable(self, where, specs):
        for name, spec in specs.items():
            spec = spec or {}
            self.check_grantable(f"{where}, overwrite for {name}",
                                 (spec.get("allow") or []) + (spec.get("deny") or []))

    def plan_overwrites(self, channel, where, specs):
        """Plan overwrite updates on an existing channel/category. Returns True if anything changes."""
        changed = False
        for name, spec in specs.items():
            desired = build_overwrite(spec)
            target = self.overwrite_target(name)
            current = channel.overwrites.get(target) if target else None
            if current is not None and current.pair() == desired.pair():
                continue
            changed = True
            self.check_overwrites_grantable(where, {name: spec})
            self.plan.add("~", f"set permissions for '{name}' on {where}: {describe_overwrite(spec)}",
                          partial(self.set_overwrite, channel, name, desired))
        return changed

    async def set_overwrite(self, channel, target_name, overwrite):
        await channel.set_permissions(self.overwrite_target(target_name), overwrite=overwrite, reason=REASON)

    # --- categories

    def plan_category(self, spec):
        name = spec["name"]
        where = f"category '{name}'"
        overwrites = self.valid_overwrite_targets(where, spec.get("overwrites"))
        category = self.by_state("categories", name, self.guild.categories)
        if category is None:
            found = [c for c in self.guild.categories if c.name.lower() == name.lower()]
            if len(found) > 1:
                self.warn(f"{where}: {len(found)} categories share this name; using the first one")
            category = found[0] if found else None

        if category is None:
            self.check_overwrites_grantable(where, overwrites)
            details = f" with permissions for {', '.join(overwrites)}" if overwrites else ""
            self.plan.add("+", f"create {where}{details}", partial(self.create_category, name, overwrites))
            return
        self.categories[name] = category
        self.state["categories"][name] = category.id
        if not self.plan_overwrites(category, where, overwrites):
            self.plan.unchanged += 1

    async def create_category(self, name, overwrite_specs):
        kwargs = {"overwrites": self.resolve_overwrites(overwrite_specs)} if overwrite_specs else {}
        category = await self.guild.create_category(name, reason=REASON, **kwargs)
        self.categories[name] = category
        self.state["categories"][name] = category.id

    # --- channels

    async def plan_channel(self, cat_name, spec):
        name, ctype_name = spec["name"], spec["type"]
        ctype = CHANNEL_TYPES[ctype_name]
        key = f"{cat_name or ''}/{name}"
        where = f"{ctype_name} channel '{name}'" + (f" in '{cat_name}'" if cat_name else "")
        overwrites = self.valid_overwrite_targets(where, spec.get("overwrites"))
        settings = {k: spec[k] for k in CHANNEL_SETTINGS[ctype_name] if k in spec}

        candidates = [c for c in self.guild.channels if c.type == ctype]
        channel = self.by_state("channels", key, candidates)
        if channel is None:
            wanted = norm_channel_name(name, ctype_name)
            found = [c for c in candidates if norm_channel_name(c.name, ctype_name) == wanted]
            if len(found) > 1:
                found = [c for c in found if (c.category.name.lower() if c.category else None)
                         == (cat_name.lower() if cat_name else None)]
            if len(found) > 1:
                self.warn(f"{where}: several channels share this name; skipped. "
                          "Rename duplicates so the manager knows which to use.")
                return
            channel = found[0] if found else None

        if channel is None and ctype_name in COMMUNITY_TYPES and "COMMUNITY" not in self.guild.features:
            self.warn(f"{where}: {ctype_name} channels need Community enabled on the server; skipped "
                      "for now. Enable Community and run the plan again to create it.")
            return
        if channel is None:
            self.check_overwrites_grantable(where, overwrites)
            extras = [f"{k}={short(repr(v), 30)}" for k, v in settings.items()]
            if overwrites:
                extras.append(f"permissions for {', '.join(overwrites)}")
            details = f" ({'; '.join(extras)})" if extras else ""
            self.plan.add("+", f"create {where}{details}",
                          partial(self.create_channel, key, cat_name, spec, settings, overwrites))
            await self.plan_messages(key, where, None, spec.get("messages") or [])
            return

        self.channels[key] = channel
        self.state["channels"][key] = channel.id
        changes = {k: v for k, v in settings.items() if getattr(channel, k) != v}
        notes = [f"{k}: {short(repr(getattr(channel, k)), 30)} -> {short(repr(v), 30)}" for k, v in changes.items()]
        current_cat = channel.category.name if channel.category else None
        moving = (current_cat or "").lower() != (cat_name or "").lower()
        if moving:
            notes.append(f"move from '{current_cat or 'no category'}' to '{cat_name or 'no category'}'")
        if notes:
            self.plan.add("~", f"update {where}: {'; '.join(notes)}",
                          partial(self.edit_channel, channel, changes, cat_name if moving else False))
        changed = self.plan_overwrites(channel, where, overwrites) or bool(notes)
        changed = await self.plan_messages(key, where, channel, spec.get("messages") or []) or changed
        if not changed:
            self.plan.unchanged += 1

    async def create_channel(self, key, cat_name, spec, settings, overwrite_specs):
        kwargs = dict(name=spec["name"], category=self.categories.get(cat_name), reason=REASON, **settings)
        if overwrite_specs:
            # Without explicit overwrites a new channel inherits its category's permissions.
            kwargs["overwrites"] = self.resolve_overwrites(overwrite_specs)
        ctype = spec["type"]
        if ctype in ("text", "announcement"):
            channel = await self.guild.create_text_channel(news=ctype == "announcement", **kwargs)
        elif ctype == "voice":
            channel = await self.guild.create_voice_channel(**kwargs)
        elif ctype == "stage":
            channel = await self.guild.create_stage_channel(**kwargs)
        else:
            channel = await self.guild.create_forum(**kwargs)
        self.channels[key] = channel
        self.state["channels"][key] = channel.id

    async def edit_channel(self, channel, changes, move_to):
        kwargs = dict(changes)
        if move_to is not False:
            kwargs["category"] = self.categories.get(move_to) if move_to else None
        await channel.edit(reason=REASON, **kwargs)

    async def edit(self, obj, changes):
        await obj.edit(reason=REASON, **changes)

    # --- bots

    async def plan_bots_role(self, role_name):
        """Give every bot in the server (found through its managed role) the shared bots role."""
        role = self.roles.get(role_name)
        for managed in self.guild.roles:
            if not managed.is_bot_managed():
                continue
            try:
                bot = self.guild.get_member(managed.tags.bot_id) or await self.guild.fetch_member(managed.tags.bot_id)
            except discord.NotFound:
                continue
            if role is not None and role in bot.roles:
                self.plan.unchanged += 1
                continue
            self.plan.add("~", f"give role '{role_name}' to bot '{bot.display_name}'",
                          partial(self.add_role, bot, role_name))

    async def add_role(self, member, role_name):
        await member.add_roles(self.roles[role_name], reason=REASON)

    # --- messages

    async def plan_messages(self, key, where, channel, specs):
        changed = False
        for spec in specs:
            msg_key = f"{key}#{spec['key']}"
            content, embed = render_message(spec)
            preview = short(content or embed.title or embed.description or "")
            existing = await self.find_message(channel, msg_key, content, embed, where) if channel else None
            if existing is None:
                changed = True
                self.plan.add("+", f"post message '{spec['key']}' in {where}: {preview!r}",
                              partial(self.send_message, key, msg_key, content, embed))
                continue
            self.state["messages"][msg_key] = existing.id
            if (existing.content or None) == content and \
                    embed_signature(existing.embeds[0] if existing.embeds else None) == embed_signature(embed):
                continue
            changed = True
            self.plan.add("~", f"edit the bot's message '{spec['key']}' in {where}: {preview!r}",
                          partial(self.edit_message, existing, content, embed))
        return changed

    async def find_message(self, channel, msg_key, content, embed, where):
        """The bot's earlier copy of this message, from state or recent history."""
        try:
            if msg_id := self.state["messages"].get(msg_key):
                try:
                    return await channel.fetch_message(msg_id)
                except discord.NotFound:
                    pass
            async for msg in channel.history(limit=HISTORY_LIMIT):
                if msg.author.id != self.me.id:
                    continue
                if embed and msg.embeds and msg.embeds[0].title == embed.title:
                    return msg
                if not embed and msg.content == content:
                    return msg
        except discord.Forbidden:
            self.warn(f"{where}: the bot can't read message history, so it can't tell whether the "
                      "message was already posted")
        return None

    async def send_message(self, channel_key, msg_key, content, embed):
        message = await self.channels[channel_key].send(content=content, embed=embed)
        self.state["messages"][msg_key] = message.id

    async def edit_message(self, message, content, embed):
        await message.edit(content=content, embed=embed)

    # --- report

    def note_untouched(self):
        managed_ids = {i for kind in ("roles", "categories", "channels") for i in self.state[kind].values()}
        for c in self.guild.channels:
            if c.id not in managed_ids:
                self.plan.untouched.append(f"{'category' if isinstance(c, discord.CategoryChannel) else 'channel'} "
                                           f"'{c.name}'")
        for r in self.guild.roles:
            if r.id not in managed_ids and not r.is_default() and not r.managed:
                self.plan.untouched.append(f"role '{r.name}'")


def describe_role(kwargs):
    parts = []
    if "colour" in kwargs:
        parts.append(f"color #{kwargs['colour'].value:06x}")
    for key in ("hoist", "mentionable"):
        if kwargs.get(key):
            parts.append("shown separately" if key == "hoist" else "mentionable")
    if "permissions" in kwargs:
        names = perm_names(kwargs["permissions"])
        parts.append(f"permissions: {', '.join(names) if names else 'none'}")
    return f" ({'; '.join(parts)})" if parts else ""


def describe_role_changes(role, changes):
    parts = []
    for key, new in changes.items():
        if key == "permissions":
            old_names, new_names = set(perm_names(role.permissions)), set(perm_names(new))
            diff = [f"+{p}" for p in sorted(new_names - old_names)] + [f"-{p}" for p in sorted(old_names - new_names)]
            parts.append(f"permissions {' '.join(diff)}")
        elif key == "colour":
            parts.append(f"color #{role.colour.value:06x} -> #{new.value:06x}")
        else:
            parts.append(f"{key} {getattr(role, key)} -> {new}")
    return "; ".join(parts)


def describe_overwrite(spec):
    spec = spec or {}
    parts = [f"{label} {', '.join(spec[key])}" for key, label in (("allow", "allow"), ("deny", "deny"))
             if spec.get(key)]
    return "; ".join(parts) or "reset to neutral"


def print_plan(guild, layout_path, plan):
    print(f"Plan for '{guild.name}' from {layout_path}\n")
    if plan.actions:
        for action in plan.actions:
            print(f"  {action.symbol} {action.text}")
    else:
        print("  No changes: the server already matches the layout.")
    print(f"\n{sum(a.symbol == '+' for a in plan.actions)} to create, "
          f"{sum(a.symbol == '~' for a in plan.actions)} to update, {plan.unchanged} already match.")
    if plan.untouched:
        print(f"Not in the layout, left alone: {', '.join(plan.untouched)}")
    for warning in plan.warnings:
        print(f"  ! {warning}")
    print("Nothing will be deleted.")
    if plan.actions:
        print(f"\nPlan hash: {plan.hash}")


# ---------------------------------------------------------------- export

def export_overwrites(channel):
    out = {}
    for target, overwrite in channel.overwrites.items():
        if not isinstance(target, discord.Role):
            continue  # member-specific overwrites aren't part of layouts
        allow, deny = overwrite.pair()
        spec = {k: v for k, v in (("allow", perm_names(allow)), ("deny", perm_names(deny))) if v}
        if spec:
            out["@everyone" if target.is_default() else target.name] = spec
    return out


def export_channel(channel):
    ctype = TYPE_NAMES.get(channel.type)
    if ctype is None:
        return None
    spec = {"name": channel.name, "type": ctype}
    for key in CHANNEL_SETTINGS[ctype]:
        value = getattr(channel, key, None)
        if value:
            spec[key] = value
    if not channel.permissions_synced and (overwrites := export_overwrites(channel)):
        spec["overwrites"] = overwrites
    return spec


async def export_layout(guild):
    layout = {"roles": [], "categories": [], "channels": []}
    for role in reversed(guild.roles):
        if role.is_default() or role.managed:
            continue
        spec = {"name": role.name}
        if role.colour.value:
            spec["color"] = f"#{role.colour.value:06x}"
        if role.hoist:
            spec["hoist"] = True
        if role.mentionable:
            spec["mentionable"] = True
        spec["permissions"] = perm_names(role.permissions)
        layout["roles"].append(spec)
    for category, channels in guild.by_category():
        specs = [s for s in map(export_channel, channels) if s]
        if category is None:
            layout["channels"] = specs
            continue
        cat_spec = {"name": category.name}
        if overwrites := export_overwrites(category):
            cat_spec["overwrites"] = overwrites
        cat_spec["channels"] = specs
        layout["categories"].append(cat_spec)
    return layout


# ---------------------------------------------------------------- commands

def cmd_export(args):
    async def task(client, guild):
        layout = await export_layout(guild)
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"# Snapshot of '{guild.name}'\n"
                       + yaml.safe_dump(layout, allow_unicode=True, sort_keys=False), encoding="utf-8")
        print(f"Wrote {out} ({len(layout['roles'])} roles, {len(layout['categories'])} categories)")
    run_with_guild(task)


def cmd_plan_or_apply(args):
    layout_bytes = Path(args.layout).read_bytes()
    try:
        layout = load_layout(args.layout)
    except LayoutError as exc:
        print(f"Layout {args.layout} has problems:\n{exc}", file=sys.stderr)
        sys.exit(1)

    async def task(client, guild):
        everything, state = load_state(guild.id)
        planner = Planner(guild, layout, layout_bytes, state)
        plan = await planner.build()
        print_plan(guild, args.layout, plan)
        if args.command == "plan" or not plan.actions:
            return
        if args.confirm:
            if args.confirm != plan.hash:
                print(f"\nNot applied: the plan changed since you reviewed it (expected {args.confirm}, "
                      f"now {plan.hash}). Review the plan above and confirm again.")
                return
        elif sys.stdin.isatty():
            answer = await asyncio.to_thread(input, f"\nApply these {len(plan.actions)} changes? Type 'yes': ")
            if answer.strip().lower() != "yes":
                print("Not applied.")
                return
        else:
            print(f"\nNot applied. To apply, run again with --confirm {plan.hash}")
            return

        print("\nApplying...")
        done = 0
        try:
            for action in plan.actions:
                await action.run()
                done += 1
                print(f"  done {action.symbol} {action.text}")
        except discord.HTTPException as exc:
            print(f"  FAILED {action.symbol} {action.text}\n    {exc}")
        finally:
            save_state(everything)  # remember what was created so reruns don't duplicate it
        print(f"\n{done}/{len(plan.actions)} changes applied.")

    run_with_guild(task)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="snapshot the server as a layout file")
    export.add_argument("-o", "--output", default="layouts/current.yaml")
    for name in ("plan", "apply"):
        p = sub.add_parser(name, help="preview changes" if name == "plan" else "apply changes after approval")
        p.add_argument("layout")
        if name == "apply":
            p.add_argument("--confirm", metavar="HASH", help="plan hash from the preview you approved")
    args = parser.parse_args()
    cli_main(lambda: cmd_export(args) if args.command == "export" else cmd_plan_or_apply(args))


if __name__ == "__main__":
    main()
