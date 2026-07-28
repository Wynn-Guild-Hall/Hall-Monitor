"""Tortoise ORM models — one source of truth for the SQLite schema."""

from tortoise import fields
from tortoise.models import Model


class PendingInvite(Model):
    """Single-use Discord invite bound to a Minecraft UUID awaiting redemption."""

    id = fields.IntField(pk=True)
    mc_uuid = fields.CharField(max_length=36, unique=True)
    # Captured at mint time from the eligibility check, which already
    # fetches it — a UUID is not something to show a human.
    mc_username = fields.CharField(max_length=16, null=True)
    guild_tag = fields.CharField(max_length=8)
    roles_bits = fields.IntField()
    discord_invite_code = fields.CharField(max_length=32, unique=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "pending_invite"


class Delegate(Model):
    """Persistent MC-UUID ↔ Discord-user binding for a guild representative."""

    id = fields.IntField(pk=True)
    mc_uuid = fields.CharField(max_length=36, unique=True)
    mc_username = fields.CharField(max_length=16, null=True)
    discord_user_id = fields.BigIntField(unique=True)
    # The guild they represent, fixed at verification.
    guild_tag = fields.CharField(max_length=8)
    # The guild Wynncraft last reported them in, refreshed by the hourly
    # watch. Differing from `guild_tag` is what makes someone external;
    # NULL means guildless or never polled, neither of which is.
    current_guild_tag = fields.CharField(max_length=8, null=True)
    joined_at = fields.DatetimeField(auto_now_add=True)
    left_at = fields.DatetimeField(null=True)

    class Meta:
        table = "delegate"


class NotabilityCache(Model):
    """Per-guild cached notability result plus the signals that produced it."""

    id = fields.IntField(pk=True)
    guild_tag = fields.CharField(max_length=8, unique=True)
    is_notable = fields.BooleanField()
    signals_json = fields.TextField()
    # The numbers behind the signals — board ranks, level, territories,
    # wars. The booleans say *whether* a guild qualifies; these say by how
    # much, which is what breaks ties when more guilds qualify than there
    # are emote slots to go round (`services/emote_slots.py`).
    metrics_json = fields.TextField(default="{}")
    # Wynncraft's spelling of the guild's full name, harvested from the
    # same leaderboards the signals come from. The roster prints it, and
    # nothing else in the bot knows a guild by anything but its tag.
    guild_name = fields.CharField(max_length=64, null=True)
    # Rank on Wynnpool's `guildLevel` board, which is the roster's running
    # order. NULL for a guild that isn't on it — those sort last.
    level_rank = fields.IntField(null=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "notability_cache"


class ForceOverride(Model):
    """A janitor/monitor-issued override that forces a state for a bounded time."""

    id = fields.IntField(pk=True)
    kind = fields.CharField(max_length=32)
    subject = fields.CharField(max_length=64)
    payload_json = fields.TextField(default="{}")
    expires_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "force_override"


class DashKV(Model):
    """Contact-owned key/value store surfaced by ``~dash``."""

    id = fields.IntField(pk=True)
    guild_tag = fields.CharField(max_length=8)
    key = fields.CharField(max_length=64)
    value_json = fields.TextField(default="null")
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "dash_kv"
        unique_together = (("guild_tag", "key"),)


class GuildRole(Model):
    """A Discord role this bot created for a guild tag.

    Recorded so the reconcile pass knows which roles are ours to delete.
    ``ensure_guild_role`` adopts a role a human made under the same name,
    and a deletion sweep can't tell those apart by name alone — every
    ``@VETS`` in message history would become ``@deleted-role`` to find out.
    """

    id = fields.IntField(pk=True)
    guild_tag = fields.CharField(max_length=8, unique=True)
    discord_role_id = fields.BigIntField(unique=True)
    # Hash of the banner PNG last written as the role's display icon, so
    # the hourly pass can tell "already right" from "never set" without
    # an edit. Servers below boost level 2 have no role icons at all and
    # this stays NULL forever.
    icon_hash = fields.CharField(max_length=64, null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "guild_role"


class RosterMessage(Model):
    """One of the bot's messages in the Current Guilds channel.

    The roster spans several messages, and Discord has no way to insert
    one above another — so the on-screen order is the order they were
    sent, and ``position`` is what lets a sync tell "edit message 3" from
    "everything from message 3 down has to be re-sent". ``guild_tags``
    records what went into each one, so a wrong-looking roster can be
    read back from the database instead of from the channel.
    """

    id = fields.IntField(pk=True)
    position = fields.IntField(unique=True)
    discord_message_id = fields.BigIntField(unique=True)
    guild_tags = fields.TextField(default="")
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "roster_message"


class GuildEmote(Model):
    """A custom emote this bot uploaded for a guild's banner.

    Recorded for the same reason as :class:`GuildRole`: the eviction pass
    may only delete emotes *we* made. The community's emote list is not
    ours to prune, and an emote that happens to share a guild's tag might
    be somebody's joke from two years ago.

    ``image_hash`` is the rendered PNG's digest — a banner that hasn't
    changed is not re-uploaded, because every upload is a new emote ID and
    every message that already used the old one breaks.
    """

    id = fields.IntField(pk=True)
    guild_tag = fields.CharField(max_length=8, unique=True)
    discord_emoji_id = fields.BigIntField(unique=True)
    image_hash = fields.CharField(max_length=64)
    # When the banner was last fetched and compared, as distinct from
    # when it was last *uploaded* (`created_at`). A banner that never
    # changes never moves `created_at`, so without this the re-check
    # would come due once and then fire on every pass forever.
    checked_at = fields.DatetimeField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "guild_emote"


class GuildContact(Model):
    """Which delegate currently holds each contact role for a guild."""

    id = fields.IntField(pk=True)
    guild_tag = fields.CharField(max_length=8)
    role = fields.CharField(max_length=16)
    delegate = fields.ForeignKeyField("models.Delegate", related_name="contact_assignments")
    assigned_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "guild_contact"
        unique_together = (("guild_tag", "role"),)
