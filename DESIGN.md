# Hall-Monitor — Design

## 1. Process model

Hall-Monitor is a single Python process. Inside it, two asyncio tasks run concurrently:

- The **discord.py bot** (`commands.Bot`, `~` prefix).
- The **FastAPI sidecar** (`uvicorn.Server.serve()` scheduled as a task).

Both share the same Tortoise ORM connection to `hall-monitor.db` under `DATA_DIR`. Running them in one process keeps the DB single-writer and lets sidecar routes call bot methods directly.

Entry point: [`src/hall_monitor/__main__.py`](src/hall_monitor/__main__.py).

## 2. End-to-end join flow

1. A representative visits `hall.wynnvets.org/join`, enters their Minecraft username.
2. Hallway's JS calls `GET /api/join/lookup?username=X`. Hall-Monitor resolves the UUID (Mojang, PlayerDB fallback), asks Wynncraft's API whether they're a chief/owner of a notable guild, and returns `{eligible, guild_tag, current_contacts_per_role}` for the UI to render.
3. The user ticks the contact roles they want. The UI updates a live "type `hall request N` on verify.wynnvets.org" hint, where `N` is a bit-field over the roles (see §5).
4. The user joins `verify.wynnvets.org` in Minecraft and types `hall request 14`.
5. Picolimbo forwards `GET /api/verify/{uuid}/hall request 14` to Hall-Monitor.
6. Hall-Monitor parses the subcommand, re-runs the Wynncraft eligibility check (authoritative), mints a single-use Discord invite, and returns `{"kick_message": "<welcome text with discord.gg URL>"}`.
7. Picolimbo disconnects the player with that message. The player clicks the invite in their MC client's kick screen.
8. On `on_member_join`, Hall-Monitor resolves which invite was used, applies the encoded contact roles (kicking prior conflicting holders — see §6), promotes the `PendingInvite` to a `Delegate`, sets the nickname, and ensures the guild's aesthetic role exists.

## 3. PendingInvite lifecycle invariants

Enforced in `services/discord_invites.py`:

- **One `PendingInvite` per MC UUID.** Re-requesting revokes the previous Discord invite and mints a fresh one.
- **Zero `PendingInvite`s if the UUID already has a `Delegate` row for a member still present in the server.** MC-time verify returns a "you're already in" kick message.
- **Expiry sweep** (`scheduler.py`, every `PENDING_INVITE_SWEEP_SECONDS`) deletes rows older than `PENDING_INVITE_TTL_MINUTES` (default 45) and revokes the associated Discord invite. Belt-and-braces against the bot going down mid-flow.
- **On successful join**, the row is deleted synchronously as part of the promotion to `Delegate`.
- **On failed role application**, the row stays until the sweep collects it — safer than a `Delegate` without roles.

## 4. Notability

`services/notability.py` aggregates five independent signals against a guild tag and stores the result in `notability_cache`. Signals:

1. Top-25 average online (last 5 days) on `api.wynnpool.com/leaderboard/guild-average-online`.
2. Level 100+ on `api.wynnpool.com/leaderboard/guildLevel`.
3. Season placements (top 3 in last 10 seasons, or top 10 in last 5, or top-25 average across last 5).
4. Average territory ownership > 20 across the last 5 days, provided the current season hasn't ended (`api.wynncraft.com/v3/guild/seasons`).
5. A janitor/monitor-issued force override (`force_override` table) with an expiry.

The scheduler refreshes every `NOTABILITY_REFRESH_SECONDS` (default 3600). Transitions trigger delegate ↔ relegate role swaps.

## 5. Role-bit encoding

`services/role_bits.py` owns the single source of truth: `ROLE_BITS: dict[int, str]`. Bit 0 = Events, Bit 1 = Housing, Bit 2 = Warring, Bit 3 = Ownership. Bits 4+ are reserved.

Adding a role is a one-line map addition; old codes stay valid. Codes carrying an unknown bit are rejected with a clear kick message.

## 6. Contacts

`services/contacts.py` enforces per-role uniqueness within a guild. Assigning a new contact displaces the old one; a delegate who ends up with zero contact roles is kicked from the server. The UI's "conflict warning" on `/join` reads directly from `guild_contact`.

## 7. Cog organisation

`discord_bot/cogs/` is grouped by domain (`general/`, `moderation/`, `force/`, `admin/`, `listeners/`). The auto-loader in `discord_bot/__init__.py` walks the tree and calls `setup()` on any file or package that exposes one. Sub-commands with distinct permissions live in per-file modules inside `cogs/force/`; the group cog imports and registers them at setup time.

Ad-hoc admin scripts live at `cogs/admin/scripts/` — drop a file with `async def main(ctx, *args)` and it's callable as `~script <name>`.

## 8. External API clients

`external/` holds one client per third-party API. Guarantees:

- **Mojang** is preferred; **PlayerDB** is the fallback when Mojang ratelimits.
- **Wynncraft** reads `WYNNCRAFT_API_TOKEN` and sends it as a bearer header when set. Unset works too (shared anon ratelimit).
- **Wynnpool** is unauthenticated.

## 9. Portability

To move off Wynncraft Veterans infrastructure:

- Change the picolimbo forwarding endpoint (currently `verify.wynnvets.org`) to point at a new limbo server; the chat-forward patch contract is `GET /api/verify/{uuid}/{msg}` returning `{"kick_message": str | null}`.
- Change any mentions of `hall.wynnvets.org` to the new domain.
