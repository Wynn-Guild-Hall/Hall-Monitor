# Hall-Monitor

Discord bot for the Wynncraft Guild Hall. Full architecture reference is in [DESIGN.md](DESIGN.md); read it before non-trivial changes.

## Fast facts

- **Runtime:** Python 3.12, discord.py + FastAPI sharing one asyncio event loop and one Tortoise ORM connection.
- **Command prefix:** `~` (no slash commands).
- **Persistence:** SQLite via Tortoise ORM + Aerich migrations, at `${DATA_DIR}/hall-monitor.db`.
- **HTTP sidecar:** listens on `HALL_MONITOR_PORT` for two integrations — picolimbo (`/api/verify/{uuid}/{msg}`) and the Hallway website (`/api/join/lookup`).
- **Deployed** as its own stack in the wynnvets [vets-deploy](../vets-deploy) repo on the `verify` + `hall-internal` networks. Never publicly exposed via Traefik.

## Layout landmarks

- `src/hall_monitor/discord_bot/cogs/<domain>/<cmd>.py` — one file per command. Force sub-commands are split further because permissions differ per sub-command.
- `src/hall_monitor/services/` — framework-agnostic business logic. Importable from cogs OR routes.
- `src/hall_monitor/external/` — one HTTP client per third-party API.
- `src/hall_monitor/sidecar/routes/` — FastAPI routes.
- `src/hall_monitor/db/models.py` — Tortoise models. Model edits require `aerich migrate -n <slug>`.
- `src/hall_monitor/discord_bot/cogs/admin/scripts/` — ad-hoc scripts callable via `~script <name>`.

## Required Discord permissions

The verify flow mints single-use invites and hands out roles when they're redeemed, so the bot's role in the guild needs:

- **Create Instant Invite** (on the welcome channel) — every MC-time `hall request <N>` mints one.
- **Manage Server** — to revoke the prior outstanding invite when a UUID re-requests before TTL, for the sweep job, and to read the guild's invite list. That last one is what tells `on_member_join` which invite a member came through; Manage Channel on the welcome channel alone is *not* enough for it (see DESIGN.md §3.1).
- **Manage Roles** — to apply the delegate and contact roles on join. The bot's own role must also sit **above** every role it hands out, or Discord rejects the assignment and the join goes unverified.

## Don't

- Don't register slash commands — the bot is deliberately prefix-only.
- Don't add features when a stub bug fix is requested — the codebase is early skeleton and easy to over-elaborate.
- Don't hard-code Discord role IDs — every ID lives in `config.py` and comes from env.
