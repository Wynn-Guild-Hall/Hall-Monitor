"""Rendering a guild's Minecraft banner, and spending the emote budget.

The render tests use real pattern SVGs held as fixtures rather than
mocked bytes: the whole design rests on the art being a *mask* — black
shapes whose opacity carries Minecraft's shading — and a fake SVG would
let that assumption pass a test it doesn't hold in production. Both are
small enough to read.
"""

import io
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import httpx
import pytest
from PIL import Image

from hall_monitor.db.models import (
    ForceOverride,
    GuildEmote,
    GuildRole,
    NotabilityCache,
)
from hall_monitor.external import wynnpool
from hall_monitor.services import banner_render, emote_slots, guild_roles, roster

# A solid full-field shape. Everything it covers takes the layer colour.
SOLID_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 320">'
    '<rect width="160" height="320"/></svg>'
)

# The shape Wynnpool actually ships: black, with an opacity class. This
# is the shading, and it has to survive as partial coverage.
HALF_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 160 320">'
    "<defs><style>.cls-1{opacity:0.5;}</style></defs>"
    '<rect class="cls-1" width="160" height="320"/></svg>'
)


def _banner(base="WHITE", layers=()):
    return wynnpool.Banner(
        base=base,
        tier=1,
        layers=tuple(wynnpool.BannerLayer(colour=c, pattern=p) for c, p in layers),
    )


def _pixels(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGBA")


def _centre(png: bytes) -> tuple[int, int, int, int]:
    image = _pixels(png)
    return image.getpixel((image.width // 2, image.height // 2))


@pytest.fixture
def patterns(monkeypatch):
    """Serve pattern art from a dict; an absent key is a Wynnpool 404."""
    art = {}

    async def fake(pattern, *, urgent=False):
        return art.get(pattern)

    monkeypatch.setattr(wynnpool, "banner_pattern", fake)
    return art


# --------------------------------------------------------------------------
# The dye table
# --------------------------------------------------------------------------


def test_every_dye_wynncraft_returns_is_known():
    """The set observed across the whole guild-level leaderboard. `SILVER`
    is the one that matters — it's what the API says, and Wynnpool's own
    lookup is keyed on `LIGHT_GRAY`, so their site renders it as nothing."""
    observed = {
        "BLACK", "BLUE", "BROWN", "CYAN", "GRAY", "GREEN", "LIGHT_BLUE", "LIME",
        "MAGENTA", "ORANGE", "PINK", "PURPLE", "RED", "SILVER", "WHITE", "YELLOW",
    }
    assert observed <= set(banner_render.DYES)


def test_silver_and_light_gray_are_the_same_dye():
    assert banner_render.dye("SILVER") == banner_render.dye("LIGHT_GRAY")


def test_dye_names_fold_case():
    assert banner_render.dye("red") == banner_render.dye("RED") == (0x99, 0x33, 0x33)


def test_an_unknown_dye_is_loud_rather_than_invisible(caplog):
    """A transparent hole would read as a rendering bug; magenta reads as
    "a dye nobody mapped", which is what it is."""
    assert banner_render.dye("TEAL") == (0xFF, 0x00, 0xFF)
    assert "unknown dye" in caplog.text


# --------------------------------------------------------------------------
# Compositing
# --------------------------------------------------------------------------


async def test_a_bare_banner_is_its_base_colour(patterns):
    png = await banner_render.render_banner(_banner(base="RED"))
    assert _centre(png)[:3] == (0x99, 0x33, 0x33)


async def test_a_layer_paints_its_own_colour_not_the_arts(patterns):
    """The art is black. If black comes out, we're drawing the picture
    rather than using it as a mask, and every banner is a silhouette."""
    patterns["SOLID"] = SOLID_SVG
    png = await banner_render.render_banner(
        _banner(base="WHITE", layers=[("BLUE", "SOLID")])
    )
    assert _centre(png)[:3] == (0x33, 0x4C, 0xB2)


async def test_partial_opacity_survives_as_partial_coverage(patterns):
    """Minecraft's shading lives in the art's opacity. A half-alpha layer
    has to blend with what's under it, not replace it."""
    patterns["HALF"] = HALF_SVG
    png = await banner_render.render_banner(
        _banner(base="WHITE", layers=[("BLACK", "HALF")])
    )
    red = _centre(png)[0]
    assert 0x19 < red < 0xFF, "neither the base nor the layer, but between them"


async def test_layers_stack_in_order(patterns):
    patterns["SOLID"] = SOLID_SVG
    png = await banner_render.render_banner(
        _banner(base="WHITE", layers=[("RED", "SOLID"), ("BLUE", "SOLID")])
    )
    assert _centre(png)[:3] == (0x33, 0x4C, 0xB2), "the last layer is on top"


async def test_a_pattern_wynnpool_doesnt_publish_is_skipped(patterns, caplog):
    """GLOBE, PIGLIN and both DIAGONAL_UPs 404 today. A guild wearing one
    still gets a banner from its other layers."""
    patterns["SOLID"] = SOLID_SVG
    with caplog.at_level(logging.INFO):
        png = await banner_render.render_banner(
            _banner(base="WHITE", layers=[("BLUE", "SOLID"), ("RED", "GLOBE")])
        )
    assert _centre(png)[:3] == (0x33, 0x4C, 0xB2)
    assert "no art published for GLOBE" in caplog.text


async def test_unrasterisable_art_costs_one_layer_not_the_banner(patterns, caplog):
    patterns["BROKEN"] = "<svg not actually xml"
    patterns["SOLID"] = SOLID_SVG
    png = await banner_render.render_banner(
        _banner(base="WHITE", layers=[("BLUE", "SOLID"), ("RED", "BROKEN")])
    )
    assert _centre(png)[:3] == (0x33, 0x4C, 0xB2)


# --------------------------------------------------------------------------
# The emote canvas
# --------------------------------------------------------------------------


def _banner_box(image):
    """Where the banner artwork sits, inside the frame."""
    height = banner_render.EMOTE_SIZE - 2 * banner_render.FRAME_MARGIN
    width = round(height * 160 / 320)
    left = (banner_render.EMOTE_SIZE - width) // 2
    return left, banner_render.FRAME_MARGIN, width, height


async def test_the_banner_is_letterboxed_not_stretched(patterns):
    """A banner is 1:2 and an emote is square. Stretching it produces a
    different banner, and recognising it is the entire point."""
    png = await banner_render.render_banner(_banner(base="RED"))
    image = _pixels(png)
    left, top, width, height = _banner_box(image)

    assert image.size == (128, 128)
    assert image.getpixel((64, 64))[:3] == banner_render.dye("RED")
    assert image.getpixel((1, 64))[3] == 0, "transparent outside the frame"
    assert height == 2 * width, "1:2 preserved"


async def test_the_frame_covers_no_part_of_the_artwork(patterns):
    """Some patterns draw right to the banner's edge — `BORDER` is a ring
    around it — so the frame has to sit outside, not on top."""
    patterns["SOLID"] = SOLID_SVG
    png = await banner_render.render_banner(
        _banner(base="BLUE", layers=[("BLUE", "SOLID")])
    )
    image = _pixels(png)
    left, top, width, height = _banner_box(image)

    for point in (
        (left, top),                       # each corner of the artwork
        (left + width - 1, top),
        (left, top + height - 1),
        (left + width - 1, top + height - 1),
    ):
        assert image.getpixel(point)[:3] == banner_render.dye("BLUE"), point


async def test_a_plain_white_banner_is_visible_on_a_white_background(patterns):
    """`Zamn` really does fly one, and without the frame the roster row
    looks like it simply has no emote on Discord's light theme."""
    png = await banner_render.render_banner(_banner(base="WHITE"))
    image = _pixels(png)
    left, top, _, _ = _banner_box(image)

    edge = image.getpixel((left - 1, top + 20))
    assert edge[3] == 255, "there is a frame"
    assert edge[:3] != (255, 255, 255), "and it isn't white"


async def test_a_blank_guild_banner_is_not_the_no_slot_placeholder(patterns):
    """Zamn's real banner is an empty one. It still has to be tellable
    from "this guild didn't get an emote slot"."""
    theirs = await banner_render.render_banner(_banner(base="WHITE"))
    placeholder = await banner_render.render_placeholder()

    assert banner_render.image_hash(theirs) != banner_render.image_hash(placeholder)


async def test_an_emote_fits_discords_size_cap(patterns):
    patterns["SOLID"] = SOLID_SVG
    png = await banner_render.render_banner(
        _banner(base="WHITE", layers=[("BLUE", "SOLID")])
    )
    assert len(png) < 256 * 1024


async def test_the_same_banner_hashes_the_same(patterns):
    """The hash is what stops an hourly re-upload, and a re-upload moves
    the emote ID and breaks every message already using it."""
    patterns["SOLID"] = SOLID_SVG
    banner = _banner(base="WHITE", layers=[("BLUE", "SOLID")])
    first = await banner_render.render_banner(banner)
    second = await banner_render.render_banner(banner)

    assert banner_render.image_hash(first) == banner_render.image_hash(second)
    other = await banner_render.render_banner(_banner(base="RED"))
    assert banner_render.image_hash(other) != banner_render.image_hash(first)


# --------------------------------------------------------------------------
# Emote slots
# --------------------------------------------------------------------------


class FakeEmoji:
    def __init__(self, emoji_id, name, animated=False):
        self.id = emoji_id
        self.name = name
        self.animated = animated
        self.deleted = False

    async def delete(self, *, reason=None):
        self.deleted = True


class FakeDiscordGuild:
    def __init__(
        self, *, emoji_limit=50, features=(), premium_tier=0, placeholder=True
    ):
        self.emojis = []
        self.emoji_limit = emoji_limit
        self.features = list(features)
        self.premium_tier = premium_tier
        self.roles = []
        self.id = 1
        self._next_id = 900
        # Most cases are about guild banners, not about the blank one, so
        # it starts present and its slot is already spent — otherwise
        # every `emoji_limit` here would have to be read as "one more
        # than the number of guilds I mean".
        if placeholder:
            self.emojis.append(FakeEmoji(1, emote_slots.PLACEHOLDER_NAME))

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_custom_emoji(self, *, name, image, reason=None):
        self._next_id += 1
        emoji = FakeEmoji(self._next_id, name)
        self.emojis.append(emoji)
        return emoji


def _banners(guild):
    """The guild banners live in the list — the blank one excluded, since
    it holds a slot in every case and would otherwise have to be spelled
    out in each assertion."""
    return [
        emoji.name
        for emoji in guild.emojis
        if not emoji.deleted and emoji.name != emote_slots.PLACEHOLDER_NAME
    ]


@pytest.fixture(autouse=True)
def emotes_on(monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.emote_slots.settings.roster_emotes_enabled", True
    )
    monkeypatch.setattr(
        "hall_monitor.services.emote_slots.settings.roster_emote_reserve", 0
    )


@pytest.fixture
def reserve(monkeypatch):
    def _set(n):
        monkeypatch.setattr(
            "hall_monitor.services.emote_slots.settings.roster_emote_reserve", n
        )
    return _set


@pytest.fixture
def banners(monkeypatch):
    """One distinct PNG per guild tag, without going near Wynnpool."""
    async def fake(guild_tag, guild_name):
        if guild_name is None:
            return None
        return f"png-for-{guild_tag}".encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", fake)


async def _make_stale(tag):
    """Age a banner past its re-check window."""
    from datetime import datetime, timezone

    row = await GuildEmote.get(guild_tag=tag)
    row.checked_at = datetime.now(timezone.utc) - emote_slots.RECHECK_AFTER * 2
    await row.save()


async def _notable(tag, name, rank, *, signals=None, metrics=None):
    """A cached guild. `signals`/`metrics` decide how strongly it counts,
    which is what the emote budget is handed out on."""
    await NotabilityCache.create(
        guild_tag=tag, is_notable=True,
        signals_json=json.dumps(signals or {"level_100_plus": True}),
        metrics_json=json.dumps(metrics or {"guild_level": 100}),
        guild_name=name, level_rank=rank,
    )


async def test_the_feature_can_be_turned_off(db, banners, monkeypatch):
    monkeypatch.setattr(
        "hall_monitor.services.emote_slots.settings.roster_emotes_enabled", False
    )
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild(placeholder=False)

    summary = await emote_slots.reconcile(guild)

    assert summary.budget == 0
    assert guild.emojis == [], "not even the blank banner"


async def test_the_budget_is_the_servers_free_slots(db, banners):
    """Members can't add emotes, so the list is ours to fill — and its
    size is the boost level, not a number anyone configured."""
    guild = FakeDiscordGuild(emoji_limit=4)  # one holds the blank banner

    assert await emote_slots.budget(guild) == 3


async def test_a_slot_is_held_for_the_blank_banner_before_it_exists(db, banners):
    """Otherwise the last guild in the budget is minted into the space
    the placeholder is about to need, and fails."""
    guild = FakeDiscordGuild(emoji_limit=4, placeholder=False)

    assert await emote_slots.budget(guild) == 3


async def test_emotes_a_human_uploaded_hold_their_slots(db, banners):
    """Minting into space that isn't there fails on the last few, which
    reads as the mint being broken rather than the list being full."""
    guild = FakeDiscordGuild(emoji_limit=4)
    guild.emojis.append(FakeEmoji(2, "party_parrot"))

    assert await emote_slots.budget(guild) == 2


async def test_animated_emotes_dont_count(db, banners):
    """Discord counts them against a separate pool of the same size."""
    guild = FakeDiscordGuild(emoji_limit=4)
    guild.emojis.append(FakeEmoji(2, "dancing", animated=True))

    assert await emote_slots.budget(guild) == 3


async def test_a_reserve_leaves_an_admin_room_to_upload(db, banners, reserve):
    """A full list means adding one costs deleting a banner, which the
    next pass would put straight back."""
    reserve(2)
    guild = FakeDiscordGuild(emoji_limit=4)

    assert await emote_slots.budget(guild) == 1


# --------------------------------------------------------------------------
# The blank banner
# --------------------------------------------------------------------------


async def test_the_first_run_mints_its_own_blank_banner(db, banners):
    """Nobody should have to remember to upload one for a fresh deploy."""
    guild = FakeDiscordGuild(emoji_limit=4, placeholder=False)

    summary = await emote_slots.reconcile(guild)

    assert summary.placeholder is True
    assert [e.name for e in guild.emojis] == [emote_slots.PLACEHOLDER_NAME]


async def test_a_blank_banner_someone_made_by_hand_is_adopted(db, banners):
    """Found by name, so an operator's own is kept rather than doubled."""
    guild = FakeDiscordGuild(emoji_limit=4)
    theirs = guild.emojis[0]

    summary = await emote_slots.reconcile(guild)

    assert summary.placeholder is False
    assert guild.emojis[0] is theirs and not theirs.deleted


async def test_the_blank_banner_is_never_evicted(db, banners):
    """It belongs to no guild, so nothing can push it out of the budget —
    and the roster's whole fallback rests on it."""
    await _notable("AAA", "Alpha", 1)
    guild = FakeDiscordGuild(emoji_limit=2, placeholder=False)
    await emote_slots.reconcile(guild)

    await NotabilityCache.filter(guild_tag="AAA").update(is_notable=False)
    await emote_slots.reconcile(guild)

    assert [e.name for e in guild.emojis if not e.deleted] == [
        emote_slots.PLACEHOLDER_NAME
    ]


async def test_a_blank_banner_that_wont_upload_is_not_fatal(db, banners, caplog):
    """The roster falls back to a plain flag, which is what it did before
    the placeholder existed."""
    guild = FakeDiscordGuild(emoji_limit=4, placeholder=False)
    guild.create_custom_emoji = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "no slots")
    )

    summary = await emote_slots.reconcile(guild)

    assert summary.placeholder is False
    assert "plain flag" in caplog.text


async def test_the_blank_banner_reads_on_both_themes(db):
    """White vanishes on the light theme and black on the dark one, the
    same problem the guild-role contrast clamp solves."""
    png = await banner_render.render_placeholder()
    red, green, blue, alpha = _centre(png)

    assert (red, green, blue) == banner_render.dye("SILVER")
    assert alpha == 255


async def test_slots_go_to_the_most_strongly_notable_guilds(db, banners):
    """Not roster order. A guild qualifying on three signals has more
    claim on a scarce slot than one scraping in on a single leaderboard,
    whatever the level board says."""
    await _notable(
        "WEAK", "Weakly", 1,  # top of the level board, one signal
        signals={"level_100_plus": True}, metrics={"guild_level": 130},
    )
    await _notable(
        "STRG", "Strongly", 90,  # bottom of it, three signals
        signals={
            "level_100_plus": True, "war_count": True, "territory_ownership": True
        },
        metrics={"guild_level": 101, "wars": 90_000, "territories": 40},
    )
    guild = FakeDiscordGuild(emoji_limit=2)  # one slot after the blank banner

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 1
    assert _banners(guild) == ["STRG"]


async def test_ties_on_count_are_broken_by_the_numbers(db, banners):
    await _notable(
        "LOWL", "Lower", 5,
        signals={"level_100_plus": True}, metrics={"guild_level": 101},
    )
    await _notable(
        "HIGH", "Higher", 6,
        signals={"level_100_plus": True}, metrics={"guild_level": 140},
    )
    guild = FakeDiscordGuild(emoji_limit=2)

    await emote_slots.reconcile(guild)

    assert _banners(guild) == ["HIGH"]


async def test_a_guild_we_have_never_measured_sorts_last(db, banners):
    """A fresh `~force notable` has no cached signals. We know nothing
    that would justify putting it ahead of a guild we've measured."""
    await _notable("MEAS", "Measured", 50)
    await ForceOverride.create(kind="notable", subject="NEWG", expires_at=None)
    guild = FakeDiscordGuild(emoji_limit=2)

    await emote_slots.reconcile(guild)

    assert _banners(guild) == ["MEAS"]


async def test_gaining_a_boost_level_mints_more(db, banners):
    """The boost is a thing somebody paid for and then watches for."""
    for i, tag in enumerate(("AAA", "BBB", "CCC"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=2)
    await emote_slots.reconcile(guild)
    assert _banners(guild) == ["AAA"]

    guild.emoji_limit = 4  # boosted
    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 2 and _banners(guild) == ["AAA", "BBB", "CCC"]


async def test_losing_a_boost_level_evicts_the_tail(db, banners):
    """Discord doesn't remove the overflow — it just refuses every later
    upload, which looks like the mint silently failing."""
    for i, tag in enumerate(("AAA", "BBB", "CCC"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=4)
    await emote_slots.reconcile(guild)
    assert _banners(guild) == ["AAA", "BBB", "CCC"]

    guild.emoji_limit = 2  # boost lapsed
    summary = await emote_slots.reconcile(guild)

    assert summary.evicted == 2
    assert await GuildEmote.all().count() == 1
    assert _banners(guild) == ["AAA"]


async def test_an_unchanged_banner_is_not_re_uploaded(db, banners):
    """Every upload is a new emote ID, so re-minting breaks every message
    already using the old one."""
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    await emote_slots.reconcile(guild)
    first = guild.emojis[-1]

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 0 and summary.refreshed == 0
    assert _banners(guild) == [first.name] and not first.deleted


async def test_a_changed_banner_is_replaced(db, monkeypatch):
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    version = ["first"]

    async def fake(guild_tag, guild_name):
        return version[0].encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", fake)
    await emote_slots.reconcile(guild)
    original = guild.emojis[-1]

    # A changed banner is noticed at the weekly re-check, not on the next
    # pass — the skip is what keeps a settled server off Wynnpool.
    version[0] = "second"
    await _make_stale("VETS")
    summary = await emote_slots.reconcile(guild)

    assert summary.refreshed == 1
    assert original.deleted, "Discord has no replace-in-place for emotes"
    assert (await GuildEmote.get(guild_tag="VETS")).discord_emoji_id != original.id


async def test_a_guild_overtaken_on_notability_is_evicted(db, banners):
    """Nothing recycled emotes, and the boundary moves whenever the
    signals do — a full list makes every later mint fail."""
    await _notable("AAA", "Alpha", 1)
    guild = FakeDiscordGuild(emoji_limit=2)
    await emote_slots.reconcile(guild)
    old = guild.emojis[-1]

    # BBB now qualifies on two signals to AAA's one.
    await _notable(
        "BBB", "Beta", 9,
        signals={"level_100_plus": True, "war_count": True},
        metrics={"guild_level": 100, "wars": 60_000},
    )
    summary = await emote_slots.reconcile(guild)

    assert summary.evicted == 1 and old.deleted
    assert await GuildEmote.filter(guild_tag="AAA").count() == 0
    assert summary.minted == 1


async def test_only_emotes_we_made_are_ever_deleted(db, banners):
    """An emote sharing a guild's tag might be somebody's own from years
    ago, and deleting it breaks every message that used it."""
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    theirs = FakeEmoji(99, "VETS")
    guild.emojis.append(theirs)

    await emote_slots.reconcile(guild)

    assert not theirs.deleted
    assert await GuildEmote.filter(guild_tag="VETS").count() == 1


async def test_a_guild_with_no_resolved_name_is_skipped(db, banners):
    """Wynnpool's banner endpoint only takes names, so a tag with none
    has nothing to ask for."""
    await NotabilityCache.create(
        guild_tag="ZZZZ", is_notable=True, signals_json="{}", level_rank=1
    )
    guild = FakeDiscordGuild()

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 0 and _banners(guild) == []


async def test_a_full_slot_list_fails_loudly_rather_than_silently(
    db, banners, caplog
):
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    guild.create_custom_emoji = AsyncMock(
        side_effect=discord.HTTPException(MagicMock(), "no slots")
    )

    summary = await emote_slots.reconcile(guild)

    assert summary.failed == 1
    assert "couldn't upload" in caplog.text


# --------------------------------------------------------------------------
# The second consumer — the role's display icon
# --------------------------------------------------------------------------


def _role(role_id=7, name="VETS"):
    role = MagicMock()
    role.id = role_id
    role.name = name
    role.edit = AsyncMock()
    return role


async def test_an_unboosted_server_skips_role_icons(db, banners):
    """Role icons need boost level 2. Below it every write is a 403, so
    it's detected rather than retried hourly."""
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=())
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 0
    role.edit.assert_not_awaited()


async def test_a_boosted_server_puts_the_banner_on_the_role(db, banners):
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 1
    assert role.edit.await_args.kwargs["display_icon"] == b"png-for-VETS"
    assert (await GuildRole.get(guild_tag="VETS")).icon_hash is not None


async def test_an_unchanged_role_icon_is_not_rewritten(db, banners):
    """An unconditional edit per hour is an audit-log entry per hour."""
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)
    await emote_slots.reconcile(guild)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 0
    assert role.edit.await_count == 1


async def test_eviction_takes_the_role_icon_off_too(db, banners):
    """An icon outliving the emote it came from drifts the moment the
    guild changes its banner."""
    await _notable("AAA", "Alpha", 1)
    role = _role(role_id=7, name="AAA")
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]
    await GuildRole.create(guild_tag="AAA", discord_role_id=role.id)
    await emote_slots.reconcile(guild)

    await NotabilityCache.filter(guild_tag="AAA").update(is_notable=False)
    await emote_slots.reconcile(guild)

    assert role.edit.await_args.kwargs["display_icon"] is None
    assert (await GuildRole.get(guild_tag="AAA")).icon_hash is None


async def test_a_role_we_didnt_create_is_never_decorated(db, banners):
    """Same rule as deleting one: adopting a role by name doesn't make it
    ours to write to."""
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]  # resolvable by name, but no GuildRole row

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 0
    role.edit.assert_not_awaited()


# --------------------------------------------------------------------------
# Emote naming
# --------------------------------------------------------------------------


def test_an_emote_name_survives_a_tag_with_punctuation():
    """Guild tags are normally alphanumeric, but the API permits spaces
    and underscores — and Discord rejects anything else with a 400."""
    assert emote_slots._emote_name("VETS") == "VETS"
    assert emote_slots._emote_name("A B") == "A_B"
    assert len(emote_slots._emote_name("X")) >= 2


# --------------------------------------------------------------------------
# Boost level, in both directions
# --------------------------------------------------------------------------


async def test_dropping_below_boost_level_2_forgets_the_role_icons(db, banners):
    """Discord takes the icons off and tells us nothing. A remembered
    hash would read as "already set" forever, and the roles would stay
    bare straight through the next boost."""
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",), premium_tier=2)
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)
    await emote_slots.reconcile(guild)
    assert (await GuildRole.get(guild_tag="VETS")).icon_hash is not None

    guild.features = []  # boost lapsed
    await emote_slots.reconcile(guild)

    assert (await GuildRole.get(guild_tag="VETS")).icon_hash is None


async def test_regaining_boost_level_2_puts_the_icons_back(db, banners):
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",), premium_tier=2)
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)
    await emote_slots.reconcile(guild)
    guild.features = []
    await emote_slots.reconcile(guild)
    role.edit.reset_mock()

    guild.features = ["ROLE_ICONS"]  # boosted again
    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 1
    assert role.edit.await_args.kwargs["display_icon"] == b"png-for-VETS"


def _boost_listener():
    from hall_monitor.discord_bot.cogs.listeners import on_boost

    return on_boost


async def test_a_boost_change_reconciles_immediately(db, banners, monkeypatch):
    """A boost is a thing somebody paid for and then watches for; "it'll
    sort itself out within the hour" is a poor answer to that."""
    on_boost = _boost_listener()
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_boost.settings.discord_guild_id", 0
    )
    await _notable("AAA", "Alpha", 1)
    await _notable("BBB", "Beta", 2)
    before = FakeDiscordGuild(emoji_limit=2)
    after = FakeDiscordGuild(emoji_limit=3, premium_tier=1)

    await on_boost.OnBoost(MagicMock()).on_guild_update(before, after)

    assert _banners(after) == ["AAA", "BBB"]


async def test_an_unrelated_guild_edit_does_nothing(db, banners, monkeypatch):
    """`GUILD_UPDATE` fires for the name, the icon, the AFK timeout. A
    full emote reconcile on each would be absurd."""
    on_boost = _boost_listener()
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_boost.settings.discord_guild_id", 0
    )
    await _notable("AAA", "Alpha", 1)
    before = FakeDiscordGuild(emoji_limit=50)
    after = FakeDiscordGuild(emoji_limit=50)

    await on_boost.OnBoost(MagicMock()).on_guild_update(before, after)

    assert _banners(after) == []


async def test_losing_the_role_icon_feature_alone_is_a_relevant_change(db, banners, monkeypatch):
    """The slot count is unchanged between tier 2 and tier 2 minus one
    boost, but the icons still need forgetting."""
    on_boost = _boost_listener()
    monkeypatch.setattr(
        "hall_monitor.discord_bot.cogs.listeners.on_boost.settings.discord_guild_id", 0
    )
    before = FakeDiscordGuild(emoji_limit=50, features=("ROLE_ICONS",))
    after = FakeDiscordGuild(emoji_limit=50)

    assert on_boost._relevant_change(before, after) is not None


async def test_the_placeholder_is_named_for_the_reserved_guild(db):
    """`NONE` is Wynncraft's own reserved tag, so no real guild can ever
    collide with it — unlike a name that was only unique by convention."""
    assert emote_slots.PLACEHOLDER_NAME == "NONE"
    assert roster.PLACEHOLDER_EMOTE_NAME == emote_slots.PLACEHOLDER_NAME


async def test_an_older_placeholder_is_renamed_not_replaced(db, banners):
    """A new emote is a new ID, and every message carrying the old one
    would break. Renaming keeps it."""
    guild = FakeDiscordGuild(emoji_limit=4, placeholder=False)
    old = FakeEmoji(1, "Empty_Banner")
    old.edit = AsyncMock(side_effect=lambda **kw: setattr(old, "name", kw["name"]))
    guild.emojis.append(old)

    summary = await emote_slots.reconcile(guild)

    assert summary.placeholder is False, "nothing new was uploaded"
    assert old.name == "NONE" and old.id == 1, "same emote, same ID"


async def test_a_settled_server_makes_no_upstream_requests(db, monkeypatch):
    """The whole point of the skip: fifty guilds, zero Wynnpool calls,
    once everything is minted and recently checked."""
    fetches = []

    async def counting(guild_tag, guild_name):
        fetches.append(guild_tag)
        return f"png-for-{guild_tag}".encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", counting)
    for i, tag in enumerate(("AAA", "BBB"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=3)
    await emote_slots.reconcile(guild)
    assert len(fetches) == 2

    fetches.clear()
    await emote_slots.reconcile(guild)

    assert fetches == [], "nothing changed, so nothing was asked"


async def test_a_stale_banner_is_looked_at_again(db, banners):
    from datetime import datetime, timedelta, timezone

    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild(emoji_limit=2)
    await emote_slots.reconcile(guild)

    row = await GuildEmote.get(guild_tag="VETS")
    row.checked_at = datetime.now(timezone.utc) - emote_slots.RECHECK_AFTER * 2
    await row.save()

    assert emote_slots._is_stale(await GuildEmote.get(guild_tag="VETS"))


async def test_a_pass_stops_at_its_fetch_cap_and_says_so(db, banners, monkeypatch):
    """Wynnpool rate-limits at around a dozen; a roster is fifty guilds.
    A pass that stopped early must not look like a finished one."""
    monkeypatch.setattr(emote_slots, "FETCHES_PER_PASS", 2)
    for i, tag in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=10)

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 2 and summary.pending == 2
    assert "2 still to fetch" in summary.line()


async def test_one_guilds_rate_limit_doesnt_cost_the_rest(db, monkeypatch):
    """The failure that took `~script emotes` down: a 429 on the twelfth
    guild threw away the eleven banners already uploaded."""
    async def limited(guild_tag, guild_name):
        if guild_tag == "BBB":
            request = MagicMock()
            response = MagicMock()
            response.status_code = 429
            raise httpx.HTTPStatusError("429", request=request, response=response)
        return f"png-for-{guild_tag}".encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", limited)
    for i, tag in enumerate(("AAA", "BBB", "CCC"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=10)

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 1, "AAA survived"
    assert summary.failed == 1
    assert _banners(guild) == ["AAA"]


async def test_a_rate_limit_stops_the_pass_rather_than_grinding(db, monkeypatch):
    """Forty more guilds would collect forty more 429s and no banners."""
    attempted = []

    async def limited(guild_tag, guild_name):
        attempted.append(guild_tag)
        response = MagicMock()
        response.status_code = 429
        raise httpx.HTTPStatusError("429", request=MagicMock(), response=response)

    monkeypatch.setattr(emote_slots, "rendered_banner", limited)
    for i, tag in enumerate(("AAA", "BBB", "CCC", "DDD"), start=1):
        await _notable(tag, f"Guild {tag}", i)
    guild = FakeDiscordGuild(emoji_limit=10)

    summary = await emote_slots.reconcile(guild)

    assert attempted == ["AAA"], "stopped at the first 429"
    assert summary.pending == 3


async def test_a_redesigned_banner_can_be_forced_before_the_window(db, monkeypatch):
    """Guilds redesign about twice a year. Polling for that often would
    be thousands of requests to catch a hundred events, so the window
    stays slow and this is the path for a redesign somebody spotted."""
    version = ["first"]

    async def fake(guild_tag, guild_name):
        return version[0].encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", fake)
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild(emoji_limit=2)
    await emote_slots.reconcile(guild)
    original = guild.emojis[-1]

    version[0] = "redesigned"
    summary = await emote_slots.refresh_guild(guild, "VETS")

    assert summary.refreshed == 1 and original.deleted
    assert await emote_slots.holds_emote(guild, "VETS")


async def test_forcing_an_unchanged_banner_leaves_it_alone(db, banners):
    """The reply has to be able to say "nothing to do" — an operator who
    forced a refresh wants to know whether it actually moved."""
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild(emoji_limit=2)
    await emote_slots.reconcile(guild)
    minted = guild.emojis[-1]

    summary = await emote_slots.refresh_guild(guild, "VETS")

    assert summary.refreshed == 0 and summary.minted == 0
    assert not minted.deleted


async def test_holds_emote_is_false_for_a_guild_on_the_blank_banner(db, banners):
    await _notable("AAA", "Alpha", 1)
    await _notable("BBB", "Beta", 2)
    guild = FakeDiscordGuild(emoji_limit=2)  # room for one guild
    await emote_slots.reconcile(guild)

    assert await emote_slots.holds_emote(guild, "AAA")
    assert not await emote_slots.holds_emote(guild, "BBB")


async def test_a_changed_emote_asks_the_roster_to_redraw(db, banners, monkeypatch):
    """A re-minted emote has a new ID, so the roster messages drawn
    minutes earlier point at one that no longer exists."""
    from hall_monitor import scheduler

    asked = []
    monkeypatch.setattr(roster, "request_sync", lambda guild: asked.append(guild))
    monkeypatch.setattr(roster, "sync_channel", AsyncMock(return_value=None))
    monkeypatch.setattr(
        scheduler.notability, "refresh_all", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        scheduler.delegate_registry, "refresh_current_guilds",
        AsyncMock(return_value=(0, 0)),
    )
    monkeypatch.setattr(
        scheduler.transitions, "reconcile",
        AsyncMock(return_value=MagicMock(line=lambda: "")),
    )
    monkeypatch.setattr(
        "hall_monitor.scheduler.settings.discord_guild_id", 1
    )
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild(emoji_limit=2)
    bot = MagicMock()
    bot.get_guild = lambda gid: guild

    await scheduler.refresh_and_reconcile(bot)

    assert asked == [guild], "the roster was asked to redraw"
