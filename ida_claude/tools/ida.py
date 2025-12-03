"""
Core IDA Pro tools for the Claude agent.

These tools provide the interface between Claude and IDA Pro,
allowing the agent to:
- Navigate and inspect the binary
- Read disassembly and decompiled code
- Make modifications (rename, comment)
- Search for patterns and cross-references
"""

from collections.abc import Callable
from functools import wraps
from typing import TypeVar

from . import tool

# IDA imports (only available when running inside IDA)
try:
    import ida_bytes
    import ida_funcs
    import ida_hexrays
    import ida_kernwin
    import ida_lines
    import ida_loader
    import ida_name
    import ida_segment
    import ida_undo
    import ida_xref
    import idaapi
    import idautils
    import idc

    IDA_AVAILABLE = True
except ImportError:
    IDA_AVAILABLE = False


T = TypeVar("T")


def _xref_type_name(xtype: int) -> str:
    """Convert xref type to readable name."""
    if not IDA_AVAILABLE:
        return str(xtype)

    # Code xref types
    if xtype == ida_xref.fl_CF:
        return "call_far"
    elif xtype == ida_xref.fl_CN:
        return "call_near"
    elif xtype == ida_xref.fl_JF:
        return "jump_far"
    elif xtype == ida_xref.fl_JN:
        return "jump_near"
    elif xtype == ida_xref.fl_F:
        return "flow"
    # Data xref types
    elif xtype == ida_xref.dr_O:
        return "offset"
    elif xtype == ida_xref.dr_W:
        return "write"
    elif xtype == ida_xref.dr_R:
        return "read"
    elif xtype == ida_xref.dr_T:
        return "text"
    elif xtype == ida_xref.dr_I:
        return "info"
    else:
        return f"type_{xtype}"


def _run_on_main(func: Callable[[], T]) -> T:
    """Run a function on IDA's main thread and return the result."""
    result = []
    error = []

    def wrapper():
        try:
            result.append(func())
        except Exception as e:
            error.append(e)

    # MFF_WRITE allows modifications, MFF_READ is read-only
    ida_kernwin.execute_sync(wrapper, ida_kernwin.MFF_WRITE)

    if error:
        raise error[0]
    return result[0] if result else None


def ida_main_thread(func: Callable) -> Callable:
    """Decorator to run a function on IDA's main thread. Also checks IDA availability."""

    @wraps(func)
    def wrapper(*args, **kwargs):
        if not IDA_AVAILABLE:
            raise RuntimeError("IDA Pro is not available")
        return _run_on_main(lambda: func(*args, **kwargs))

    return wrapper


def _parse_ea(ea: str | int | None) -> int:
    """Parse an address from string or int."""
    if ea is None:
        return idaapi.BADADDR
    if isinstance(ea, int):
        return ea
    ea = ea.strip().lower()
    # Handle special keywords
    if ea in ("here", "current", "cursor", "screen"):
        return idc.get_screen_ea()
    # Handle hex formats
    if ea.startswith("0x"):
        return int(ea, 16)
    if ea.endswith("h"):
        return int(ea[:-1], 16)
    try:
        return int(ea, 16)
    except ValueError:
        return int(ea)


# =============================================================================
# Navigation Tools
# =============================================================================


@tool(
    name="get_cursor_position",
    description="Get the current cursor position (effective address) in IDA.",
)
@ida_main_thread
def get_cursor_position() -> dict:
    """Get the current cursor EA."""
    ea = idc.get_screen_ea()
    func = ida_funcs.get_func(ea)

    result = {
        "ea": hex(ea),
        "in_function": func is not None,
    }

    if func:
        result["function_name"] = ida_funcs.get_func_name(func.start_ea)
        result["function_start"] = hex(func.start_ea)
        result["function_end"] = hex(func.end_ea)

    return result


@tool(
    name="goto_address",
    description="Navigate to a specific address in IDA.",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address to navigate to (hex string like '0x401000' or name)",
            },
        },
        "required": ["ea"],
    },
)
@ida_main_thread
def goto_address(ea: str) -> dict:
    """Navigate to an address."""

    # Try as address first
    try:
        addr = _parse_ea(ea)
    except ValueError:
        # Try as name
        addr = ida_name.get_name_ea(idaapi.BADADDR, ea)

    if addr == idaapi.BADADDR:
        return {"success": False, "error": f"Invalid address or name: {ea}"}

    ida_kernwin.jumpto(addr)
    return {"success": True, "ea": hex(addr)}


# =============================================================================
# Code Reading Tools
# =============================================================================


@tool(
    name="get_function",
    description="Get information about a function, including its decompiled pseudocode. Provide either 'ea' (an address inside the function) or 'name' (function name).",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address inside the function (hex string)",
            },
            "name": {
                "type": "string",
                "description": "Function name to look up",
            },
        },
    },
)
@ida_main_thread
def get_function(ea: str = None, name: str = None) -> dict:
    """Get function info and decompiled code."""

    # Resolve address
    if ea:
        addr = _parse_ea(ea)
    elif name:
        addr = ida_name.get_name_ea(idaapi.BADADDR, name)
    else:
        addr = idc.get_screen_ea()

    if addr == idaapi.BADADDR:
        return {"error": "Could not resolve address"}

    func = ida_funcs.get_func(addr)
    if not func:
        return {"error": f"No function at {hex(addr)}"}

    result = {
        "name": ida_funcs.get_func_name(func.start_ea),
        "start": hex(func.start_ea),
        "end": hex(func.end_ea),
        "size": func.end_ea - func.start_ea,
    }

    # Try to decompile
    try:
        if ida_hexrays.init_hexrays_plugin():
            cfunc = ida_hexrays.decompile(func.start_ea)
            if cfunc:
                result["pseudocode"] = str(cfunc)
                result["decompiled"] = True
            else:
                result["decompiled"] = False
                result["error_decompile"] = "Decompilation failed"
    except Exception as e:
        result["decompiled"] = False
        result["error_decompile"] = str(e)

    return result


@tool(
    name="get_disassembly",
    description="Get disassembly for an address range or function.",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Start address (hex string)",
            },
            "count": {
                "type": "integer",
                "description": "Number of instructions to disassemble (default: 20)",
            },
            "function": {
                "type": "boolean",
                "description": "If true, disassemble the entire function containing ea",
            },
        },
    },
)
@ida_main_thread
def get_disassembly(ea: str = None, count: int = 20, function: bool = False) -> dict:
    """Get disassembly listing."""

    addr = _parse_ea(ea) if ea else idc.get_screen_ea()
    if addr == idaapi.BADADDR:
        return {"error": "Invalid address"}

    lines = []

    if function:
        func = ida_funcs.get_func(addr)
        if not func:
            return {"error": f"No function at {hex(addr)}"}

        current = func.start_ea
        while current < func.end_ea:
            line = ida_lines.tag_remove(ida_lines.generate_disasm_line(current, 0))
            lines.append({"ea": hex(current), "disasm": line})
            next_addr = idc.next_head(current, func.end_ea)
            if next_addr <= current:  # Prevent infinite loop
                break
            current = next_addr
    else:
        current = addr
        for _ in range(count):
            if current == idaapi.BADADDR:
                break
            line = ida_lines.tag_remove(ida_lines.generate_disasm_line(current, 0))
            lines.append({"ea": hex(current), "disasm": line})
            current = idc.next_head(current, idaapi.BADADDR)

    return {
        "start": hex(addr),
        "count": len(lines),
        "lines": lines,
    }


@tool(
    name="get_bytes",
    description="Read raw bytes from an address.",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address to read from (hex string)",
            },
            "size": {
                "type": "integer",
                "description": "Number of bytes to read (default: 32)",
            },
        },
        "required": ["ea"],
    },
)
@ida_main_thread
def get_bytes(ea: str, size: int = 32) -> dict:
    """Read raw bytes."""

    # Cap size to prevent reading huge amounts
    MAX_SIZE = 1024 * 1024  # 1MB
    size = min(size, MAX_SIZE)

    addr = _parse_ea(ea)
    if addr == idaapi.BADADDR:
        return {"error": "Invalid address"}

    data = ida_bytes.get_bytes(addr, size)
    if data is None:
        return {"error": f"Could not read {size} bytes at {hex(addr)}"}

    return {
        "ea": hex(addr),
        "size": len(data),
        "hex": data.hex(),
        "printable": "".join(chr(b) if 32 <= b < 127 else "." for b in data),
    }


# =============================================================================
# Modification Tools
# =============================================================================


@tool(
    name="rename_function",
    description="Rename a function.",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address inside the function",
            },
            "old_name": {
                "type": "string",
                "description": "Current function name (alternative to ea)",
            },
            "new_name": {
                "type": "string",
                "description": "New name for the function",
            },
        },
        "required": ["new_name"],
    },
)
@ida_main_thread
def rename_function(new_name: str, ea: str = None, old_name: str = None) -> dict:
    """Rename a function."""

    if ea:
        addr = _parse_ea(ea)
    elif old_name:
        addr = ida_name.get_name_ea(idaapi.BADADDR, old_name)
    else:
        addr = idc.get_screen_ea()

    if addr == idaapi.BADADDR:
        return {"error": "Could not resolve function address"}

    func = ida_funcs.get_func(addr)
    if not func:
        return {"error": f"No function at {hex(addr)}"}

    old = ida_funcs.get_func_name(func.start_ea)
    success = idc.set_name(func.start_ea, new_name, idc.SN_CHECK)

    if success:
        return {"success": True, "old_name": old, "new_name": new_name, "ea": hex(func.start_ea)}
    else:
        return {"success": False, "error": "Failed to rename function"}


@tool(
    name="rename_variable",
    description="Rename a local variable in a function's decompiled view.",
    parameters={
        "type": "object",
        "properties": {
            "function_ea": {
                "type": "string",
                "description": "Address inside the function containing the variable",
            },
            "old_name": {
                "type": "string",
                "description": "Current variable name",
            },
            "new_name": {
                "type": "string",
                "description": "New variable name",
            },
        },
        "required": ["old_name", "new_name"],
    },
)
@ida_main_thread
def rename_variable(old_name: str, new_name: str, function_ea: str = None) -> dict:
    """Rename a local variable."""

    if not ida_hexrays.init_hexrays_plugin():
        return {"error": "Hex-Rays decompiler not available"}

    addr = _parse_ea(function_ea) if function_ea else idc.get_screen_ea()
    func = ida_funcs.get_func(addr)
    if not func:
        return {"error": f"No function at {hex(addr)}"}

    try:
        cfunc = ida_hexrays.decompile(func.start_ea)
        if not cfunc:
            return {"error": "Decompilation failed"}

        # Find the variable
        for lvar in cfunc.lvars:
            if lvar.name == old_name:
                success = ida_hexrays.rename_lvar(func.start_ea, old_name, new_name)
                if success:
                    return {"success": True, "old_name": old_name, "new_name": new_name}
                else:
                    return {"success": False, "error": "rename_lvar failed"}

        return {"error": f"Variable '{old_name}' not found in function"}

    except Exception as e:
        return {"error": str(e)}


@tool(
    name="set_comment",
    description="Set a comment at an address.",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address to comment",
            },
            "comment": {
                "type": "string",
                "description": "Comment text",
            },
            "repeatable": {
                "type": "boolean",
                "description": "If true, comment appears everywhere this address is referenced",
            },
        },
        "required": ["ea", "comment"],
    },
)
@ida_main_thread
def set_comment(ea: str, comment: str, repeatable: bool = False) -> dict:
    """Set a comment at an address."""

    addr = _parse_ea(ea)
    if addr == idaapi.BADADDR:
        return {"error": "Invalid address"}

    success = idc.set_cmt(addr, comment, 1 if repeatable else 0)

    return {"success": bool(success), "ea": hex(addr)}


@tool(
    name="set_function_comment",
    description="Set a comment for an entire function (appears at function start).",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Address inside the function",
            },
            "comment": {
                "type": "string",
                "description": "Comment text",
            },
        },
        "required": ["comment"],
    },
)
@ida_main_thread
def set_function_comment(comment: str, ea: str = None) -> dict:
    """Set a function comment."""

    addr = _parse_ea(ea) if ea else idc.get_screen_ea()
    func = ida_funcs.get_func(addr)
    if not func:
        return {"error": f"No function at {hex(addr)}"}

    idc.set_func_cmt(func.start_ea, comment, 0)
    return {"success": True, "ea": hex(func.start_ea)}


# =============================================================================
# Search Tools
# =============================================================================


@tool(
    name="get_xrefs_to",
    description="Get cross-references TO an address (who references this?).",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Target address",
            },
            "name": {
                "type": "string",
                "description": "Symbol name (alternative to ea)",
            },
        },
    },
)
@ida_main_thread
def get_xrefs_to(ea: str = None, name: str = None) -> dict:
    """Get xrefs to an address."""

    if ea:
        addr = _parse_ea(ea)
    elif name:
        addr = ida_name.get_name_ea(idaapi.BADADDR, name)
    else:
        addr = idc.get_screen_ea()

    if addr == idaapi.BADADDR:
        return {"error": "Invalid address"}

    xrefs = []
    for xref in idautils.XrefsTo(addr):
        func = ida_funcs.get_func(xref.frm)
        xrefs.append(
            {
                "from": hex(xref.frm),
                "type": _xref_type_name(xref.type),
                "function": ida_funcs.get_func_name(func.start_ea) if func else None,
            }
        )

    return {
        "target": hex(addr),
        "count": len(xrefs),
        "xrefs": xrefs,
    }


@tool(
    name="get_xrefs_from",
    description="Get cross-references FROM an address (what does this reference?).",
    parameters={
        "type": "object",
        "properties": {
            "ea": {
                "type": "string",
                "description": "Source address",
            },
        },
    },
)
@ida_main_thread
def get_xrefs_from(ea: str = None) -> dict:
    """Get xrefs from an address."""

    addr = _parse_ea(ea) if ea else idc.get_screen_ea()
    if addr == idaapi.BADADDR:
        return {"error": "Invalid address"}

    xrefs = []
    for xref in idautils.XrefsFrom(addr):
        name = ida_name.get_name(xref.to)
        xrefs.append(
            {
                "to": hex(xref.to),
                "type": _xref_type_name(xref.type),
                "name": name if name else None,
            }
        )

    return {
        "source": hex(addr),
        "count": len(xrefs),
        "xrefs": xrefs,
    }


@tool(
    name="list_functions",
    description="List functions in the binary.",
    parameters={
        "type": "object",
        "properties": {
            "start": {
                "type": "integer",
                "description": "Start index for pagination (default: 0)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum functions to return (default: 100)",
            },
            "filter": {
                "type": "string",
                "description": "Only include functions containing this substring",
            },
        },
    },
)
@ida_main_thread
def list_functions(start: int = 0, limit: int = 100, filter: str = None) -> dict:
    """List functions."""

    functions = []
    count = 0

    for func_ea in idautils.Functions():
        name = ida_funcs.get_func_name(func_ea)

        if filter and filter.lower() not in name.lower():
            continue

        if count < start:
            count += 1
            continue

        if len(functions) >= limit:
            break

        func = ida_funcs.get_func(func_ea)
        functions.append(
            {
                "ea": hex(func_ea),
                "name": name,
                "size": func.end_ea - func.start_ea if func else 0,
            }
        )
        count += 1

    return {
        "start": start,
        "count": len(functions),
        "functions": functions,
    }


@tool(
    name="search_strings",
    description="Search for strings in the binary.",
    parameters={
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "description": "Substring to search for (case-insensitive)",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum results (default: 50)",
            },
        },
        "required": ["pattern"],
    },
)
@ida_main_thread
def search_strings(pattern: str, limit: int = 50) -> dict:
    """Search for strings."""

    results = []
    pattern_lower = pattern.lower()

    for s in idautils.Strings():
        text = str(s)
        if pattern_lower in text.lower():
            results.append(
                {
                    "ea": hex(s.ea),
                    "text": text,
                    "length": s.length,
                }
            )
            if len(results) >= limit:
                break

    return {
        "pattern": pattern,
        "count": len(results),
        "strings": results,
    }


# =============================================================================
# Utility Tools
# =============================================================================


@tool(
    name="refresh_view",
    description="Refresh IDA's disassembly and decompiler views.",
)
@ida_main_thread
def refresh_view() -> dict:
    """Refresh IDA views."""

    ida_kernwin.refresh_idaview_anyway()

    # Try to refresh pseudocode view too
    widget = ida_kernwin.find_widget("Pseudocode-A")
    if widget:
        ida_kernwin.activate_widget(widget, True)

    return {"success": True}


@tool(
    name="get_segment_info",
    description="Get information about memory segments in the binary.",
)
@ida_main_thread
def get_segment_info() -> dict:
    """Get segment information."""

    segments = []
    for seg_ea in idautils.Segments():
        seg = ida_segment.getseg(seg_ea)
        segments.append(
            {
                "name": ida_segment.get_segm_name(seg),
                "start": hex(seg.start_ea),
                "end": hex(seg.end_ea),
                "size": seg.end_ea - seg.start_ea,
                "permissions": f"{'r' if seg.perm & 4 else '-'}{'w' if seg.perm & 2 else '-'}{'x' if seg.perm & 1 else '-'}",
            }
        )

    return {"segments": segments}


# =============================================================================
# Snapshot Tools
# =============================================================================


@tool(
    name="take_snapshot",
    description="Take a database snapshot to save the current state. Use before making significant changes.",
    parameters={
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "Description of this snapshot (e.g., 'Before renaming functions')",
            },
        },
        "required": ["description"],
    },
)
@ida_main_thread
def take_snapshot(description: str) -> dict:
    """Take a database snapshot."""

    snapshot = ida_loader.snapshot_t()
    snapshot.desc = description[:127]  # Max 128 chars

    success, error_msg = ida_kernwin.take_database_snapshot(snapshot)

    if success:
        return {"success": True, "description": description}
    else:
        return {"success": False, "error": error_msg}


@tool(
    name="list_snapshots",
    description="List all database snapshots.",
)
@ida_main_thread
def list_snapshots() -> dict:
    """List all snapshots."""

    root = ida_loader.snapshot_t()
    if not ida_loader.build_snapshot_tree(root):
        return {"snapshots": []}

    snapshots = []
    for i in range(root.children.size()):
        snap = root.children.at(i)
        snapshots.append(
            {
                "id": snap.id,
                "description": snap.desc,
                "filename": snap.filename,
            }
        )

    return {"snapshots": snapshots}


@tool(
    name="restore_snapshot",
    description="Restore a database snapshot by its ID. Warning: This reloads the database.",
    parameters={
        "type": "object",
        "properties": {
            "snapshot_id": {
                "type": "integer",
                "description": "Snapshot ID from list_snapshots",
            },
        },
        "required": ["snapshot_id"],
    },
)
@ida_main_thread
def restore_snapshot(snapshot_id: int) -> dict:
    """Restore a database snapshot."""

    # Find the snapshot
    root = ida_loader.snapshot_t()
    if not ida_loader.build_snapshot_tree(root):
        return {"error": "Failed to build snapshot tree"}

    target = None
    for i in range(root.children.size()):
        snap = root.children.at(i)
        if snap.id == snapshot_id:
            target = snap
            break

    if not target:
        return {"error": f"Snapshot {snapshot_id} not found"}

    # Restore is async - callback is called when done
    def on_restore(userdata, err):
        pass  # Nothing to do, IDA handles the reload

    success = ida_kernwin.restore_database_snapshot(target, on_restore, None)

    if success:
        return {"success": True, "message": "Restore initiated - database will reload"}
    else:
        return {"success": False, "error": "Failed to initiate restore"}


# =============================================================================
# Undo/Redo Tools
# =============================================================================


@tool(
    name="get_undo_status",
    description="Get the current undo/redo status - what actions can be undone or redone.",
)
@ida_main_thread
def get_undo_status() -> dict:
    """Get undo/redo status."""

    return {
        "can_undo": ida_undo.get_undo_action_label() or None,
        "can_redo": ida_undo.get_redo_action_label() or None,
    }


@tool(
    name="undo",
    description="Undo the last action. Returns the action that was undone.",
)
@ida_main_thread
def undo() -> dict:
    """Perform undo."""

    label = ida_undo.get_undo_action_label()
    if not label:
        return {"success": False, "error": "Nothing to undo"}

    success = ida_undo.perform_undo()
    return {"success": success, "action": label}


@tool(
    name="redo",
    description="Redo the last undone action. Returns the action that was redone.",
)
@ida_main_thread
def redo() -> dict:
    """Perform redo."""

    label = ida_undo.get_redo_action_label()
    if not label:
        return {"success": False, "error": "Nothing to redo"}

    success = ida_undo.perform_redo()
    return {"success": success, "action": label}


# =============================================================================
# Script Execution Tools
# =============================================================================


@tool(
    name="execute_script",
    description="""Execute Python code inside IDA Pro. The code runs in an isolated namespace without access to the plugin's internal variables.

Use this for complex operations that can't be done with other tools, such as:
- Custom analysis algorithms
- Batch operations on multiple addresses/functions
- Complex data structure parsing
- Interacting with IDA APIs not exposed by other tools

The code has access to all IDA Python modules (ida_*, idc, idautils, idaapi).
Any print() output is captured and returned.""",
    parameters={
        "type": "object",
        "properties": {
            "code": {
                "type": "string",
                "description": "Python code to execute.",
            },
        },
        "required": ["code"],
    },
)
@ida_main_thread
def execute_script(code: str) -> dict:
    """Execute Python code inside IDA using IDAPython_ExecScript for isolation."""
    import io
    import os
    import sys
    import tempfile

    # Create temp file with the code
    fd, path = tempfile.mkstemp(suffix=".py", prefix="ida_claude_")
    try:
        os.write(fd, code.encode("utf-8"))
        os.close(fd)

        # Fresh globals dict - isolated from plugin namespace
        exec_globals = {"__name__": "__main__"}

        # Capture stdout/stderr
        stdout_capture = io.StringIO()
        stderr_capture = io.StringIO()
        old_stdout, old_stderr = sys.stdout, sys.stderr

        try:
            sys.stdout = stdout_capture
            sys.stderr = stderr_capture

            # Run via IDA's official script runner
            error = idaapi.IDAPython_ExecScript(path, exec_globals, False)

            output = stdout_capture.getvalue()
            stderr_output = stderr_capture.getvalue()
            if stderr_output:
                output = (output + "\n[stderr]\n" + stderr_output).strip()

            if error:
                return {
                    "success": False,
                    "error": error,
                    "output": output,
                }

            return {
                "success": True,
                "output": output,
            }
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

    finally:
        # Clean up temp file
        try:
            os.unlink(path)
        except OSError:
            pass
