"""Discord role sync for a guild's aesthetic role plus delegate/relegate flags."""


async def ensure_guild_role(guild_tag: str, colour_hex: str):
    """Create or update the guild's Discord role with the given colour."""
    raise NotImplementedError


async def set_delegate(member) -> None:
    raise NotImplementedError


async def set_relegate(member) -> None:
    raise NotImplementedError
