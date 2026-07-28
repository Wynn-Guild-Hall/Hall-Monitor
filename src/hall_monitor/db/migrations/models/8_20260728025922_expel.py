from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        CREATE TABLE IF NOT EXISTS "expel_motion" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL,
    "opened_by_discord_user_id" BIGINT NOT NULL,
    "opened_by_guild_tag" VARCHAR(8) NOT NULL,
    "discord_channel_id" BIGINT,
    "discord_message_id" BIGINT UNIQUE,
    "state" VARCHAR(16) NOT NULL,
    "tally_json" TEXT NOT NULL,
    "created_at" TIMESTAMP NOT NULL,
    "resolved_at" TIMESTAMP
) /* A delegate's motion to remove a guild from the Hall, and its vote. */;
        CREATE TABLE IF NOT EXISTS "expel_ban" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL UNIQUE,
    "reason" TEXT NOT NULL,
    "created_by_discord_user_id" BIGINT,
    "created_at" TIMESTAMP NOT NULL,
    "motion_id" INT REFERENCES "expel_motion" ("id") ON DELETE CASCADE
) /* A guild barred from the Hall. */;
        CREATE TABLE IF NOT EXISTS "expel_vote" (
    "id" INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL,
    "guild_tag" VARCHAR(8) NOT NULL,
    "discord_user_id" BIGINT NOT NULL,
    "yay" INT NOT NULL,
    "cast_at" TIMESTAMP NOT NULL,
    "motion_id" INT NOT NULL REFERENCES "expel_motion" ("id") ON DELETE CASCADE,
    CONSTRAINT "uid_expel_vote_motion__f26387" UNIQUE ("motion_id", "guild_tag")
) /* One guild's vote on one motion. */;"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        DROP TABLE IF EXISTS "expel_motion";
        DROP TABLE IF EXISTS "expel_ban";
        DROP TABLE IF EXISTS "expel_vote";"""


MODELS_STATE = (
    "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"
    "CLYMx3YU16qyYmPyzgqGxLuceczL7/D/fd0Fo="
)
