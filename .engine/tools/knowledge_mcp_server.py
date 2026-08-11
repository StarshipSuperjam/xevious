#!/usr/bin/env python3
"""The graph-query MCP server: core's conforming fallback for knowledge-retrieval.

This is the named fallback the knowledge-retrieval interface declares
(.engine/interfaces/knowledge-retrieval.json, handle 'engine-knowledge-graph'). It is a thin MCP
transport over the knowledge_query op-set: it exposes a content-free `health` availability probe and the
four declared retrieval operations (get-entity / find / neighbors / relate) as MCP tools. The retrieval
operations delegate to knowledge_query, which reads
the committed knowledge graph through the gitignored SQLite index, rebuilds that index from the
committed graph when it is missing, and falls back to a live walk of the surfaces when the committed
graph is absent (degrade-to-git-native).

Built on the official MCP SDK (the `mcp` package) so protocol conformance — the handshake, capability
negotiation, framing, and future protocol-version changes — is maintained upstream rather than
hand-written; a richer knowledge-retrieval implementation overrides this floor by presence at the same
engine-prefixed server name. The server is registered, definition-only, in the root .mcp.json; the
operator's one-time approval is the operator's own (never engine-written).

Run (normally launched by the platform via .mcp.json over stdio):
  uv run --directory .engine --frozen -- python tools/knowledge_mcp_server.py
"""
from __future__ import annotations
import os
import sys

# Third-party import first: it needs nothing from the path bootstrap below, and importing it above the
# sys.path mutation closes the shadowing hazard (a same-named module in tools/ could otherwise win).
from mcp.server import MCPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import knowledge_query as kq  # noqa: E402

SERVER_NAME = "engine-knowledge-graph"

server = MCPServer(SERVER_NAME)


@server.tool(name="health", description="Content-free availability probe for this exact "
             "engine-knowledge-graph server. Returns only its fixed identity and status; reads no graph, "
             "rebuilds no query cache, walks no project files, and changes no state.")
def health() -> dict:
    return {"status": "ok", "server": SERVER_NAME}


# Every tool answers through kq.with_degrade, which returns (result, degrade_note_or_None). When the read
# came from a LIVE WALK (the committed graph was absent or damaged), the response also carries a `degraded`
# key whose plain-language line names the fault and the regenerate-and-commit repair — so an in-session
# caller relays it instead of silently answering from a degraded source. The key is absent on a normal
# (fresh/committed) read. `_merge` attaches it under the tool's own result key.
def _merge(key: str, result, degraded) -> dict:
    out = {key: result}
    if degraded:
        out["degraded"] = degraded
    return out


@server.tool(name="get-entity", description="Fetch one entity by id, with its declared attributes "
             "(status, and where applicable tier/title/discriminators) and outgoing edges; returns "
             "{entity} or {entity: null}. Attributes are as-of the last graph regeneration: treat status "
             "as last-known and verify a supersession against the live file before asserting it; relay "
             "lifecycle/tier tokens to the operator in plain language, never the raw token. A `degraded` "
             "key may accompany the result when the committed map was absent or damaged — relay it.")
def get_entity(id: str) -> dict:
    result, degraded = kq.with_degrade(lambda c: kq._get_entity(c, id))
    return _merge("entity", result, degraded)


@server.tool(name="find", description="List the entities matching a selector (surface type, "
             "source-path glob, and/or owning module); an empty selector matches every entity. A "
             "`degraded` key may accompany the result when the committed map was absent or damaged — relay it.")
def find(type: str | None = None, path_glob: str | None = None, owner: str | None = None) -> dict:
    result, degraded = kq.with_degrade(lambda c: kq._find(c, type, path_glob, owner))
    return _merge("entities", result, degraded)


@server.tool(name="neighbors", description="The entities adjacent to one entity by edge traversal — "
             "direction 'out' (declared edges), 'in' (reverse), or 'both'; optional edge_filter and "
             "depth (default 1) for transitive traversal. A `degraded` key may accompany the result when "
             "the committed map was absent or damaged — relay it.")
def neighbors(id: str, edge_filter: list[str] | None = None, direction: str = "out",
              depth: int = 1) -> dict:
    result, degraded = kq.with_degrade(
        lambda c: kq._neighbors(c, id, edge_filter, direction, depth))
    return _merge("neighbors", result, degraded)


@server.tool(name="relate", description="The shortest edge path between two entities (edges followed "
             "in either direction) as an ordered id list, or null if they are not connected. A `degraded` "
             "key may accompany the result when the committed map was absent or damaged — relay it.")
def relate(id_a: str, id_b: str) -> dict:
    result, degraded = kq.with_degrade(lambda c: kq._relate(c, id_a, id_b))
    return _merge("path", result, degraded)


def main() -> None:
    server.run()  # stdio transport by default


if __name__ == "__main__":
    main()
