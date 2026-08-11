"""mcp_test_support.py — the ONE place tests talk MCP to an engine server.

Both MCP server test suites (tools/test_knowledge_query.py and tools/memory/test_mcp_server.py) exercise
their server through the SDK's in-memory client via these helpers, so the suite proves what a real caller
sees — registration, schema generation, serialization — not what the server object's internals return. The
result-unwrapping lives here and nowhere else: the SDK has renamed its server class once and reshaped its
call-tool result once already, so the next churn touches this file alone, not every call site.

TWO FACTS THIS FILE ENCODES, both established against the real mcp 2.0.0 package:

  * A raising tool does NOT raise here. Over the protocol the SDK catches the exception and returns an
    error RESULT (`is_error=True` with the message as text content) — unlike a direct call on the server
    object, which propagates. `call_tool_json` therefore asserts not-error before parsing, so a tool that
    starts failing can never read as a green test; `call_tool_expect_error` is the deliberate path for
    asserting a refusal.
  * The JSON payload rides in the text content. A `-> dict` annotation does not populate
    `structured_content` in 2.0.0 (measured, not assumed), so parsing `content[0].text` is the honest
    unwrap, not a legacy habit.
"""
from __future__ import annotations
import json

from mcp import Client


async def call_tool_json(server, name: str, args: dict) -> dict:
    """Call tool `name` on `server` through an in-memory client and return its JSON payload.

    Raises AssertionError when the tool errored — the protocol reports that as an error result, not an
    exception, and an unchecked result would let a broken tool pass green."""
    async with Client(server) as client:
        res = await client.call_tool(name, args)
        if res.is_error:
            text = res.content[0].text if res.content else "<no content>"
            raise AssertionError(f"tool {name!r} errored: {text}")
        if not res.content:
            # A future SDK minor shifting success payloads to structured_content-only would land here as a
            # diagnosable message, not a bare IndexError — SDK-shape churn is this file's job to contain.
            raise AssertionError(f"tool {name!r} succeeded but returned no text content "
                                 f"(structured_content={res.structured_content!r}) — the SDK's result "
                                 f"shape has changed; update mcp_test_support to match")
        return json.loads(res.content[0].text)


async def call_tool_expect_error(server, name: str, args: dict) -> str:
    """Call tool `name` expecting a tool-level refusal; return the error text.

    The counterpart to `call_tool_json`: a test asserting that bad arguments are refused uses this, because
    over the protocol the refusal arrives as `is_error=True`, never as a raised exception."""
    async with Client(server) as client:
        res = await client.call_tool(name, args)
        if not res.is_error:
            raise AssertionError(f"tool {name!r} unexpectedly succeeded with {args!r}")
        return res.content[0].text if res.content else ""


async def list_tool_objects(server) -> list:
    """The server's tool list as seen by a real client — objects with `.name` and `.description`."""
    async with Client(server) as client:
        return list((await client.list_tools()).tools)


async def stdio_health(engine_dir: str, script_rel: str, timeout_s: float = 120.0) -> dict:
    """Launch a server exactly as .mcp.json does — a real `uv run --frozen` subprocess over stdio — complete
    the protocol handshake, call `health`, and return its payload.

    This is the launch-seam smoke test the in-memory client cannot provide: it is the only coverage of
    `server.run()`, the frozen-environment resolution, and the stdio handshake — the seam where a dead
    server fails SILENTLY in a deployment (it simply never appears in the model's tool list). The timeout
    bounds the first-launch case where uv must still materialize the environment.

    HEALTH-ONLY BY DESIGN. The stdio client hands the child an allowlisted environment (HOME/PATH/…), so
    a test override like ENGINE_MEMORY_DIR CANNOT reach it — a richer call here would hit the operator's
    REAL store, not a test cabinet. `health` is content-free, which is what makes this safe; keep it so.

    A failure here re-raises with the command and the likely causes named — a bare TimeoutError or a
    nested ExceptionGroup explains nothing to the person whose server just silently failed to launch.

    THE ASYNCIO LOGGER IS SILENCED FOR THE CALL'S DURATION, and the reason is an SDK import side effect
    this file exists to absorb: `MCPServer.__init__` reconfigures the ROOT logger (logging.basicConfig
    with a real stderr handle) the moment a server module is imported, which binds a handler BEFORE
    unittest's `-b` buffering takes hold — so asyncio's subprocess-transport chatter (loops run in debug
    mode under IsolatedAsyncioTestCase) would otherwise print on passing tests even under `-b`, defeating
    CI's discard-on-success hygiene. Measured, not assumed."""
    import asyncio
    import logging

    from mcp import ClientSession, StdioServerParameters, stdio_client

    argv = ["uv", "run", "--directory", engine_dir, "--frozen", "--", "python", script_rel]
    params = StdioServerParameters(command=argv[0], args=argv[1:])

    async def _go() -> dict:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                res = await session.call_tool("health", {})
                return json.loads(res.content[0].text)

    asyncio_log = logging.getLogger("asyncio")
    prev_level = asyncio_log.level
    asyncio_log.setLevel(logging.WARNING)
    try:
        return await asyncio.wait_for(_go(), timeout=timeout_s)
    except TimeoutError as e:
        raise AssertionError(
            f"the MCP server did not answer `health` within {timeout_s:.0f}s.\n"
            f"  launch: {' '.join(argv)}\n"
            f"  Likely causes: a first launch fetching packages without a network connection, or the "
            f"server crashing before the handshake. Run the launch command by hand to see its stderr."
        ) from e
    except Exception as e:  # cancellation and interrupts pass through untouched — they are not launch failures
        raise AssertionError(
            f"the MCP server failed to launch or complete the stdio handshake.\n"
            f"  launch: {' '.join(argv)}\n"
            f"  Underlying error: {type(e).__name__}: {e}\n"
            f"  Run the launch command by hand to see the server's own stderr."
        ) from e
    finally:
        asyncio_log.setLevel(prev_level)
