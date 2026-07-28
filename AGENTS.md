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
- `src/hall_monitor/db/models.py` — Tortoise models. Model edits require `aerich migrate --name <slug>`, committed with the change; `db.migrate()` applies them at boot. Migrations are package *data*, not a package — see DESIGN.md §10 before touching how they're shipped.
- `src/hall_monitor/discord_bot/cogs/admin/scripts/` — ad-hoc scripts callable via `~script <name>`.

## Required Discord permissions

The verify flow mints single-use invites and hands out roles when they're redeemed, so the bot's role in the guild needs:

- **Create Instant Invite** (on the welcome channel) — every MC-time `HALL<NN>` code mints one.
- **Manage Server** — to revoke the prior outstanding invite when a UUID re-requests before TTL, for the sweep job, and to read the guild's invite list. That last one is what tells `on_member_join` which invite a member came through; Manage Channel on the welcome channel alone is *not* enough for it (see DESIGN.md §3.1).
- **Manage Roles** — to apply the delegate and contact roles on join, to strip a contact role from whoever a new verification displaces, and to create/recolour the per-guild aesthetic role (see DESIGN.md §11).
- **Kick Members** — a displaced contact left holding no contact roles is removed from the server (see DESIGN.md §6).
- **Manage Nicknames** — every representative's nickname carries their guild tag (see DESIGN.md §13). Note that *nobody* can rename a server owner, Administrator included.
- **Manage Messages** — on the roster channel, because the Current Guilds roster is the bot's alone and a sync deletes anything in it the bot doesn't own (DESIGN.md §14); and anywhere `~echo`, `~embed` or `~expel_motion` might be typed, since all three delete the invoking message (§18.2, §16.4).
- **Manage Expressions** — the roster's entries lead with each guild's Minecraft banner as a custom emote (see DESIGN.md §15). Banners fill whatever slots the server isn't otherwise using, so the count follows the boost level; only emotes the bot itself created are ever deleted.
- **Mention @everyone** (on the delegate channel) — the Hall is called to an expel motion with a single `@here`, once three guilds have already voted to expel, and that is the *only* ping this bot ever sends (see DESIGN.md §16.4). Without the permission it renders as plain text and nobody is notified. **Manage Messages** is also wanted anywhere `~expel_motion` might be typed: the command is DM-only because the mover is anonymous, and a public invocation is deleted rather than left naming them.

Administrator covers all eight (and overrides channel overwrites), which is how the bot is set up in production — it holds the monitor role. What Administrator does **not** waive is role position: only the guild owner can assign a role above their own highest one, and the same rule decides who the bot may kick. The bot's role has to sit above Guild Hall Delegate and the four contact roles, or `add_roles` comes back 403 and the join lands in the failed-role-application path.

## Test guilds

Use **`WYNN`** for anything that needs a live guild to poke at. It's a
real but tiny admin guild, so every API call resolves the way it would in
production while no real signal marks it major — which makes it the
honest subject for `~force major`, since you can watch a guild flip
false → true → false. An invented tag like `ZZZZ` 404s at the per-guild
lookup and never exercises that path at all.

`VETS` is the stock example for a guild that *is* major.

## Poking the clients on a running container

Each `external/` module holds one long-lived `httpx.AsyncClient` with a
connection pool, which is right for a process that runs a single event
loop for its lifetime. It does mean an ad-hoc script has to use **one**
`asyncio.run` for everything — two calls into the same client from two
loops picks a pooled connection off the first loop and dies with
`RuntimeError: Event loop is closed`, several frames deep in httpcore
and looking nothing like the actual cause.

```bash
docker exec hall-monitor python -c "
import asyncio
from hall_monitor.external import mojang, wynncraft

async def main():
    print(await mojang.username_to_uuid('Notch'))
    print((await wynncraft.get_guild_by_prefix('VETS')).level)
    print(len(await wynncraft.get_seasons()))

asyncio.run(main())
"
```

## Don't

- Don't register slash commands — the bot is deliberately prefix-only.
- Don't add features when a stub bug fix is requested — the codebase is early skeleton and easy to over-elaborate.
- Don't hard-code Discord role IDs — every ID lives in `config.py` and comes from env.
