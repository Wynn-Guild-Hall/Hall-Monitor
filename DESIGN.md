# Hall-Monitor — Design

## 1. Process model

Hall-Monitor is a single Python process. Inside it, two asyncio tasks run concurrently:

- The **discord.py bot** (`commands.Bot`, `~` prefix).
- The **FastAPI sidecar** (`uvicorn.Server.serve()` scheduled as a task).

Both share the same Tortoise ORM connection to `hall-monitor.db` under `DATA_DIR`. Running them in one process keeps the DB single-writer and lets sidecar routes call bot methods directly.

Entry point: [`src/hall_monitor/__main__.py`](src/hall_monitor/__main__.py).

## 2. End-to-end join flow

**Status:** implemented end to end (Stages 3–10).

1. A representative visits `hall.wynnvets.org/join`, enters their Minecraft username.
2. Hallway's JS calls `GET /api/join/lookup?username=X`. Hall-Monitor resolves the UUID (Mojang, PlayerDB fallback), asks Wynncraft's API whether they're a chief/owner of a notable guild, and returns `{eligible, guild_tag, mc_username, current_contacts_per_role}` for the UI to render. On failure the response carries a `reason` field (`"not chief or owner"` / `"guild not notable"`); unknown username → HTTP 404.
3. The user ticks the contact roles they want. The UI updates a live "type `HALL14` on verify.wynnvets.org" hint, where the digits are a bit-field over the roles (see §5).
4. The user joins `verify.wynnvets.org` in Minecraft and types `HALL14`.
5. Picolimbo matches the `hall` route prefix, strips it, and forwards `GET /api/verify/{uuid}/14` to Hall-Monitor over the `verify` Docker network. That route is deliberately not web-reachable — Hallway's nginx proxies only `/api/join/` — because its sole proof that the requester owns the Minecraft account is that they connected to the limbo server as it.
6. Hall-Monitor parses the code, re-runs the Wynncraft eligibility check (authoritative), mints a single-use Discord invite, and returns it as a `kick_message`. Every *rejection* comes back as a `chat_message` instead, leaving the player connected — being disconnected for a mistyped code means reconnecting just to retry. Success is the one case that disconnects, because the invite has to be readable after the session ends, and nothing on a Minecraft disconnect screen is clickable: the message leads with the bare invite code to type into Discord's join dialog, with the `discord.gg` URL after it for anyone who'd rather paste a link. Picolimbo renders kick reasons as MiniMessage (auth-stack patch `0005`), so the code is coloured apart from the instructions around it — only the vanilla colour names and formatting tags parse, and a test pins the message to that set.
7. Picolimbo disconnects the player with that message. The player clicks the invite in their MC client's kick screen.
8. On `on_member_join`, Hall-Monitor resolves which invite was used (see §3.1), ensures the guild's aesthetic role exists (§11), applies it alongside the delegate role and the encoded contact roles (kicking prior conflicting holders — see §6), promotes the `PendingInvite` to a `Delegate`, and sets the nickname to `Username [TAG]` (§13).

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
3. Season placements — top **5** in any of the last 10 seasons, top **6** in any of the last 5, or mean rank across the last 5 **≤ 15**. The bounds live in `notability.SEASON_PEAK_LAST_10` / `SEASON_PEAK_LAST_5` / `SEASON_MEAN_LAST_5`, named for what they are rather than their values — an earlier pair called `TOP_3` and `TOP_10` outlived the numbers they were named after. Wynnpool's season-rating payload identifies guilds by name only (no prefix field), so this signal matches on guild name, case-insensitively — the tag is still checked first should the shape ever gain one. The three rules are kept apart in `notability.season_rules` and recorded per guild, because they make very different claims: one outstanding season two years ago and a steady mid-table record both satisfy "season placement", and only one of them is a case for tightening. Tightened once already, from 3/10/25 to 5/6/15 — which took season placement from 29 guilds to 16. The loose one was the five-season peak: at ≤10 a single top-10 season carried a guild for two and a half years. `~script season` reports them separately, and `~script season <TAG>` shows a guild's rank in each of the last ten. Note the boards are top-100, so a missing season is *outside the top 100* rather than a bad rank — and the mean rule deliberately requires a placement in every one of the last five, or a guild that turned up once and came second would qualify on a single appearance.
4. Territory ownership — more than 20 territories **sustained across five days**, while a Wynncraft season is running (`api.wynncraft.com/v3/guild/seasons`). See §4.1.
5. War count > 50 000 on the Wynncraft guild payload.
6. A janitor/monitor-issued force override (`force_override` table with `kind="notable"`) with no expiry or an expiry in the future.

**All six signals come from bulk leaderboards.** A sweep makes no per-guild call at all, and costs about a dozen requests in steady state no matter how many guilds it evaluates.

Most of what it *used* to cost was season boards: ten of them, every hour. A finished season's ratings never change again, so its board is fetched once per process and kept (`_season_boards`), leaving at most one live board to re-read. And a board that can't be fetched is treated as an **empty** board rather than a failed sweep — the `gather` originally had no `return_exceptions`, so a single 429 aborted the entire refresh. An empty board keeps the list positionally aligned, so "the last five seasons" still means the last five and the guild simply doesn't place in the one that couldn't be read; the only effect is that signal 3 may read false. A missing season is worth far less than a missing sweep.

Territory ownership and war count get this from a property of top-N boards: Wynnpool publishes `guildWars` and `guildTerritories` capped at 100 rows, and while a board's *floor* sits below our threshold, a guild missing from it must be under that threshold — otherwise it would have displaced the bottom entry. At the time of writing the wars board reaches down to ~4 100 (threshold 50 000) and territories to 0 (threshold 20), so both are comfortably decisive. `_board_decides` re-checks it every sweep and logs a warning if a floor ever climbs past its threshold, at which point the per-guild `external.guild_stats` fallback takes over for guilds off the board.

Candidates are drawn from **every** board Wynnpool publishes — the four signal boards plus `guildTotalRaids` and the five raid boards. Those six answer no signal, but a guild ranked for raids may well qualify on level or wars, and it can't if we never evaluate it. That takes the candidate set from 113 guilds to ~279. A candidate board that fails is logged and skipped: it costs coverage, not correctness. That distinction matters when reading the cache back: a war count of `null` across every notable guild means nobody was asked, not that nobody qualifies. `refresh_all(exhaustive=True)` — `~script refresh_notability full` — evaluates every signal regardless, at the cost of a per-guild request for every candidate. It's pointless for deciding notability and necessary for deciding thresholds.

`is_notable(tag)` reads from the cache; on a miss it falls back to an inline single-guild evaluation that hits every relevant API and populates the cache row. `refresh_all()` collects candidate tags from every Wynnpool leaderboard, every `Delegate` row, and every `ForceOverride(kind="notable")`, then re-evaluates them all. The scheduler runs it every `NOTABILITY_REFRESH_SECONDS` (default 3600).

`~force notable <tag> <time>` writes a `ForceOverride` row. Janitors are capped at three months — long enough to carry a guild through a quiet patch, short enough that nobody parks a guild in the Hall indefinitely. There's no floor. Monitors have no ceiling and can pass `0` for a permanent override (`services/time_parse.py` owns the parsing). `~unforce notable <tag>` deletes the row.

What a change in notability *does* to a guild's representatives — the delegate ↔ relegate swap, the contact roles, the guild colour — is §12. Nothing here dispatches on the change itself; the reconcile reads the cache and makes Discord match.

### 4.1 Sustained territory

A snapshot cannot answer signal 4. Any guild can take dozens of territories briefly, and small ones can sit on a few indefinitely in low-contest FFA zones; what marks a major guild is keeping **twenty for five days**, which needs on-call war teams across timezones and the eco to fund them. Wynnpool's `guildTerritories` board couldn't answer it either — it's a lagging snapshot that once credited two guilds with 61 and 57 territories while the game said they held none, and both were notable on the strength of it.

So the sweep samples Wynncraft's live territory map each pass (`/v3/guild/list/territory` — one request, every territory with its current holder, authoritative) and `services/territory_history.py` reads the series back. Three decisions make it a measure rather than a number that looks like one:

- **A fraction of the window, not a minimum.** A strong guild can be pushed down to a handful of territories for a few hours and take it back; that's an ordinary night, provided they reclaim it. Requiring every reading to clear the bar would disqualify precisely the guilds the signal exists to find. `SUSTAINED_FRACTION` (0.8) leaves room to be under it for a full day in total across the five, while still failing a guild that dropped and never recovered.
- **Sweeps are the denominator, not the guild's own rows.** A guild that started holding two days ago has a flawless record over the two days it's been watched, and that is the claim we are specifically not making.
- **The window must be covered before it can be judged**, measured from the oldest reading still retained rather than from the span of readings inside the window — those can only span the full window if a sample lands exactly on its edge, so comparing them to it would leave `covered` essentially never true. Retention runs a day longer than the window so this can be asked at all.

Guilds that stop holding are recorded as **zero** rather than dropped. Sampling only the holders would leave a wiped guild's last good readings standing for the rest of the window — the exact failure the snapshot had. Territory holders are also candidates in their own right, since a guild can hold half the map without placing on any Wynnpool board.

**The signal reads false for everyone until five days of history exist.** That's honest rather than unfortunate: nothing has yet demonstrated the thing being asked about. `~script territory` shows how much history there is and each guild's record; `~script territory <TAG>` explains one guild's verdict.

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

Two things reach the assign path: `on_member_join` claims every slot a verification code asked for, and `~force assign <user> <contactType>` claims one by hand. **Neither may claim a slot for a representative playing for a different guild** — `assign_contact` raises `NotTheirGuild`, and the command explains it and points at `~force guild`. That rule has to live with the grant rather than only in the reconcile: when the two disagreed, `~force assign` reported success, added the role, and the next reconcile stripped it again, which looked from the outside like the command silently failing. The command scopes by tier — a janitor or monitor assigns within *the target's* guild, an ownership contact only within their own — and either way the target must already have a `Delegate` row, since the slot is a foreign key onto it. `~unforce assign` vacates a slot, with the same kick-if-last consequence.

Guild tags are matched case-insensitively (`services/guild_tag.py`) so `~force assign` hits the row the join flow wrote, and the slot's *stored* spelling is left alone: two rows differing only in case would break an invariant the `unique_together` can't see. Discord-side failures (role removal, kick) are logged rather than raised — the database is the roster's source of truth, and a stale role on a displaced holder is visible and fixable in a way a slot that silently refused to move is not.

**Contact roles are gated on notability and on representation** (`sync_contact_roles`, driven by §12's reconcile), and the two gates resolve differently on purpose.

**A guild that isn't notable keeps its slots.** It has dropped out of the Hall for now, so its four contact roles are withdrawn and handed back — to the same people — when it returns. The `GuildContact` rows don't move: notability decides who may *use* a contact role, the row decides who holds the slot, and clearing them would cost a returning guild four re-verifications and lose the record of who to hand the roles back to. Nobody is kicked for it either — the kick is what happens when you lose your slot to someone else, not when your guild has a quiet quarter.

**A holder who no longer speaks for the guild vacates the slot outright** — drifted elsewhere on their own, or repointed by `~force guild`. Not withheld: a slot somebody holds and cannot use is two answers to one question, and every screen that shows it then has to pick one. That is exactly what happened in practice — the roster printed `unclaimed`, the member list showed no contact role, and `~script standing` reported the slot as theirs. Anyone reading two of those concludes one is broken. So the row goes, the role comes off, and **nobody is kicked**: moving guilds already costs a representative their standing and their guild colour (§12.1); it doesn't cost them the room. Coming back re-earns the slot rather than restoring it, which is the honest reading — while they were gone it was free for anyone to claim.

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

Every client funnels through `external/_client.py`, which owns the shared retry/timeout/bucket-queue policy: 10 s timeout, one 500 ms retry on 5xx, 429 pauses the bucket for the `Retry-After`/`RateLimit-Reset` window and re-raises so callers can fall back. **Callers with nothing to fall back to pass `retry_429=True`** — every Wynnpool call does — which waits out that pause and tries once more instead. Wynnpool rate-limits per IP and publishes data nothing else has, so raising there only moves the problem up to a caller whose sole option is to abandon the sweep; a slow answer is strictly better than none. Requests are serialised per bucket with a priority queue; user-facing lookups pass `urgent=True` to jump ahead of background work. Responses come back as frozen dataclasses (`wynncraft.Guild`, `wynnpool.GuildDetails`, `LeaderboardEntry`, …) so no downstream code should be reaching into raw JSON.

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

**Status:** implemented (Stage 8; standing roles and the guild watch in Stage 9)

`services/transitions.py` holds a **reconcile**, not a set of edge-triggered transition handlers: it reads what's notable now and makes Discord match, rather than diffing against a remembered previous state. That makes the pass safe to run repeatedly, lets it heal a server that drifted while the bot was down or while an edit 403'd, and means there's no "previous state" record that can itself go stale. It runs after each notability sweep (one scheduler job, so the reconcile reads the numbers the sweep just wrote) and on demand via `~script reconcile`.

Only guilds with a *presence* are visited — a role we created, a live delegate, or a claimed contact slot. The cache knows a couple of hundred guilds and all but a handful have nothing here to reconcile; iterating the cache would mint a role for every guild in Wynncraft. Tags are deduplicated case-insensitively, or two spellings of one guild would take turns undoing each other. One guild's failure is logged and skipped rather than ending the pass.

Per guild it settles three things: each representative's standing, the contact roles (§6), and the aesthetic role.

### 12.1 Standing

Every representative wears exactly one of three roles, and the reconcile both grants the right one and strips the other two — a member holding two of them reads as two different answers to the same question.

| Standing | When |
|---|---|
| **Delegate** | their guild is notable and they're still in it |
| **Relegate** | their guild isn't notable |
| **External Relegate** | they've moved to a *different* guild |

Moving guilds outranks notability: a representative who has left can't be promoted back by their old guild having a good month. An external representative also comes out of the guild's aesthetic role and **vacates their contact slots** — wearing `VETS` while playing for someone else misinforms every other guild in the room, which is the one thing the colour exists to get right, and the same argument applies to being listed as who to ask about that guild. The slot is given up rather than withheld; see §6 for why holding one you cannot use is worse than not holding it.

**Nobody is kicked and no row is deleted for any of this.** Guilds are expected to move in and out of the Hall, and the point of keeping the `Delegate` row is that coming back costs nothing — no re-verification, and the same people get their slots back.

Note the asymmetry the design brief asks for: being *guildless* is not being external. Someone between guilds is still the person their guild sent, and the alternative relegates anyone who leaves for an afternoon. Only actively sitting in a different guild counts.

### 12.2 The guild watch

Wynncraft pushes nothing, so guild membership is polled: `delegate_registry.refresh_current_guilds` asks for each live delegate's current guild once an hour (one request each, serialised on the player bucket) and writes it to `Delegate.current_guild_tag`. `guild_tag` — the guild they verified as a representative of — never changes; the two differing is the whole definition of external.

The poll only gathers facts; the reconcile that follows in the same job acts on them. That split is what keeps the standing decision in one place and lets `~script guild_watch` + `~script reconcile` reproduce an hour's work by hand. A lookup that fails leaves the stored value alone rather than resetting it: a 429 recorded as "no guild" would read back as the delegate having *rejoined* their guild, quietly promoting someone the last sweep relegated.

A brand-new delegate gets `current_guild_tag` seeded at registration — verification proves they're a chief of that guild right then, so starting them as unknown would only invite a wrong answer until the first poll.

**Known gap — re-representation.** An external representative who becomes a chief of their *new* guild can't verify for it. (`~force guild` covers this in practice — §12.3 — but as an override with an expiry rather than a durable change to the row.) `mint_invite` refuses while their `Delegate` row is live (§3), and releasing the row wouldn't help: clicking an invite as an **existing member fires no `GUILD_MEMBER_ADD`**, so nothing consumes the `PendingInvite`. Short of leaving the server, only a direct edit of the `Delegate` row fixes it.

This is accepted rather than solved. These are major guilds, so someone becoming a *new* guild's representative within a day or two of switching is rare, and the hourly watch means the switch itself is always noticed. The fix is a monitor command (`~force rep`, Stage 15) that re-points the row, vacates the old guild's contact slots without a kick, and verifies chief-hood of the new guild itself rather than trusting the operator.

### 12.3 Saying who someone represents — `~force guild`

`~force guild <user> <TAG> <time>` is a janitor asserting **who a member speaks for**, and it outranks both the guild they verified as a chief of and whatever the watch sees. `delegate_registry.represented_guild` folds the three sources into the one answer everything downstream keys on: the tag on their nickname, the colour they wear, whose contact slots they may hold, and whose notability decides their standing. Force someone to `ANO` and they are, for every purpose the Hall has, an ANO representative — including `~force assign`, which gives them ANO's slot rather than refusing.

It solves two shapes of problem:

- **Repointing.** Somebody now speaks for a different guild than the one they verified with, and re-verifying isn't available while their delegate row is live (§12.2). This is the supported answer to that until `~force rep` makes it durable.
- **Correcting the watch.** A rep mid-transfer flickers, an alt account shows the wrong guild, a shared account shows whoever logged in last. Forcing the guild they already represent pins it and undoes a wrong External Relegate.

**A forced representative is never external**, because the watch disagreeing is the entire situation being overridden. The override sits *in front of* `current_guild_tag`, never in it: the watch rewrites that column hourly, so a forced value stored there would survive exactly until the next sweep. Expired rows read as absent but are left in place, so a janitor can still see what was forced and when it ran out. Duration gating is the shared one (`time_parse.gating_rejection`): janitors capped at three months, monitors unbounded and allowed `0`.

**A command that applies an override says what it applied.** `~force guild` and `~unforce guild` settle the target on the spot and report the end state — "Now standing `delegate`, wearing `ANO`" — or say plainly that nothing needed changing. That wording exists because three separate bugs reached production wearing the same disguise: the command replied with confident success, the hourly reconcile silently disagreed, and the gap between them read as a delay rather than a fault. A no-op that announces itself is a bug report; a no-op that doesn't is a support ticket a week later.

Two consequences worth stating, because both were bugs first:

- **A member wears exactly one guild role.** The colour is a claim about who they speak for; two of them is two claims. `sync_guild_role_membership` strips every *other* role we created, so a repoint moves the colour rather than adding one. Roles adopted by name are never stripped, for the same reason they're never deleted.
- **The reconcile groups members by who they represent, not by the row they were written with.** Otherwise the repointed member's old guild would settle them, undoing what their new guild's pass just did — and their new guild might have no other presence at all, so `guilds_present` counts forced tags too, and `settle_members` mints the role on demand.

### 12.4 The aesthetic role

Three outcomes:

- **Recoloured** — notable, so it carries the Athena hue.
- **Greyed** — not notable, but people still wear it. Deleting it would rewrite every past `@TAG` in the channel history to `@deleted-role`; the colour going away says the same thing reversibly.
- **Deleted** — a role *we* created, holding nobody, for a guild with no live delegate and no verification in flight. It has no members to lose and no history worth keeping, and the next join recreates it, so leaving it would spend one of Discord's 250 role slots on nothing.

Two guards on that deletion, because it's the irreversible one. **A role we didn't create is never deleted** — one adopted by name might be somebody's own, and by the time you find out, the mentions are already broken; `GuildRole` records the ones we minted precisely so the sweep can tell. And **a pending invite counts as in use**: the join listener creates the role and then applies it, so a sweep landing between the two would delete the role out from under an `add_roles` already in flight and fail a verification. A `PendingInvite` row exists for the whole of that window — it's only deleted once the `Delegate` row is written, and the `Delegate` row is what the first guard sees.

## 13. Nicknames

**Status:** implemented (Stage 10)

Every representative wears their guild's tag in the member list — `Holidaze [VETS]` — so a conversation in the Hall carries who someone speaks for without anyone checking a roster. The tag follows the same rule as the standing role (§12.1): their guild's, or `[EXT]` once they've moved elsewhere.

**Only the suffix is ours.** `services/nicknames.py` re-attaches the tag to whatever the member set rather than replacing it, so renaming yourself is fine — you just can't drop the tag while doing it. On a join, where there's no nickname to preserve, the visible part is seeded from their Minecraft username, which is the name the Hall knows them by. When the pair won't fit Discord's 32 characters the *visible* part is truncated, never the tag.

Enforcement runs on `on_member_update` and once at the end of the join. Two things keep that affordable: the listener returns immediately unless the nickname or the roles changed, and `enforce` makes no Discord request when the nickname is already right. That second check is also what stops the loop — our own rename arrives back as another update, and it has to be the *content* that settles it rather than any in-memory flag, which wouldn't survive a restart or a second shard.

Nobody without a `Delegate` row is touched, and observers are excluded outright. Staff, guests and bots keep whatever they set: renaming a member the Hall knows nothing about is a bot reaching well past what it was invited to do. Two rename failures are expected rather than exceptional — Discord lets nobody rename a server owner, and role position governs the rest — so both are logged and skipped rather than raised.

## 14. The Current Guilds roster

**Status:** implemented (Stage 11)

`services/roster.py` keeps one channel — `ROSTER_CHANNEL_ID`, the Current Guilds channel — holding a list of every notable guild with its four contacts. It's how the Hall is legible without knowing a delegate first: the answer to "who do I ask about housing in Sequoia?" is a channel rather than a DM chain. Each guild reads as its banner emote, its full name, its tag, and one line per contact slot with `unclaimed` where nobody holds it.

Like §12 this is a **reconcile**, not a set of edge-triggered edits. `sync_channel` renders what the channel should say and makes it say that; nothing tracks what changed, so there's no event that can be missed and a channel somebody mangled by hand heals on the next pass. It runs at the end of the hourly job, after the reconcile, so it publishes what that pass just settled — and that hourly run is the **backstop** for the event-driven calls below, which means a missing hook costs an hour of staleness rather than permanent drift.

Three properties of Discord's message model shape the whole sync:

- **Messages can't be reordered or inserted.** A roster spanning eight messages is eight messages in send order, so replacing the third means re-sending the third onward. `sync_channel` edits the longest intact prefix in place and rebuilds from the first gap — a message deleted by hand, a run that died mid-send, or the channel simply disagreeing with our recorded order, which is resolved in the channel's favour because that's what readers see. `RosterMessage` records position and message ID for exactly this walk.
- **2000 characters.** Guilds are packed into messages without ever splitting one across a boundary — half a guild's contacts at the bottom of one message and half at the top of the next reads as two guilds. The cost is that one guild gaining a contact can reflow everything below it, which is the case the prefix-and-rebuild walk exists for.
- **Editing is cheap, sending is not.** A message whose content already matches is left strictly alone. Getting this wrong is visible: an "edited" marker would appear on the entire roster every hour.

**Nothing else may post there.** Any message in the channel that isn't a tracked roster message of ours is deleted — somebody talking in the wrong place, or the residue of a pass that died between sending a message and recording it. The second is why it can't simply skip messages by other authors: an orphan of our own would otherwise sit above the roster forever. This needs **Manage Messages**.

### 14.1 What it lists, and in what order

Order is Wynnpool's `guildLevel` board, rank 1 first, with guilds absent from it after the ranked ones alphabetically. Both the rank and the guild's full name are columns on `NotabilityCache`, written by the hourly sweep from leaderboards it already holds — so **rendering the roster costs no third-party request at all**, and the ordering is exactly as fresh as the notability behind it. The name matters because nothing else in the bot knows a guild by anything but its tag.

A guild can be notable without appearing on a board that carries a prefix, though — delegate guilds and forced tags join the candidate set on their own, and season boards publish no prefix at all — so those have no name from the sweep. `VETS` is exactly that case, and the roster printed it as `**VETS** (`VETS`)` until `notability._learn_missing_names` asked for it. The lookup is `external.guild_name_for`, which is **Wynncraft-only**: Wynnpool addresses guilds by name and publishes no prefix route, so it can't answer a tag→name question. A name found once is cached, so a guild costs one lookup ever; a tag nothing knows is re-asked each sweep, which is one request an hour and starts working the day the guild becomes real. The same fill-in feeds signal 3, which matches season boards by name and so could never fire for a guild whose name we'd never learned.

Candidates are the notability cache plus live `notable` force overrides, answered in bulk (`notability.active_notable_overrides`) rather than by asking `is_notable` per guild: that call falls through to a live evaluation on a cache miss, and a render is not the place to discover a guild needs twenty leaderboard fetches. The override half is what makes `~force notable NEWG 3mo` appear immediately — an entry that waited for the next sweep would read as the command not having worked. Tags fold case, so `VETS` and `vets` are one entry.

A slot whose holder no longer speaks for the guild prints as `unclaimed`, and by the time the roster next renders the reconcile has normally vacated the row as well (§6) — the check here covers the seconds in between, so the roster never names somebody the next pass is about to remove. A holder who has left the server also reads as unclaimed, but their row *does* stay: leaving isn't the same as changing sides, and coming back costs no re-verification.

The banner emote is resolved by **name**, per guild tag, falling back to a shared `:Empty_Banner:` and then to a plain unicode flag. Stage 12 mints the per-guild ones; until then every guild wears the placeholder, and a server missing even that still renders — a missing emote must not be the thing that stops the roster.

### 14.2 When it redraws

Besides the hourly pass, `roster.request_sync(guild)` is called from the join listener, the new `on_member_remove` listener, `~script refresh_notability`, and — via `ForceGroup.cog_after_invoke` — **every `~force`/`~unforce` sub-command that runs cleanly**. That last one is on the group rather than on each sub-command deliberately: hooking them individually is how `~force guild` and `~force notable` shipped without one, and the symptom was the worst kind — the command reported success, the roster silently disagreed, and the gap read as lag rather than a fault (the same shape as the four bugs in §12.3's history). A sub-command added later is covered without anyone remembering to. It is **debounced** (`SYNC_DELAY_SECONDS`): a verification claims up to four contact slots and then sets a nickname, and one redraw a few seconds later costs a single channel read instead of five, landing after the join has settled rather than partway through it. It's fire-and-forget — nothing a caller does should fail because a channel couldn't be redrawn.

**The roster never notifies anyone.** Contacts are named with a real `<@id>`, so an entry stays a live link to the person, but every message is sent *and edited* with `AllowedMentions.none()`. The channel is redrawn on every join, leave, force and hourly sweep, and four contacts a guild being pinged each time would make it unreadable within a day. Note this suppresses the notification only — the mention still renders as their name in the usual highlight. The `edit` half is the easy one to miss: an edit that omits `allowed_mentions` falls back to the default and pings.

`~script roster` runs the same pass on demand. Like `~script reconcile` it reads the notability cache rather than refreshing it, so `~script refresh_notability` comes first if that matters.

The `on_member_remove` listener is deliberately small: it sets `Delegate.left_at` and asks for a redraw. The `GuildContact` rows stay, on the same reasoning as everywhere else — coming back costs no re-verification, and the display side already reads an absent holder as unclaimed. Marking the row is what stops the guild watch spending a Wynncraft request per sweep on somebody who has gone.

## 15. Guild banner emotes

**Status:** implemented (Stage 12)

Each roster entry leads with the guild's own Minecraft banner as a custom emote, so the list reads the way the guilds look in-game rather than as forty rows of identical placeholder. `services/banner_render.py` draws it and `services/emote_slots.py` decides which guilds get one.

### 15.1 Drawing a banner

A Wynncraft banner is a base dye colour plus an ordered stack of patterns, each in a dye colour of its own (`wynnpool.guild_details(name).banner`). Wynnpool publishes the pattern art as SVG at `www.wynnpool.com/banners/<PATTERN>.svg` — a different host from the API, so it gets its own requester and its own rate-limit bucket, and the art is cached for the life of the process because a given pattern's SVG never changes.

**The art is a mask, not a picture.** Every shape in it is black at an opacity of 1, 0.5 or 0.25; the colour comes from us and the alpha from the art, and those partial opacities are Minecraft's own shading. Compositing is therefore: fill the canvas with the base dye, then for each layer take a solid sheet of its dye colour, cut it to the art's alpha channel, and alpha-composite it on. That's not inferred — it's what Wynnpool's own site does, which renders each layer as a coloured `div` with `mask: url(/banners/<PATTERN>.svg); mask-size: cover`. Drawing the art directly instead would make every banner a black silhouette.

Two deliberate departures from that reference:

- **`SILVER` is a colour here.** Wynncraft's API returns `SILVER` where modern Minecraft says `LIGHT_GRAY`, and Wynnpool's lookup table is keyed only on the modern name — so a silver banner renders with *no base colour at all* on their site. Both names map to `#999999` in `DYES`. An unmapped dye renders magenta rather than transparent: a hole reads as a rendering bug, magenta reads as "a dye nobody mapped", which is what it is.
- **A missing pattern is skipped, not fatal.** Wynnpool publishes no art for several real patterns (`GLOBE`, `PIGLIN`, `DIAGONAL_UP_LEFT`, `DIAGONAL_UP_RIGHT`). A guild wearing one still gets a banner from its remaining layers, which is far closer to right than no banner. The 404 is cached, so it costs one request ever rather than one per render.

Rasterisation is **resvg** (`resvg-py`), not cairosvg: it ships self-contained wheels for Linux and Windows, so the image needs no `libcairo` and the render tests run on a developer's machine. It also handles the `<style>` opacity classes and the two gradient patterns natively, which a hand-rolled rasteriser would have had to grow — every other pattern is rectilinear, but `GRADIENT` and `GRADIENT_UP` are not.

The composite runs at the art's own 160×320 and the result is centred on a **transparent 128×128 square**. A banner is 1:2 and an emote is square; stretching it produces a different banner, and recognising it is the entire point.

**Every banner gets a thin grey frame**, drawn in the margin rather than on the artwork. Some guilds fly a plain white banner — `Zamn` does, and it's a legitimate design, not a missing one — and a white rectangle against Discord's light theme is *nothing at all*: the roster row appears to have no emote. The frame gives every banner an edge to be found by. Because it sits outside the art it covers no part of any design, not even a `BORDER` pattern's outer ring; the banner is scaled down slightly to make room, which costs a little size and no detail. Its width is set by what survives the downscale to roughly 22px inline — anything thinner aliases into a faint tint and the white banner stays invisible — and on the dark theme it's very nearly imperceptible, which is the point: it costs nothing where it isn't needed. Compositing is CPU work on a worker thread (`asyncio.to_thread`) — five layers of rasterisation on the event loop would stall the gateway heartbeat, and a reconcile does this for every guild in the budget.

### 15.2 How many banners, and which

Members can't upload emotes here, so the list is the bot's to manage and **banners fill whatever the server isn't otherwise using**. Nothing configures the count: `emote_slots.budget` derives it from `guild.emoji_limit` minus the emotes a human uploaded minus one for the blank banner (§15.4) minus `ROSTER_EMOTE_RESERVE`. Animated emotes don't count — Discord gives them a separate pool of the same size, and banners are static.

That derivation is the whole boost-level story. `emoji_limit` *is* the boost level (50 slots at tier 0, 100 at 1, 150 at 2, 250 at 3), so a server that gains one simply has a bigger number on the next pass and mints further down the list; one that loses a level has a smaller number and evicts the tail. Discord doesn't remove the overflow itself — it just refuses every subsequent upload, which reads as the mint silently failing rather than as the list being full. The reserve exists so an admin wanting to upload something doesn't have to delete a banner the next pass would put straight back.

**Which guilds get one is decided by how strongly each is notable, not by roster order.** Notability is a yes/no, but a guild qualifying on four signals is more securely part of the Hall than one scraping in on a single leaderboard — so when there are more notable guilds than slots, the placeholder falls to the ones qualifying by the least. `notability.strength` sorts on the number of matched signals first, then breaks ties with the numbers behind them (`NotabilityCache.metrics_json`, written by the same sweep), signal by signal in §4's order. An unmatched signal contributes zero, so there's no credit for *nearly* qualifying; rank-based signals are inverted, since rank 1 is the strongest and is the one place a raw value sorts exactly backwards. A guild with no cached measurement — a fresh `~force notable` — sorts last, because nothing about it justifies displacing a guild we've actually measured, and the tag is the final tiebreak purely so the order is stable between passes.

Note this is deliberately a *different* order from §14.1's. The roster is sorted for someone reading down it; the slots go to whoever has most claim on one.

Guilds above the line get their banner minted; guilds that fall below it are evicted, and **eviction runs before minting** so the freed slots are available to the guilds that displaced them — otherwise a full list makes every mint fail and the boundary never moves. A boost change doesn't wait for the hourly pass: `cogs/listeners/on_boost.py` reconciles on `GUILD_UPDATE`, filtered to changes that actually moved the slot count or the role-icon feature, since that event also fires for the server's name and icon.

#### Staying inside Wynnpool's rate limit

A banner costs one call to Wynnpool's guild endpoint, that endpoint starts answering 429 at around a dozen requests, and a roster is fifty guilds. Two things keep a pass inside it:

- **An unchanged banner is never re-fetched.** `GuildEmote.checked_at` records when a banner was last *looked at*, as distinct from `created_at`, which records when it last *changed* — a banner that never changes never moves `created_at`, so using that would bring the re-check due once and then fire every pass forever. A guild whose emote is up and was checked inside `RECHECK_AFTER` (a week) costs nothing, so a settled server makes **no upstream requests at all** and only a new or genuinely stale guild spends anything.
- **What does need fetching goes at a trickle**, `FETCH_INTERVAL_S` apart, and the pass runs to **completion** at that pace rather than stopping at a cap. Fifty guilds is a few minutes, which is fine for something nobody is waiting on and much better than an operator running the same command five times to fill a fresh server. `~script emotes` reports progress into its own message as it goes. The wait falls *after* a request that actually happened, so guilds needing nothing cost no time and a settled server finishes instantly.

  That return value has to mean "a request happened", not "this guild was considered" — a guild whose name was never resolved reaches nobody, and once it counted as a fetch anyway, three passes in a row spent their entire allowance on guilds they had never contacted.

Guilds do redesign their banners, roughly **twice a year each**, and that number is what sets the window rather than any request budget. Polling can't win it: across fifty guilds that's about a hundred real changes a year, and a weekly window spends ~2 500 requests catching them — while halving the window to two days triples the cost and still doesn't help whoever noticed a redesign this morning. So the automatic window stays polite and slow, and `~script emotes <TAG>` forces one guild immediately, bypassing `checked_at` entirely. For an event this rare, the on-demand path is the one that matters; the sweep is just there so nothing stays wrong indefinitely.

One guild's failure never costs the rest — that's the same rule as every other sweep here (§12, §4) and it was learned the hard way, when a 429 on the twelfth guild threw away the eleven banners already uploaded and answered the operator with "that broke on my end". A 429 that survives the client's own retry additionally **stops** the pass rather than grinding through forty more guilds to collect forty more of them, and the summary says so — a pass that was cut short must never read as a finished one.

The skip has one exception worth naming. Role icons ride on the same bytes, so a server that has just regained boost level 2 would sit iconless until the weekly re-check — `_icon_out_of_sync` spots exactly that (the role's recorded hash disagreeing with the emote's) and spends a fetch on it. Losing the feature needs no bytes at all, so `guild_roles.forget_role_icons` clears the records once per pass rather than per guild; otherwise a settled server, which skips every guild, would never clear them.

Two invariants, both the same shape as §11's rules for roles:

- **Only emotes we created are ever deleted.** `GuildEmote` records the ones we minted and they're resolved by **ID, never by name**. An emote that happens to share a guild's tag might be somebody's own from years ago, and deleting it breaks every message that used it, irreversibly.
- **An unchanged banner is never re-uploaded.** Discord has no replace-in-place for emotes, so a re-mint is a delete and an upload and the ID *moves* — breaking every message and role icon already pointing at the old one. `GuildEmote.image_hash` is what decides, so a re-render producing identical bytes costs nothing. When an emote *does* move, the scheduler asks the roster to redraw: it was written minutes earlier and now names an emote that no longer exists.

The roster picks them up on its own: `roster.emote_for` tries our recorded emote by ID, falls back to the shared blank banner for guilds outside the budget, and then to a plain unicode flag. A missing emote must never be the thing that stops the roster. Uploading needs **Manage Expressions**.

### 15.4 The blank banner

Guilds outside the budget wear a shared placeholder, and **the bot mints it itself** on the first pass rather than waiting for someone to upload one. It goes through the same pipeline as the real banners, so it matches them in size and proportion instead of sitting beside them as a differently-shaped emoji — and it has no layers, so it needs no pattern art and no network. A fallback that depends on Wynnpool being up isn't much of a fallback.

It's named **`NONE`**, after Wynncraft's reserved guild "Nobody", and that name is load-bearing: `NONE` is reserved by the server and no real guild can ever hold it, so the placeholder sits in the same tag-named scheme as every other banner with no possibility of collision. A name like `Empty_Banner` was only unique by convention. An older placeholder under that name is **renamed in place** rather than replaced, because a new emote is a new ID and every message already carrying the old one would break.

Nobody's banner genuinely is an empty one — no patterns, no dyeing — so rendering it from no layers isn't an approximation. Nothing publishes it (Nobody 404s on Wynnpool and on Wynncraft by both name and prefix, and never appears in the territory list) and nothing needs to. The one liberty is the colour: an undyed banner is white, and a guild flying a *real* blank white banner would then be pixel-identical to "this guild has no emote slot". Those two need telling apart, so the placeholder is silver. (Visibility isn't the reason — §15.1's frame already handles that.)

It's found by **name**, which is what lets an operator's own hand-made one be adopted rather than duplicated, and it is **never deleted** — it belongs to no guild, so nothing can evict it, and the roster's entire fallback chain rests on it. It costs a slot like anything else, and `budget` holds one back for it before it exists: otherwise the last guild in the budget would be minted into the space the placeholder is about to need, and fail. If the upload fails there's no fuss — the roster drops to a plain unicode flag, which is exactly what it did before this existed.

### 15.3 The same image on the role

The rendered PNG has a second consumer: the guild role's `display_icon`, which shows immediately left of the name in the member list and in every `@VETS` mention. That is the *only* way a banner can sit beside a role name — a custom emote in a role name renders as literal text (§11) — so the icon is the whole mechanism, not a nicety.

`emote_slots` drives it rather than the role reconcile, because it already holds the bytes; rendering again in §12's pass would double the work per guild per hour. Role icons need the server at **boost level 2** (`ROLE_ICONS` in `guild.features`), and below that every write is a 403 — so an unboosted server is detected and skipped rather than made to fail hourly, and a role without an icon works exactly as it did before.

**Losing level 2 clears the recorded hashes.** Discord strips the icons from every role and tells us nothing, so a remembered hash would say "already set" forever and the roles would stay bare straight through the next boost. Forgetting them on the way down is what makes regaining the level put them back. That's not an edit — there's nothing to write to below the threshold — just the record catching up with what Discord already did. The icon is written only when its hash differs, since an unconditional edit per hour is an audit-log entry per hour, and it's *cleared* when a guild is evicted from the budget: an icon outliving the emote it came from would drift the moment the guild changed its banner. Only roles we created are decorated, on the same reasoning as never deleting one we didn't.

`~script render_banner <TAG>` renders a guild's banner and posts it as an attachment with its hash, so it can be checked against the in-game article without spending a slot to find out. `~script emotes` runs the whole reconcile on demand, and `~script emotes <TAG>` re-fetches one guild's banner immediately — the answer to a redesign somebody has already spotted.
