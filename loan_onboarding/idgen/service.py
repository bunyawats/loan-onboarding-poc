"""The plainest leaf in CLAUDE.md's module dependency graph -- one pure
function, zero I/O, zero state, imports nothing else in this codebase.
Every module that assigns a primary key (`customer/`, `account/`,
`application/`) imports this one to generate it, then handles its own
insert-time collision retry (see CLAUDE.md's "Data storage" -- collision
handling deliberately lives at each insert site, not here)."""

from __future__ import annotations

import secrets

_DIGITS = "0123456789"


def generate_id(prefix: str, length: int) -> str:
    suffix = "".join(secrets.choice(_DIGITS) for _ in range(length))
    return f"{prefix}-{suffix}"
