"""Chat tools for the ``hello`` reference capability package.

This package is a no-op reference fixture for the Phase A capability-
package framework (carpenter-core PR for
``feat/d8-capability-package-framework``).  It contributes one
read-only chat tool that returns a static greeting.

It exists to:

1. Validate the cross-repo loader path end-to-end — when this repo
   is cloned next to carpenter-core, the platform discovers and
   registers this tool with no user config (per leadership D22).
2. Serve as a minimal template for authoring real capability packages
   in Phase B+.

Real packages should provide meaningful chat-boundary read tools, KB
articles documenting their behaviour, and (in later phases) data
models, triggers, and arc templates.  Anything that crosses the trust
boundary — JUDGE code, platform-boundary tools, policy allowlist
pre-population — is rejected at package-load time by the framework's
security guards.
"""

from carpenter.chat_tool_loader import chat_tool


@chat_tool(
    description=(
        "Reference no-op tool from the 'hello' capability package. "
        "Returns a static greeting; intended for verifying the "
        "capability-package loader is wired up.  Use only for "
        "framework health checks; not a real capability."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": (
                    "Optional name to greet.  Defaults to 'world'."
                ),
            },
        },
        "required": [],
    },
    capabilities=["pure"],
    trust_boundary="chat",
)
def hello_world(tool_input, **kwargs):
    """Return a static greeting."""
    name = (tool_input or {}).get("name") or "world"
    if not isinstance(name, str):
        name = "world"
    # Trim aggressively so a malicious caller cannot smuggle large
    # blobs through the response.  Read-only tools have no security
    # impact, but defending the surface is cheap.
    name = name.strip()[:64] or "world"
    return f"Hello, {name}! (from the carpenter-packages 'hello' package)"
