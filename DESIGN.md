# Hall-Monitor — Design

## 1. Process model

Hall-Monitor is a single Python process. Inside it, two asyncio tasks run concurrently:

- The **discord.py bot** (`commands.Bot`, `~` prefix).
- The **FastAPI sidecar** (`uvicorn.Server.serve()` scheduled as a task).

Both share the same Tortoise ORM connection to `hall-monitor.db` under `DATA_DIR`. Running them in one process keeps the DB single-writer and lets sidecar routes call bot methods directly.

Entry point: [`src/hall_monitor/__main__.py`](src/hall_monitor/__main__.py).

## 2. End-to-end join flow

**Status:** implemented end to end (Stages 3–6). Step 8's contact displacement lands in Stage 7, the guild aesthetic role in Stage 8, and the nickname in Stage 10.

1. A representative visits `hall.wynnvets.org/join`, enters their Minecraft username.
2. Hallway's JS calls `GET /api/join/lookup?username=X`. Hall-Monitor resolves the UUID (Mojang, PlayerDB fallback), asks Wynncraft's API whether they're a chief/owner of a notable guild, and returns `{eligible, guild_tag, mc_username, current_contacts_per_role}` for the UI to render. On failure the response carries a `reason` field (`"not chief or owner"` / `"guild not notable"`); unknown username → HTTP 404.
3. The user ticks the contact roles they want. The UI updates a live "type `HALL14` on verify.wynnvets.org" hint, where the digits are a bit-field over the roles (see §5).
4. The user joins `verify.wynnvets.org` in Minecraft and types `HALL14`.
5. Picolimbo matches the `hall` route prefix, strips it, and forwards `GET /api/verify/{uuid}/14` to Hall-Monitor over the `verify` Docker network. That route is deliberately not web-reachable — Hallway's nginx proxies only `/api/join/` — because its sole proof that the requester owns the Minecraft account is that they connected to the limbo server as it.
6. Hall-Monitor parses the code, re-runs the Wynncraft eligibility check (authoritative), mints a single-use Discord invite, and returns it as a `kick_message`. Every *rejection* comes back as a `chat_message` instead, leaving the player connected — being disconnected for a mistyped code means reconnecting just to retry. Success is the one case that disconnects, because the invite has to be readable after the session ends, and nothing on a Minecraft disconnect screen is clickable: the message leads with the bare invite code to type into Discord's join dialog, with the `discord.gg` URL after it for anyone who'd rather paste a link.
7. Picolimbo disconnects the player with that message. The player clicks the invite in their MC client's kick screen.
8. On `on_member_join`, Hall-Monitor resolves which invite was used (see §3.1), applies the delegate role plus the encoded contact roles (kicking prior conflicting holders — see §6), promotes the `PendingInvite` to a `Delegate`, sets the nickname, and ensures the guild's aesthetic role exists.

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

## 5. The code

A representative types `HALL<NN>` on the verify server: a fixed `HALL` marker plus the role-bits integer, zero-padded to two digits. That's six characters — deliberately the same width as a dazebot account-link code, because the verify server prompts for "your code" and shouldn't need to explain which kind.

The two code spaces cannot collide. dazebot draws its codes from `ABCDEFGHJKMNPQRSTUVWXYZ23456789` (visually-confusable characters removed), which contains no `L`, so nothing it issues can begin with `HALL`. `tests/unit/test_mc_command.py` pins that property.

Picolimbo routes on the prefix `hall` — no trailing space, since the code is a single token — and strips it, so `services/mc_command.py` normally receives bare digits. It accepts the marker as well, which keeps a hand-run `curl` identical to what the player typed. That prefix is broad enough to catch ordinary chat starting with those letters, so a line with no digit in it is dropped silently instead of disconnecting someone mid-conversation.

`services/role_bits.py` owns the single source of truth for the digits: `ROLE_BITS: dict[int, str]`. Bit 0 = Events, Bit 1 = Housing, Bit 2 = Warring, Bit 3 = Ownership. Bits 4+ are reserved.

Adding a role is a one-line map addition; old codes stay valid. Codes carrying an unknown bit are rejected with a clear kick message. Hallway builds the same string in JS for its live display — `mc_command.format_code` and `static/js/request_code.js` have to move together.

## 6. Contacts

**Status:** read side implemented (Stage 3); assign/displace/kick lands in Stage 7.

`services/contacts.py` enforces per-role uniqueness within a guild. Assigning a new contact displaces the old one; a delegate who ends up with zero contact roles is kicked from the server. The UI's "conflict warning" on `/join` reads directly from `guild_contact` — the Stage 3 `current_contacts_for_guild(tag)` helper drops rows whose delegate has left the Discord server (cross-checked against `bot.get_guild(...).get_member(...)` when the bot handle is available).

## 7. Cog organisation

`discord_bot/cogs/` is grouped by domain (`general/`, `moderation/`, `force/`, `admin/`, `listeners/`). The auto-loader in `discord_bot/__init__.py` walks the tree and calls `setup()` on any file or package that exposes one. Sub-commands with distinct permissions live in per-file modules inside `cogs/force/`; the group cog imports and registers them at setup time.

Ad-hoc admin scripts live at `cogs/admin/scripts/` — drop a file with `async def main(ctx, *args)` and it's callable as `~script <name>`.

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
