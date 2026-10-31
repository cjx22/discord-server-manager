# Server Manager

Manages a Discord server from YAML layout files. It always shows a preview first and never deletes anything.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # then fill in DISCORD_BOT_TOKEN and DISCORD_GUILD_ID
```

`.env` is in `.gitignore`. Never commit it, and never paste the token anywhere else.

## Commands

| Command | What it does |
|---|---|
| `.\.venv\Scripts\python test_connection.py` | Read-only: lists the server's channels and roles and checks the bot's permissions |
| `.\.venv\Scripts\python manage.py export` | Read-only: saves the current server to `layouts/current.yaml` (useful as a backup and a starting point) |
| `.\.venv\Scripts\python manage.py plan layouts/x.yaml` | Read-only: previews what would be created or updated and prints a plan hash |
| `.\.venv\Scripts\python manage.py apply layouts/x.yaml --confirm <hash>` | Applies the plan, but only if it still matches the hash you reviewed |

## How it stays safe and repeatable

- **No deletes.** Channels, roles, messages and overwrites that aren't in the layout are listed as "left alone".
- **No duplicates.** Existing items are matched by name, and IDs of items the tool created or adopted are saved in `data/state.json`. If two items share a name, the tool skips them and warns you instead of guessing.
- **Approval is tied to the preview.** `apply` recomputes the plan and refuses to run if it differs from the hash you approved.
- **Only listed fields are managed.** If a role in the layout has no `color`, its color is never touched.
- **Welcome messages** each have a `key`. Reruns edit the bot's existing message instead of posting a new one.

## Recreating a layout from screenshots

Put screenshots of the channel list, the role list and any channel settings in `layouts/references/`, then ask Claude to recreate the layout. Claude reads them, writes a `layouts/<name>.yaml`, and runs `plan` so you can review it before anything is applied.

## Limitations

- New roles are created at the bottom of the role list. Drag them into order in Discord.
- The bot can only edit roles below its own highest role, and (unless it has Administrator) it can only grant permissions it has itself. The preview warns about both.
- Announcement, stage and forum channels require Community to be enabled on the server.
