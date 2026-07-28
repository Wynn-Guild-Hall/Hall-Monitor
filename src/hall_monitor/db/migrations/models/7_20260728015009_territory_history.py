from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "territory_sample" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL,
    "territories" INT NOT NULL,
    "sampled_at" TIMESTAMP NOT NULL
) /* One sweep's reading of how much territory a guild held. */;
CREATE INDEX IF NOT EXISTS "idx_territory_s_guild_t_d9b357" ON "territory_sample" ("guild_tag", "sampled_at");"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "territory_sample";"""


MODELS_STATE = (
    "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"
)
