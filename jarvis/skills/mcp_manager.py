"""Model Context Protocol (MCP) bridge and external server runner for Jarvis 2.0."""
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
import asyncio

from jarvis.core.mcp_manager import (
    MCPManager as CoreMCPManager,
    MCPClientSession,
    register_discovered_mcp_tools,
    MCP_PROTOCOL_VERSION,
)

logger = logging.getLogger("Jarvis.MCP")


class MCPManager(CoreMCPManager):
    """Manages connections to external MCP servers defined in config/mcp_servers.json."""

    def __init__(self, config: Optional[Dict[str, Any]] = None):
        super().__init__(config=config)
        self.servers_config = self._load_servers_config()

    def _load_servers_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("mcpServers", {})
        except Exception as e:
            logger.error(f"Failed to parse MCP config: {e}")
            return {}

    async def list_available_mcp_tools_async(self) -> List[Dict[str, Any]]:
        """Queries enabled MCP servers asynchronously for exposed tools."""
        return self.list_available_mcp_tools()


__all__ = [
    "MCPManager",
    "MCPClientSession",
    "register_discovered_mcp_tools",
    "MCP_PROTOCOL_VERSION",
]
