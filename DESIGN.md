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
2. Hallway's JS calls `GET /api/join/lookup?username=X`. Hall-Monitor resolves the UUID (Mojang, PlayerDB fallback), asks Wynncraft's API whether they're a chief/owner of a major guild, and returns `{eligible, guild_tag, mc_username, current_contacts_per_role}` for the UI to render. On failure the response carries a `reason` field (`"not chief or owner"` / `"guild not major"`); unknown username → HTTP 404.
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

## 4. Major-guild status

**Status:** implemented (Stage 2)

`services/major_guilds.py` aggregates seven independent signals against a guild tag and stores the result in `major_guild_cache`. Signals:

1. Top-25 average online (last 5 days) on `api.wynnpool.com/leaderboard/guild-average-online`.
2. Level 100+ on `api.wynnpool.com/leaderboard/guildLevel`.
3. Season placements — top **5** in any of the last 10 seasons, top **6** in any of the last 5, or mean rank across the last 5 **≤ 15**. The bounds live in `major_guilds.SEASON_PEAK_LAST_10` / `SEASON_PEAK_LAST_5` / `SEASON_MEAN_LAST_5`, named for what they are rather than their values — an earlier pair called `TOP_3` and `TOP_10` outlived the numbers they were named after. Wynnpool's season-rating payload identifies guilds by name only (no prefix field), so this signal matches on guild name, case-insensitively — the tag is still checked first should the shape ever gain one. The three rules are kept apart in `major_guilds.season_rules` and recorded per guild, because they make very different claims: one outstanding season two years ago and a steady mid-table record both satisfy "season placement", and only one of them is a case for tightening. Tightened once already, from 3/10/25 to 5/6/15 — which took season placement from 29 guilds to 16. The loose one was the five-season peak: at ≤10 a single top-10 season carried a guild for two and a half years. `~script season` reports them separately, and `~script season <TAG>` shows a guild's rank in each of the last ten. Note the boards are top-100, so a missing season is *outside the top 100* rather than a bad rank — and the mean rule deliberately requires a placement in every one of the last five, or a guild that turned up once and came second would qualify on a single appearance.
4. Territory ownership — more than 20 territories **sustained across five days**, while a Wynncraft season is running (`api.wynncraft.com/v3/guild/seasons`). See §4.1.
5. War count > 50 000 on the Wynncraft guild payload.
6. Guild raids — top 25 on Wynnpool's `guildTotalRaids`, **or** top 15 on any single raid's `<raid>SrGuilds` board. Two rules kept apart in `major_guilds.raid_rules` for the same reason as §3's three: "near the top of every raid combined" and "near the top of one raid" are different claims. The bounds differ deliberately — 25 on the aggregate mirrors signal 1 against a board of comparable depth, while a single raid is held tighter because placing well on one of five says less than placing well across all of them. The six boards were already being fetched to widen the candidate set; this reads them.
7. A janitor/monitor-issued force override (`force_override` table with `kind="major"`) with no expiry or an expiry in the future.

**All seven signals come from bulk data.** A sweep makes no per-guild call at all, and costs about a dozen requests in steady state no matter how many guilds it evaluates.

Most of what it *used* to cost was season boards: ten of them, every hour. A finished season's ratings never change again, so its board is fetched once per process and kept (`_season_boards`), leaving at most one live board to re-read. And a board that can't be fetched is treated as an **empty** board rather than a failed sweep — the `gather` originally had no `return_exceptions`, so a single 429 aborted the entire refresh. An empty board keeps the list positionally aligned, so "the last five seasons" still means the last five and the guild simply doesn't place in the one that couldn't be read; the only effect is that signal 3 may read false. A missing season is worth far less than a missing sweep.

Territory ownership and war count get this from a property of top-N boards: Wynnpool publishes `guildWars` and `guildTerritories` capped at 100 rows, and while a board's *floor* sits below our threshold, a guild missing from it must be under that threshold — otherwise it would have displaced the bottom entry. At the time of writing the wars board reaches down to ~4 100 (threshold 50 000) and territories to 0 (threshold 20), so both are comfortably decisive. `_board_decides` re-checks it every sweep and logs a warning if a floor ever climbs past its threshold, at which point the per-guild `external.guild_stats` fallback takes over for guilds off the board.

Candidates are drawn from **every** board Wynnpool publishes — the four signal boards plus `guildTotalRaids` and the five raid boards. Those six answer no signal, but a guild ranked for raids may well qualify on level or wars, and it can't if we never evaluate it. That takes the candidate set from 113 guilds to ~279. A candidate board that fails is logged and skipped: it costs coverage, not correctness. That distinction matters when reading the cache back: a war count of `null` across every major guild means nobody was asked, not that nobody qualifies. `refresh_all(exhaustive=True)` — `~script refresh_major full` — evaluates every signal regardless, at the cost of a per-guild request for every candidate. It's pointless for deciding major-guild status and necessary for deciding thresholds.

`is_major(tag)` reads from the cache; on a miss it falls back to an inline single-guild evaluation that hits every relevant API and populates the cache row. `refresh_all()` collects candidate tags from every Wynnpool leaderboard, every `Delegate` row, and every `ForceOverride(kind="major")`, then re-evaluates them all. The scheduler runs it every `MAJOR_GUILD_REFRESH_SECONDS` (default 3600).

`~force major <tag> <time>` writes a `ForceOverride` row. Janitors are capped at three months — long enough to carry a guild through a quiet patch, short enough that nobody parks a guild in the Hall indefinitely. There's no floor. Monitors have no ceiling and can pass `0` for a permanent override (`services/time_parse.py` owns the parsing). `~unforce major <tag>` deletes the row.

What a change in major-guild status *does* to a guild's representatives — the delegate ↔ relegate swap, the contact roles, the guild colour — is §12. Nothing here dispatches on the change itself; the reconcile reads the cache and makes Discord match.

### 4.1 Sustained territory

A snapshot cannot answer signal 4. Any guild can take dozens of territories briefly, and small ones can sit on a few indefinitely in low-contest FFA zones; what marks a major guild is keeping **twenty for five days**, which needs on-call war teams across timezones and the eco to fund them. Wynnpool's `guildTerritories` board couldn't answer it either — it's a lagging snapshot that once credited two guilds with 61 and 57 territories while the game said they held none, and both were major on the strength of it.

So the sweep samples Wynncraft's live territory map each pass (`/v3/guild/list/territory` — one request, every territory with its current holder, authoritative) and `services/territory_history.py` reads the series back. Three decisions make it a measure rather than a number that looks like one:

- **A fraction of the window, not a minimum.** A strong guild can be pushed down to a handful of territories for a few hours and take it back; that's an ordinary night, provided they reclaim it. Requiring every reading to clear the bar would disqualify precisely the guilds the signal exists to find. `SUSTAINED_FRACTION` (0.8) leaves room to be under it for a full day in total across the five, while still failing a guild that dropped and never recovered.
- **Sweeps are the denominator, not the guild's own rows.** A guild that started holding two days ago has a flawless record over the two days it's been watched, and that is the claim we are specifically not making.
- **The window must be covered before it can be judged**, measured from the oldest reading still retained rather than from the span of readings inside the window — those can only span the full window if a sample lands exactly on its edge, so comparing them to it would leave `covered` essentially never true. Retention runs a day longer than the window so this can be asked at all.

Guilds that stop holding are recorded as **zero** rather than dropped. Sampling only the holders would leave a wiped guild's last good readings standing for the rest of the window — the exact failure the snapshot had. Territory holders are also candidates in their own right, since a guild can hold half the map without placing on any Wynnpool board.

**The signal reads false for everyone until five days of history exist.** That's honest rather than unfortunate: nothing has yet demonstrated the thing being asked about. `~script territory` shows how much history there is and each guild's record; `~script territory <TAG>` explains one guild's verdict.

### Guild tags

`services/guild_tag.py` is the one place that decides whether two tags mean the same guild. Tags are normally three or four characters and case-agnostic, but Wynncraft permits two rarities worth not being surprised by: case *can* in principle be significant, and spaces and underscores are legal characters.

So Wynncraft's spelling is what gets stored and displayed — uppercasing on the way in would discard information we were handed — and only comparison folds case. DB lookups on a tag use `__iexact` for the same reason. This matters most where a human types the tag: `~force major vets` has to reach the guild the sweep cached as `VETS`, or the override silently applies to nothing. A tag containing a space needs quoting at the call site (`~force major "My Guild" 3mo`), which discord.py's argument parser handles.

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

**Contact roles are gated on major-guild status and on representation** (`sync_contact_roles`, driven by §12's reconcile), and the two gates resolve differently on purpose.

**A guild that isn't major keeps its slots.** It has dropped out of the Hall for now, so its four contact roles are withdrawn and handed back — to the same people — when it returns. The `GuildContact` rows don't move: major-guild status decides who may *use* a contact role, the row decides who holds the slot, and clearing them would cost a returning guild four re-verifications and lose the record of who to hand the roles back to. Nobody is kicked for it either — the kick is what happens when you lose your slot to someone else, not when your guild has a quiet quarter.

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
- **Wynnpool** is unauthenticated, and its rate limit is far more forgiving than Wynncraft's. Its guild endpoint mirrors the same territory and war numbers, so `external.guild_stats(name, tag)` asks it first and falls through to Wynncraft — the authority — on any failure, *including* a 404, since Wynnpool only knows guilds it has indexed. Wynncraft errors propagate rather than being swallowed: a 429 recorded as "no territories, no wars" would read as a guild silently losing its major_guilds. Wynnpool addresses guilds by name only, so a tag with no known name goes straight to Wynncraft's prefix route.

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

## 12. Reconciling against major-guild status

**Status:** implemented (Stage 8; standing roles and the guild watch in Stage 9)

`services/transitions.py` holds a **reconcile**, not a set of edge-triggered transition handlers: it reads what's major now and makes Discord match, rather than diffing against a remembered previous state. That makes the pass safe to run repeatedly, lets it heal a server that drifted while the bot was down or while an edit 403'd, and means there's no "previous state" record that can itself go stale. It runs after each major-guild sweep (one scheduler job, so the reconcile reads the numbers the sweep just wrote) and on demand via `~script reconcile`.

Only guilds with a *presence* are visited — a role we created, a live delegate, or a claimed contact slot. The cache knows a couple of hundred guilds and all but a handful have nothing here to reconcile; iterating the cache would mint a role for every guild in Wynncraft. Tags are deduplicated case-insensitively, or two spellings of one guild would take turns undoing each other. One guild's failure is logged and skipped rather than ending the pass.

Per guild it settles three things: each representative's standing, the contact roles (§6), and the aesthetic role.

### 12.1 Standing

Every representative wears exactly one of three roles, and the reconcile both grants the right one and strips the other two — a member holding two of them reads as two different answers to the same question.

| Standing | When |
|---|---|
| **Delegate** | their guild is major and they're still in it |
| **Relegate** | their guild isn't major |
| **External Relegate** | they've moved to a *different* guild |

Moving guilds outranks major-guild status: a representative who has left can't be promoted back by their old guild having a good month. An external representative also comes out of the guild's aesthetic role and **vacates their contact slots** — wearing `VETS` while playing for someone else misinforms every other guild in the room, which is the one thing the colour exists to get right, and the same argument applies to being listed as who to ask about that guild. The slot is given up rather than withheld; see §6 for why holding one you cannot use is worse than not holding it.

**Nobody is kicked and no row is deleted for any of this.** Guilds are expected to move in and out of the Hall, and the point of keeping the `Delegate` row is that coming back costs nothing — no re-verification, and the same people get their slots back.

Note the asymmetry the design brief asks for: being *guildless* is not being external. Someone between guilds is still the person their guild sent, and the alternative relegates anyone who leaves for an afternoon. Only actively sitting in a different guild counts.

### 12.2 The guild watch

Wynncraft pushes nothing, so guild membership is polled: `delegate_registry.refresh_current_guilds` asks for each live delegate's current guild once an hour (one request each, serialised on the player bucket) and writes it to `Delegate.current_guild_tag`. `guild_tag` — the guild they verified as a representative of — never changes; the two differing is the whole definition of external.

The poll only gathers facts; the reconcile that follows in the same job acts on them. That split is what keeps the standing decision in one place and lets `~script guild_watch` + `~script reconcile` reproduce an hour's work by hand. A lookup that fails leaves the stored value alone rather than resetting it: a 429 recorded as "no guild" would read back as the delegate having *rejoined* their guild, quietly promoting someone the last sweep relegated.

A brand-new delegate gets `current_guild_tag` seeded at registration — verification proves they're a chief of that guild right then, so starting them as unknown would only invite a wrong answer until the first poll.

**Whether the poll is working is itself observable.** Keeping the last known guild on failure is right per call and unbounded across calls: sustained 429s or a payload shape change would freeze every delegate's guild and nothing would say so, quietly turning the brief's "noticed within 48h" into "never". `Delegate.current_guild_checked_at` moves **only on a successful answer**, so `delegate_registry.stale_delegates()` can name anyone the watch hasn't reached inside `STALE_AFTER` (48h). The hourly job logs a warning when that list is non-empty and `~script delegate_status` prints it. A delegate who has *never* been checked only counts once they're older than the window — a row written minutes ago simply hasn't had its first sweep, and reporting it would make a fresh verification look like a fault. All of them going overdue at once is the signal that matters: that's the poll failing, not any one account.

**The re-representation gap, and `~force rep`.** An external representative who becomes a chief of their *new* guild can't verify for it: `mint_invite` refuses while their `Delegate` row is live (§3), and releasing the row wouldn't help either, because clicking an invite as an **existing member fires no `GUILD_MEMBER_ADD`**, so nothing consumes the `PendingInvite`. Short of leaving the server, only a direct edit of the row fixes it.

`~force rep <user> <TAG>` is that edit, and it is **not** `~force guild` (§12.3). That one asserts who somebody speaks for temporarily, as an override sitting in front of their row and expiring on its own; this rewrites the row, because it isn't a correction — it's the paperwork for a change that really happened. Monitor-gated and logged at warning level.

Four things it does, each of which would be a bug if left out:

- **It asks Wynncraft rather than trusting the operator.** This is the one place a human asserts something the game is otherwise authoritative on, so chief-hood of the target guild is verified first and the command refuses otherwise — naming the guild Wynncraft *does* have them in, since "you meant SEQ" is more use than "no". Verified before anything is written, so a refusal leaves the member exactly as they were.
- **The old guild's contact slots are vacated without a kick.** They're staying, and nobody took anything off them; the kick belongs to losing a slot to somebody else (§6). `contacts.vacate_holdings` is that path.
- **A live `~force guild` override is dropped.** It sits in front of the row, so leaving one would make the command appear to have done nothing at all — the exact failure shape §12.3's history is a list of.
- **It settles and reports the end state**, rather than leaving standing to the next reconcile as originally planned. Stage 10's structural fix supersedes that: a command whose effect only shows up an hour later is indistinguishable from one that silently did nothing.

There is deliberately no `~unforce rep`. Nothing remembers the previous guild once the row is rewritten, and re-running the command is the undo.

### 12.3 Saying who someone represents — `~force guild`

`~force guild <user> <TAG> <time>` is a janitor asserting **who a member speaks for**, and it outranks both the guild they verified as a chief of and whatever the watch sees. `delegate_registry.represented_guild` folds the three sources into the one answer everything downstream keys on: the tag on their nickname, the colour they wear, whose contact slots they may hold, and whose major-guild status decides their standing. Force someone to `ANO` and they are, for every purpose the Hall has, an ANO representative — including `~force assign`, which gives them ANO's slot rather than refusing.

It solves two shapes of problem:

- **Repointing.** Somebody now speaks for a different guild than the one they verified with, and re-verifying isn't available while their delegate row is live (§12.2). This is the temporary answer; `~force rep` is the durable one, and it drops any override left here.
- **Correcting the watch.** A rep mid-transfer flickers, an alt account shows the wrong guild, a shared account shows whoever logged in last. Forcing the guild they already represent pins it and undoes a wrong External Relegate.

**A forced representative is never external**, because the watch disagreeing is the entire situation being overridden. The override sits *in front of* `current_guild_tag`, never in it: the watch rewrites that column hourly, so a forced value stored there would survive exactly until the next sweep. Expired rows read as absent but are left in place, so a janitor can still see what was forced and when it ran out. Duration gating is the shared one (`time_parse.gating_rejection`): janitors capped at three months, monitors unbounded and allowed `0`.

**A command that applies an override says what it applied.** `~force guild` and `~unforce guild` settle the target on the spot and report the end state — "Now standing `delegate`, wearing `ANO`" — or say plainly that nothing needed changing. That wording exists because three separate bugs reached production wearing the same disguise: the command replied with confident success, the hourly reconcile silently disagreed, and the gap between them read as a delay rather than a fault. A no-op that announces itself is a bug report; a no-op that doesn't is a support ticket a week later.

Two consequences worth stating, because both were bugs first:

- **A member wears exactly one guild role.** The colour is a claim about who they speak for; two of them is two claims. `sync_guild_role_membership` strips every *other* role we created, so a repoint moves the colour rather than adding one. Roles adopted by name are never stripped, for the same reason they're never deleted.
- **The reconcile groups members by who they represent, not by the row they were written with.** Otherwise the repointed member's old guild would settle them, undoing what their new guild's pass just did — and their new guild might have no other presence at all, so `guilds_present` counts forced tags too, and `settle_members` mints the role on demand.

### 12.4 The aesthetic role

Three outcomes:

- **Recoloured** — major, so it carries the Athena hue.
- **Greyed** — not major, but people still wear it. Deleting it would rewrite every past `@TAG` in the channel history to `@deleted-role`; the colour going away says the same thing reversibly.
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

`services/roster.py` keeps one channel — `ROSTER_CHANNEL_ID`, the Current Guilds channel — holding a list of every major guild with its four contacts. It's how the Hall is legible without knowing a delegate first: the answer to "who do I ask about housing in Sequoia?" is a channel rather than a DM chain. Each guild reads as its banner emote, its full name, its tag, and one line per contact slot with `unclaimed` where nobody holds it.

Like §12 this is a **reconcile**, not a set of edge-triggered edits. `sync_channel` renders what the channel should say and makes it say that; nothing tracks what changed, so there's no event that can be missed and a channel somebody mangled by hand heals on the next pass. It runs at the end of the hourly job, after the reconcile, so it publishes what that pass just settled — and that hourly run is the **backstop** for the event-driven calls below, which means a missing hook costs an hour of staleness rather than permanent drift.

Three properties of Discord's message model shape the whole sync:

- **Messages can't be reordered or inserted.** A roster spanning eight messages is eight messages in send order, so replacing the third means re-sending the third onward. `sync_channel` edits the longest intact prefix in place and rebuilds from the first gap — a message deleted by hand, a run that died mid-send, or the channel simply disagreeing with our recorded order, which is resolved in the channel's favour because that's what readers see. `RosterMessage` records position and message ID for exactly this walk.
- **2000 characters.** Guilds are packed into messages without ever splitting one across a boundary — half a guild's contacts at the bottom of one message and half at the top of the next reads as two guilds. The cost is that one guild gaining a contact can reflow everything below it, which is the case the prefix-and-rebuild walk exists for.
- **Editing is cheap, sending is not.** A message whose content already matches is left strictly alone. Getting this wrong is visible: an "edited" marker would appear on the entire roster every hour.

**Nothing else may post there.** Any message in the channel that isn't a tracked roster message of ours is deleted — somebody talking in the wrong place, or the residue of a pass that died between sending a message and recording it. The second is why it can't simply skip messages by other authors: an orphan of our own would otherwise sit above the roster forever. This needs **Manage Messages**.

### 14.1 What it lists, and in what order

Order is Wynnpool's `guildLevel` board, rank 1 first, with guilds absent from it after the ranked ones alphabetically. Both the rank and the guild's full name are columns on `MajorGuildCache`, written by the hourly sweep from leaderboards it already holds — so **rendering the roster costs no third-party request at all**, and the ordering is exactly as fresh as the major-guild status behind it. The name matters because nothing else in the bot knows a guild by anything but its tag.

A guild can be major without appearing on a board that carries a prefix, though — delegate guilds and forced tags join the candidate set on their own, and season boards publish no prefix at all — so those have no name from the sweep. `VETS` is exactly that case, and the roster printed it as `**VETS** (`VETS`)` until `major_guilds._learn_missing_names` asked for it. The lookup is `external.guild_name_for`, which is **Wynncraft-only**: Wynnpool addresses guilds by name and publishes no prefix route, so it can't answer a tag→name question. A name found once is cached, so a guild costs one lookup ever; a tag nothing knows is re-asked each sweep, which is one request an hour and starts working the day the guild becomes real. The same fill-in feeds signal 3, which matches season boards by name and so could never fire for a guild whose name we'd never learned.

Candidates are the major-guild cache plus live `major` force overrides, answered in bulk (`major_guilds.active_major_overrides`) rather than by asking `is_major` per guild: that call falls through to a live evaluation on a cache miss, and a render is not the place to discover a guild needs twenty leaderboard fetches. The override half is what makes `~force major NEWG 3mo` appear immediately — an entry that waited for the next sweep would read as the command not having worked. Tags fold case, so `VETS` and `vets` are one entry.

A slot whose holder no longer speaks for the guild prints as `unclaimed`, and by the time the roster next renders the reconcile has normally vacated the row as well (§6) — the check here covers the seconds in between, so the roster never names somebody the next pass is about to remove. A holder who has left the server also reads as unclaimed, but their row *does* stay: leaving isn't the same as changing sides, and coming back costs no re-verification.

The banner emote is resolved by **name**, per guild tag, falling back to a shared `:Empty_Banner:` and then to a plain unicode flag. Stage 12 mints the per-guild ones; until then every guild wears the placeholder, and a server missing even that still renders — a missing emote must not be the thing that stops the roster.

### 14.2 When it redraws

Besides the hourly pass, `roster.request_sync(guild)` is called from the join listener, the new `on_member_remove` listener, `~script refresh_major`, and — via `ForceGroup.cog_after_invoke` — **every `~force`/`~unforce` sub-command that runs cleanly**. That last one is on the group rather than on each sub-command deliberately: hooking them individually is how `~force guild` and `~force major` shipped without one, and the symptom was the worst kind — the command reported success, the roster silently disagreed, and the gap read as lag rather than a fault (the same shape as the four bugs in §12.3's history). A sub-command added later is covered without anyone remembering to. It is **debounced** (`SYNC_DELAY_SECONDS`): a verification claims up to four contact slots and then sets a nickname, and one redraw a few seconds later costs a single channel read instead of five, landing after the join has settled rather than partway through it. It's fire-and-forget — nothing a caller does should fail because a channel couldn't be redrawn.

**The roster never notifies anyone.** Contacts are named with a real `<@id>`, so an entry stays a live link to the person, but every message is sent *and edited* with `AllowedMentions.none()`. The channel is redrawn on every join, leave, force and hourly sweep, and four contacts a guild being pinged each time would make it unreadable within a day. Note this suppresses the notification only — the mention still renders as their name in the usual highlight. The `edit` half is the easy one to miss: an edit that omits `allowed_mentions` falls back to the default and pings.

`~script roster` runs the same pass on demand. Like `~script reconcile` it reads the major-guild cache rather than refreshing it, so `~script refresh_major` comes first if that matters.

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

**Which guilds get one is decided by how strongly each is major, not by roster order.** Major-guild status is a yes/no, but a guild qualifying on four signals is more securely part of the Hall than one scraping in on a single leaderboard — so when there are more major guilds than slots, the placeholder falls to the ones qualifying by the least. `major_guilds.strength` sorts on the number of matched signals first, then breaks ties with the numbers behind them (`MajorGuildCache.metrics_json`, written by the same sweep), signal by signal in §4's order. An unmatched signal contributes zero, so there's no credit for *nearly* qualifying; rank-based signals are inverted, since rank 1 is the strongest and is the one place a raw value sorts exactly backwards. A guild with no cached measurement — a fresh `~force major` — sorts last, because nothing about it justifies displacing a guild we've actually measured, and the tag is the final tiebreak purely so the order is stable between passes.

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

## 16. Expulsion

**Status:** implemented (Stage 13)

The Hall can remove a guild from itself. `~expel_motion <TAG>` puts it to the delegates, and if 51% of them vote yay the guild's representatives are removed from the server and the guild is barred from coming back. `services/expel_motion.py` owns the vote; `services/expel.py` owns the ban and the removal, so a monitor's `~force expel` (Stage 15) can reach the same end state without one.

### 16.1 Who votes

**Guilds vote, not people.** `ExpelVote` is unique per `(motion, guild)` and a later vote from any of a guild's representatives replaces the earlier one — a guild that sent three people is still one guild. The ballot says so out loud when it happens, because a representative finding out by accident that a colleague overrode them is worse than being told.

**The electorate is the guilds *seated* in the Hall**, not every guild the major-guild cache marks major. That distinction is load-bearing rather than pedantic: the cache knows around forty major guilds and a handful have ever sent anyone, so a 51% bar against the whole set could not be cleared by unanimity of everyone present. Seated means at least one `Delegate` row that hasn't left, whose member is still in the server, and whose standing works out to `delegate` — the same line §6 draws for holding a contact slot, and for the same reason: a relegated or external representative isn't currently speaking for a guild in the Hall.

**Abstention counts against.** The bar is 51% of the electorate, not of those who turn out, so a motion carried by three guilds out of twenty doesn't pass. Removing a guild should need the Hall to want it, not merely to fail to object. The live post says so in as many words, because a voter deciding whether to bother needs to know that not voting *is* a vote.

**The accused guild doesn't vote, and isn't in the denominator.** Sitting on the jury for your own trial is the obvious problem; leaving them in the denominator as a guaranteed non-yay is the quiet version of it, silently raising the bar for everybody else.

The threshold is integer arithmetic — `yay * 100 >= 51 * electorate` — not a float comparison against `0.51`. That constant has no exact binary representation, so a float would decide the 51-of-100 case by rounding error rather than by the rule, and 51 of 100 is precisely the case somebody will eventually want to argue about.

### 16.2 The electorate moves under the motion

Guilds gain and lose major-guild status, verify, and leave while a vote is open. The tally is therefore recomputed against the *current* electorate every time it's read, and a vote from a guild that has since lost its seat stops counting — it shouldn't still be pushing a motion along behind it. Two consequences, both of which shape the code:

- **A motion can pass with no new vote**, if the electorate shrinks under a standing yay count. So resolution is checked on the hourly sweep as well as after each button press; were it only the button, a motion could sit carried and unnoticed until somebody happened to vote again.
- **There is deliberately no early failure.** "This can no longer pass" isn't a stable fact when the denominator can shrink, so a motion ends by carrying or by lapsing at its deadline (`EXPEL_MOTION_DAYS`, default 7) — never by being declared dead while it might still recover. Carrying is checked *before* the deadline, so a motion that reaches the bar on its final day carries rather than lapsing on a technicality.

The split that carried a motion is recorded on the row at resolution rather than recomputed, for the same reason: re-deriving it a week later would answer with today's guilds instead of the ones who actually voted.

### 16.3 Anonymity

**The bot never says who voted which way, or who moved it.** The live post carries turnout and the bar — "9 of 20 guilds have voted · 11 yay needed" — and the split is published only once the motion is resolved. Nothing anywhere names the mover or the guild they moved for.

Neither is squeamishness. A running yay counter next to a member list is enough to infer individual votes, which would make the vote anonymity decorative; and a named mover turns "the Hall is considering this" into "these people came for you", which is a thing guilds would retaliate over and a reason not to move a motion that ought to be moved. The tests are written as indistinguishability rather than absence — two motions with the same turnout and opposite splits must render *identically*, and no rendered string may contain the mover's tag or ID — because "the number isn't printed" is a weaker claim that a reworded sentence can quietly break.

`~script motions` shows a monitor exactly that and no more. Anonymity a staff command quietly opts out of isn't something anyone can rely on. What it adds is the **electorate itself** — which guilds are seated, and so entitled to vote — since that's the number a motion's fate turns on, and nothing else could answer why a motion was stuck needing seven yays in a Hall that felt smaller than that.

Note that anonymity is a property of what the bot *renders*, not of what it stores. The rows record who moved and who cast what: something unauditable is a worse problem than something unpublished.

**The result bar is unattributed, and that's the decision, not the default.** A resolved motion shows its outcome as one row of squares — yay, then silent, then nay — sorted by outcome and carrying no identity at all. `services/guild_bar.py` can render the *attributed* form, each guild's banner emote directly over its own square, and it is genuinely clearer:

```
:SEQ::Aeq::TAq::AVO::PUN::ANO:
🟩🟩🟩⬜⬜🟥
```

It is not used here. That row is a permanent, screenshottable record of which guild voted to remove which other guild, and the entire point of settling this by vote is that a guild can leave without anybody being left with somebody to blame. The clarity is real and the cost is a grudge that outlives the argument. The module exists so the attributed form is available to everything *else* — turnouts, sign-ups, territory splits — where telling the guilds apart is the point rather than the hazard.

The bar appears **only on a resolved motion**. One that grew as votes arrived would let anyone watching correlate a new square with whoever happened to be online, which is precisely the leak the turnout-only rule closes.

### 16.4 The ballot, and the one ping

**`~expel_motion` is DM-only**, and that follows directly from the mover being anonymous: running it in a channel names them to everyone who can read it, and nothing the bot does afterwards takes that back. So a public invocation is refused, the message deleted if we have Manage Messages there, and the explanation sent by DM rather than posted underneath — a reply saying "motions are anonymous" under a visible one only draws eyes to it. When the delete fails the DM says so outright, because being told nothing would leave the mover thinking they were covered. Anonymity you have to remember to use isn't anonymity.

The command still posts nothing on its own: it answers with what a motion does and a button, and re-checks every precondition when that button is pressed, since minutes can pass and in them a monitor can ban the guild or another motion can open.

The ballot is two buttons under one message with fixed `custom_id`s; the *message* identifies which motion they belong to. That's what lets one registered view serve every motion, including ones opened before the last restart — per-motion custom_ids would need re-registering on boot, and buttons that went dead after a deploy would be indistinguishable from a bot that had stopped working. Every press replies **ephemerally**, whatever the outcome, including every rejection: a button that does nothing visible is the same to the voter as a bot that's down.

**The motion post notifies nobody.** One member deciding to ping the server is not something this bot permits — people leave servers over stray pings, and a motion nobody else has backed is one member's opinion, not the Hall's business. Instead the Hall is called to a motion **once**, with an `@here` in the delegate channel (`DELEGATE_CHANNEL_ID`), and only once `ANNOUNCE_AT_YAY` — three — guilds have voted yay. At that point the vote is genuinely live and everyone's attention is fairly claimed.

Three conditions on that announcement, and the last two matter as much as the first: the motion must still be **open** (a carried motion needs no audience and a lapsed one has none), and it must not already have been announced — hence `ExpelMotion.announced_at`, since "three guilds are behind it" stays true for the rest of the vote and would otherwise fire on every hourly pass. The row is stamped only once the message is actually out, so a send that failed is retried next pass rather than silently spending the one announcement a motion gets. The message says "at least three guilds" rather than the live count: the trigger is public and the two are the same number in every case but a shrinking electorate, so printing the count would leak the tally to buy nothing.

An unset `DELEGATE_CHANNEL_ID` means motions are never announced. They still run, still resolve, and still remove a guild — the ping is how people find out sooner, not part of the mechanism.

This is the **only** `@here` or role mention the bot ever sends. The roster must never ping (§14.2) because it's redrawn hourly; this one is rare, gated on other people's support, and capped at one per motion.

### 16.5 Where a ban bites

A ban is one `ExpelBan` row. There's no suspended or partially-expelled state, and lifting it is a delete: the guild's former representatives verify again from scratch, which is what returning means for anyone else who left. Nothing invents a restore path, because a "restored" delegate is a state nothing else in the Hall can reach. `~unforce expel` says so in its reply rather than leaving a monitor to discover it.

**`~force expel <TAG>`** reaches the same end state without a vote — same ban row, same removal, same cleared slots — and is monitor-only and logged at warning level. It exists for what a vote can't answer: a guild that has to go now, and one that never had a seat and so could never be moved against (`~expel_motion` requires a *seated* target, §16.1). Against a guild with no presence here it removes nobody and says so, because barring a guild before it ever arrives is an ordinary thing to do and has to read as done rather than as a command that failed.

It also **closes any open motion** against that guild, recorded as `superseded` — not `passed`, which claims a vote that never happened, and not `lapsed`, which says the Hall declined. Leaving it open would put live buttons under a question nobody can answer any more, about a guild that has already gone, and that motion could still reach the bar later and re-run the removal.

It has to hold in five places, and all five, or a removed guild finds its own way back:

- **The `/join` lookup** — the Hallway page is where a chief starts, so a ban that only bit later would walk them through picking roles and generating a code before anything told them. Note the `reason` string is a **contract**: `lookup.js` branches on the exact values, and one added without a matching branch falls through to "not chief or owner of a major guild", which for an expelled guild is both wrong and maddening.
- **The verify route** — a chief of a banned guild is answered in chat and no invite is minted. Both this and the lookup check *before* major-guild status, deliberately: an expelled guild can be perfectly major, and telling a chief their guild "isn't major" sends them off chasing leaderboards over a decision the Hall made about them.
- **The join listener** — an invite lives ten minutes, so one minted just before the vote carried is still redeemable. The redemption is checked too and the member removed on arrival. The `PendingInvite` goes with them rather than being left for the sweep: unlike a failed role application this isn't a state a retry improves, and leaving it would have the sweep revoke an invite already consumed.
- **The roster** — a banned guild isn't listed whatever its major-guild status says. Expulsion is about welcome, not significance, and the channel is a list of who's *in* the Hall.
- **The hourly sweep** — `expel.enforce` re-removes anyone speaking for a banned guild. That's the backstop for the other three and for everything nobody thought of: a kick that came back 403, a `~force guild` pointed at a banned tag, a member who joined while the bot was down. Every one of those is a guild the Hall voted out quietly walking back in, and none of them is an event that could have been hooked. Same reconcile shape as §12 and §14.

Removal goes by who *represents* the tag, not by the tag on the row — `~force guild` can repoint that, and settling by the row would miss a repointed member while catching one who has moved on. The ban is written **first**, before anybody is removed: a removal that dies partway still leaves the door shut, whereas the other order leaves a window in which the representatives are gone and the door is open. Kicks that fail are logged and counted rather than raised, and `Delegate` rows are marked left rather than deleted — the history survives, and `mint_invite` reads the same column.

**Needs Kick Members**, which §6 already required; **Manage Messages** on any channel a motion might be typed in, to scrub a public invocation; and **Mention @everyone** on the delegate channel, or the one `@here` renders as plain text. It needs `NOTIFICATIONS_CHANNEL_ID` set — without it `~expel_motion` refuses rather than posting a vote somewhere only the mover can see — and `DELEGATE_CHANNEL_ID` if the Hall is ever to be called to one.

## 17. Looking after the process — `~manage`

**Status:** implemented (Stage 15)

Everything under `~force` changes what the Hall *is*. `~manage` changes what the process is doing, and the two are kept apart so an audit log reads properly: `~force expel` is a decision, `~manage reload_cogs` is maintenance. Monitor-only throughout.

`~manage refresh_major` and `~manage resync_roster` **delegate to the `~script` modules** rather than reimplementing them. They have to mean the same thing as `~script refresh_major` and `~script roster`, and one of them throttling its progress edits while the other doesn't is exactly the divergence nobody notices until an operator reports different behaviour from two commands that sound identical. The scripts own the work; these are aliases with a name a monitor can guess.

`~manage reload_cogs` re-imports every cog without restarting. It **names the ones that failed**, because a partial reload leaves the process running some old code and some new, and which is which decides whether the next thing you try proves anything at all. A module with no `setup()` isn't a failure, just not a cog.

`~manage shutdown` exits, and the container's `restart: unless-stopped` is what brings the bot back — exiting *is* the restart. Two details:

- **It drains the roster first.** `roster.request_sync` is debounced and fire-and-forget, so exiting seconds after a `~force` would drop a redraw nothing else is going to make until the next hourly pass.
- **It sends itself `SIGTERM` rather than calling `sys.exit`.** Uvicorn is serving on another task in the same event loop and `bot.close()` doesn't stop it. `SIGTERM` is what `docker stop` sends, so the process takes its ordinary shutdown path rather than a novel one only this command exercises.

The reply says what's *expected* to happen rather than promising the bot will be back, since nothing in the process re-launches it if that restart policy is ever removed.

## 18. Observers, and the janitor's own voice

**Status:** implemented (Stage 14)

### 18.1 Observers

The Hall is a room of guild representatives. An observer is the exception a janitor makes by hand — a Wynncraft admin, a partner community's organiser, somebody helping run an event. They get in and they get to read, and that is the whole of it: no delegate standing, no contact slots, no guild colour, no tag on their nickname.

**The mechanism is an absent `Delegate` row**, and that's the design rather than an omission. The guild watch iterates delegates; the reconcile settles delegates; the roster names contacts; `nicknames.enforce` returns early for anyone the Hall has no row for. So an observer is invisible to every hourly pass *by construction*, instead of every pass carrying an "unless they're an observer" branch that somebody eventually forgets. `nicknames` also checks the observer role explicitly, which covers the one case the missing row doesn't: a former representative handed the observer role by hand still keeps their own name.

`~force observer <username>` mints the invite. Two things about its shape:

- **`roles_bits` carries a sentinel, `role_bits.OBSERVER` = `-1`.** An empty role set would decode indistinguishably from `HALL00` — a delegate who asked for no contact roles — and the two need telling apart. `-1` is unreachable from the MC side by construction, since a `HALL<NN>` code parses to 0–99, so a negative value can only ever have been written by this command. `decode` **raises** on it rather than returning an empty set, so anything that treats an observer invite as a role set fails loudly instead of quietly applying nothing; callers ask `is_observer` first.
- **`guild_tag` is the reserved `NONE`**, borrowed from the blank banner (§15.4) for the same reason: the column isn't nullable, an observer has no guild, and Wynncraft reserves that tag so no real guild can collide with it. The join listener checks for an observer **before** it checks the expel ban, because "is `NONE` expelled?" is a question about nothing — and in the other order a stray `~force expel NONE` would silently turn away every observer, for a reason nobody would find.

The invite is still keyed to a Minecraft account, because that's what `PendingInvite` is keyed on. It proves nothing — no chief check, no guild — the janitor's word is the whole authority, hence janitor-gated and logged at warning level.

**That binding is kept**, in an `Observer` row, rather than dying with the invite. It's a separate table rather than a flag on `Delegate` for the reason above: a flagged row would be seen by every pass and each would need its own exception. Two things depend on the record existing, and the second is why it was added:

- The bot can say who an observer is a year later, and `~script standing` answers for them properly instead of "no delegate row" — which reads as the bot having lost track of somebody it deliberately doesn't track.
- **An observer who becomes a chief can be recognised.** Without the record this was an outright bug rather than a gap: they have no `Delegate` row, so they sail past `mint_invite`'s guard, get handed an invite, click it — and *nothing happens*, because an existing member joining fires no `GUILD_MEMBER_ADD` so the `PendingInvite` is never consumed. No error, no explanation, and `~force rep` refused them too. Now `mint_invite` raises `AlreadyObserving`, the MC-time reply names the command to ask for, and **`~force rep` promotes them**: same Wynncraft chief check as a re-point, run against the Minecraft account the record holds. The observer role comes off and the record goes, because the two states are exclusive — holding both would mean being counted by the reconcile *and* skipped by the nickname enforcer.

`~unforce observer` cancels an unused invite, and — once one has been used — stands the observer down instead: role off, record gone, **no kick**. Removing what somebody was given isn't the same as removing them (§6), and a janitor who wants them gone can kick them, which is separate and visible. Leaving the server drops the record outright rather than marking it left, unlike a `Delegate` row: that row is kept so a return costs no re-verification, whereas an observer binding is about nobody once the account has gone.

**It lives a week, not ten minutes**, and that needed a schema change. The MC flow's ten-minute `max_age` is right for a code typed in-game by somebody already at the keyboard and useless for one a janitor has to paste into a DM and then wait on. So `PendingInvite.expires_at` records a per-row lifetime, NULL meaning the default — which leaves every MC-minted row behaving exactly as before. **Both** the sweep and the used-invite matcher read it, because they have to agree: a row swept early is a join that can't resolve, and a matcher judging a week-long invite by the ten-minute window reads a perfectly live one as long expired and refuses the join. That second half is the easy one to miss, since nothing fails until somebody actually redeems an old invite.

`~unforce observer` cancels an unused one. It **refuses** to touch a representative's invite: that's somebody's verification in flight, and letting a typo here cost them a re-verification — for a reason they'd have no way to see — is exactly the failure shape §12.3 is a list of.

### 18.2 Speaking as the bot

Four commands, in two pairs. All of them say something as the bot and delete the invoking message; the reason is the room, since an announcement in a person's name carries their guild's weight with it, and in a hall of rival guilds that's often not what's wanted.

| | janitor | monitor |
|---|---|---|
| message | `~silent_echo` (alias `~echo`) | `~noisy_echo` |
| panel | `~silent_embed` (alias `~embed`) | `~noisy_embed` |

**Two commands rather than one with a flag.** Naming a role in a notice is routine; waking three hundred people is not, and a single command has to guess wrong half the time. The tiers follow that cost — pinging the server is a monitor's call, the same judgement §16.4 makes when it gates the expel announcement on three guilds agreeing. `~echo` and `~embed` alias the **silent** ones, so the short name anybody reaches for first is the one that can't wake the room. A silent command still *renders* mentions — `@Guild Hall Delegate` looks like itself and links through — it simply doesn't ring.

**Mentions inside an embed never notify anybody**, and that's Discord rather than a choice here: notifications are raised from a message's *content*, and an embed's body is not content. A `@here` in a description renders as `@here` and rings for nobody whatever `allowed_mentions` says. So a `~noisy_embed` that only relaxed the mention rules would be a command claiming to ping and silently not doing it — the exact failure shape §12.3 is a list of. Hence **`ping=`**, whose text is sent as the message content above the embed, where notifications actually come from. `~silent_embed` **refuses** `ping=` rather than dropping it, so nobody walks away believing they notified the room.

**The invoking message is deleted last**, only once the post is out. The other order loses what somebody has just written whenever the send fails — and what these carry is usually an announcement that took a few minutes to word. A delete that fails is logged and the post stands; it needs **Manage Messages** in that channel, which §16.4 already wanted for scrubbing a public `~expel_motion`.

`~echo` carries attachments across by re-downloading and re-uploading them, the only way — Discord offers no means of moving an attachment between messages. One that fails is skipped rather than costing the whole echo: a missing image is visible and fixable, a swallowed announcement isn't.

The embed commands take **one-shot `key=value` syntax rather than an interactive prompt**, which the plan left open. A prompt means conversational state per user across messages, and every way out of it — they wander off, they answer with a different command, the bot restarts halfway — is a state somebody has to handle. The command is one line either way, and a line can be edited and re-run when it comes out wrong, which a half-finished prompt can't. Anything left after the recognised keys becomes the description, so `~embed Just a sentence` does the obvious thing; somebody's first use will not have the syntax in front of them. When Discord refuses an embed its own complaint is relayed verbatim — it validates server-side, and "Not a well formed URL" is far more use to the author than "that broke on my end".

## 19. The guild dashboard — `~dash`

**Status:** implemented (Stage 16)

Each guild answers a short set of questions about itself — are you recruiting, how do you apply, when do you war — and a Hallway page renders the answers. `~dash` is how a contact fills them in, `services/dash_schema.py` declares what can be asked, and `services/dash.py` stores it.

### 19.1 Keys are declared, not invented

A contact sets the **value** of a key; they cannot bring a new key into existence. Keys live in `dash_schema.KEYS`, and if a runtime path to add one is ever wanted it is monitor-only — never a delegate.

That isn't tidiness. **The consumer is a template, not a dump.** The page renders these into a layout with headings and labels, so a guild that invented `recruitment_status_2` would produce a value nothing knows how to show, and one that wrote `recruitmentStatus` would silently drop off a comparison the page was trying to draw. Free-form keys make a page that can only ever print whatever it's given, which is a worse page.

It also makes **unset a real answer**. With a fixed set, a guild that hasn't filled something in renders as "unset" — information — where under free-form keys an absent key was indistinguishable from one nobody had thought of. And it retires the original per-guild key cap, which existed to bound something that can no longer grow.

**No lists.** A multi-value answer is a scalar with a convention — comma-separated, one per line — chosen by whoever first has a key that wants one, because that's a decision with a real consumer attached and making it in the abstract fixes a shape before anything has to live in it. Dropping it also removed everything it dragged along: an `add`/`remove` pair, duplicate handling, case-folded entry matching, a length cap on the array, and the "which of the two did you mean" question. `Key.kind` is where `list` slots in if it's ever wanted, and the verbs don't change shape when it does.

Adding a key is one line and no migration. Removing one leaves stored rows **orphaned rather than deleted** — they're skipped on read, so a key can be retired and restored without losing what guilds had written.

### 19.2 The commands, and why the listing is mandatory

`~dash toggle <key> yes|no` for a `bool`, `~dash set <key> <value>` for a `scalar`, `~dash unset <key>` for either.

**A bare `~dash` lists every key, its kind, its description and this guild's current value**, and that is not decoration. With keys declared, the listing is the *only* way to discover what can be set — a command that refuses unknown keys without showing the known ones is unusable. So the unknown-key refusal carries the same list, and a kind mismatch names the command the key *does* take rather than saying "invalid".

Two refusals worth stating:

- **An over-long value is refused, not truncated.** Something silently cut at 512 characters is worse than something that didn't save, because nothing tells the author the end of their sentence has gone. The reply gives both numbers.
- **`unset` on something already unset says so.** A no-op that announces itself is a bug report; one that doesn't is a support ticket a week later (§12.3).

Values land against the guild the invoker **represents**, not the tag on their row — `~force guild` can repoint that, and using the row would let somebody repointed to ANO carry on editing VETS's page. Staff pass the contact gate by nesting, so a janitor with no `Delegate` row reaches the command and is told there's no dashboard for them to edit, rather than having one guessed at.

Storage is one `DashKV` row per `(guild_tag, key)`, holding JSON. **Unset is the absence of a row**, not a stored null, so a guild that never answered and one that answered and then cleared it read identically — which is what the page wants and what `~dash unset` promises. A row whose JSON is unreadable is logged and treated as unset: a page should render a guild with one bad row rather than fail on it.

### 19.3 Nothing is hidden any more

`~dash` was the last command carrying `hidden=True`, the marker for "not built yet" (§7). `tests/unit/test_help.py` carried a list of the remaining ones for sixteen stages; that list is now empty and the assertion has inverted — **no command in the tree may be hidden**. A stub added later has to be deliberate enough to change that test.
