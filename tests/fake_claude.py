"""A scripted model behind the real Claude Agent SDK client.

The SDK talks to the Claude Code CLI over a Transport. This one plays the CLI's side of that protocol from a
script, so the assistant's tests run everything that is Bosun's and the SDK's (the client, the control
protocol, the in-process MCP bridge, the tool server, the approval gate, the registry) with no network and no
Claude. A script is an `async def script(model)`:

    async def script(model):
        runs = await model.call("list_runs", {"status": "problems"})   # a real tool call, through the SDK
        await model.say(f"It failed: {runs['result'][0]['error']}")

`model.call` returns the tool result's JSON as the model would read it. `model.prompt` is what it was sent.
"""

import asyncio
import json
import uuid

from claude_agent_sdk import Transport

SERVER = "bosun"
COST = 0.0123  # what one scripted turn "costs"


class Model:
    """What a script can do: see its prompt and its tools, call a tool, say something."""

    def __init__(self, cli: "FakeClaude", prompt: str):
        self._cli = cli
        self.prompt = prompt
        self.calls: list[tuple[str, dict]] = []
        self.results: list[dict] = []
        self._handshake = False
        self.tools: dict[str, dict] = {}

    async def _mcp(self, method: str, params: dict | None = None, notify: bool = False) -> dict:
        message = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            message["id"] = self._cli.next_id()
        reply = await self._cli.control({"subtype": "mcp_message", "server_name": SERVER, "message": message})
        return reply.get("mcp_response", {})

    async def list_tools(self) -> dict[str, dict]:
        if not self._handshake:
            await self._mcp("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                           "clientInfo": {"name": "fake-claude", "version": "0"}})
            await self._mcp("notifications/initialized", notify=True)
            listed = await self._mcp("tools/list", {})
            self.tools = {t["name"]: t for t in listed["result"]["tools"]}
            self._handshake = True
        return self.tools

    async def call(self, tool: str, arguments: dict | None = None) -> dict:
        await self.list_tools()
        use_id = f"toolu_{uuid.uuid4().hex[:12]}"
        self.calls.append((tool, arguments or {}))
        self._cli.emit({"type": "assistant", "session_id": "fake", "message": {
            "model": "claude-fake", "id": f"msg_{uuid.uuid4().hex[:8]}", "role": "assistant",
            "content": [{"type": "tool_use", "id": use_id, "name": f"mcp__{SERVER}__{tool}", "input": arguments or {}}]}})
        reply = await self._mcp("tools/call", {"name": tool, "arguments": arguments or {}})
        content = (reply.get("result") or {}).get("content") or [{"type": "text", "text": json.dumps(reply.get("error"))}]
        self._cli.emit({"type": "user", "session_id": "fake", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use_id, "content": content}]}})
        self._cli.steps += 1
        parsed = json.loads(content[0]["text"])
        self.results.append(parsed)
        return parsed

    async def say(self, text: str) -> None:
        half = len(text) // 2
        for piece in (text[:half], text[half:]):
            self._cli.emit({"type": "stream_event", "uuid": uuid.uuid4().hex, "session_id": "fake",
                            "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}}})
            await asyncio.sleep(0)
        self._cli.emit({"type": "assistant", "session_id": "fake", "message": {
            "model": "claude-fake", "id": f"msg_{uuid.uuid4().hex[:8]}", "role": "assistant",
            "content": [{"type": "text", "text": text}]}})


class FakeClaude(Transport):
    def __init__(self, script, options=None):
        self.script, self.options = script, options
        self.model: Model | None = None
        self.interrupted = False
        self.steps = 1
        self._out: asyncio.Queue | None = None
        self._waiting: dict[str, asyncio.Future] = {}
        self._task: asyncio.Task | None = None
        self._ids = 0
        self._ready = False

    def next_id(self) -> int:
        self._ids += 1
        return self._ids

    def emit(self, message: dict) -> None:
        assert self._out is not None
        self._out.put_nowait(message)

    async def control(self, request: dict) -> dict:
        """A control request from the CLI's side (an MCP message for Bosun's in-process server)."""
        request_id = f"cli_{self.next_id()}"
        future = self._waiting[request_id] = asyncio.get_running_loop().create_future()
        self.emit({"type": "control_request", "request_id": request_id, "request": request})
        return await future

    # --- Transport -------------------------------------------------------------------------------------------------
    async def connect(self) -> None:
        self._out = asyncio.Queue()
        self._ready = True

    async def write(self, data: str) -> None:
        for line in data.splitlines():
            if not line.strip():
                continue
            message = json.loads(line)
            kind = message.get("type")
            if kind == "control_request":
                subtype = message["request"]["subtype"]
                self.emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": message["request_id"], "response": {}}})
                if subtype == "interrupt":
                    self.interrupted = True
                    if self._task is not None:
                        self._task.cancel()
            elif kind == "control_response":
                response = message["response"]
                future = self._waiting.pop(response["request_id"], None)
                if future is not None and not future.done():
                    if response.get("subtype") == "error":
                        future.set_exception(RuntimeError(response.get("error")))
                    else:
                        future.set_result(response.get("response") or {})
            elif kind == "user":
                content = message["message"]["content"]
                self.model = Model(self, content if isinstance(content, str) else json.dumps(content))
                self._task = asyncio.create_task(self._play())

    async def _play(self) -> None:
        assert self.model is not None
        self.emit({"type": "system", "subtype": "init", "session_id": "fake", "tools": [], "model": "claude-fake"})
        subtype, error, reason = "success", False, "completed"
        try:
            await self.script(self.model)
        except asyncio.CancelledError:
            subtype, error, reason = "error_during_execution", True, "aborted_tools"
        except Exception as exc:  # a broken script is a failed turn, loudly
            subtype, error, reason = "error_during_execution", True, f"script: {exc!r}"
        self.emit({"type": "result", "subtype": subtype, "duration_ms": 12, "duration_api_ms": 10, "is_error": error,
                   "num_turns": self.steps, "session_id": "fake", "total_cost_usd": COST, "terminal_reason": reason,
                   "result": reason if error else "done",
                   "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 50,
                             "cache_creation_input_tokens": 10},
                   "modelUsage": {"claude-fake": {"inputTokens": 100, "outputTokens": 20, "cacheReadInputTokens": 50,
                                                  "cacheCreationInputTokens": 10, "costUSD": COST}}})

    async def read_messages(self):
        assert self._out is not None
        while True:
            message = await self._out.get()
            if message is None:
                return
            yield message

    async def close(self) -> None:
        self._ready = False
        if self._task is not None:
            self._task.cancel()
        if self._out is not None:
            self._out.put_nowait(None)

    def is_ready(self) -> bool:
        return self._ready

    async def end_input(self) -> None:
        pass
