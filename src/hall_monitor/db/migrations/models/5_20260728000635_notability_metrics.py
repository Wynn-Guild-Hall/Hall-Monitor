from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    # The DEFAULT is not decoration. SQLite refuses to add a NOT NULL
    # column to a table that already has rows unless there's a value to
    # give them — and every existing row here needs one. Aerich generated
    # this without it, which passed against an empty test database and
    # took the bot down on a production one.
    return """
        ALTER TABLE "notability_cache" ADD "metrics_json" TEXT NOT NULL DEFAULT '{}';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "notability_cache" DROP COLUMN "metrics_json";"""


MODELS_STATE = (
    "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"
)
