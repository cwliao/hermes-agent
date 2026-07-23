"""Hermes plugin registration for the offline Mermaid PNG renderer."""

from plugins.mermaid_renderer.tools import RENDER_MERMAID_SCHEMA, handle_render_mermaid
from plugins.mermaid_renderer.cli import mermaid_renderer_command, register_cli


def register(ctx) -> None:
    """Expose the single bounded Mermaid rendering tool."""
    ctx.register_tool(
        name="render_mermaid",
        toolset="mermaid_renderer",
        schema=RENDER_MERMAID_SCHEMA,
        handler=handle_render_mermaid,
        description="Render safe Mermaid text to a local PNG media artifact.",
        emoji="📊",
    )
    ctx.register_cli_command(
        name="mermaid-renderer",
        help="Inspect or clean bounded Mermaid PNG artifacts",
        setup_fn=register_cli,
        handler_fn=mermaid_renderer_command,
        description="Operator-only status and explicit cleanup for Mermaid renderer PNG artifacts.",
    )
