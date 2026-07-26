"""Guild colour lookup from the Athena guild list, plus Discord-visible contrast."""


async def lookup(guild_tag: str) -> str:
    """Return the raw hex colour associated with a guild tag."""
    raise NotImplementedError


def to_discord_visible(hex_colour: str) -> str:
    """Adjust a raw colour to a variant that reads on both light and dark Discord themes."""
    raise NotImplementedError
