"""One canonical shape for Minecraft UUIDs.

Mojang and PlayerDB hand back 32 bare hex characters. Minecraft itself
uses the dashed 8-4-4-4-12 form, so that's what picolimbo forwards to
``/api/verify``, what ends up in ``Delegate.mc_uuid``, and what
Wynncraft's ``/v3/player/{uuid}`` route accepts — it 404s on the bare
form, silently turning a guild chief into "not chief or owner".

Normalising every UUID as it enters means the database holds one shape
and every outbound call sends one shape.
"""

import uuid as _uuid


def dashed(value: str) -> str:
    """Canonical dashed, lower-case UUID.

    Accepts either form. Anything unparseable is handed back untouched —
    test fixtures use readable stand-ins like ``uuid-chief``, and it isn't
    this function's place to reject what the API it's headed for will
    judge for itself.
    """
    stripped = value.strip()
    try:
        return str(_uuid.UUID(stripped))
    except ValueError:
        return stripped
