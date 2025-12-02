"""
IDA Claude - Claude-powered reverse engineering assistant for IDA Pro

This is the main plugin entry point that IDA loads.
"""


def PLUGIN_ENTRY():
    """Called by IDA to load the plugin."""
    from ida_claude.plugin import IdaClaudePlugin

    return IdaClaudePlugin()
