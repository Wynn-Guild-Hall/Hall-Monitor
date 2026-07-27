from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "guild_role" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL UNIQUE,
    "discord_role_id" BIGINT NOT NULL UNIQUE,
    "created_at" TIMESTAMP NOT NULL
) /* A Discord role this bot created for a guild tag. */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "guild_role";"""


MODELS_STATE = (
    "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"
    "f3WQd9M="
)
