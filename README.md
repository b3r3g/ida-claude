# IDA Claude

Claude AI assistant embedded in IDA Pro.

## Requirements

- IDA Pro 9.2+ (uses PySide6/Qt6)
- Python 3.10+
- Anthropic API key

## Install

### 1. Install dependencies

Open a terminal and run with IDA's Python (not your system Python):

```bash
# Use the Python that IDA is using (see tip below to find it)
# Windows
& "C:\Users\YOU\AppData\Local\Programs\Python\Python312\python.exe" -m pip install anthropic markdown

# Linux/macOS
/usr/bin/python3 -m pip install anthropic markdown
```

> **Tip**: Run `import sys; print(sys.prefix)` in IDA's Python console to find which Python IDA uses. Then run `<that path>/python.exe -m pip install ...`

### 2. Copy plugin to IDA

Copy `ida_claude.py` and the `ida_claude/` folder to your IDA plugins directory:

| OS | Plugins folder |
|----|----------------|
| Windows | `C:\Program Files\IDA Professional 9.x\plugins\` |
| Linux | `~/.idapro/plugins/` |
| macOS | `~/Library/Application Support/IDA Pro/plugins/` |

Final structure:
```
plugins/
  ida_claude.py        # entry point (this file)
  ida_claude/          # package folder
    __init__.py
    plugin.py
    widget.py
    client.py
    loop.py
    config.py
    tools/
      __init__.py
      ida.py
```

### 3. Set API key

Either:
- **Environment variable** (recommended): Set `ANTHROPIC_API_KEY` before launching IDA
  ```bash
  # Windows (PowerShell)
  $env:ANTHROPIC_API_KEY = "sk-ant-..."

  # Linux/macOS
  export ANTHROPIC_API_KEY="sk-ant-..."
  ```
- **Plugin settings**: Click Settings button in the plugin UI after first launch

## Usage

1. Open IDA, load a binary
2. Press `Ctrl+Alt+C` or menu `Edit > Plugins > Claude AI`
3. Chat with Claude about the binary

### Keyboard shortcuts

- `Ctrl+Alt+C` - Open/focus Claude panel
- `Ctrl+Enter` - Send message (in input box)
- `Enter` - Newline (in input box)


## Configuration

Config stored in IDA user directory: `ida_claude_config.json`

```json
{
  "api_key": "sk-ant-...",
  "model": "claude-sonnet-4-20250514",
  "max_tokens": 8192
}
```

Click **Settings** button in the plugin to edit.

## Cost

Uses Claude API directly. Typical costs:
- Prompt caching reduces repeat query costs by ~90%
- Cache TTL: 5 minutes (shown in UI)
- See Anthropic pricing: https://anthropic.com/pricing

## License

MIT
