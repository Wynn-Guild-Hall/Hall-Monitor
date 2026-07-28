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
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "guild_role"


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
