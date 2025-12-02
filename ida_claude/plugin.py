"""
IDA Pro plugin integration.

Provides:
- Plugin registration
- Widget-based chat interface
- Keyboard shortcuts
- Menu items
"""

import idaapi

# Import tools to register them


class IdaClaudePlugin(idaapi.plugin_t):
    """Main IDA plugin class."""

    flags = idaapi.PLUGIN_KEEP
    comment = "Claude AI Assistant for reverse engineering"
    help = "Claude-powered analysis assistant"
    wanted_name = "Claude AI"
    wanted_hotkey = "Ctrl+Alt+C"

    def __init__(self):
        super().__init__()
        self.widget = None

    def init(self):
        """Plugin initialization."""
        # Check if we have hex-rays (nice to have but not required)
        try:
            import ida_hexrays

            if not ida_hexrays.init_hexrays_plugin():
                print("[Claude] Hex-Rays not available - decompilation disabled")
        except ImportError:
            print("[Claude] Hex-Rays not available - decompilation disabled")

        # Add menu item
        self._add_menu()

        print("[Claude] Plugin loaded. Press Ctrl+Shift+C or use Edit > Claude AI to open.")
        return idaapi.PLUGIN_KEEP

    def _add_menu(self):
        """Add menu item."""
        # Register the action first
        action_desc = idaapi.action_desc_t(
            "claude:show",  # action name
            "Claude AI",  # label
            ShowClaudeHandler(),  # handler
            "Ctrl+Alt+C",  # shortcut
            "Open Claude AI assistant",  # tooltip
            -1,  # icon
        )
        idaapi.register_action(action_desc)

        # Then attach to menu
        idaapi.attach_action_to_menu(
            "Edit/Plugins/",  # menu path
            "claude:show",  # action name
            idaapi.SETMENU_APP,
        )

    def run(self, arg):
        """Called when plugin is run (hotkey or menu)."""
        from .widget import show_widget

        self.widget = show_widget()

    def term(self):
        """Plugin termination."""
        idaapi.unregister_action("claude:show")


class ShowClaudeHandler(idaapi.action_handler_t):
    """Action handler to show Claude widget."""

    def __init__(self):
        idaapi.action_handler_t.__init__(self)

    def activate(self, ctx):
        from .widget import show_widget

        show_widget()
        return 1

    def update(self, ctx):
        return idaapi.AST_ENABLE_ALWAYS
