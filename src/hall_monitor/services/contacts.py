"""Per-role contact assignment with uniqueness enforcement.

One contact per role per guild; assigning a new one displaces the old one,
and members with no remaining contact roles are kicked from the server.
"""


async def assign_contact(guild_tag: str, role: str, delegate) -> None:
    raise NotImplementedError


async def current_holder(guild_tag: str, role: str):
    raise NotImplementedError


async def resolve_conflicts_and_kick_if_empty(guild_tag: str, roles: set[str], new_holder) -> None:
    raise NotImplementedError
