"""Persistent MC UUID ↔ Discord user bindings for guild representatives."""


async def get_by_mc_uuid(mc_uuid: str):
    raise NotImplementedError


async def get_by_discord_user_id(discord_user_id: int):
    raise NotImplementedError


async def is_current_member(mc_uuid: str) -> bool:
    """True iff a Delegate row exists for this UUID and its Discord user is still in the guild."""
    raise NotImplementedError


async def register(mc_uuid: str, discord_user_id: int, guild_tag: str):
    raise NotImplementedError


async def mark_left(discord_user_id: int) -> None:
    raise NotImplementedError
