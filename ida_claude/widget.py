"""
Custom chat widget for IDA Claude.

Block-based chat UI where each message is a separate widget.
"""

import html
import json
import threading

import ida_kernwin
import idaapi
import idc
from PySide6.QtCore import QObject, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

try:
    import markdown
except ImportError as e:
    raise ImportError("markdown library required: pip install markdown") from e


class SettingsDialog(QDialog):
    """Settings dialog for API key and other config."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Claude Settings")
        self.setMinimumWidth(400)

        layout = QVBoxLayout(self)

        # Form layout for settings
        form = QFormLayout()

        # API Key
        self.api_key_edit = QLineEdit()
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit.setPlaceholderText("sk-ant-...")
        form.addRow("API Key:", self.api_key_edit)

        # Show/hide API key button
        self.show_key_btn = QPushButton("Show")
        self.show_key_btn.setCheckable(True)
        self.show_key_btn.toggled.connect(self._toggle_key_visibility)
        form.addRow("", self.show_key_btn)

        # Max tokens
        self.max_tokens_edit = QLineEdit()
        self.max_tokens_edit.setPlaceholderText("8192")
        form.addRow("Max Tokens:", self.max_tokens_edit)

        # Interleaved thinking (thinking between tool calls)
        self.interleaved_checkbox = QCheckBox("Interleaved thinking (Claude 4+)")
        self.interleaved_checkbox.setToolTip("Allow Claude to think between tool calls")
        form.addRow("", self.interleaved_checkbox)

        layout.addLayout(form)

        # Config file path info
        from .config import Config

        config_path = Config._config_path()
        path_label = QLabel(f"Config: {config_path}")
        path_label.setStyleSheet("color: #666; font-size: 10px;")
        path_label.setWordWrap(True)
        layout.addWidget(path_label)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # Load current config
        self._load_config()

    def _load_config(self):
        from .config import get_config

        config = get_config()
        self.api_key_edit.setText(config.api_key)
        self.max_tokens_edit.setText(str(config.max_tokens))
        self.interleaved_checkbox.setChecked(config.interleaved_thinking)

    def _toggle_key_visibility(self, checked: bool):
        if checked:
            self.api_key_edit.setEchoMode(QLineEdit.Normal)
            self.show_key_btn.setText("Hide")
        else:
            self.api_key_edit.setEchoMode(QLineEdit.Password)
            self.show_key_btn.setText("Show")

    def get_values(self) -> dict:
        """Get the edited values."""
        try:
            max_tokens = int(self.max_tokens_edit.text() or "8192")
        except ValueError:
            max_tokens = 8192
        return {
            "api_key": self.api_key_edit.text().strip(),
            "max_tokens": max_tokens,
            "interleaved_thinking": self.interleaved_checkbox.isChecked(),
        }


class ConversationListDialog(QDialog):
    """Dialog to list, select, and delete conversations."""

    conversation_selected = Signal(str)  # Emits conversation ID (empty = new)

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.setWindowTitle("Conversations")
        self.setMinimumSize(400, 300)

        layout = QVBoxLayout(self)

        # List widget
        self.list_widget = QListWidget()
        self.list_widget.itemDoubleClicked.connect(self._on_select)
        layout.addWidget(self.list_widget)

        # Buttons
        btn_layout = QHBoxLayout()

        self.new_btn = QPushButton("New")
        self.new_btn.clicked.connect(self._on_new)
        btn_layout.addWidget(self.new_btn)

        self.load_btn = QPushButton("Load")
        self.load_btn.clicked.connect(self._on_load)
        btn_layout.addWidget(self.load_btn)

        self.delete_btn = QPushButton("Delete")
        self.delete_btn.clicked.connect(self._on_delete)
        btn_layout.addWidget(self.delete_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("Close")
        self.close_btn.clicked.connect(self.close)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

        self._refresh_list()

    def _refresh_list(self):
        self.list_widget.clear()
        for conv in self.manager.list_conversations():
            # Format: "Title (date)"
            date_str = conv["updated_at"][:10] if conv["updated_at"] else ""
            item = QListWidgetItem(f"{conv['title']} ({date_str})")
            item.setData(Qt.UserRole, conv["id"])
            self.list_widget.addItem(item)

    def _on_select(self, item):
        conv_id = item.data(Qt.UserRole)
        self.conversation_selected.emit(conv_id)
        self.accept()

    def _on_load(self):
        item = self.list_widget.currentItem()
        if item:
            self._on_select(item)

    def _on_new(self):
        self.conversation_selected.emit("")  # Empty = new conversation
        self.accept()

    def _on_delete(self):
        item = self.list_widget.currentItem()
        if item:
            conv_id = item.data(Qt.UserRole)
            reply = QMessageBox.question(
                self,
                "Delete Conversation",
                "Delete this conversation?",
                QMessageBox.Yes | QMessageBox.No,
            )
            if reply == QMessageBox.Yes:
                self.manager.delete_conversation(conv_id)
                self._refresh_list()


def markdown_to_html(text: str) -> str:
    """Convert markdown to HTML."""
    if not text:
        return ""
    return markdown.markdown(text, extensions=["fenced_code", "tables", "nl2br"])


class Signals(QObject):
    """Signals for thread-safe UI updates."""

    add_user_message = Signal(str)
    add_assistant_message = Signal(str)
    add_tool_message = Signal(str, str, str)  # (tool_id, header, raw_json for copying)
    update_tool_result = Signal(str, str, str)  # (tool_id, summary, raw_result_json)
    add_error_message = Signal(str)
    add_system_message = Signal(str)
    # Streaming: show "thinking" with token count, then finalize
    start_thinking = Signal()
    update_thinking = Signal(int)  # token count
    finish_thinking = Signal(str)  # final text
    # Extended thinking (Claude's reasoning)
    add_extended_thinking = Signal(str)  # thinking content
    set_status = Signal(str)
    set_usage = Signal(dict)  # usage stats
    clear_chat = Signal()
    # Tool approval (manual mode)
    request_tool_approval = Signal(str, str, str)  # (tool_name, args_json, tool_id)
    tool_approval_response = Signal(str, bool)  # (tool_id, approved)
    # Streaming block start events (int = agent_message_index)
    start_thinking_block = Signal(int)  # Start yellow thinking block
    start_text_block = Signal(int)  # Start green assistant block
    start_tool_block = Signal(str, str, int)  # Start tool block (name, id, agent_msg_idx)
    # Block complete events (content shown after block finishes)
    complete_thinking_block = Signal(str)  # Set full thinking content
    complete_text_block = Signal(str)  # Set full text content


class MessageBlock(QFrame):
    """A single message block."""

    def __init__(self, role: str, header_text: str = None, parent=None):
        super().__init__(parent)
        self.role = role
        self._header_text = header_text  # Custom header (e.g., tool name)
        self._raw_text = ""  # Store raw text for markdown conversion
        self._collapsed = False
        self.message_index = None  # Set by ChatView
        self.agent_message_index = None  # For syncing with agent.messages
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        # Header row with label and action buttons
        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(4)

        self.header = QLabel(self._get_header_text())
        self.header.setStyleSheet(self._get_header_style())
        font = self.header.font()
        font.setBold(True)
        font.setPointSize(9)
        self.header.setFont(font)
        header_layout.addWidget(self.header)

        header_layout.addStretch()

        # Button styling
        btn_style = """
            QPushButton {
                border: none;
                background: transparent;
                color: #999;
                font-size: 11px;
                padding: 0px 2px;
            }
            QPushButton:hover {
                background: #ddd;
                color: #333;
                border-radius: 2px;
            }
        """

        # Copy button (first, before other action buttons)
        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedSize(40, 18)
        self.copy_btn.setStyleSheet(
            "font-size: 9px; color: transparent; background: transparent; border: none;"
        )
        self.copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(self.copy_btn)

        # Redo button (user messages only)
        self.redo_btn = None
        if role == "user":
            self.redo_btn = QPushButton("\u21bb")  # ↻
            self.redo_btn.setFixedSize(18, 18)
            self.redo_btn.setToolTip("Redo from this message")
            self.redo_btn.setStyleSheet(btn_style)
            self.redo_btn.clicked.connect(self._request_redo)
            header_layout.addWidget(self.redo_btn)

        # Collapse button
        self.collapse_btn = QPushButton("\u25bc")  # ▼
        self.collapse_btn.setFixedSize(18, 18)
        self.collapse_btn.setToolTip("Collapse/expand")
        self.collapse_btn.setStyleSheet(btn_style)
        self.collapse_btn.clicked.connect(self._toggle_collapse)
        header_layout.addWidget(self.collapse_btn)

        # Remove button
        self.remove_btn = QPushButton("\u2715")  # ✕
        self.remove_btn.setFixedSize(18, 18)
        self.remove_btn.setToolTip("Remove this and following messages")
        self.remove_btn.setStyleSheet(btn_style)
        self.remove_btn.clicked.connect(self._request_remove)
        header_layout.addWidget(self.remove_btn)

        layout.addLayout(header_layout)

        # Content - use QLabel for simple text, renders HTML fine
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content.setStyleSheet(self._get_content_style())
        self.content.setTextFormat(Qt.RichText)
        self.content.setOpenExternalLinks(False)
        self.content.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        layout.addWidget(self.content)

        self.setStyleSheet(self._get_frame_style())

    def _get_header_text(self) -> str:
        if self._header_text:
            return self._header_text
        if self.role == "user":
            return "You"
        elif self.role == "assistant":
            return "Claude"
        elif self.role == "tool":
            return "Tool"
        elif self.role == "error":
            return "Error"
        elif self.role == "thinking":
            return "Thinking"
        else:
            return "System"

    def _get_header_style(self) -> str:
        if self.role == "user":
            return "color: #0066cc;"
        elif self.role == "assistant":
            return "color: #006600;"
        elif self.role == "error":
            return "color: #cc0000;"
        elif self.role == "thinking":
            return "color: #996600;"
        else:
            return "color: #666666;"

    def _get_content_style(self) -> str:
        base = "p { margin: 0; }"  # Reset paragraph margins from HTML/markdown
        if self.role == "error":
            return f"{base} color: #cc0000;"
        elif self.role in ("tool", "system"):
            return f"{base} color: #666666;"
        elif self.role == "thinking":
            return f"{base} color: #666666; font-style: italic;"
        else:
            return base

    def _get_frame_style(self) -> str:
        if self.role == "user":
            return "MessageBlock { background-color: #e8f0fe; border: 1px solid #c4d7f5; }"
        elif self.role == "assistant":
            return "MessageBlock { background-color: #f0f7f0; border: 1px solid #c4e0c4; }"
        elif self.role == "error":
            return "MessageBlock { background-color: #fee8e8; border: 1px solid #f5c4c4; }"
        elif self.role == "thinking":
            return "MessageBlock { background-color: #fff8e8; border: 1px solid #f5e0c4; }"
        else:
            return "MessageBlock { background-color: #f5f5f5; border: 1px solid #e0e0e0; }"

    def set_text(self, text: str):
        self._raw_text = text
        if self.role == "assistant":
            self.content.setText(markdown_to_html(text))
        else:
            self.content.setText(text)

    def append_text(self, text: str):
        self._raw_text += text
        if self.role == "assistant":
            self.content.setText(markdown_to_html(self._raw_text))
        else:
            self.content.setText(self._raw_text)

    def _on_copy(self):
        """Copy raw text to clipboard."""
        QApplication.clipboard().setText(self._raw_text)

    def _toggle_collapse(self):
        """Toggle collapsed state."""
        self.set_collapsed(not self._collapsed)

    def set_collapsed(self, collapsed: bool):
        """Set collapsed state."""
        self._collapsed = collapsed
        self.collapse_btn.setText("\u25b6" if collapsed else "\u25bc")  # ▶ or ▼
        self.content.setVisible(not collapsed)

    def _request_remove(self):
        """Request removal of this message and following."""
        chat_view = self._find_chat_view()
        if chat_view:
            chat_view.remove_requested.emit(self)

    def _request_redo(self):
        """Request redo from this message."""
        chat_view = self._find_chat_view()
        if chat_view:
            chat_view.redo_requested.emit(self)

    def _find_chat_view(self):
        """Find parent ChatView."""
        parent = self.parent()
        while parent:
            if isinstance(parent, ChatView):
                return parent
            parent = parent.parent()
        return None

    def enterEvent(self, event):
        """Show copy button on hover."""
        self.copy_btn.setStyleSheet("font-size: 9px;")
        super().enterEvent(event)

    def leaveEvent(self, event):
        """Hide copy button when mouse leaves."""
        self.copy_btn.setStyleSheet(
            "font-size: 9px; color: transparent; background: transparent; border: none;"
        )
        super().leaveEvent(event)


class ChatView(QScrollArea):
    """Scrollable container for message blocks."""

    # Signals for message actions
    remove_requested = Signal(object)  # MessageBlock to remove from
    redo_requested = Signal(object)  # MessageBlock to redo

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Container widget
        self.container = QWidget()
        self.container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Maximum)
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(8)

        self.setWidget(self.container)
        self.setWidgetResizable(True)

        self.tool_blocks: dict[str, MessageBlock] = {}  # Track tool blocks by tool_id
        self.thinking_block = None
        self.message_blocks: list[MessageBlock] = []  # Track all message blocks

        # Streaming blocks (live-updated during response)
        self.streaming_thinking_block: MessageBlock | None = None
        self.streaming_text_block: MessageBlock | None = None

        # Debounced scroll timer - prevents flickering from multiple rapid scroll requests
        self._scroll_timer = QTimer(self)
        self._scroll_timer.setSingleShot(True)
        self._scroll_timer.setInterval(50)  # 50ms debounce
        self._scroll_timer.timeout.connect(self._do_scroll_to_bottom)

    def add_message(
        self, text: str, role: str, header_text: str = None, raw_text: str = None
    ) -> MessageBlock:
        """Add a new message block."""
        block = MessageBlock(role, header_text=header_text)
        block.set_text(text)
        if raw_text:
            block._raw_text = raw_text  # Override for copying (e.g., tool JSON)

        # Track message block
        self.message_blocks.append(block)
        block.message_index = len(self.message_blocks) - 1

        # Default collapse for thinking blocks
        if role == "thinking":
            block.set_collapsed(True)

        self.layout.addWidget(block)

        # Scroll to bottom (debounced)
        self._scroll_to_bottom()

        return block

    def update_tool_with_result(self, tool_id: str, result: str, raw_result: str = None):
        """Update a specific tool block with its result."""
        block = self.tool_blocks.get(tool_id)
        if block:
            # Update visual display only - just show the summary
            block.content.setText(result)
            # Append raw result JSON for copying (to _raw_text only)
            if raw_result:
                block._raw_text += f"\n\nResult:\n{raw_result}"
            # Remove from tracking dict
            del self.tool_blocks[tool_id]
            self._scroll_to_bottom()

    def start_thinking(self):
        """Show a thinking indicator block."""
        self.thinking_block = MessageBlock("assistant")
        self.thinking_block.content.setText("Thinking...")
        self.layout.addWidget(self.thinking_block)
        self._scroll_to_bottom()

    def update_thinking(self, tokens: int):
        """Update thinking block with token count."""
        if self.thinking_block:
            self.thinking_block.content.setText(f"Thinking... ({tokens} tokens)")

    def finish_thinking(self, text: str):
        """Replace thinking block with actual content."""
        if self.thinking_block:
            if text.strip():
                self.thinking_block.set_text(text)  # Use set_text for markdown
            else:
                # No text content (just tool calls), remove the block
                self.layout.removeWidget(self.thinking_block)
                self.thinking_block.deleteLater()
            self.thinking_block = None
            self.layout.invalidate()  # Force layout recalculation
            self.container.updateGeometry()  # Update container size
            self._scroll_to_bottom()

    # Streaming block methods - create blocks immediately when streaming starts
    def start_streaming_thinking(self, agent_msg_idx: int):
        """Create a thinking block with placeholder."""
        self.streaming_thinking_block = MessageBlock("thinking")
        self.streaming_thinking_block.set_text("...")  # Placeholder
        self.message_blocks.append(self.streaming_thinking_block)
        self.streaming_thinking_block.message_index = len(self.message_blocks) - 1
        self.streaming_thinking_block.agent_message_index = agent_msg_idx
        self.streaming_thinking_block.set_collapsed(True)  # Start collapsed
        self.layout.addWidget(self.streaming_thinking_block)
        self._scroll_to_bottom()

    def complete_streaming_thinking(self, content: str):
        """Set final content for thinking block."""
        if self.streaming_thinking_block:
            self.streaming_thinking_block.set_text(content)
            self._scroll_to_bottom()

    def start_streaming_text(self, agent_msg_idx: int):
        """Create an assistant text block with placeholder."""
        self.streaming_text_block = MessageBlock("assistant")
        self.streaming_text_block.set_text("...")  # Placeholder
        self.message_blocks.append(self.streaming_text_block)
        self.streaming_text_block.message_index = len(self.message_blocks) - 1
        self.streaming_text_block.agent_message_index = agent_msg_idx
        self.layout.addWidget(self.streaming_text_block)
        self._scroll_to_bottom()

    def complete_streaming_text(self, content: str):
        """Set final content for text block."""
        if self.streaming_text_block:
            self.streaming_text_block.set_text(content)
            self._scroll_to_bottom()

    def start_streaming_tool(self, tool_name: str, tool_id: str, agent_msg_idx: int):
        """Create a tool block when tool use starts streaming."""
        header = f"\u25cf {tool_name}"  # ● tool_name
        block = MessageBlock("tool", header_text=header)
        block.set_text("...")  # Placeholder while input streams
        block._raw_text = f"Tool: {tool_name}\nID: {tool_id}\n"
        self.message_blocks.append(block)
        block.message_index = len(self.message_blocks) - 1
        block.agent_message_index = agent_msg_idx
        self.tool_blocks[tool_id] = block  # Track by tool_id
        self.layout.addWidget(block)
        self._scroll_to_bottom()

    def finish_streaming(self):
        """Clean up streaming block references after response completes."""
        self.streaming_thinking_block = None
        self.streaming_text_block = None

    def clear_messages(self):
        """Clear all messages."""
        while self.layout.count() > 0:
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.tool_blocks.clear()
        self.thinking_block = None
        self.streaming_thinking_block = None
        self.streaming_text_block = None
        self.message_blocks.clear()

    def remove_from(self, block: MessageBlock):
        """Remove block and all following blocks."""
        if block not in self.message_blocks:
            return
        idx = self.message_blocks.index(block)

        # Remove from UI
        for b in self.message_blocks[idx:]:
            self.layout.removeWidget(b)
            b.deleteLater()

        # Update tracking
        self.message_blocks = self.message_blocks[:idx]

        # Clear stale tool block references
        self.tool_blocks = {
            tid: blk for tid, blk in self.tool_blocks.items() if blk in self.message_blocks
        }
        if self.thinking_block and self.thinking_block not in self.message_blocks:
            self.thinking_block = None

    def _scroll_to_bottom(self):
        """Request a scroll to bottom (debounced to prevent flickering)."""
        # Restart the timer - this debounces rapid scroll requests
        self._scroll_timer.start()

    def _do_scroll_to_bottom(self):
        """Actually perform the scroll (called by debounce timer)."""
        # Force Qt to process pending layout changes before reading scroll position
        self.container.updateGeometry()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def _force_scroll_to_bottom(self):
        """Immediately scroll to bottom, bypassing debounce."""
        self.container.updateGeometry()
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())

    def resizeEvent(self, event):
        super().resizeEvent(event)


class ContextBar(QFrame):
    """Shows current IDA context."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)

        self.context_label = QLabel("(no context)")
        self.context_label.setStyleSheet("color: #666;")
        layout.addWidget(self.context_label)

        layout.addStretch()

        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setFixedHeight(22)
        self.refresh_btn.clicked.connect(self.update_context)
        layout.addWidget(self.refresh_btn)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_context)
        self.timer.start(1000)

    def update_context(self):
        try:
            ea = idc.get_screen_ea()
            func = idaapi.get_func(ea)
            if func:
                func_name = idc.get_func_name(func.start_ea)
                offset = ea - func.start_ea
                self.context_label.setText(f"{func_name}+{offset:#x} @ {ea:#x}")
            else:
                self.context_label.setText(f"@ {ea:#x}")
        except Exception:
            self.context_label.setText("(error)")

    def get_context(self) -> dict:
        try:
            ea = idc.get_screen_ea()
            func = idaapi.get_func(ea)
            ctx = {"cursor_ea": ea, "cursor_ea_hex": f"{ea:#x}"}
            if func:
                ctx["function_start"] = func.start_ea
                ctx["function_name"] = idc.get_func_name(func.start_ea)
                ctx["offset_in_function"] = ea - func.start_ea
            return ctx
        except Exception:
            return {"error": "Failed to get context"}


class InputBox(QPlainTextEdit):
    """Multi-line input."""

    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFont(QFont("Consolas", 10))
        self.setPlaceholderText("Type message... (Ctrl+Enter to send)")
        self.setMaximumHeight(80)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Return and event.modifiers() == Qt.ControlModifier:
            text = self.toPlainText().strip()
            if text:
                self.submitted.emit(text)
                self.clear()
        else:
            super().keyPressEvent(event)


class CacheTTLIndicator(QWidget):
    """Circular progress indicator for cache TTL."""

    CACHE_TTL_SECONDS = 300  # 5 minutes

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(20, 20)
        self._progress = 0.0  # 0.0 to 1.0
        self._seconds_left = 0
        self._expired = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

        self.setToolTip("Cache TTL")

    def start_countdown(self):
        """Start the 5-minute countdown."""
        self._seconds_left = self.CACHE_TTL_SECONDS
        self._progress = 1.0
        self._expired = False
        self._timer.start(1000)  # Tick every second
        self.update()

    def reset(self):
        """Reset the indicator."""
        self._timer.stop()
        self._progress = 0.0
        self._seconds_left = 0
        self._expired = False
        self.setToolTip("Cache TTL")
        self.update()

    def _tick(self):
        self._seconds_left -= 1
        if self._seconds_left <= 0:
            self._progress = 0.0
            self._expired = True
            self._timer.stop()
            self.setToolTip("Cache expired")
        else:
            self._progress = self._seconds_left / self.CACHE_TTL_SECONDS
            self.setToolTip(f"Cache TTL: {self._seconds_left}s")
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Background circle - red if expired, gray otherwise
        if self._expired:
            painter.setPen(QPen(QColor("#f44336"), 2))  # Red
        else:
            painter.setPen(QPen(QColor("#ddd"), 2))
        painter.drawEllipse(QRectF(2, 2, 16, 16))

        if self._progress > 0:
            # Progress arc (green)
            painter.setPen(QPen(QColor("#4caf50"), 2))
            # Arc is in 1/16th of a degree, starts at 12 o'clock (90°), goes clockwise (negative)
            start_angle = 90 * 16
            span_angle = -int(self._progress * 360 * 16)
            painter.drawArc(QRectF(2, 2, 16, 16), start_angle, span_angle)


class StatusBar(QFrame):
    """Status bar with usage stats."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFrameStyle(QFrame.StyledPanel)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 8, 2)

        self.status_label = QLabel("Ready")
        layout.addWidget(self.status_label)

        layout.addStretch()

        # Usage stats - clickable button styled as label
        self.stats_btn = QPushButton("")
        self.stats_btn.setStyleSheet(
            """
            QPushButton {
                color: #666;
                background: transparent;
                border: none;
                text-align: right;
                padding: 0 4px;
            }
            QPushButton:hover {
                color: #333;
                text-decoration: underline;
            }
            """
        )
        self.stats_btn.setCursor(Qt.PointingHandCursor)
        self.stats_btn.clicked.connect(self._show_stats_popup)
        layout.addWidget(self.stats_btn)

        # Cache TTL indicator
        self.cache_indicator = CacheTTLIndicator()
        layout.addWidget(self.cache_indicator)

        # Store request history for popup
        self._request_stats: list[dict] = []
        self._total_stats: dict = {}

    def set_status(self, status: str):
        self.status_label.setText(status)

    @staticmethod
    def _format_tokens(n: int) -> str:
        """Format token count (e.g., 1234 -> '1.2k')."""
        if n >= 1000:
            return f"{n / 1000:.1f}k"
        return str(n)

    def set_usage(self, usage: dict):
        """Display usage statistics."""
        if not usage:
            self.stats_btn.setText("")
            self._request_stats = []
            self._total_stats = {}
            return

        # Handle new format with requests list and total
        if "requests" in usage and "total" in usage:
            self._request_stats = usage["requests"]
            self._total_stats = usage["total"]
            total = usage["total"]
        else:
            # Legacy format (single usage dict)
            self._request_stats = [usage]
            self._total_stats = usage
            total = usage

        # Display total in button using token flow format
        # From cache (X) + new in (uncachable (Y) + cached (Z)) → out (W)
        uncachable = total.get("input_tokens", 0)
        output_tok = total.get("output_tokens", 0)
        cached = total.get("cache_creation_input_tokens", 0)
        from_cache = total.get("cache_read_input_tokens", 0)

        # Compact format for status bar
        fmt = self._format_tokens
        text = f"⟳{fmt(from_cache)} + ({fmt(uncachable)} + {fmt(cached)}) → {fmt(output_tok)}"

        # Add request count if multiple
        if len(self._request_stats) > 1:
            text += f" ({len(self._request_stats)} reqs)"

        # Start TTL countdown when cache is written
        if cached > 0:
            self.cache_indicator.start_countdown()

        self.stats_btn.setText(text)

    def _show_stats_popup(self):
        """Show popup with per-request stats breakdown."""
        if not self._request_stats:
            return

        # Build popup text using token flow format:
        # From cache (X) + new in (uncachable (Y) + cached (Z)) → out (W)
        lines = []
        for i, req in enumerate(self._request_stats, 1):
            uncachable = req.get("input_tokens", 0)
            output_tok = req.get("output_tokens", 0)
            cached = req.get("cache_creation_input_tokens", 0)
            from_cache = req.get("cache_read_input_tokens", 0)

            line = (
                f"Request {i}: From cache ({from_cache}) + "
                f"new in (uncachable ({uncachable}) + cached ({cached})) "
                f"→ out ({output_tok})"
            )
            lines.append(line)

        # Add total line
        if len(self._request_stats) > 1 and self._total_stats:
            lines.append("─" * 60)
            total = self._total_stats
            uncachable = total.get("input_tokens", 0)
            output_tok = total.get("output_tokens", 0)
            cached = total.get("cache_creation_input_tokens", 0)
            from_cache = total.get("cache_read_input_tokens", 0)

            line = (
                f"Total: From cache ({from_cache}) + "
                f"new in (uncachable ({uncachable}) + cached ({cached})) "
                f"→ out ({output_tok})"
            )
            lines.append(line)

        # Show in message box
        msg = QMessageBox(self)
        msg.setWindowTitle("Usage Statistics")
        msg.setText("\n".join(lines))
        msg.setStandardButtons(QMessageBox.Ok)
        msg.exec()

    def clear_stats(self):
        """Clear usage statistics and reset cache indicator."""
        self.stats_btn.setText("")
        self._request_stats = []
        self._total_stats = {}
        self.cache_indicator.reset()


class ClaudeWidget(idaapi.PluginForm):
    """Main Claude chat widget."""

    def __init__(self):
        super().__init__()
        self.signals = Signals()
        self.agent = None
        self.client = None
        self.conv_manager = None
        self._parent_widget = None
        self._tool_names: dict[str, str] = {}  # Track tool names by tool_id for result summaries
        # Tool approval (manual mode)
        self._approval_event = threading.Event()
        self._approval_result = False
        self._current_approval_id = ""
        # Usage stats tracking
        self._request_stats: list[dict] = []  # Each API response's stats
        self._total_stats = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def OnCreate(self, form):
        self._parent_widget = self.FormToPyQtWidget(form)
        self._init_ui()
        self._connect_signals()
        self._init_agent()

    def OnClose(self, form):
        """Called when the widget is closed."""
        global _widget
        _widget = None  # Reset so next Show() creates fresh instance

    def _init_ui(self):
        layout = QVBoxLayout(self._parent_widget)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(4)

        # Top bar
        top_bar = QHBoxLayout()
        top_bar.setSpacing(4)

        self.settings_btn = QPushButton("Settings")
        top_bar.addWidget(self.settings_btn)

        self.history_btn = QPushButton("History")
        top_bar.addWidget(self.history_btn)

        top_bar.addStretch()

        self.clear_btn = QPushButton("Clear")
        top_bar.addWidget(self.clear_btn)

        layout.addLayout(top_bar)

        # Chat view
        self.chat_view = ChatView()
        layout.addWidget(self.chat_view, stretch=1)

        # Context bar
        self.context_bar = ContextBar()
        layout.addWidget(self.context_bar)

        # Input
        self.input_box = InputBox()
        layout.addWidget(self.input_box)

        # Bottom buttons
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(4)

        self.send_btn = QPushButton("Send")
        btn_layout.addWidget(self.send_btn)

        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        btn_layout.addWidget(self.stop_btn)

        # Manual mode checkbox
        self.manual_mode_cb = QCheckBox("Manual")
        self.manual_mode_cb.setToolTip("Approve each tool call before execution")
        btn_layout.addWidget(self.manual_mode_cb)

        btn_layout.addStretch()

        # Thinking toggle and budget selector
        self.think_btn = QPushButton("Think")
        self.think_btn.setCheckable(True)
        self.think_btn.setToolTip("Enable extended thinking")
        btn_layout.addWidget(self.think_btn)

        self.think_budget_selector = QComboBox()
        self.think_budget_selector.addItem("Light (4k)", 4096)
        self.think_budget_selector.addItem("Medium (12k)", 12288)
        self.think_budget_selector.addItem("Deep (24k)", 24576)
        self.think_budget_selector.setCurrentIndex(1)  # Default to Medium
        self.think_budget_selector.setEnabled(False)  # Disabled until thinking enabled
        self.think_budget_selector.setToolTip("Thinking budget (tokens)")
        btn_layout.addWidget(self.think_budget_selector)

        # Model selector
        btn_layout.addWidget(QLabel("Model:"))
        self.model_selector = QComboBox()
        self.model_selector.setMinimumWidth(150)
        btn_layout.addWidget(self.model_selector)

        # Effort selector (Opus 4.5 only)
        self.effort_selector = QComboBox()
        self.effort_selector.addItem("High", "high")
        self.effort_selector.addItem("Medium", "medium")
        self.effort_selector.addItem("Low", "low")
        self.effort_selector.setToolTip("Effort level (Opus 4.5 only)")
        self.effort_selector.setVisible(False)  # Hidden by default
        btn_layout.addWidget(self.effort_selector)

        layout.addLayout(btn_layout)

        # Status
        self.status_bar = StatusBar()
        layout.addWidget(self.status_bar)

    def _connect_signals(self):
        self.input_box.submitted.connect(self._on_submit)
        self.send_btn.clicked.connect(self._on_send_clicked)
        self.stop_btn.clicked.connect(self._on_stop_clicked)
        self.clear_btn.clicked.connect(self._on_clear_clicked)
        self.history_btn.clicked.connect(self._on_history_clicked)
        self.settings_btn.clicked.connect(self._on_settings_clicked)
        self.think_btn.toggled.connect(self._on_think_toggled)
        self.think_budget_selector.currentIndexChanged.connect(self._on_think_budget_changed)

        # Thread-safe signals
        def add_user_msg(t):
            block = self.chat_view.add_message(t, "user")
            # Track agent message index for syncing
            # Note: message will be added to agent.messages when chat() is called,
            # so the index is the current length (where it will be added)
            if self.agent:
                block.agent_message_index = len(self.agent.messages)
            # Defer to next event loop cycle so layout updates first
            QTimer.singleShot(0, self.chat_view._force_scroll_to_bottom)

        self.signals.add_user_message.connect(add_user_msg)
        self.signals.add_assistant_message.connect(
            lambda t: self.chat_view.add_message(t, "assistant")
        )
        self.signals.add_tool_message.connect(
            lambda tool_id, header, raw: self._add_tool_block(tool_id, header, raw)
        )
        self.signals.update_tool_result.connect(
            lambda tool_id, summary, raw: self.chat_view.update_tool_with_result(
                tool_id, summary, raw
            )
        )
        self.signals.add_error_message.connect(lambda t: self.chat_view.add_message(t, "error"))
        self.signals.add_system_message.connect(lambda t: self.chat_view.add_message(t, "system"))
        self.signals.start_thinking.connect(self.chat_view.start_thinking)
        self.signals.update_thinking.connect(self.chat_view.update_thinking)
        self.signals.finish_thinking.connect(self.chat_view.finish_thinking)
        self.signals.add_extended_thinking.connect(
            lambda t: self.chat_view.add_message(t, "thinking")
        )
        self.signals.set_status.connect(self.status_bar.set_status)
        self.signals.set_usage.connect(self.status_bar.set_usage)
        self.signals.clear_chat.connect(self.chat_view.clear_messages)
        # Tool approval signals (manual mode)
        self.signals.request_tool_approval.connect(self._show_tool_approval_dialog)
        self.signals.tool_approval_response.connect(self._on_approval_response)
        # ChatView message action signals
        self.chat_view.remove_requested.connect(self._on_remove_message)
        self.chat_view.redo_requested.connect(self._on_redo_message)
        # Streaming block signals
        self.signals.start_thinking_block.connect(self.chat_view.start_streaming_thinking)
        self.signals.start_text_block.connect(self.chat_view.start_streaming_text)
        self.signals.start_tool_block.connect(self.chat_view.start_streaming_tool)
        self.signals.complete_thinking_block.connect(self.chat_view.complete_streaming_thinking)
        self.signals.complete_text_block.connect(self.chat_view.complete_streaming_text)

    def _init_agent(self):
        from .client import ClaudeClient
        from .config import get_config
        from .conversation import get_conversation_manager
        from .loop import AgentLoop

        config = get_config()
        self.conv_manager = get_conversation_manager()
        if not config.api_key:
            self.chat_view.add_message("No API key. Set ANTHROPIC_API_KEY.", "error")
            return

        self.client = ClaudeClient(
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            thinking_enabled=config.thinking_enabled,
            thinking_budget=config.thinking_budget,
            interleaved_thinking=config.interleaved_thinking,
            effort=config.effort,
        )

        self.agent = AgentLoop(
            client=self.client,
            on_tool_call=self._on_tool_call,
            on_tool_result=self._on_tool_result,
            on_usage=self._on_usage,
            on_tool_approve=self._on_tool_approve,
            # Block start callbacks
            on_thinking_start=self._on_thinking_start,
            on_text_start=self._on_text_start,
            on_tool_start=self._on_tool_start,
            # Block complete callbacks
            on_thinking_complete=self._on_thinking_complete,
            on_text_complete=self._on_text_complete,
        )

        # Populate model selector
        self._load_models(config.model)

        # Connect model change
        self.model_selector.currentIndexChanged.connect(self._on_model_changed)

        # Sync thinking UI with config
        self.think_btn.setChecked(config.thinking_enabled)
        self.think_budget_selector.setEnabled(config.thinking_enabled)
        # Find matching budget preset or default to Medium
        budget_idx = self.think_budget_selector.findData(config.thinking_budget)
        if budget_idx >= 0:
            self.think_budget_selector.setCurrentIndex(budget_idx)

        # Sync effort UI with config
        effort_idx = self.effort_selector.findData(config.effort)
        if effort_idx >= 0:
            self.effort_selector.setCurrentIndex(effort_idx)
        # Show effort selector only for Opus 4.5
        is_opus = "opus-4-5" in config.model
        self.effort_selector.setVisible(is_opus)
        # Connect effort change
        self.effort_selector.currentIndexChanged.connect(self._on_effort_changed)

        status = f"Ready. Model: {config.model}"
        if config.thinking_enabled:
            status += " (thinking enabled)"
        self.chat_view.add_message(status, "system")

    def _load_models(self, current_model: str):
        """Load available models into selector."""
        try:
            models = self.client.list_models()
            self.model_selector.clear()

            current_idx = 0
            for i, m in enumerate(models):
                self.model_selector.addItem(m.display_name, m.id)
                if m.id == current_model:
                    current_idx = i

            self.model_selector.setCurrentIndex(current_idx)
        except Exception:
            # Fallback: just add current model
            self.model_selector.addItem(current_model, current_model)

    def _on_model_changed(self, index: int):
        """Handle model selection change."""
        if index < 0 or not self.client:
            return

        model_id = self.model_selector.itemData(index)
        if model_id:
            self.client.set_model(model_id)
            self.chat_view.add_message(
                f"Switched to: {self.model_selector.currentText()}", "system"
            )

            # Show/hide effort selector based on model
            is_opus = "opus-4-5" in model_id
            self.effort_selector.setVisible(is_opus)

            # Save to config
            from .config import get_config

            config = get_config()
            config.model = model_id
            config.save()

    def _on_think_toggled(self, enabled: bool):
        """Handle thinking toggle."""
        self.think_budget_selector.setEnabled(enabled)

        if not self.client:
            return

        # Update client
        self.client.thinking_enabled = enabled
        budget = self.think_budget_selector.currentData()
        self.client.thinking_budget = budget

        # budget_tokens must be < max_tokens, auto-adjust if needed
        if enabled and budget >= self.client.max_tokens:
            self.client.max_tokens = budget + 4096  # Room for output

        # Save to config
        from .config import get_config

        config = get_config()
        config.thinking_enabled = enabled
        config.thinking_budget = budget
        if enabled and budget >= config.max_tokens:
            config.max_tokens = budget + 4096
        config.save()

        status = "Thinking enabled" if enabled else "Thinking disabled"
        if enabled:
            status += f" ({self.think_budget_selector.currentText()})"
        self.chat_view.add_message(status, "system")

    def _on_think_budget_changed(self, index: int):
        """Handle thinking budget change."""
        if index < 0 or not self.client:
            return

        budget = self.think_budget_selector.itemData(index)
        if budget and self.think_btn.isChecked():
            self.client.thinking_budget = budget

            # budget_tokens must be < max_tokens, auto-adjust if needed
            if budget >= self.client.max_tokens:
                self.client.max_tokens = budget + 4096

            # Save to config
            from .config import get_config

            config = get_config()
            config.thinking_budget = budget
            if budget >= config.max_tokens:
                config.max_tokens = budget + 4096
            config.save()

            self.chat_view.add_message(
                f"Thinking budget: {self.think_budget_selector.currentText()}", "system"
            )

    def _on_effort_changed(self, index: int):
        """Handle effort level change."""
        if index < 0 or not self.client:
            return

        effort = self.effort_selector.itemData(index)
        if effort:
            self.client.effort = effort

            # Save to config
            from .config import get_config

            config = get_config()
            config.effort = effort
            config.save()

            self.chat_view.add_message(f"Effort: {effort}", "system")

    def _on_submit(self, text: str):
        if not text or not self.agent:
            return

        # Prevent double submission
        if not self.send_btn.isEnabled():
            return

        self.signals.add_user_message.emit(text)

        self.send_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.status_bar.set_status("Thinking...")

        # Show "Thinking..." placeholder (will be replaced when actual block starts)
        self.signals.start_thinking.emit()

        context = self.context_bar.get_context()
        prompt = text
        if context and "function_name" in context:
            prompt = f"[Context: {context['function_name']} @ {context['cursor_ea_hex']}]\n\n{text}"

        import threading

        def run():
            try:
                self.agent.chat(prompt, stream=True)
                # Clean up - streaming blocks were created/updated during streaming
                # Remove "Thinking..." placeholder if it wasn't replaced (no thinking/text blocks)
                self.signals.finish_thinking.emit("")
                # Clean up streaming block references
                ida_kernwin.execute_sync(
                    lambda: self.chat_view.finish_streaming(), ida_kernwin.MFF_FAST
                )
                # Auto-save conversation
                if self.conv_manager and self.agent:
                    self.conv_manager.save_agent_messages(self.agent.messages)
            except Exception as e:
                self.signals.finish_thinking.emit("")  # Remove thinking placeholder
                self.signals.add_error_message.emit(str(e))
            finally:
                ida_kernwin.execute_sync(self._reset_ui, ida_kernwin.MFF_FAST)

        threading.Thread(target=run, daemon=True).start()

    def _reset_ui(self):
        self.send_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.status_bar.set_status("Ready")

    def _on_send_clicked(self):
        text = self.input_box.toPlainText().strip()
        if text:
            self.input_box.clear()
            self._on_submit(text)

    def _on_stop_clicked(self):
        if self.agent:
            self.agent.cancel()
        self._reset_ui()

    def _on_clear_clicked(self):
        # Cancel any running operation first
        if self.agent:
            self.agent.cancel()
        self._reset_ui()

        self.signals.clear_chat.emit()
        self.status_bar.clear_stats()
        # Reset usage tracking
        self._request_stats = []
        self._total_stats = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        if self.agent:
            self.agent.clear_history()
        # Start a new conversation
        if self.conv_manager:
            self.conv_manager.new_conversation()

    def _on_remove_message(self, block):
        """Remove message and all following, truncate agent history."""
        if not self.agent:
            return

        # Get agent_message_index from the clicked block
        agent_idx = block.agent_message_index
        if agent_idx is None:
            return

        # Find the first UI block with this agent_message_index
        # (e.g., if clicking on text block, we also want to remove the thinking block before it)
        first_block = block
        for b in self.chat_view.message_blocks:
            if b.agent_message_index == agent_idx:
                first_block = b
                break

        # Truncate agent messages
        self.agent.messages = self.agent.messages[:agent_idx]

        # Remove from UI (from first block with this index onward)
        self.chat_view.remove_from(first_block)

        # Save updated conversation
        if self.conv_manager and self.agent:
            self.conv_manager.save_agent_messages(self.agent.messages)

    def _on_redo_message(self, block):
        """Redo from a user message - remove and resubmit."""
        if block.role != "user":
            return

        # Get the message text before removing
        text = block._raw_text or block.content.toPlainText()

        # Remove this message and everything after
        self._on_remove_message(block)

        # Resubmit the message
        if text.strip():
            self._on_submit(text)

    def _on_settings_clicked(self):
        dialog = SettingsDialog(self._parent_widget)
        if dialog.exec() == QDialog.Accepted:
            values = dialog.get_values()

            # Update and save config
            from .config import get_config

            config = get_config()
            config.api_key = values["api_key"]
            config.max_tokens = values["max_tokens"]
            config.interleaved_thinking = values["interleaved_thinking"]
            config.save()

            # Reinitialize client with new API key/settings
            from .client import ClaudeClient

            self.client = ClaudeClient(
                api_key=config.api_key,
                model=config.model,
                max_tokens=config.max_tokens,
                thinking_enabled=config.thinking_enabled,
                thinking_budget=config.thinking_budget,
                interleaved_thinking=config.interleaved_thinking,
                effort=config.effort,
            )

            # Update agent's client reference
            if self.agent:
                self.agent.client = self.client

            self.chat_view.add_message("Settings saved.", "system")

    def _on_history_clicked(self):
        """Show conversation history dialog."""
        if not self.conv_manager:
            return
        dialog = ConversationListDialog(self.conv_manager, self._parent_widget)
        dialog.conversation_selected.connect(self._on_conversation_selected)
        dialog.exec()

    def _on_conversation_selected(self, conv_id: str):
        """Handle conversation selection from history dialog."""
        if not conv_id:
            # New conversation
            if self.conv_manager:
                self.conv_manager.new_conversation()
            self.signals.clear_chat.emit()
            if self.agent:
                self.agent.clear_history()
            self.status_bar.clear_stats()
            # Reset usage tracking
            self._request_stats = []
            self._total_stats = {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
            self.chat_view.add_message("Started new conversation.", "system")
        else:
            # Load existing conversation
            self._restore_conversation(conv_id)

    def _restore_conversation(self, conv_id: str):
        """Restore UI and agent state from a saved conversation."""
        if not self.conv_manager:
            return

        messages = self.conv_manager.load_conversation(conv_id)
        if not messages:
            self.chat_view.add_message("Failed to load conversation.", "error")
            return

        # Clear current UI
        self.signals.clear_chat.emit()
        self.status_bar.clear_stats()
        # Reset usage tracking
        self._request_stats = []
        self._total_stats = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

        # Restore agent messages
        if self.agent:
            self.agent.messages = messages
            self.agent._recent_tool_calls.clear()  # Reset doom loop tracker

        # Replay messages to UI
        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")

            if role == "user":
                # User message - content is string or list with tool_result
                if isinstance(content, str):
                    self.chat_view.add_message(content, "user")
                # Skip tool_result messages in UI (they're shown with tool calls)

            elif role == "assistant":
                # Assistant message - extract text from content blocks
                if isinstance(content, str):
                    self.chat_view.add_message(content, "assistant")
                elif isinstance(content, list):
                    # Find text, thinking, and tool_use blocks
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "thinking":
                                # Show thinking block
                                thinking_text = block.get("thinking", "")
                                if thinking_text.strip():
                                    self.chat_view.add_message(thinking_text, "thinking")
                            elif block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    self.chat_view.add_message(text, "assistant")
                            elif block.get("type") == "tool_use":
                                # Show tool call with formatted args
                                name = block.get("name", "unknown")
                                tool_input = block.get("input", {})
                                args_parts = []
                                for k, v in tool_input.items():
                                    v_str = f'"{v}"' if isinstance(v, str) else str(v)
                                    if len(v_str) > 30:
                                        v_str = v_str[:27] + '..."'
                                    args_parts.append(f"{k}: {v_str}")
                                args_str = ", ".join(args_parts)
                                header = f"● {name}({args_str})"
                                self.chat_view.add_message("", "tool", header_text=header)

        title = self.conv_manager.get_conversation_title(conv_id)
        self.chat_view.add_message(f"Loaded: {title}", "system")

    # Block start callbacks - create UI blocks immediately when streaming starts
    def _on_thinking_start(self):
        """Called when a thinking block starts streaming."""
        # Remove the old "Thinking..." placeholder if it exists
        self.signals.finish_thinking.emit("")
        # Start the actual thinking block with agent message index
        # The assistant message will be at len(agent.messages) when added
        agent_msg_idx = len(self.agent.messages) if self.agent else 0
        self.signals.start_thinking_block.emit(agent_msg_idx)

    def _on_text_start(self):
        """Called when a text block starts streaming."""
        # Remove any leftover "Thinking..." placeholder
        self.signals.finish_thinking.emit("")
        agent_msg_idx = len(self.agent.messages) if self.agent else 0
        self.signals.start_text_block.emit(agent_msg_idx)

    def _on_tool_start(self, tool_name: str, tool_id: str):
        """Called when a tool use block starts streaming."""
        agent_msg_idx = len(self.agent.messages) if self.agent else 0
        self.signals.start_tool_block.emit(tool_name, tool_id, agent_msg_idx)

    # Block complete callbacks - set content when block finishes
    def _on_thinking_complete(self, content: str):
        """Called when a thinking block completes with full content."""
        self.signals.complete_thinking_block.emit(content)

    def _on_text_complete(self, content: str):
        """Called when a text block completes with full content."""
        self.signals.complete_text_block.emit(content)

    def _on_usage(self, usage: dict):
        # Store this request's stats
        self._request_stats.append(usage.copy())
        # Accumulate totals
        for key in usage:
            if key in self._total_stats:
                self._total_stats[key] = self._total_stats.get(key, 0) + usage.get(key, 0)
        # Update UI with both per-request list and totals
        self.signals.set_usage.emit(
            {
                "requests": self._request_stats,
                "total": self._total_stats,
            }
        )

    def _summarize_tool_result(self, tool_name: str, result) -> str:
        """Generate smart summary for tool results."""
        if not isinstance(result, dict):
            s = str(result)
            return s[:50] + "..." if len(s) > 50 else s

        # Handle errors
        if "error" in result:
            err = result["error"]
            return f"Error: {err[:40]}..." if len(err) > 40 else f"Error: {err}"

        # Tool-specific summaries
        if tool_name == "get_cursor_position":
            name = result.get("function_name", "")
            ea = result.get("ea", "")
            return f"At {ea}" + (f" in {name}" if name else "")

        elif tool_name == "goto_address":
            return f"Jumped to {result.get('ea', '?')}"

        elif tool_name == "get_function":
            name = result.get("name", "?")
            size = result.get("size", 0)
            decomp = "decompiled" if result.get("decompiled") else "disasm only"
            return f"{name} ({size} bytes, {decomp})"

        elif tool_name == "get_disassembly":
            return f"{result.get('count', 0)} instructions"

        elif tool_name == "get_bytes":
            return f"Read {result.get('size', 0)} bytes"

        elif tool_name in ("rename_function", "rename_variable"):
            old = result.get("old_name", "?")
            new = result.get("new_name", "?")
            return f"{old} → {new}"

        elif tool_name in ("set_comment", "set_function_comment"):
            return f"Comment set at {result.get('ea', '?')}"

        elif tool_name in ("get_xrefs_to", "get_xrefs_from"):
            return f"Found {result.get('count', 0)} xrefs"

        elif tool_name == "list_functions":
            return f"Listed {result.get('count', 0)} functions"

        elif tool_name == "search_strings":
            return f"Found {result.get('count', 0)} strings"

        elif tool_name == "refresh_view":
            return "View refreshed"

        elif tool_name == "get_segment_info":
            segs = result.get("segments", [])
            return f"{len(segs)} segments"

        elif tool_name == "take_snapshot":
            return "Snapshot created"

        elif tool_name == "list_snapshots":
            snaps = result.get("snapshots", [])
            return f"{len(snaps)} snapshots"

        elif tool_name == "restore_snapshot":
            return "Restore initiated"

        elif tool_name == "get_undo_status":
            undo = result.get("can_undo") or "none"
            redo = result.get("can_redo") or "none"
            return f"Undo: {undo}, Redo: {redo}"

        elif tool_name in ("undo", "redo"):
            action = result.get("action", "")
            verb = "Undid" if tool_name == "undo" else "Redid"
            return f"{verb}: {action}" if action else f"{verb} action"

        elif tool_name == "execute_script":
            # Return full output (will be combined with code in _on_tool_result)
            output = result.get("output", "")
            return output if output else "(no output)"

        # Fallback: truncate JSON
        s = str(result)
        return s[:50] + "..." if len(s) > 50 else s

    def _add_tool_block(self, tool_id: str, header: str, raw_json: str):
        """Add a tool block and track it by tool_id (fallback when streaming didn't create one)."""
        block = self.chat_view.add_message("", "tool", header_text=header, raw_text=raw_json)
        self.chat_view.tool_blocks[tool_id] = block

    def _on_tool_call(self, tool_call):
        # Remove the "Thinking..." placeholder if it wasn't replaced
        self.signals.finish_thinking.emit("")

        # Note: With streaming, thinking and text blocks were already created
        # and filled by block start events and stream callbacks.
        # The tool block was created by tool_start, now update it with full args.

        # Special handling for execute_script: show full code in body
        if tool_call.name == "execute_script":
            header = f"\u25cf {tool_call.name}"  # ● execute_script (simple header)
            code = tool_call.input.get("code", "")
            raw_data = {"tool": tool_call.name, "input": tool_call.input}
            raw_json = json.dumps(raw_data, indent=2)

            # Wrap code in <pre> tags to preserve formatting
            code_html = f"<pre style='margin:0;white-space:pre-wrap;font-family:Consolas,monospace;'>{html.escape(code)}</pre>"

            tool_block = self.chat_view.tool_blocks.get(tool_call.id)
            if tool_block:
                tool_block.header.setText(header)
                tool_block._raw_text = code  # Plain text for copying AND for _on_tool_result
                tool_block.content.setText(code_html)  # HTML directly to QLabel
            else:
                block = self.chat_view.add_message("", "tool", header_text=header, raw_text=code)
                block._raw_text = code  # Plain text for copying AND for _on_tool_result
                block.content.setText(code_html)  # HTML directly to QLabel
                self.chat_view.tool_blocks[tool_call.id] = block
        else:
            # Format args with colon style like Claude Code
            args_str = ""
            if tool_call.input:
                args_parts = []
                for k, v in tool_call.input.items():
                    # Quote strings, leave others as-is
                    v_str = f'"{v}"' if isinstance(v, str) else str(v)
                    if len(v_str) > 30:
                        v_str = v_str[:27] + '..."'
                    args_parts.append(f"{k}: {v_str}")
                args_str = ", ".join(args_parts)

            header = f"\u25cf {tool_call.name}({args_str})"  # ● tool_name(args)

            # Build raw JSON for copying
            raw_data = {"tool": tool_call.name, "input": tool_call.input}
            raw_json = json.dumps(raw_data, indent=2)

            # Update existing tool block (created by tool_start) or create new one
            tool_block = self.chat_view.tool_blocks.get(tool_call.id)
            if tool_block:
                # Update the header and raw text of existing block
                tool_block.header.setText(header)
                tool_block._raw_text = raw_json
                tool_block.set_text("")  # Clear "..." placeholder
            else:
                # Fallback: create new block if tool_start didn't create one
                self.signals.add_tool_message.emit(tool_call.id, header, raw_json)

        self.signals.set_status.emit(f"Running {tool_call.name}...")

        # Store tool name for result summary (keyed by tool_id)
        self._tool_names[tool_call.id] = tool_call.name

        # Start new thinking block for next response (after tool result comes back)
        self.signals.start_thinking.emit()

    def _on_tool_result(self, result):
        tool_id = result.tool_call_id
        tool_name = self._tool_names.get(tool_id, "unknown")

        # Special handling for execute_script: show code + output
        if tool_name == "execute_script":
            tool_block = self.chat_view.tool_blocks.get(tool_id)
            if tool_block:
                code = tool_block._raw_text  # Plain text code stored by _on_tool_call

                if result.success:
                    output = result.result.get("output", "") if result.result else ""
                    display_output = output if output else "(no output)"
                    plain_text = f"{code}\n\n--- Output ---\n{display_output}"
                    html_display = f"<pre style='margin:0;white-space:pre-wrap;font-family:Consolas,monospace;'>{html.escape(code)}\n\n--- Output ---\n{html.escape(display_output)}</pre>"
                else:
                    plain_text = f"{code}\n\n--- Error ---\n{result.error}"
                    html_display = f"<pre style='margin:0;white-space:pre-wrap;font-family:Consolas,monospace;'>{html.escape(code)}\n\n--- Error ---\n{html.escape(result.error)}</pre>"

                tool_block.content.setText(html_display)  # HTML for display
                tool_block._raw_text = plain_text  # Plain text for copying
                # Remove from tracking
                del self.chat_view.tool_blocks[tool_id]
        elif result.success:
            # Show smart summary
            summary = self._summarize_tool_result(tool_name, result.result)
            # Build raw result JSON for copying
            raw_result = json.dumps(result.result, indent=2) if result.result else ""
            self.signals.update_tool_result.emit(tool_id, summary, raw_result)
        else:
            # Update tool block with error
            self.signals.update_tool_result.emit(tool_id, f"Error: {result.error}", "")
        # Clean up tool name tracking
        self._tool_names.pop(tool_id, None)

    def _on_tool_approve(self, tool_call) -> bool:
        """Called from background thread - must sync with UI for manual mode."""
        if not self.manual_mode_cb.isChecked():
            return True  # Auto mode - always approve

        # Reset event
        self._approval_event.clear()
        self._current_approval_id = tool_call.id

        # Format args for display
        args_str = json.dumps(tool_call.input, indent=2)

        # Request approval on main thread
        self.signals.request_tool_approval.emit(tool_call.name, args_str, tool_call.id)

        # Wait for response (with timeout)
        self._approval_event.wait(timeout=300)  # 5 min timeout

        return self._approval_result

    def _show_tool_approval_dialog(self, tool_name: str, args_str: str, tool_id: str):
        """Show dialog asking user to approve tool call (runs on main thread)."""
        msg = QMessageBox(self._parent_widget)
        msg.setWindowTitle("Approve Tool Call")
        msg.setText(f"Allow {tool_name}?")
        # Truncate very long args
        display_args = args_str[:500] + "..." if len(args_str) > 500 else args_str
        msg.setInformativeText(display_args)
        msg.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        msg.setDefaultButton(QMessageBox.Yes)

        result = msg.exec()
        approved = result == QMessageBox.Yes
        self.signals.tool_approval_response.emit(tool_id, approved)

    def _on_approval_response(self, tool_id: str, approved: bool):
        """Called on main thread when user responds to dialog."""
        if tool_id == self._current_approval_id:
            self._approval_result = approved
            self._approval_event.set()

    def Show(self):
        return idaapi.PluginForm.Show(self, "Claude AI", options=idaapi.PluginForm.WOPN_PERSIST)


_widget = None


def show_widget():
    global _widget
    if _widget is None:
        _widget = ClaudeWidget()
    _widget.Show()
    return _widget


def get_widget():
    return _widget
