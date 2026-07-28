"""Rendering a guild's Minecraft banner, and spending the emote budget.

The render tests use real pattern SVGs held as fixtures rather than
mocked bytes: the whole design rests on the art being a *mask* — black
shapes whose opacity carries Minecraft's shading — and a fake SVG would
let that assumption pass a test it doesn't hold in production. Both are
small enough to read.
"""

import io
import logging
from unittest.mock import AsyncMock, MagicMock

import discord
import pytest
from PIL import Image

from hall_monitor.db.models import GuildEmote, GuildRole, NotabilityCache
from hall_monitor.external import wynnpool
from hall_monitor.services import banner_render, emote_slots, guild_roles

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


async def test_the_banner_is_letterboxed_not_stretched(patterns):
    """A banner is 1:2 and an emote is square. Stretching it produces a
    different banner, and recognising it is the entire point."""
    png = await banner_render.render_banner(_banner(base="RED"))
    image = _pixels(png)

    assert image.size == (128, 128)
    assert image.getpixel((64, 64))[3] == 255, "banner in the middle"
    assert image.getpixel((2, 64))[3] == 0, "transparent at the edges"
    # 1:2 preserved: 128 tall means 64 wide, so the margins are 32 each.
    assert image.getpixel((31, 64))[3] == 0 and image.getpixel((33, 64))[3] == 255


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
    def __init__(self, emoji_id, name):
        self.id = emoji_id
        self.name = name
        self.deleted = False

    async def delete(self, *, reason=None):
        self.deleted = True


class FakeDiscordGuild:
    def __init__(self, *, emoji_limit=50, features=()):
        self.emojis = []
        self.emoji_limit = emoji_limit
        self.features = list(features)
        self.roles = []
        self._next_id = 900

    def get_role(self, role_id):
        return next((r for r in self.roles if r.id == role_id), None)

    async def create_custom_emoji(self, *, name, image, reason=None):
        self._next_id += 1
        emoji = FakeEmoji(self._next_id, name)
        self.emojis.append(emoji)
        return emoji


@pytest.fixture
def budget(monkeypatch):
    def _set(n):
        monkeypatch.setattr(
            "hall_monitor.services.emote_slots.settings.roster_emote_budget", n
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


async def _notable(tag, name, rank):
    await NotabilityCache.create(
        guild_tag=tag, is_notable=True, signals_json="{}",
        guild_name=name, level_rank=rank,
    )


async def test_a_zero_budget_takes_no_slots(db, budget, banners):
    """The emote list belongs to the server. Taking slots is opt-in."""
    budget(0)
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 0 and guild.emojis == []


async def test_the_budget_is_spent_in_roster_order(db, budget, banners):
    budget(2)
    await _notable("AAA", "Alpha", 1)
    await _notable("BBB", "Beta", 2)
    await _notable("CCC", "Gamma", 3)
    guild = FakeDiscordGuild()

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 2
    assert [e.name for e in guild.emojis] == ["AAA", "BBB"]


async def test_the_budget_cannot_exceed_the_servers_own_limit(db, budget, banners):
    """A boost level lost overnight shouldn't leave us minting into slots
    that no longer exist."""
    budget(10)
    guild = FakeDiscordGuild(emoji_limit=1)
    await _notable("AAA", "Alpha", 1)
    await _notable("BBB", "Beta", 2)

    summary = await emote_slots.reconcile(guild)

    assert summary.budget == 1 and summary.minted == 1


async def test_an_unchanged_banner_is_not_re_uploaded(db, budget, banners):
    """Every upload is a new emote ID, so re-minting breaks every message
    already using the old one."""
    budget(1)
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    await emote_slots.reconcile(guild)
    first = guild.emojis[0]

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 0 and summary.refreshed == 0
    assert guild.emojis == [first] and not first.deleted


async def test_a_changed_banner_is_replaced(db, budget, monkeypatch):
    budget(1)
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    version = ["first"]

    async def fake(guild_tag, guild_name):
        return version[0].encode()

    monkeypatch.setattr(emote_slots, "rendered_banner", fake)
    await emote_slots.reconcile(guild)
    original = guild.emojis[0]

    version[0] = "second"
    summary = await emote_slots.reconcile(guild)

    assert summary.refreshed == 1
    assert original.deleted, "Discord has no replace-in-place for emotes"
    assert (await GuildEmote.get(guild_tag="VETS")).discord_emoji_id != original.id


async def test_a_guild_dropping_out_of_the_budget_is_evicted(db, budget, banners):
    """Nothing recycled emotes, and the boundary moves whenever the
    leaderboard does — a full list makes every later mint fail."""
    budget(1)
    await _notable("AAA", "Alpha", 1)
    guild = FakeDiscordGuild()
    await emote_slots.reconcile(guild)
    old = guild.emojis[0]

    (await NotabilityCache.get(guild_tag="AAA")).level_rank  # unchanged row
    await NotabilityCache.filter(guild_tag="AAA").update(level_rank=5)
    await _notable("BBB", "Beta", 1)
    summary = await emote_slots.reconcile(guild)

    assert summary.evicted == 1 and old.deleted
    assert await GuildEmote.filter(guild_tag="AAA").count() == 0
    assert summary.minted == 1


async def test_only_emotes_we_made_are_ever_deleted(db, budget, banners):
    """An emote sharing a guild's tag might be somebody's own from years
    ago, and deleting it breaks every message that used it."""
    budget(1)
    await _notable("VETS", "Returners", 1)
    guild = FakeDiscordGuild()
    theirs = FakeEmoji(1, "VETS")
    guild.emojis.append(theirs)

    await emote_slots.reconcile(guild)

    assert not theirs.deleted
    assert await GuildEmote.filter(guild_tag="VETS").count() == 1


async def test_a_guild_with_no_resolved_name_is_skipped(db, budget, banners):
    """Wynnpool's banner endpoint only takes names, so a tag with none
    has nothing to ask for."""
    budget(1)
    await NotabilityCache.create(
        guild_tag="ZZZZ", is_notable=True, signals_json="{}", level_rank=1
    )
    guild = FakeDiscordGuild()

    summary = await emote_slots.reconcile(guild)

    assert summary.minted == 0 and guild.emojis == []


async def test_a_full_slot_list_fails_loudly_rather_than_silently(
    db, budget, banners, caplog
):
    budget(1)
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


async def test_an_unboosted_server_skips_role_icons(db, budget, banners):
    """Role icons need boost level 2. Below it every write is a 403, so
    it's detected rather than retried hourly."""
    budget(1)
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=())
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 0
    role.edit.assert_not_awaited()


async def test_a_boosted_server_puts_the_banner_on_the_role(db, budget, banners):
    budget(1)
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 1
    assert role.edit.await_args.kwargs["display_icon"] == b"png-for-VETS"
    assert (await GuildRole.get(guild_tag="VETS")).icon_hash is not None


async def test_an_unchanged_role_icon_is_not_rewritten(db, budget, banners):
    """An unconditional edit per hour is an audit-log entry per hour."""
    budget(1)
    await _notable("VETS", "Returners", 1)
    role = _role()
    guild = FakeDiscordGuild(features=("ROLE_ICONS",))
    guild.roles = [role]
    await GuildRole.create(guild_tag="VETS", discord_role_id=role.id)
    await emote_slots.reconcile(guild)

    summary = await emote_slots.reconcile(guild)

    assert summary.icons == 0
    assert role.edit.await_count == 1


async def test_eviction_takes_the_role_icon_off_too(db, budget, banners):
    """An icon outliving the emote it came from drifts the moment the
    guild changes its banner."""
    budget(1)
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


async def test_a_role_we_didnt_create_is_never_decorated(db, budget, banners):
    """Same rule as deleting one: adopting a role by name doesn't make it
    ours to write to."""
    budget(1)
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
