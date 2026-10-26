"""Plugin registration. Later steps attach the CLI, hooks, and tools here."""

TESTED_HERMES_VERSION = "0.21.5"

_context = None


def register(ctx) -> None:
    """Accept the Hermes plugin context. Features attach in later steps."""
    global _context
    _context = ctx


def plugin_profile() -> str | None:
    ctx = _context
    if ctx is None:
        return None
    name = getattr(ctx, "profile_name", None)
    return name if isinstance(name, str) and name else None
