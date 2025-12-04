"""
Claude API client wrapper.

Handles communication with the Anthropic API, including:
- Message creation
- Streaming responses
- Tool calling
- Prompt caching (for system prompts)
"""

from collections.abc import Generator
from dataclasses import dataclass
from typing import Any

import anthropic


@dataclass
class ToolCall:
    """Represents a tool call request from Claude."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass
class StreamDelta:
    """A chunk of streamed response."""

    # Types: "text", "tool_use", "done", "usage", "thinking", "signature", "redacted_thinking"
    #        "thinking_start", "text_start", "tool_start" (block start events)
    type: str
    text: str | None = None
    tool_call: ToolCall | None = None
    usage: dict | None = None
    thinking: str | None = None
    signature: str | None = None
    redacted_data: str | None = None  # For redacted_thinking blocks
    # For block start events
    tool_name: str | None = None
    tool_id: str | None = None


@dataclass
class Response:
    """Complete response from Claude."""

    content: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn", "tool_use", "max_tokens"
    usage: dict | None = None  # Token usage including cache info
    thinking_blocks: list[dict] | None = None  # Preserved for tool use continuations


@dataclass
class ModelInfo:
    """Information about an available model."""

    id: str
    display_name: str


class ClaudeClient:
    """Wrapper for the Anthropic API."""

    def __init__(
        self,
        api_key: str,
        model: str = "claude-sonnet-4-20250514",
        max_tokens: int = 8192,
        enable_caching: bool = True,
        thinking_enabled: bool = False,
        thinking_budget: int = 10000,
        interleaved_thinking: bool = True,
    ):
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.enable_caching = enable_caching
        self.thinking_enabled = thinking_enabled
        self.thinking_budget = thinking_budget
        self.interleaved_thinking = interleaved_thinking

    def _get_extra_headers(self) -> dict | None:
        """Get extra headers for API requests.

        For direct Claude API calls, interleaved-thinking header has no effect
        on non-Claude-4 models, so we can always pass it safely.
        """
        if self.thinking_enabled and self.interleaved_thinking:
            return {"anthropic-beta": "interleaved-thinking-2025-05-14"}
        return None

    def list_models(self) -> list[ModelInfo]:
        """List available models."""
        models = []
        page = self.client.models.list(limit=100)
        for m in page.data:
            models.append(ModelInfo(id=m.id, display_name=m.display_name))
        return models

    def set_model(self, model_id: str):
        """Change the active model."""
        self.model = model_id

    def _make_system_blocks(self, system: str | None) -> list[dict] | None:
        """Convert system prompt to blocks with caching enabled."""
        if not system:
            return None

        if not self.enable_caching:
            return system  # Return as plain string

        # Return as list of content blocks with cache_control on last block
        return [
            {
                "type": "text",
                "text": system,
                "cache_control": {"type": "ephemeral"},  # 5-minute cache
            }
        ]

    def _make_tools_with_cache(self, tools: list[dict] | None) -> list[dict] | None:
        """Add cache_control to the last tool definition."""
        if not tools:
            return None

        if not self.enable_caching or len(tools) == 0:
            return tools

        # Copy tools and add cache_control to the last one
        cached_tools = [t.copy() for t in tools]
        cached_tools[-1] = {**cached_tools[-1], "cache_control": {"type": "ephemeral"}}
        return cached_tools

    @staticmethod
    def _extract_usage(response) -> dict | None:
        """Extract usage info including cache stats from a response."""
        if not hasattr(response, "usage") or response.usage is None:
            return None
        usage = response.usage
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", None) or 0,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", None) or 0,
        }

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> Response:
        """
        Send a chat request and get a complete response.

        Args:
            messages: Conversation history
            tools: Tool definitions (Claude format)
            system: System prompt

        Returns:
            Response with content and any tool calls
        """
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = self._make_tools_with_cache(tools)
        if system:
            kwargs["system"] = self._make_system_blocks(system)
        if self.thinking_enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
        extra_headers = self._get_extra_headers()
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        response = self.client.messages.create(**kwargs)

        # Parse response
        content = ""
        tool_calls = []
        thinking_blocks = []

        for block in response.content:
            if block.type == "text":
                content += block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        input=block.input,
                    )
                )
            elif block.type == "thinking":
                # Preserve thinking blocks for tool use continuations
                thinking_blocks.append(
                    {
                        "type": "thinking",
                        "thinking": block.thinking,
                        "signature": block.signature,
                    }
                )
            elif block.type == "redacted_thinking":
                # Preserve redacted thinking blocks too
                thinking_blocks.append(
                    {
                        "type": "redacted_thinking",
                        "data": block.data,
                    }
                )

        usage = self._extract_usage(response)

        return Response(
            content=content,
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            usage=usage,
            thinking_blocks=thinking_blocks if thinking_blocks else None,
        )

    def chat_stream(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        system: str | None = None,
    ) -> Generator[StreamDelta, None, Response]:
        """
        Send a chat request and stream the response.

        Args:
            messages: Conversation history
            tools: Tool definitions (Claude format)
            system: System prompt

        Yields:
            StreamDelta chunks

        Returns:
            Response object with thinking_blocks for tool use preservation
        """
        kwargs = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if tools:
            kwargs["tools"] = self._make_tools_with_cache(tools)
        if system:
            kwargs["system"] = self._make_system_blocks(system)
        if self.thinking_enabled:
            kwargs["thinking"] = {
                "type": "enabled",
                "budget_tokens": self.thinking_budget,
            }
        extra_headers = self._get_extra_headers()
        if extra_headers:
            kwargs["extra_headers"] = extra_headers

        content = ""
        tool_calls = []
        thinking_blocks = []
        current_tool: dict | None = None
        current_thinking: dict | None = None
        current_text_block = False  # Track if we're in a text block

        with self.client.messages.stream(**kwargs) as stream:
            for event in stream:
                if event.type == "content_block_start":
                    if event.content_block.type == "tool_use":
                        current_tool = {
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "input_json": "",
                        }
                        # Yield tool start event immediately
                        yield StreamDelta(
                            type="tool_start",
                            tool_name=event.content_block.name,
                            tool_id=event.content_block.id,
                        )
                    elif event.content_block.type == "thinking":
                        current_thinking = {
                            "type": "thinking",
                            "thinking": "",
                            "signature": None,
                        }
                        # Yield thinking start event immediately
                        yield StreamDelta(type="thinking_start")
                    elif event.content_block.type == "text":
                        current_text_block = True
                        # Yield text start event immediately
                        yield StreamDelta(type="text_start")
                    elif event.content_block.type == "redacted_thinking":
                        # Redacted thinking blocks come complete, not streamed
                        redacted_block = {
                            "type": "redacted_thinking",
                            "data": event.content_block.data,
                        }
                        thinking_blocks.append(redacted_block)
                        yield StreamDelta(
                            type="redacted_thinking", redacted_data=event.content_block.data
                        )

                elif event.type == "content_block_delta":
                    if event.delta.type == "text_delta":
                        content += event.delta.text
                        # Don't yield delta - content will be shown on block complete
                    elif event.delta.type == "input_json_delta":
                        if current_tool:
                            current_tool["input_json"] += event.delta.partial_json
                    elif event.delta.type == "thinking_delta":
                        if current_thinking:
                            current_thinking["thinking"] += event.delta.thinking
                        # Don't yield delta - content will be shown on block complete
                    elif event.delta.type == "signature_delta" and current_thinking:
                        current_thinking["signature"] = event.delta.signature

                elif event.type == "content_block_stop":
                    if current_tool:
                        import json

                        try:
                            tool_input = (
                                json.loads(current_tool["input_json"])
                                if current_tool["input_json"]
                                else {}
                            )
                        except json.JSONDecodeError:
                            tool_input = {}
                        tool_calls.append(
                            ToolCall(
                                id=current_tool["id"],
                                name=current_tool["name"],
                                input=tool_input,
                            )
                        )
                        yield StreamDelta(type="tool_use", tool_call=tool_calls[-1])
                        current_tool = None
                    elif current_thinking:
                        # Yield thinking complete with full content and signature
                        yield StreamDelta(
                            type="thinking_complete",
                            thinking=current_thinking["thinking"],
                            signature=current_thinking["signature"],
                        )
                        thinking_blocks.append(current_thinking)
                        current_thinking = None
                    elif current_text_block:
                        # Yield text complete with full content
                        yield StreamDelta(type="text_complete", text=content)
                        current_text_block = False

                elif event.type == "message_stop":
                    pass  # Will handle after getting final message

            # Get final message for stop_reason and usage
            final = stream.get_final_message()
            usage = self._extract_usage(final)

            # Yield usage before done
            if usage:
                yield StreamDelta(type="usage", usage=usage)
            yield StreamDelta(type="done")

            # Return response with thinking blocks for tool use preservation
            return Response(
                content=content,
                tool_calls=tool_calls,
                stop_reason=final.stop_reason,
                usage=usage,
                thinking_blocks=thinking_blocks if thinking_blocks else None,
            )
