#!/usr/bin/env python3
"""Call the configured Datamarket MCP without dropping Hermes provenance."""

from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
from pathlib import Path


def _hermes_agent_root() -> Path:
    explicit = str(os.environ.get("HERMES_AGENT_ROOT") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    hermes_home = Path(
        str(os.environ.get("HERMES_HOME") or "").strip()
        or (Path.home() / ".hermes")
    ).expanduser()
    return hermes_home / "hermes-agent"


HERMES_AGENT_ROOT = _hermes_agent_root()
sys.path.insert(0, str(HERMES_AGENT_ROOT))

try:
    from tools.mcp_tool import (
        _call_tool_with_metadata,
        _connect_server,
        _forwarded_session_metadata,
        _load_mcp_config,
    )
except ImportError as exc:  # pragma: no cover - operator-facing install guard
    raise SystemExit(
        "RVBBIT's Hermes identity-forwarding patch is not installed. "
        "Reapply services/warehouse-mcp/hermes-forward-session-identity.patch."
    ) from exc


async def main() -> None:
    if len(sys.argv) < 2:
        program = Path(sys.argv[0]).name or "dmcp.py"
        raise SystemExit(f"usage: {program} TOOL [JSON_ARGS]")

    tool = sys.argv[1]
    raw_args = sys.argv[2] if len(sys.argv) > 2 else "{}"
    if raw_args.startswith("@"):
        raw_args = Path(raw_args[1:]).read_text()
    args = json.loads(raw_args)

    configs = _load_mcp_config()
    if "Datamarket" not in configs:
        raise SystemExit("Datamarket MCP config not found")
    config = configs["Datamarket"]
    server = await _connect_server("Datamarket", config)
    try:
        metadata = _forwarded_session_metadata(config)
        result = await _call_tool_with_metadata(
            server.session,
            tool,
            args,
            metadata,
        )
        output = {"isError": bool(result.isError), "content": []}
        for index, block in enumerate(result.content or []):
            if hasattr(block, "text"):
                output["content"].append({"type": "text", "text": block.text})
            elif hasattr(block, "data"):
                suffix = (
                    ".png"
                    if getattr(block, "mimeType", "") == "image/png"
                    else ".bin"
                )
                output_path = Path.cwd() / f"dmcp-{tool}-{index}{suffix}"
                output_path.write_bytes(base64.b64decode(block.data))
                output["content"].append(
                    {
                        "type": "image",
                        "mimeType": getattr(block, "mimeType", ""),
                        "path": str(output_path),
                    }
                )
        structured = getattr(result, "structuredContent", None)
        if structured is not None:
            output["structuredContent"] = structured
        print(json.dumps(output, ensure_ascii=False))
    finally:
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
