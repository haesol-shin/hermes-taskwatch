"""Plugin registration. CLI, hooks, and tools attach here as they land."""

from .cli import handle_cli, setup_cli
from .gate import pre_tool_call
from .hook import dispatch_tick

_context = None


def register(ctx) -> None:
    global _context
    _context = ctx
    ctx.register_cli_command(
        name="taskwatch",
        help="Watch Kanban work and block unchecked results",
        setup_fn=setup_cli,
        handler_fn=handle_cli,
        description="init, doctor, scan, and check",
    )
    ctx.register_hook("on_kanban_dispatch_tick", dispatch_tick)
    ctx.register_hook("pre_tool_call", pre_tool_call)


def plugin_profile() -> str | None:
    ctx = _context
    if ctx is None:
        return None
    name = getattr(ctx, "profile_name", None)
    return name if isinstance(name, str) and name else None
