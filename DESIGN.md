# Hall-Monitor — Design

## 1. Process model

Hall-Monitor is a single Python process. Inside it, two asyncio tasks run concurrently:

- The **discord.py bot** (`commands.Bot`, `~` prefix).
- The **FastAPI sidecar** (`uvicorn.Server.serve()` scheduled as a task).

Both share the same Tortoise ORM connection to `hall-monitor.db` under `DATA_DIR`. Running them in one process keeps the DB single-writer and lets sidecar routes call bot methods directly.

Entry point: [`src/hall_monitor/__main__.py`](src/hall_monitor/__main__.py).

## 2. End-to-end join flow

**Status:** implemented end to end (Stages 3–8). The nickname in step 8 lands in Stage 10.

1. A representative visits `hall.wynnvets.org/join`, enters their Minecraft username.
2. Hallway's JS calls `GET /api/join/lookup?username=X`. Hall-Monitor resolves the UUID (Mojang, PlayerDB fallback), asks Wynncraft's API whether they're a chief/owner of a notable guild, and returns `{eligible, guild_tag, mc_username, current_contacts_per_role}` for the UI to render. On failure the response carries a `reason` field (`"not chief or owner"` / `"guild not notable"`); unknown username → HTTP 404.
3. The user ticks the contact roles they want. The UI updates a live "type `HALL14` on verify.wynnvets.org" hint, where the digits are a bit-field over the roles (see §5).
4. The user joins `verify.wynnvets.org` in Minecraft and types `HALL14`.
5. Picolimbo matches the `hall` route prefix, strips it, and forwards `GET /api/verify/{uuid}/14` to Hall-Monitor over the `verify` Docker network. That route is deliberately not web-reachable — Hallway's nginx proxies only `/api/join/` — because its sole proof that the requester owns the Minecraft account is that they connected to the limbo server as it.
6. Hall-Monitor parses the code, re-runs the Wynncraft eligibility check (authoritative), mints a single-use Discord invite, and returns it as a `kick_message`. Every *rejection* comes back as a `chat_message` instead, leaving the player connected — being disconnected for a mistyped code means reconnecting just to retry. Success is the one case that disconnects, because the invite has to be readable after the session ends, and nothing on a Minecraft disconnect screen is clickable: the message leads with the bare invite code to type into Discord's join dialog, with the `discord.gg` URL after it for anyone who'd rather paste a link. Picolimbo renders kick reasons as MiniMessage (auth-stack patch `0005`), so the code is coloured apart from the instructions around it — only the vanilla colour names and formatting tags parse, and a test pins the message to that set.
7. Picolimbo disconnects the player with that message. The player clicks the invite in their MC client's kick screen.
8. On `on_member_join`, Hall-Monitor resolves which invite was used (see §3.1), ensures the guild's aesthetic role exists (§11), applies it alongside the delegate role and the encoded contact roles (kicking prior conflicting holders — see §6), promotes the `PendingInvite` to a `Delegate`, and sets the nickname.

## 3. PendingInvite lifecycle invariants

**Status:** implemented (Stage 6)

Enforced in `services/discord_invites.py`:

- **One `PendingInvite` per MC UUID.** Re-requesting revokes the previous Discord invite and mints a fresh one.
- **Zero `PendingInvite`s if the UUID already has a `Delegate` row for a member still present in the server.** `mint_invite` raises `AlreadyLiveDelegate`; MC-time verify translates that to a "you're already in" kick message.
- **Expiry sweep** (`scheduler.py`, every `PENDING_INVITE_SWEEP_SECONDS`) deletes rows older than `PENDING_INVITE_TTL_MINUTES` (default 45) and revokes the associated Discord invite. Belt-and-braces against the bot going down mid-flow. The scheduler binds the bot into `sweep_expired` via `functools.partial` so `discord_invites` stays a plain service module.
- **On successful join**, the row is deleted synchronously as part of the promotion to `Delegate`.
- **On failed role application**, the row stays until the sweep collects it — safer than a `Delegate` without roles. Nothing is registered either: a `Delegate` row would make the next `mint_invite` refuse the retry with "you're already verified".

### 3.1 Working out which invite was used

Discord reports that someone joined, never how. `services/discord_invites.py` keeps a `{code: uses}` snapshot of the guild's invite list — seeded on `on_ready` (and every reconnect; re-seeding is idempotent), extended by `note_minted` on every mint, and pruned by `revoke_invite`. `on_member_join` re-reads the list and diffs:

- **uses went up** — direct evidence, but only for invites Discord still lists.
- **the code vanished** — what actually happens to ours, since a `max_uses=1` invite is deleted on consumption. Expiry is indistinguishable from here, so candidates are filtered to `PendingInvite` rows younger than the invite's own `max_age`: an invite that has already expired can't be the one just consumed.

Candidates are intersected with our own `PendingInvite` rows, so a join through anyone else's invite resolves to nothing rather than to a wrong row. The vanity URL is excluded from the snapshot outright — its use count only climbs, so it would read as consumed on every join. **More than one match is treated as no match**: a mis-attribution would bind a stranger's Discord account to someone else's Minecraft UUID, which is far worse than asking the representative to type their code again.

Two constraints shape this design, both verified against discord.py 2.6.4 and Discord's API docs rather than assumed:

- **Handlers are concurrent.** `dispatch` schedules one task per listener, so two near-simultaneous joins interleave at every `await`. The snapshot read → fetch → diff → write is one critical section under an `asyncio.Lock`, with the fetch *inside* the lock.
- **`INVITE_DELETE` is not a usable signal.** Discord doesn't document it firing on max-uses exhaustion, time-expired invites are known not to fire it, and nothing orders it against `GUILD_MEMBER_ADD` anyway. The diff is the only mechanism.

Reading the invite list requires **Manage Server**. With only View Audit Log, Discord withholds invite metadata and every `uses` comes back `None` — codes are still listed, so vanish-detection keeps working while use-counting silently doesn't. Fetching per join is a known ratelimit hotspot on large guilds; at Guild Hall's join rate (a handful ever) it's not a concern.

## 4. Notability

**Status:** implemented (Stage 2)

`services/notability.py` aggregates six independent signals against a guild tag and stores the result in `notability_cache`. Signals:

1. Top-25 average online (last 5 days) on `api.wynnpool.com/leaderboard/guild-average-online`.
2. Level 100+ on `api.wynnpool.com/leaderboard/guildLevel`.
3. Season placements — top 3 in any of the last 10 seasons, top 10 in any of the last 5, or mean rank across the last 5 ≤ 25. Wynnpool's season-rating payload identifies guilds by name only (no prefix field), so this signal matches on guild name, case-insensitively — the tag is still checked first should the shape ever gain one.
4. Territory ownership > 20 while a Wynncraft season is currently running (`api.wynncraft.com/v3/guild/seasons`). Implemented as a current snapshot; a full 5-day average will need historical polling we don't yet do.
5. War count > 50 000 on the Wynncraft guild payload.
6. A janitor/monitor-issued force override (`force_override` table with `kind="notable"`) with no expiry or an expiry in the future.

**All six signals come from bulk leaderboards.** A sweep costs a fixed ~20 requests no matter how many guilds it evaluates, and makes no per-guild call at all.

Territory ownership and war count get this from a property of top-N boards: Wynnpool publishes `guildWars` and `guildTerritories` capped at 100 rows, and while a board's *floor* sits below our threshold, a guild missing from it must be under that threshold — otherwise it would have displaced the bottom entry. At the time of writing the wars board reaches down to ~4 100 (threshold 50 000) and territories to 0 (threshold 20), so both are comfortably decisive. `_board_decides` re-checks it every sweep and logs a warning if a floor ever climbs past its threshold, at which point the per-guild `external.guild_stats` fallback takes over for guilds off the board.

Candidates are drawn from **every** board Wynnpool publishes — the four signal boards plus `guildTotalRaids` and the five raid boards. Those six answer no signal, but a guild ranked for raids may well qualify on level or wars, and it can't if we never evaluate it. That takes the candidate set from 113 guilds to ~279. A candidate board that fails is logged and skipped: it costs coverage, not correctness. That distinction matters when reading the cache back: a war count of `null` across every notable guild means nobody was asked, not that nobody qualifies. `refresh_all(exhaustive=True)` — `~script refresh_notability full` — evaluates every signal regardless, at the cost of a per-guild request for every candidate. It's pointless for deciding notability and necessary for deciding thresholds.

`is_notable(tag)` reads from the cache; on a miss it falls back to an inline single-guild evaluation that hits every relevant API and populates the cache row. `refresh_all()` collects candidate tags from every Wynnpool leaderboard, every `Delegate` row, and every `ForceOverride(kind="notable")`, then re-evaluates them all. The scheduler runs it every `NOTABILITY_REFRESH_SECONDS` (default 3600).

`~force notable <tag> <time>` writes a `ForceOverride` row. Janitors are capped at three months — long enough to carry a guild through a quiet patch, short enough that nobody parks a guild in the Hall indefinitely. There's no floor. Monitors have no ceiling and can pass `0` for a permanent override (`services/time_parse.py` owns the parsing). `~unforce notable <tag>` deletes the row.

Transitions (delegate ↔ relegate role swaps on notability change) land in Stage 9.

### Guild tags

`services/guild_tag.py` is the one place that decides whether two tags mean the same guild. Tags are normally three or four characters and case-agnostic, but Wynncraft permits two rarities worth not being surprised by: case *can* in principle be significant, and spaces and underscores are legal characters.

So Wynncraft's spelling is what gets stored and displayed — uppercasing on the way in would discard information we were handed — and only comparison folds case. DB lookups on a tag use `__iexact` for the same reason. This matters most where a human types the tag: `~force notable vets` has to reach the guild the sweep cached as `VETS`, or the override silently applies to nothing. A tag containing a space needs quoting at the call site (`~force notable "My Guild" 3mo`), which discord.py's argument parser handles.

## 5. The code

A representative types `HALL<NN>` on the verify server: a fixed `HALL` marker plus the role-bits integer, zero-padded to two digits. That's six characters — deliberately the same width as a dazebot account-link code, because the verify server prompts for "your code" and shouldn't need to explain which kind.

The two code spaces cannot collide. dazebot draws its codes from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (visually-confusable characters removed), which contains no `L`, so nothing it issues can begin with `HALL`. `tests/unit/test_mc_command.py` pins that property.

Picolimbo routes on the prefix `hall` — no trailing space, since the code is a single token — and strips it, so `services/mc_command.py` normally receives bare digits. It accepts the marker as well, which keeps a hand-run `curl` identical to what the player typed. That prefix is broad enough to catch ordinary chat starting with those letters, so a line with no digit in it is dropped silently instead of disconnecting someone mid-conversation.

`services/role_bits.py` owns the single source of truth for the digits: `ROLE_BITS: dict[int, str]`. Bit 0 = Events, Bit 1 = Housing, Bit 2 = Warring, Bit 3 = Ownership. Bits 4+ are reserved.

Adding a role is a one-line map addition; old codes stay valid. Codes carrying an unknown bit are rejected with a clear kick message. Hallway builds the same string in JS for its live display — `mc_command.format_code` and `static/js/request_code.js` have to move together.

## 6. Contacts

**Status:** implemented (Stage 7)

`services/contacts.py` enforces per-role uniqueness within a guild. Assigning a new contact displaces the old one; a delegate who ends up with zero contact roles is kicked from the server, because the Hall is a room of guild representatives and a delegate representing nothing has no seat at the table. The UI's "conflict warning" on `/join` reads directly from `guild_contact` — the `current_contacts_for_guild(tag)` helper drops rows whose delegate has left the Discord server (cross-checked against `bot.get_guild(...).get_member(...)` when the bot handle is available).

Two things reach the assign path: `on_member_join` claims every slot a verification code asked for, and `~force assign <user> <contactType>` claims one by hand. The command scopes by tier — a janitor or monitor assigns within *the target's* guild, an ownership contact only within their own — and either way the target must already have a `Delegate` row, since the slot is a foreign key onto it. `~unforce assign` vacates a slot, with the same kick-if-last consequence.

Guild tags are matched case-insensitively (`services/guild_tag.py`) so `~force assign` hits the row the join flow wrote, and the slot's *stored* spelling is left alone: two rows differing only in case would break an invariant the `unique_together` can't see. Discord-side failures (role removal, kick) are logged rather than raised — the database is the roster's source of truth, and a stale role on a displaced holder is visible and fixable in a way a slot that silently refused to move is not.

**Contact roles are gated on notability** (`sync_contact_roles`, driven by §12's reconcile). The contact channels are for representatives of guilds currently in the Hall, so a guild that drops out has its four contact roles withdrawn and gets them back — to the same people — when it returns. The `GuildContact` rows never move: notability decides who may *use* a contact role, the row decides who holds the slot. Clearing the rows would cost a returning guild four re-verifications and lose the record of who to hand the roles back to. Nobody is kicked for it either, which is the same distinction — the kick is what happens when you lose your slot to someone else, not when your guild has a quiet quarter.

## 7. Cog organisation

`discord_bot/cogs/` is grouped by domain (`general/`, `moderation/`, `force/`, `admin/`, `listeners/`). The auto-loader in `discord_bot/__init__.py` walks the tree and calls `setup()` on any file or package that exposes one. Sub-commands with distinct permissions live in per-file modules inside `cogs/force/`; the group cog imports and registers them at setup time.

Ad-hoc admin scripts live at `cogs/admin/scripts/` — drop a file with `async def main(ctx, *args)` and it's callable as `~script <name>`.

**A command that isn't built yet is registered `hidden=True`, and implementing it means dropping that flag.** `~help` and the bare-group listings both build from the live command tree via `discord_bot/command_help.py`, filtered through each command's own checks (`can_run`), so they show exactly what the caller can run and nothing else — `hidden` is the one marker keeping unfinished work out of both. A command's first docstring line is its description in those listings.

**The permission tiers nest** (`discord_bot/permissions.py`): a janitor can do anything a contact can, a monitor anything a janitor can, so every gate admits the staff roles above it — `is_monitor` excepted, being the top. Gates read their IDs from `Settings` per call, not at import.

`discord_bot/errors.py` owns a single `on_command_error`. Without it discord.py logs a traceback and tells the invoker nothing, so a stub reads in Discord as the bot being broken rather than as a feature that isn't built. It maps: anything `hidden` → "isn't built yet", which outranks the rest because a check runs *before* the body and an unbuilt gated command would otherwise report a missing role; a `NotImplementedError` from a stub nobody marked hidden → the same; a failed role check → "you don't have the role for that one"; a bad or missing argument → the command's usage line; an unknown command → silence, since `~` opens plenty of ordinary sentences. Anything else is logged and owned up to. Note the asymmetry this covers: a stub behind a *command* misbehaves only when someone types it, but a stub on a **group callback** fires the moment someone types the group name alone — `~force` did exactly that until Stage 7.

## 8. External API clients

**Status:** implemented (Stage 1)

`external/` holds one client per third-party API. Guarantees:

- **UUIDs are canonically dashed.** Minecraft, and so picolimbo and `Delegate.mc_uuid`, use the 8-4-4-4-12 form; Mojang and PlayerDB return 32 bare hex characters. `external/_uuid.py` normalises on the way in, and `wynncraft.get_player_guild` dashes on the way out — that route 404s on the bare form, and a 404 there reads back as "no guild", which is indistinguishable from a player having none.
- **Mojang** is preferred; **PlayerDB** is the fallback when Mojang ratelimits (429) or the connection fails. A Mojang 404 is authoritative and does *not* trigger the fallback. The orchestrator is `external.resolve_username_to_uuid`.
- **Wynncraft** reads `WYNNCRAFT_API_TOKEN` and sends it as a bearer header when set. Unset works too (shared anon ratelimit). Wynncraft splits its rate limit across multiple buckets; `external/wynncraft.py` serialises `/v3/player/*` and `/v3/guild/*` on separate bucket queues so a bulk guild sweep can't starve a player lookup.
- **Wynnpool** is unauthenticated, and its rate limit is far more forgiving than Wynncraft's. Its guild endpoint mirrors the same territory and war numbers, so `external.guild_stats(name, tag)` asks it first and falls through to Wynncraft — the authority — on any failure, *including* a 404, since Wynnpool only knows guilds it has indexed. Wynncraft errors propagate rather than being swallowed: a 429 recorded as "no territories, no wars" would read as a guild silently losing its notability. Wynnpool addresses guilds by name only, so a tag with no known name goes straight to Wynncraft's prefix route.

Every request carries a `User-Agent` naming the bot and linking the repo (`hall-monitor/<version> (+…)`). None of these APIs require it; identifying yourself is the custom on the community ones, and it lets their operators tell our traffic from an anonymous script's. A per-client header of the same name still wins.

Every client funnels through `external/_client.py`, which owns the shared retry/timeout/bucket-queue policy: 10 s timeout, one 500 ms retry on 5xx, 429 pauses the bucket for the `Retry-After`/`RateLimit-Reset` window and re-raises so callers can fall back. Requests are serialised per bucket with a priority queue; user-facing lookups pass `urgent=True` to jump ahead of background work. Responses come back as frozen dataclasses (`wynncraft.Guild`, `wynnpool.GuildDetails`, `LeaderboardEntry`, …) so no downstream code should be reaching into raw JSON.

## 9. Portability

To move off Wynncraft Veterans infrastructure:

- Change the picolimbo forwarding endpoint (currently `verify.wynnvets.org`) to point at a new limbo server; the chat-forward patch contract is `GET /api/verify/{uuid}/{msg}` returning `{"kick_message": str | null}`.
- Change any mentions of `hall.wynnvets.org` to the new domain.

## 10. Schema migrations

**Status:** implemented (Stage 7 follow-up)

Model edits ship with an Aerich migration: `aerich migrate --name <slug>`, committed alongside the `db/models.py` change. `db.migrate()` applies whatever is outstanding at boot — one process, one SQLite file, so there's no window for two instances to race, and a deploy needing a remembered manual step eventually gets one nobody remembered.

Migrations live at `db/migrations/models/` and reach the image as **package data** (`[tool.setuptools.package-data]` in `pyproject.toml`), not as a package. Aerich discovers version files by walking the directory, so they get no `__init__.py`; without the package-data entry `pip install .` would leave them out of the wheel and the container would boot with nothing to apply and nothing to say about it.

### The pre-migration database

Stages 1–7 ran on `Tortoise.generate_schemas(safe=True)`, which creates missing *tables* and stops. A column added to an existing model stayed absent until something SELECTed it — which is how `mc_username` would have taken the bot down on deploy. Aerich was half set up: `aerich init` had run during the scaffold and `init-db` never had, so production ended up with every table, an **empty `aerich` bookkeeping table** (`aerich.models` is in the app's model list, so `generate_schemas` had been creating it all along), and no record of a migration ever being applied.

`migrate()` recognises that shape — our tables present, nothing recorded — and records the initial migration as applied *without running it*, since the tables it would create already exist. Two guards, because a wrong baseline is quiet and durable:

- **More than one migration exists** → refuse. Faking the lot would skip real schema changes.
- **The live schema doesn't match the models** → refuse, naming the missing columns. Marking a drifted schema as migrated buries the difference until some later migration trips over it.

Both raise at boot rather than degrading, and neither can fire after the first successful baseline. The emptiness of the `aerich` table is the signal, not its absence — an easy thing to get backwards.

## 11. The guild aesthetic role

**Status:** implemented (Stage 8)

Every delegate wears a role named for their guild tag, coloured with that guild's own hue, so the member list says at a glance which guilds are in the room and `@VETS` reaches their representatives. `services/guild_roles.py` creates it on demand: `ensure_guild_role` finds the role by name (case-insensitively, per §4's guild-tag rules, so a role somebody made by hand as `Vets` is adopted rather than duplicated), creates it if missing, and otherwise edits only what actually differs. A colour that still matches leaves no edit, no request, and no hourly audit-log entry.

The colour comes from Athena — the same guild cache Wynntils renders from — via `services/athena_colour.py`. Athena's raw hue is often unusable in Discord: near-black disappears on the dark theme and near-white on the light one, and a desaturated hue reads as grey mush on both. `to_discord_visible` clamps HLS lightness into `[0.40, 0.70]` and floors saturation at `0.55`, leaving hue alone — a dark red comes out a lighter red, never a different colour. An achromatic input keeps its neutrality rather than being assigned an arbitrary tint.

The guild list is one request for every guild Athena knows, so it's memoised for an hour, and a failed refresh serves the stale copy: an hour-old colour beats every guild role reverting to blurple because Athena had a bad minute. When there's nothing cached to serve, `colour_for` falls back to blurple (`DEFAULT_COLOUR`) — which is itself a fixed point of the transform, so the fallback isn't a colour we'd have rejected from Athena.

The role rides along in the join's single `add_roles` call rather than costing a second one, and it's the one part of that call allowed to be missing: **a role that can't be created doesn't stop the verification.** The delegate and contact roles *are* the verification; the guild colour is decoration, and `~script guild_role <TAG>` re-runs the whole path by hand — Athena lookup, contrast clamp, create-or-edit — for any tag. Every Discord-side failure here is logged rather than raised, matching §6.

New roles land at the bottom of the hierarchy, which is where `create_role` puts them and where an aesthetic role belongs: it grants no permissions, and a role above the bot's own would be one the bot couldn't edit afterwards.

**The role is looked up by ID first** (`GuildRole.discord_role_id`), by name second. The name belongs to whoever decorated it — `✦ VETS`, a prefix, eventually a role icon — and `ensure_guild_role` never writes it back. Matching on name alone would quietly mint a second role beside a renamed one. Note that a *custom emote* can't live in a role name at all: Discord renders `<:VETS:…>` there as literal text, and the supported equivalent is a role **icon** (`display_icon`, boost level 2+), which takes an image or a unicode emoji — the natural home for Stage 12's rendered banner.

## 12. Reconciling against notability

**Status:** implemented (Stage 8)

`services/transitions.py` holds a **reconcile**, not a set of edge-triggered transition handlers: it reads what's notable now and makes Discord match, rather than diffing against a remembered previous state. That makes the pass safe to run repeatedly, lets it heal a server that drifted while the bot was down or while an edit 403'd, and means there's no "previous state" record that can itself go stale. It runs after each notability sweep (one scheduler job, so the reconcile reads the numbers the sweep just wrote) and on demand via `~script reconcile`.

Only guilds with a *presence* are visited — a role we created, a live delegate, or a claimed contact slot. The cache knows a couple of hundred guilds and all but a handful have nothing here to reconcile; iterating the cache would mint a role for every guild in Wynncraft. Tags are deduplicated case-insensitively, or two spellings of one guild would take turns undoing each other. One guild's failure is logged and skipped rather than ending the pass.

Per guild it settles the contact roles (§6) and the aesthetic role, the latter with three outcomes:

- **Recoloured** — notable, so it carries the Athena hue.
- **Greyed** — not notable, but people still wear it. Deleting it would rewrite every past `@TAG` in the channel history to `@deleted-role`; the colour going away says the same thing reversibly.
- **Deleted** — a role *we* created, holding nobody, for a guild with no live delegate and no verification in flight. It has no members to lose and no history worth keeping, and the next join recreates it, so leaving it would spend one of Discord's 250 role slots on nothing.

Two guards on that deletion, because it's the irreversible one. **A role we didn't create is never deleted** — one adopted by name might be somebody's own, and by the time you find out, the mentions are already broken; `GuildRole` records the ones we minted precisely so the sweep can tell. And **a pending invite counts as in use**: the join listener creates the role and then applies it, so a sweep landing between the two would delete the role out from under an `add_roles` already in flight and fail a verification. A `PendingInvite` row exists for the whole of that window — it's only deleted once the `Delegate` row is written, and the `Delegate` row is what the first guard sees.
