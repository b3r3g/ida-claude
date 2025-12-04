"""
Agentic loop for Claude-powered IDA analysis.

This module handles:
- Conversation management
- Tool execution cycle
- Error handling and retries
- Context management
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from .client import ClaudeClient, Response, ToolCall
from .tools import execute as execute_tool, to_claude_format


@dataclass
class ToolResult:
    """Result of a tool execution."""

    tool_call_id: str
    success: bool
    result: Any
    error: str | None = None


@dataclass
class LoopConfig:
    """Configuration for the agentic loop."""

    max_iterations: int = 50  # Prevent infinite loops
    max_consecutive_errors: int = 3  # Stop after repeated failures
    doom_loop_threshold: int = 3  # Detect repeated identical tool calls


SYSTEM_PROMPT = """You are Claude IDA.
You are an interactive tool embedded in IDA Pro that helps users with reverse engineering tasks, among other things. Use the instructions below and the tools available to you to assist the user.


# Tone and style
- Only use emojis if the user explicitly requests it. Avoid using emojis in all communication unless asked.
- Your output will be displayed on a command line interface. Your responses should be short and concise. You can use Github-flavored markdown for formatting, and will be rendered in a monospace font using the CommonMark specification.
- Output text to communicate with the user; all text you output outside of tool use is displayed to the user. Only use tools to complete tasks. Never use tools like Bash or code comments as means to communicate with the user during the session.

# Professional objectivity
Prioritize technical accuracy and truthfulness over validating the user's beliefs. Focus on facts and problem-solving, providing direct, objective technical info without any unnecessary superlatives, praise, or emotional validation. It is best for the user if Claude honestly applies the same rigorous standards to all ideas and disagrees when necessary, even if it may not be what the user wants to hear. Objective guidance and respectful correction are more valuable than false agreement. Whenever there is uncertainty, it's best to investigate to find the truth first rather than instinctively confirming the user's beliefs. Avoid using over-the-top validation or excessive praise when responding to users such as "You're absolutely right" or similar phrases.

# Doing tasks
The user will primarily request you perform reverse engineering tasks. This includes help understanding code, finding things, and annotating the binary, and more. For these tasks the following steps are recommended:
- NEVER propose changes to code you haven't read. If a user asks about or wants you to modify a part, read it first. Understand existing code before suggesting modifications.
- Avoid over-engineering. Only make changes that are directly requested or clearly necessary. Keep solutions simple and focused.

# Tool usage policy
- You can call multiple tools in a single response. If you intend to call multiple tools and there are no dependencies between them, make all independent tool calls in parallel. Maximize use of parallel tool calls where possible to increase efficiency. However, if some tool calls depend on previous calls to inform dependent values, do NOT call these tools in parallel and instead call them sequentially. For instance, if one operation must complete before another starts, run these operations sequentially instead. Never use placeholders or guess missing parameters in tool calls.
- If the user specifies that they want you to run tools "in parallel", you MUST send a single message with multiple tool use content blocks. For example, if you need to read multiple functions in parallel, send a single message with multiple read tool calls.
- When asked about "this function" or "current function", always check the current cursor position first
- After making changes (renaming, commenting), refresh the view
- If a decompilation fails, fall back to disassembly
- Always verify addresses are valid before operations

You have access to tools that let you interact with IDA Pro:
- Read and analyze functions (both disassembly and decompiled code)
- Rename functions and variables
- Add comments
- Search for patterns, strings, and cross-references
- Navigate the binary

"""


class AgentLoop:
    """
    The core agentic loop that drives Claude + IDA interaction.

    Handles the conversation cycle:
    1. User sends message
    2. Claude responds (possibly with tool calls)
    3. Execute tools, feed results back
    4. Repeat until Claude responds without tool calls
    """

    def __init__(
        self,
        client: ClaudeClient,
        config: LoopConfig | None = None,
        system_prompt: str | None = None,
        on_tool_call: Callable[[ToolCall], None] | None = None,
        on_tool_result: Callable[[ToolResult], None] | None = None,
        on_usage: Callable[[dict], None] | None = None,
        on_tool_approve: Callable[[ToolCall], bool] | None = None,
        # Block start callbacks
        on_thinking_start: Callable[[], None] | None = None,
        on_text_start: Callable[[], None] | None = None,
        on_tool_start: Callable[[str, str], None] | None = None,  # (tool_name, tool_id)
        # Block complete callbacks (content shown after block finishes)
        on_thinking_complete: Callable[[str], None] | None = None,
        on_text_complete: Callable[[str], None] | None = None,
    ):
        """
        Initialize the agent loop.

        Args:
            client: Claude API client
            config: Loop configuration
            system_prompt: Custom system prompt (uses default if None)
            on_tool_call: Callback when tool is called (after completion)
            on_tool_result: Callback when tool completes
            on_usage: Callback for usage statistics
            on_tool_approve: Callback to approve tool call (returns True to allow)
            on_thinking_start: Callback when thinking block starts
            on_text_start: Callback when text block starts
            on_tool_start: Callback when tool use block starts (name, id)
            on_thinking_complete: Callback when thinking block completes (full content)
            on_text_complete: Callback when text block completes (full content)
        """
        self.client = client
        self.config = config or LoopConfig()
        self.system_prompt = system_prompt or SYSTEM_PROMPT
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_usage = on_usage
        self.on_tool_approve = on_tool_approve
        self.on_thinking_start = on_thinking_start
        self.on_text_start = on_text_start
        self.on_tool_start = on_tool_start
        self.on_thinking_complete = on_thinking_complete
        self.on_text_complete = on_text_complete

        # Conversation state
        self.messages: list[dict] = []

        # Doom loop detection
        self._recent_tool_calls: list[tuple[str, str]] = []  # (name, args_hash)

        # Cancellation event (thread-safe)
        self._cancelled = threading.Event()

    def _prepare_messages_with_cache(self) -> list[dict]:
        """
        Prepare messages with cache_control on the last message.

        This enables incremental caching of conversation history.
        Each turn, we cache up to the current point so subsequent
        turns can reuse the cached prefix.
        """
        if not self.messages:
            return []

        # Deep copy to avoid mutating original
        import copy

        messages = copy.deepcopy(self.messages)

        # Add cache_control to the last content block of the last message
        last_msg = messages[-1]
        content = last_msg.get("content")

        if isinstance(content, str):
            # Convert string to content block with cache
            last_msg["content"] = [
                {"type": "text", "text": content, "cache_control": {"type": "ephemeral"}}
            ]
        elif isinstance(content, list) and len(content) > 0:
            # Add cache_control to last block
            last_block = content[-1]
            if isinstance(last_block, dict):
                last_block["cache_control"] = {"type": "ephemeral"}

        return messages

    def chat(self, user_message: str, stream: bool = True) -> str:
        """
        Send a user message and get a response.

        This runs the full agentic loop:
        - Send message to Claude
        - Execute any tool calls
        - Feed results back
        - Repeat until done

        Args:
            user_message: The user's message
            stream: Whether to stream the response

        Returns:
            Final assistant response text
        """
        # Add user message
        self.messages.append(
            {
                "role": "user",
                "content": user_message,
            }
        )

        # Reset cancellation flag
        self._cancelled.clear()

        iteration = 0
        consecutive_errors = 0
        final_response = ""

        while iteration < self.config.max_iterations:
            # Check for cancellation
            if self._cancelled.is_set():
                final_response = "[Cancelled by user]"
                break

            iteration += 1

            # Get Claude's response
            tools = to_claude_format()
            cached_messages = self._prepare_messages_with_cache()

            if stream:
                response = self._chat_stream(tools, cached_messages)
            else:
                response = self.client.chat(
                    messages=cached_messages,
                    tools=tools if tools else None,
                    system=self.system_prompt,
                )

            # Add assistant response to history
            assistant_content = self._build_assistant_content(response)
            self.messages.append(
                {
                    "role": "assistant",
                    "content": assistant_content,
                }
            )

            # Report usage stats
            if self.on_usage and response.usage:
                self.on_usage(response.usage)

            # Check if we're done (no tool calls)
            if response.stop_reason != "tool_use" or not response.tool_calls:
                final_response = response.content
                break

            # Execute tool calls
            tool_results = []
            should_stop = False

            for tool_call in response.tool_calls:
                # Check for cancellation before each tool
                if self._cancelled.is_set():
                    should_stop = True
                    break

                if self.on_tool_call:
                    self.on_tool_call(tool_call)

                # Check for approval if callback set
                if self.on_tool_approve:
                    approved = self.on_tool_approve(tool_call)
                    if not approved:
                        result = ToolResult(
                            tool_call_id=tool_call.id,
                            success=False,
                            result=None,
                            error="Tool call rejected by user",
                        )
                        tool_results.append(result)
                        if self.on_tool_result:
                            self.on_tool_result(result)
                        continue

                # Doom loop detection
                if self._is_doom_loop(tool_call):
                    result = ToolResult(
                        tool_call_id=tool_call.id,
                        success=False,
                        result=None,
                        error="Detected repeated identical tool call. Please try a different approach.",
                    )
                else:
                    result = self._execute_tool(tool_call)

                tool_results.append(result)

                if self.on_tool_result:
                    self.on_tool_result(result)

                # Track consecutive errors
                if not result.success:
                    consecutive_errors += 1
                    if consecutive_errors >= self.config.max_consecutive_errors:
                        final_response = f"Stopped due to repeated errors: {result.error}"
                        should_stop = True
                        break
                else:
                    consecutive_errors = 0

            # If cancelled, add "cancelled" results for unexecuted tool calls
            # (same pattern as approval rejection at lines 251-261)
            if should_stop and self._cancelled.is_set():
                executed_ids = {r.tool_call_id for r in tool_results}
                for tc in response.tool_calls:
                    if tc.id not in executed_ids:
                        tool_results.append(
                            ToolResult(
                                tool_call_id=tc.id,
                                success=False,
                                result=None,
                                error="Cancelled by user",
                            )
                        )

            # Add tool results to conversation (for all executed tools)
            tool_result_content = []
            for result in tool_results:
                if result.success:
                    content = (
                        json.dumps(result.result)
                        if not isinstance(result.result, str)
                        else result.result
                    )
                else:
                    content = f"Error: {result.error}"

                tool_result_content.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": result.tool_call_id,
                        "content": content,
                        "is_error": not result.success,
                    }
                )

            if tool_result_content:
                self.messages.append(
                    {
                        "role": "user",
                        "content": tool_result_content,
                    }
                )

            if should_stop:
                break

        return final_response

    def _chat_stream(self, tools: list[dict], messages: list[dict]) -> Response:
        """Stream a chat response, calling on_text for each chunk."""
        content = ""
        tool_calls = []
        thinking_blocks = []
        current_thinking = {"thinking": "", "signature": None}
        usage = None

        for delta in self.client.chat_stream(
            messages=messages,
            tools=tools if tools else None,
            system=self.system_prompt,
        ):
            # Block start events
            if delta.type == "thinking_start":
                if self.on_thinking_start:
                    self.on_thinking_start()
            elif delta.type == "text_start":
                if self.on_text_start:
                    self.on_text_start()
            elif delta.type == "tool_start":
                if self.on_tool_start:
                    self.on_tool_start(delta.tool_name, delta.tool_id)
            # Block complete events (content buffered, shown when block ends)
            elif delta.type == "thinking_complete" and delta.thinking:
                current_thinking["thinking"] = delta.thinking
                current_thinking["signature"] = delta.signature  # Capture signature
                if self.on_thinking_complete:
                    self.on_thinking_complete(delta.thinking)
            elif delta.type == "text_complete" and delta.text:
                content = delta.text
                if self.on_text_complete:
                    self.on_text_complete(delta.text)
            elif delta.type == "redacted_thinking" and delta.redacted_data:
                # Redacted thinking blocks are complete, add directly
                thinking_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": delta.redacted_data,
                    }
                )
            elif delta.type == "tool_use" and delta.tool_call:
                # Finalize thinking block before tool use
                if current_thinking["thinking"]:
                    thinking_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": current_thinking["thinking"],
                            "signature": current_thinking["signature"],
                        }
                    )
                    current_thinking = {"thinking": "", "signature": None}
                tool_calls.append(delta.tool_call)
            elif delta.type == "usage" and delta.usage:
                usage = delta.usage
            elif delta.type == "done":
                # Finalize any remaining thinking
                if current_thinking["thinking"]:
                    thinking_blocks.append(
                        {
                            "type": "thinking",
                            "thinking": current_thinking["thinking"],
                            "signature": current_thinking["signature"],
                        }
                    )
                break

        # Determine stop reason
        stop_reason = "tool_use" if tool_calls else "end_turn"

        return Response(
            content=content,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            usage=usage,
            thinking_blocks=thinking_blocks if thinking_blocks else None,
        )

    def _build_assistant_content(self, response: Response) -> list[dict]:
        """Build the assistant message content block."""
        content = []

        # Thinking blocks must come first (required for tool use with thinking)
        if response.thinking_blocks:
            content.extend(response.thinking_blocks)

        if response.content:
            content.append(
                {
                    "type": "text",
                    "text": response.content,
                }
            )

        for tc in response.tool_calls:
            content.append(
                {
                    "type": "tool_use",
                    "id": tc.id,
                    "name": tc.name,
                    "input": tc.input,
                }
            )

        return content if content else [{"type": "text", "text": ""}]

    def _execute_tool(self, tool_call: ToolCall) -> ToolResult:
        """Execute a single tool call."""
        try:
            result = execute_tool(tool_call.name, tool_call.input)
            return ToolResult(
                tool_call_id=tool_call.id,
                success=True,
                result=result,
            )
        except KeyError:
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                result=None,
                error=f"Unknown tool: {tool_call.name}",
            )
        except Exception as e:
            return ToolResult(
                tool_call_id=tool_call.id,
                success=False,
                result=None,
                error=f"{type(e).__name__}: {str(e)}",
            )

    def _is_doom_loop(self, tool_call: ToolCall) -> bool:
        """Check if we're in a doom loop (repeated identical calls)."""
        # Create a hash of the call
        args_hash = json.dumps(tool_call.input, sort_keys=True)
        call_sig = (tool_call.name, args_hash)

        # Check recent calls
        recent_same = sum(1 for c in self._recent_tool_calls if c == call_sig)

        # Add to history (keep last N)
        self._recent_tool_calls.append(call_sig)
        if len(self._recent_tool_calls) > 10:
            self._recent_tool_calls.pop(0)

        return recent_same >= self.config.doom_loop_threshold

    def clear_history(self):
        """Clear conversation history."""
        self.messages.clear()
        self._recent_tool_calls.clear()

    def cancel(self):
        """Cancel the current operation."""
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        """Check if the current operation is cancelled."""
        return self._cancelled.is_set()
