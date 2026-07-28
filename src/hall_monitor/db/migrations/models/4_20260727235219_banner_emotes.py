from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "guild_emote" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL UNIQUE,
    "discord_emoji_id" BIGINT NOT NULL UNIQUE,
    "image_hash" VARCHAR(64) NOT NULL,
    "created_at" TIMESTAMP NOT NULL
) /* A custom emote this bot uploaded for a guild's banner. */;
        ALTER TABLE "guild_role" ADD "icon_hash" VARCHAR(64);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "guild_role" DROP COLUMN "icon_hash";
        DROP TABLE IF EXISTS "guild_emote";"""


MODELS_STATE = (
    "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"
    "MnsfGjjJu+/gKTez+/8D+llt4g=="
)
