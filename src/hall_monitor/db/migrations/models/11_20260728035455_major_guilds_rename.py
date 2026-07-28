from tortoise import BaseDBAsyncClient

RUN_IN_TRANSACTION = True


# "Notable" became "major" throughout — the Hall's own word for these
# guilds. Aerich generated this as CREATE + DROP, which is a data-losing
# no-op dressed as a rename: every cached verdict, every learned guild
# name and every level rank would go, and the next sweep would quietly
# rebuild them, so nothing would look broken until somebody asked why
# the roster was empty for an hour. Rewritten by hand as an actual
# rename. MODELS_STATE below is Aerich's and is left alone — it's the
# schema description the *next* migration diffs against, and it's
# correct either way.
#
# The `force_override` rows are data, not schema, and they need moving
# too: `kind` is a bare string, so a `~force notable VETS 3mo` written
# last week would simply stop being found.


async def upgrade(db: BaseDBAsyncClient) -> str:
    return """
        ALTER TABLE "notability_cache" RENAME TO "major_guild_cache";
        ALTER TABLE "major_guild_cache" RENAME COLUMN "is_notable" TO "is_major";
        UPDATE "force_override" SET "kind" = 'major' WHERE "kind" = 'notable';"""


async def downgrade(db: BaseDBAsyncClient) -> str:
    return """
        UPDATE "force_override" SET "kind" = 'notable' WHERE "kind" = 'major';
        ALTER TABLE "major_guild_cache" RENAME COLUMN "is_major" TO "is_notable";
        ALTER TABLE "major_guild_cache" RENAME TO "notability_cache";"""


MODELS_STATE = (
    "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"
)
