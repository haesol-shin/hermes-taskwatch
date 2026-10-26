"""Plugin registration. CLI, hooks, and tools attach here as they land."""

from functools import partial

from .cli import handle_cli, setup_cli
from .gate import pre_tool_call
from .hook import dispatch_tick
from .tools import (
    APPROVE_SCHEMA,
    CREATE_SCHEMA,
    REJECT_SCHEMA,
    taskwatch_approve,
    taskwatch_create,
    taskwatch_reject,
)


def register(ctx) -> None:
    ctx.register_cli_command(
        name="taskwatch",
        help="Watch Kanban work and block unchecked results",
        setup_fn=setup_cli,
        handler_fn=partial(handle_cli, deps={"profile": ctx.profile_name}),
        description="init, doctor, scan, and check",
    )
    ctx.register_hook(
        "on_kanban_dispatch_tick", partial(dispatch_tick, profile_name=ctx.profile_name)
    )
    ctx.register_hook("pre_tool_call", partial(pre_tool_call, profile_name=ctx.profile_name))
    ctx.register_tool(
        name="taskwatch_create", toolset="taskwatch", schema=CREATE_SCHEMA, handler=taskwatch_create
    )
    ctx.register_tool(
        name="taskwatch_reject", toolset="taskwatch", schema=REJECT_SCHEMA, handler=taskwatch_reject
    )
    ctx.register_tool(
        name="taskwatch_approve",
        toolset="taskwatch",
        schema=APPROVE_SCHEMA,
        handler=taskwatch_approve,
    )
