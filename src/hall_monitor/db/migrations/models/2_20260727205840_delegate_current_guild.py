from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "delegate" ADD "current_guild_tag" VARCHAR(8);"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "delegate" DROP COLUMN "current_guild_tag";"""


MODELS_STATE = (
    "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"
    "MxP7tIHdottrjxnMbv8DpJ3wLg=="
)
