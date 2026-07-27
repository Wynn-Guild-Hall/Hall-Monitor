from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "dash_kv" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL,
    "key" VARCHAR(64) NOT NULL,
    "value_json" TEXT NOT NULL,
    "updated_at" TIMESTAMP NOT NULL,
    CONSTRAINT "uid_dash_kv_guild_t_1fbe90" UNIQUE ("guild_tag", "key")
) /* Contact-owned key/value store surfaced by ``~dash``. */;
CREATE TABLE IF NOT EXISTS "delegate" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "mc_uuid" VARCHAR(36) NOT NULL UNIQUE,
    "mc_username" VARCHAR(16),
    "discord_user_id" BIGINT NOT NULL UNIQUE,
    "guild_tag" VARCHAR(8) NOT NULL,
    "joined_at" TIMESTAMP NOT NULL,
    "left_at" TIMESTAMP
) /* Persistent MC-UUID ↔ Discord-user binding for a guild representative. */;
CREATE TABLE IF NOT EXISTS "force_override" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "kind" VARCHAR(32) NOT NULL,
    "subject" VARCHAR(64) NOT NULL,
    "payload_json" TEXT NOT NULL,
    "expires_at" TIMESTAMP,
    "created_at" TIMESTAMP NOT NULL
) /* A janitor/monitor-issued override that forces a state for a bounded time. */;
CREATE TABLE IF NOT EXISTS "guild_contact" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL,
    "role" VARCHAR(16) NOT NULL,
    "assigned_at" TIMESTAMP NOT NULL,
    "delegate_id" INT NOT NULL REFERENCES "delegate" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_guild_conta_guild_t_dba6c4" UNIQUE ("guild_tag", "role")
) /* Which delegate currently holds each contact role for a guild. */;
CREATE TABLE IF NOT EXISTS "notability_cache" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL UNIQUE,
    "is_notable" INT NOT NULL,
    "signals_json" TEXT NOT NULL,
    "updated_at" TIMESTAMP NOT NULL
) /* Per-guild cached notability result plus the signals that produced it. */;
CREATE TABLE IF NOT EXISTS "pending_invite" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "mc_uuid" VARCHAR(36) NOT NULL UNIQUE,
    "mc_username" VARCHAR(16),
    "guild_tag" VARCHAR(8) NOT NULL,
    "roles_bits" INT NOT NULL,
    "discord_invite_code" VARCHAR(32) NOT NULL UNIQUE,
    "created_at" TIMESTAMP NOT NULL
) /* Single-use Discord invite bound to a Minecraft UUID awaiting redemption. */;
CREATE TABLE IF NOT EXISTS "aerich" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "version" VARCHAR(255) NOT NULL,
    "app" VARCHAR(100) NOT NULL,
    "content" JSON NOT NULL
);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        """


MODELS_STATE = (
    "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"
    "n3uE8ZzO7/B2ZRkD0="
)
