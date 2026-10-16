"""Small in-process MCP protocol check; never calls a backend or binds a port."""
from starlette.testclient import TestClient

from main import create_app


def main() -> None:
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(create_app(), base_url="http://127.0.0.1:8001") as client:
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-03-26", "capabilities": {},
                "clientInfo": {"name": "runtime-check", "version": "1"}}})
        response.raise_for_status()
        headers["MCP-Protocol-Version"] = response.json()["result"]["protocolVersion"]
        if response.headers.get("Mcp-Session-Id"):
            raise RuntimeError("Runtime must support stateless MCP requests")
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "method": "notifications/initialized"})
        response.raise_for_status()
        response = client.post("/mcp", headers=headers, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        response.raise_for_status()
        names = {tool["name"] for tool in response.json()["result"]["tools"]}
        if not {"get_agent_home", "create_post", "send_note"}.issubset(names):
            raise RuntimeError("Required MCP tools are missing")
    print("Runtime MCP initialization and tool registration passed")


if __name__ == "__main__":
    main()
