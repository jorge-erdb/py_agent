import json
import asyncio
import logging
from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

class Tool:
    """Base class for all agent tools."""
    def __init__(self, name: str, description: str, func, parameters: dict = None):
        self.name = name
        self.description = description
        self.func = func
        self.parameters = parameters or {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs):
        if asyncio.iscoroutinefunction(self.func):
            return await self.func(**kwargs)
        return self.func(**kwargs)

class Agent:
    def __init__(self, name: str, persona: str, base_url: str = "http://localhost:8080/v1", api_key: str = "sk-no-key-needed", model: str = "local-model", timeout: int = 600, initial_history: list[dict] = None, system_prompt: str = None):
        """
        Args:
            name: Agent name used in system prompt.
            persona: System prompt description.
            base_url: OpenAI-compatible API URL.
            api_key: API key for the LLM server.
            model: Model name sent to the LLM.
            timeout: Request timeout in **seconds** (default 600 / 10 min).
            initial_history: Initial conversation history.
            system_prompt: Prebuilt system prompt. Built once at process boot
                and shared by every agent on the instance — see agent.prompt.
                Falls back to name + persona when not supplied.
        """
        self.name = name
        self.persona = persona
        self.model = model
        self.system_prompt = system_prompt or f"You are {name}. {persona}"
        # Async client: the reasoning loop runs inside the server's event loop,
        # so a blocking call here would stall every other session for the full
        # duration of inference.
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout)
        
        if initial_history is not None:
            self.history = initial_history
        else:
            self.history = [
                {"role": "system", "content": self.system_prompt}
            ]
        self.tools = {}

    def register_tool(self, tool: Tool):
        """Registers a tool for the agent to use."""
        self.tools[tool.name] = tool

    async def _get_tool_definitions(self):
        """Converts registered tools to OpenAI tool format."""
        if not self.tools:
            return None
        return [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                },
            }
            for tool in self.tools.values()
        ]

    async def process_messages(self, new_messages: list[dict]):
        """
        Async generator that yields each agent message incrementally
        so the frontend can render them immediately.

        Yields dicts with keys: role, content, and optionally tool_calls,
        tool_call_id.

        The final yield is a sentinel: {"_final": True, "response": ..., "history": ...}
        """
        for msg in new_messages:
            self.history.append(msg)

        while True:
            # 1. Get response from LLM
            try:
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=self.history,
                    tools=await self._get_tool_definitions() if self.tools else None,
                    tool_choice="auto" if self.tools else None
                )
            except Exception as e:
                logger.error("LLM call failed: %s", e)
                yield {"role": "system", "content": f"Error contacting the model: {str(e)}"}
                # Break so history stays clean and the session can be reused
                break

            message = response.choices[0].message

            # Build hist_entry but don't append to history yet
            hist_entry = {
                "role": message.role or "assistant",
                "content": message.content,
            }

            if message.tool_calls:
                hist_entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        }
                    }
                    for tc in message.tool_calls
                ]

            # Yield the assistant message immediately
            yield hist_entry

            # Now append to history — only after we've yielded successfully
            self.history.append(hist_entry)

            # 2. Execute tool calls one at a time, yielding each result
            if message.tool_calls:
                for tool_call in message.tool_calls:
                    function_name = tool_call.function.name

                    # Local models emit malformed `arguments` often enough that a
                    # parse failure has to become a tool result rather than an
                    # exception: raising here tears down the NDJSON stream, and
                    # since the turn is only persisted once this generator
                    # finishes, the whole turn would be lost. Handing the error
                    # back as the result lets the model correct itself instead.
                    try:
                        function_args = json.loads(tool_call.function.arguments)
                        if not isinstance(function_args, dict):
                            raise ValueError("expected a JSON object")
                    except (json.JSONDecodeError, ValueError, TypeError) as e:
                        logger.warning("Malformed arguments for %s: %s", function_name, e)
                        function_args = {}
                        result_str = (
                            f"Error: could not parse the arguments to {function_name} as JSON: {e}. "
                            "Re-issue the call with valid JSON."
                        )
                    else:
                        logger.info("Tool call: %s(%s)", function_name, function_args)

                        if function_name in self.tools:
                            try:
                                result = await self.tools[function_name].execute(**function_args)
                                result_str = str(result)
                            except Exception as e:
                                logger.error("Tool %s error: %s", function_name, e)
                                result_str = f"Error: {str(e)}"
                        else:
                            logger.warning("Unknown tool: %s", function_name)
                            result_str = f"Error: Tool {function_name} not found."

                    tool_entry = {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": result_str,
                        "command": function_args.get("command"),
                        "tool_name": function_name,
                    }
                    self.history.append(tool_entry)
                    yield tool_entry

                # After processing all tool calls, loop back to call LLM again
                continue

            else:
                # No more tool calls, this is the final answer
                logger.info("Final response: %s", (message.content or "")[:200])
                yield {"_final": True, "response": message.content or "", "history": self.history.copy()}
                break
