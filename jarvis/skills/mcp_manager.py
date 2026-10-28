"""Model Context Protocol (MCP) bridge and external server runner for Jarvis 2.0."""
import logging
from typing import Any

from jarvis.core.mcp_manager import (
    MCP_PROTOCOL_VERSION,
    MCPClientSession,
    register_discovered_mcp_tools,
)
from jarvis.core.mcp_manager import (
    MCPManager as CoreMCPManager,
)

logger = logging.getLogger("Jarvis.MCP")


class MCPManager(CoreMCPManager):
    """Manages connections to external MCP servers defined in config/mcp_servers.json."""

    def __init__(self, config: dict[str, Any] | None = None):
        super().__init__(config=config)
        self.servers_config = self._load_servers_config()

    def _load_servers_config(self) -> dict[str, Any]:
        return self.load_servers_config()

    async def list_available_mcp_tools_async(self) -> list[dict[str, Any]]:
        """Queries enabled MCP servers asynchronously for exposed tools."""
        return self.list_available_mcp_tools()


__all__ = [
    "MCP_PROTOCOL_VERSION",
    "MCPClientSession",
    "MCPManager",
    "register_discovered_mcp_tools",
]
