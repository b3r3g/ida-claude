"""
Custom chat widget for IDA Claude.

Block-based chat UI where each message is a separate widget.
"""

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
    add_tool_message = Signal(str)
    update_tool_result = Signal(str)  # Update last tool block with result
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


class MessageBlock(QFrame):
    """A single message block."""

    def __init__(self, role: str, parent=None):
        super().__init__(parent)
        self.role = role
        self._raw_text = ""  # Store raw text for markdown conversion
        self.setFrameStyle(QFrame.StyledPanel | QFrame.Raised)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Minimum)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(2)

        # Header row with label and copy button
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

        self.copy_btn = QPushButton("Copy")
        self.copy_btn.setFixedSize(40, 18)
        self.copy_btn.setStyleSheet("font-size: 9px;")
        self.copy_btn.clicked.connect(self._on_copy)
        header_layout.addWidget(self.copy_btn)

        layout.addLayout(header_layout)

        # Content - use QLabel for simple text, renders HTML fine
        self.content = QLabel()
        self.content.setWordWrap(True)
        self.content.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.content.setStyleSheet(self._get_content_style())
        self.content.setTextFormat(Qt.RichText)
        self.content.setOpenExternalLinks(False)
        layout.addWidget(self.content)

        self.setStyleSheet(self._get_frame_style())

    def _get_header_text(self) -> str:
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
        if self.role == "error":
            return "color: #cc0000;"
        elif self.role in ("tool", "system"):
            return "color: #666666;"
        elif self.role == "thinking":
            return "color: #666666; font-style: italic;"
        else:
            return ""

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


class ChatView(QScrollArea):
    """Scrollable container for message blocks."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)

        # Container widget
        self.container = QWidget()
        self.layout = QVBoxLayout(self.container)
        self.layout.setContentsMargins(4, 4, 4, 4)
        self.layout.setSpacing(8)
        self.layout.setAlignment(Qt.AlignTop)  # Align messages to top

        self.setWidget(self.container)

        self.current_tool_block = None
        self.thinking_block = None

    def add_message(self, text: str, role: str) -> MessageBlock:
        """Add a new message block."""
        block = MessageBlock(role)
        block.set_text(text)

        self.layout.addWidget(block)

        # Track tool block for result updates
        if role == "tool":
            self.current_tool_block = block

        # Scroll to bottom
        QTimer.singleShot(10, self._scroll_to_bottom)

        return block

    def update_tool_with_result(self, result: str):
        """Append result to the current tool block."""
        if self.current_tool_block:
            self.current_tool_block.append_text("\n-> " + result)
            self.current_tool_block = None
            QTimer.singleShot(10, self._scroll_to_bottom)

    def start_thinking(self):
        """Show a thinking indicator block."""
        self.thinking_block = MessageBlock("assistant")
        self.thinking_block.content.setText("Thinking...")
        self.layout.addWidget(self.thinking_block)
        QTimer.singleShot(10, self._scroll_to_bottom)

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
            # Force container to recalculate size
            self.container.adjustSize()
            QTimer.singleShot(10, self._scroll_to_bottom)

    def clear_messages(self):
        """Clear all messages."""
        while self.layout.count() > 0:
            item = self.layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.current_tool_block = None
        self.thinking_block = None
        # Force container to recalculate size
        self.container.adjustSize()

    def _scroll_to_bottom(self):
        # Only auto-scroll if already near the bottom
        scrollbar = self.verticalScrollBar()
        at_bottom = scrollbar.value() >= scrollbar.maximum() - 50
        if at_bottom:
            scrollbar.setValue(scrollbar.maximum())

    def _force_scroll_to_bottom(self):
        # Force scroll (used for new user messages)
        self.verticalScrollBar().setValue(self.verticalScrollBar().maximum())


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

        # Usage stats
        self.stats_label = QLabel("")
        self.stats_label.setStyleSheet("color: #666;")
        layout.addWidget(self.stats_label)

        # Cache TTL indicator
        self.cache_indicator = CacheTTLIndicator()
        layout.addWidget(self.cache_indicator)

    def set_status(self, status: str):
        self.status_label.setText(status)

    def set_usage(self, usage: dict):
        """Display usage statistics."""
        if not usage:
            self.stats_label.setText("")
            return

        parts = []
        input_tok = usage.get("input_tokens", 0)
        output_tok = usage.get("output_tokens", 0)
        cache_create = usage.get("cache_creation_input_tokens", 0)
        cache_read = usage.get("cache_read_input_tokens", 0)

        parts.append(f"In: {input_tok}")
        parts.append(f"Out: {output_tok}")

        if cache_read > 0:
            parts.append(f"Cache: {cache_read}")
        if cache_create > 0:
            parts.append(f"CacheWrite: {cache_create}")
            # Start TTL countdown when cache is written
            self.cache_indicator.start_countdown()

        self.stats_label.setText(" | ".join(parts))

    def clear_stats(self):
        """Clear usage statistics and reset cache indicator."""
        self.stats_label.setText("")
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
        # Buffered streaming state
        self._stream_buffer = ""
        self._stream_tokens = 0
        self._thinking_buffer = ""

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

        btn_layout.addStretch()

        # Thinking toggle and budget selector
        self.think_btn = QPushButton("Think")
        self.think_btn.setCheckable(True)
        self.think_btn.setToolTip("Enable extended thinking")
        btn_layout.addWidget(self.think_btn)

        self.think_budget_selector = QComboBox()
        self.think_budget_selector.addItem("Light", 4096)
        self.think_budget_selector.addItem("Medium", 10000)
        self.think_budget_selector.addItem("Deep", 20000)
        self.think_budget_selector.setCurrentIndex(1)  # Default to Medium
        self.think_budget_selector.setEnabled(False)  # Disabled until thinking enabled
        self.think_budget_selector.setToolTip("Thinking budget (tokens)")
        btn_layout.addWidget(self.think_budget_selector)

        # Model selector
        btn_layout.addWidget(QLabel("Model:"))
        self.model_selector = QComboBox()
        self.model_selector.setMinimumWidth(150)
        btn_layout.addWidget(self.model_selector)

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
            self.chat_view.add_message(t, "user")
            QTimer.singleShot(10, self.chat_view._force_scroll_to_bottom)

        self.signals.add_user_message.connect(add_user_msg)
        self.signals.add_assistant_message.connect(
            lambda t: self.chat_view.add_message(t, "assistant")
        )
        self.signals.add_tool_message.connect(lambda t: self.chat_view.add_message(t, "tool"))
        self.signals.update_tool_result.connect(self.chat_view.update_tool_with_result)
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
        )

        self.agent = AgentLoop(
            client=self.client,
            on_text=self._on_stream_text,
            on_thinking=self._on_extended_thinking,
            on_tool_call=self._on_tool_call,
            on_tool_result=self._on_tool_result,
            on_usage=self._on_usage,
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

        # Reset buffers
        self._stream_buffer = ""
        self._stream_tokens = 0
        self._thinking_buffer = ""
        self.signals.start_thinking.emit()

        context = self.context_bar.get_context()
        prompt = text
        if context and "function_name" in context:
            prompt = f"[Context: {context['function_name']} @ {context['cursor_ea_hex']}]\n\n{text}"

        import threading

        def run():
            try:
                self.agent.chat(prompt, stream=True)
                # First, remove the "Thinking..." indicator
                self.signals.finish_thinking.emit("")
                # Then display extended thinking (yellow block)
                if self._thinking_buffer.strip():
                    self.signals.add_extended_thinking.emit(self._thinking_buffer)
                    self._thinking_buffer = ""
                # Then display any remaining text response (green block)
                if self._stream_buffer.strip():
                    self.signals.add_assistant_message.emit(self._stream_buffer)
                self._stream_buffer = ""
                self._stream_tokens = 0
                # Auto-save conversation
                if self.conv_manager and self.agent:
                    self.conv_manager.save_agent_messages(self.agent.messages)
            except Exception as e:
                self.signals.finish_thinking.emit("")  # Remove thinking block
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
        if self.agent:
            self.agent.clear_history()
        # Start a new conversation
        if self.conv_manager:
            self.conv_manager.new_conversation()

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
                    # Find text blocks
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    self.chat_view.add_message(text, "assistant")
                            elif block.get("type") == "tool_use":
                                # Show tool call
                                name = block.get("name", "unknown")
                                self.chat_view.add_message(f"{name}(...)", "tool")

        title = self.conv_manager.get_conversation_title(conv_id)
        self.chat_view.add_message(f"Loaded: {title}", "system")

    def _on_stream_text(self, text: str):
        # Buffer the text and update token count
        self._stream_buffer += text
        self._stream_tokens += 1  # Rough estimate (actual tokens != chunks)
        self.signals.update_thinking.emit(self._stream_tokens)

    def _on_extended_thinking(self, thinking: str):
        # Buffer extended thinking for display
        self._thinking_buffer += thinking

    def _on_usage(self, usage: dict):
        # Show stats from this API call (not accumulated)
        self.signals.set_usage.emit(usage)

    def _on_tool_call(self, tool_call):
        # First, remove the "Thinking..." indicator
        self.signals.finish_thinking.emit("")

        # Then display extended thinking (yellow block)
        if self._thinking_buffer.strip():
            self.signals.add_extended_thinking.emit(self._thinking_buffer)
            self._thinking_buffer = ""

        # Then display any buffered text response (green block)
        if self._stream_buffer.strip():
            self.signals.add_assistant_message.emit(self._stream_buffer)
        self._stream_buffer = ""
        self._stream_tokens = 0

        # Format args
        args_str = ""
        if tool_call.input:
            args_parts = []
            for k, v in tool_call.input.items():
                v_str = str(v)
                if len(v_str) > 30:
                    v_str = v_str[:30] + "..."
                args_parts.append(f"{k}={v_str}")
            args_str = ", ".join(args_parts)

        self.signals.add_tool_message.emit(f"{tool_call.name}({args_str})")
        self.signals.set_status.emit(f"Running {tool_call.name}...")

        # Start new thinking block for next response
        self.signals.start_thinking.emit()

    def _on_tool_result(self, result):
        if result.success:
            # Show truncated result in same tool block
            result_str = str(result.result) if result.result else "(no output)"
            if len(result_str) > 50:
                result_str = result_str[:50] + "..."
            self.signals.update_tool_result.emit(result_str)
        else:
            self.signals.add_error_message.emit(result.error)

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
