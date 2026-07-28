from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "roster_message" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "position" INT NOT NULL UNIQUE,
    "discord_message_id" BIGINT NOT NULL UNIQUE,
    "guild_tags" TEXT NOT NULL,
    "updated_at" TIMESTAMP NOT NULL
) /* One of the bot's messages in the Current Guilds channel. */;
        ALTER TABLE "notability_cache" ADD "level_rank" INT;
        ALTER TABLE "notability_cache" ADD "guild_name" VARCHAR(64);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "notability_cache" DROP COLUMN "level_rank";
        ALTER TABLE "notability_cache" DROP COLUMN "guild_name";
        DROP TABLE IF EXISTS "roster_message";"""


MODELS_STATE = (
    "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"
)
