#!/usr/bin/env python3
"""Start the groundtruth MCP server.

    python scripts/serve_mcp.py             # streamable-HTTP on 127.0.0.1:8765/mcp
    python scripts/serve_mcp.py --stdio     # stdio transport for a local MCP client

Config (env): GT_MCP_HOST, GT_MCP_PORT, GT_MCP_API_KEY, GT_SEARCH_BACKEND.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from groundtruth import mcp  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(mcp.main())
