"""
Conversation persistence for IDA Claude.

Saves and loads conversation history (AgentLoop.messages format) to enable
resuming conversations across sessions.
"""

import json
import uuid
from datetime import datetime
from pathlib import Path


class ConversationManager:
    """Manages conversation persistence."""

    def __init__(self):
        self._current_id: str | None = None
        self._conversations_dir = self._get_conversations_dir()
        self._conversations_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _get_conversations_dir() -> Path:
        """Get the conversations directory path."""
        try:
            import ida_diskio

            user_dir = ida_diskio.get_user_idadir()
            return Path(user_dir) / "ida_claude_conversations"
        except ImportError:
            return Path.home() / ".ida_claude" / "conversations"

    @property
    def current_id(self) -> str | None:
        """Get current conversation ID."""
        return self._current_id

    def new_conversation(self) -> str:
        """Start a new conversation, return its ID."""
        self._current_id = str(uuid.uuid4())
        return self._current_id

    def save_agent_messages(self, messages: list):
        """Save AgentLoop.messages directly to current conversation."""
        if not messages:
            return

        # Auto-create conversation if none exists
        if not self._current_id:
            self.new_conversation()

        # Extract title from first user message
        title = "New Conversation"
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    title = content[:50].strip()
                    if len(content) > 50:
                        title += "..."
                break

        now = datetime.utcnow().isoformat() + "Z"
        path = self._conversations_dir / f"{self._current_id}.json"

        # Load existing to preserve created_at
        created_at = now
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    existing = json.load(f)
                    created_at = existing.get("created_at", now)
            except Exception:
                pass

        data = {
            "id": self._current_id,
            "title": title,
            "created_at": created_at,
            "updated_at": now,
            "messages": messages,
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    def load_conversation(self, conv_id: str) -> list | None:
        """Load a conversation by ID, return messages for AgentLoop."""
        path = self._conversations_dir / f"{conv_id}.json"
        if not path.exists():
            return None

        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            self._current_id = conv_id
            return data.get("messages", [])
        except Exception:
            return None

    def list_conversations(self) -> list[dict]:
        """List all saved conversations (id, title, updated_at)."""
        result = []
        for f in self._conversations_dir.glob("*.json"):
            try:
                with open(f, encoding="utf-8") as fp:
                    data = json.load(fp)
                    result.append(
                        {
                            "id": data.get("id", f.stem),
                            "title": data.get("title", "Untitled"),
                            "updated_at": data.get("updated_at", ""),
                        }
                    )
            except Exception:
                continue

        # Sort by updated_at descending (most recent first)
        result.sort(key=lambda x: x["updated_at"], reverse=True)
        return result

    def delete_conversation(self, conv_id: str) -> bool:
        """Delete a conversation by ID."""
        path = self._conversations_dir / f"{conv_id}.json"
        if path.exists():
            path.unlink()
            # Clear current if deleted
            if self._current_id == conv_id:
                self._current_id = None
            return True
        return False

    def get_conversation_title(self, conv_id: str) -> str:
        """Get title of a conversation."""
        path = self._conversations_dir / f"{conv_id}.json"
        if path.exists():
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                return data.get("title", "Untitled")
            except Exception:
                pass
        return "Untitled"


# Global instance
_manager: ConversationManager | None = None


def get_conversation_manager() -> ConversationManager:
    """Get the global conversation manager."""
    global _manager
    if _manager is None:
        _manager = ConversationManager()
    return _manager
