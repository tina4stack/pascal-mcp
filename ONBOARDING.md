# Claude Pascal MCP — Team Handbook

A self-contained guide for uploading to Claude Teams / project knowledge. Everything a teammate needs to set up and use the Claude Pascal MCP server lives in this file.

Repo: `git@github.com:tina4stack/pascal-mcp.git`
License: MIT

---

## What this is

An MCP (Model Context Protocol) server that lets Claude compile, run, and interact with Pascal/Delphi desktop applications. It bundles five capability areas:

1. **Pascal/Delphi compilation** — Free Pascal (`fpc`), Delphi 32-bit (`dcc32`), and Delphi 64-bit (`dcc64`).
2. **Windows desktop automation** — screenshot, click, type, and key-send to running app windows without stealing focus.
3. **IDE observer** — capture screenshots of RAD Studio / Delphi / Lazarus and surface compiler errors with surrounding source context.
4. **Android device automation (ADB)** — screenshots, taps, swipes, text input, key events, app management, and file transfer.
5. **Preview bridge** — a Starlette HTTP server that serves live screenshots of desktop apps so Claude's web preview panel can interact with them.

---

## Prerequisites

- **Python 3.11+**
- **[uv](https://docs.astral.sh/uv/)** package manager
- **A Pascal compiler** — Free Pascal, Delphi, or RAD Studio. If none are installed, the `setup_fpc` tool will download and install Free Pascal for you.
- **Windows** for the desktop-automation and IDE-observer tools (uses Win32 APIs).
- **ADB on `PATH`** for the Android tools (ships with Android SDK Platform Tools).

The server auto-detects compilers in this priority order:

1. Free Pascal (`fpc`)
2. Delphi 64-bit (`dcc64`)
3. Delphi 32-bit (`dcc32`)

It checks the system `PATH` first, then known install directories:

- `C:\FPC\*\bin\*\fpc.exe`
- `C:\Lazarus\fpc\*\bin\*\fpc.exe`
- `C:\Program Files (x86)\Embarcadero\Studio\*\bin\dcc*.exe`

You can always pass an explicit `compiler="C:\\full\\path\\to\\dcc64.exe"` to override detection.

---

## First-time setup

Three ways to install — pick the one that fits.

### A. From PyPI (recommended once published)

No clone, no `uv sync` — `uvx` fetches and runs in one shot:

The PyPI distribution keeps its original `claude-pascal-mcp` name for compatibility. The server and repository use `pascal-mcp`.

```bash
uvx --from claude-pascal-mcp pascal-mcp
```

`.mcp.json`:

```json
{
  "mcpServers": {
    "pascal-mcp": {
      "command": "uvx",
      "args": ["--from", "claude-pascal-mcp", "pascal-mcp"]
    }
  }
}
```

### B. From GitHub (no PyPI account needed)

Run it straight from the repo. Pin a tag for reproducibility.

```bash
uvx --from git+https://github.com/tina4stack/pascal-mcp pascal-mcp
```

`.mcp.json`:

```json
{
  "mcpServers": {
    "pascal-mcp": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/tina4stack/pascal-mcp", "pascal-mcp"]
    }
  }
}
```

### C. Local dev clone (for contributors)

```bash
git clone git@github.com:tina4stack/pascal-mcp.git
cd pascal-mcp
uv sync

# Run the MCP server (stdio mode)
uv run pascal-mcp

# Or run the preview bridge (HTTP mode)
uv run pascal-preview
```

`.mcp.json`:

```json
{
  "mcpServers": {
    "pascal-mcp": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/pascal-mcp", "pascal-mcp"]
    }
  }
}
```

### Register with Claude Code

Pick the matching install form:

```bash
claude mcp add --transport stdio pascal-mcp -- uvx --from claude-pascal-mcp pascal-mcp
claude mcp add --transport stdio pascal-mcp -- uvx --from git+https://github.com/tina4stack/pascal-mcp pascal-mcp
claude mcp add --transport stdio pascal-mcp -- uv run --directory /path/to/pascal-mcp pascal-mcp
```

### Register with Claude Desktop

Add the same `mcpServers` block to `claude_desktop_config.json`.

### Enable the preview bridge

Add to `.claude/launch.json` in the project you're working in:

```json
{
  "version": "0.0.1",
  "configurations": [
    {
      "name": "pascal-preview",
      "runtimeExecutable": "/path/to/pascal-mcp/.venv/Scripts/pythonw.exe",
      "runtimeArgs": ["-m", "pascal_mcp.preview_bridge"],
      "port": 18080,
      "autoPort": true
    }
  ]
}
```

Then call `preview_start("pascal-preview")` from Claude Code to open the preview panel.

---

## Driving it from Claude — example prompts

- "Compile and run this Pascal program." → `run_pascal`
- "Build my project at `C:\src\MyApp\MyApp.dproj`." → `build_dproj`
- "Generate a Delphi app with a button and a memo, then launch it." → `compile_delphi_project` + `launch_app`
- "Screenshot the RAD Studio IDE and tell me what compiler errors are showing." → `observe_ide` + `read_ide_errors`
- "Take a screenshot of my running app and click the Save button." → `screenshot_app` + `app_click`
- "What Android devices are connected? Tap the login button at (540, 1200)." → `adb_devices` + `adb_tap`

The MCP server instructs Claude to **always route Pascal/Delphi work through these tools** — never `msbuild`, raw shell calls to `dcc32`, etc. That keeps compiler invocation, output capture, and path handling consistent across the team.

### Critical tool distinctions

| Need | Use | Don't confuse with |
|------|-----|---------------------|
| Compile a snippet of source | `compile_pascal` | — |
| Build a real, existing `.dproj` (multi-unit, custom search paths, defines, resources) | `build_dproj` | `compile_delphi_project` |
| Scaffold a throwaway demo project from a template (TButton/TEdit/TLabel/TMemo) | `compile_delphi_project` | `build_dproj` |
| Run a console program and capture output | `run_pascal` | `launch_app` |
| Launch a GUI app that needs to keep running | `launch_app` | `run_pascal` |
| Syntax check without linking | `check_syntax` | — |

---

## Full tool reference

### Pascal/Delphi

| Tool | Description |
|------|-------------|
| `get_compiler_info` | Detect available compilers and show versions. |
| `compile_pascal` | Compile a single-file Pascal source. |
| `build_dproj` | Build an existing `.dproj` with its own units, search paths, defines, resources. |
| `compile_delphi_project` | Generate a new throwaway Delphi project from a template (DPR + PAS + DFM). Cannot build an existing `.dproj`. |
| `run_pascal` | Compile and execute a console program, capturing stdout/stderr. |
| `launch_app` | Compile and launch a GUI app in the background (no focus stealing). |
| `check_syntax` | Syntax check only — no linking. |
| `parse_form` | Parse DFM / FMX / LFM form files. |
| `setup_fpc` | Download and install Free Pascal as a fallback if no compiler is found. |

### Windows desktop interaction

| Tool | Description |
|------|-------------|
| `screenshot_app` | Capture a screenshot of a running app window (non-intrusive). |
| `list_app_windows` | List visible windows on the desktop. |
| `app_click` | Click a Windows app window at screenshot pixel coordinates. |
| `app_type` | Type text into a Windows app window. |
| `app_key` | Send a key or shortcut (`ctrl+a`, `enter`, `alt+f4`, …) to a window. |

Clicks use `PostMessage` with automatic child-window targeting so they land on the correct control. Typing and key events use `SendInput` for full Unicode and modifier support.

### IDE observer

| Tool | Description |
|------|-------------|
| `observe_ide` | Capture an IDE screenshot and scan project files. |
| `read_ide_errors` | Read source code around compiler error locations. |
| `list_project_files` | List source files in a Delphi / Lazarus project. |

### Android (ADB)

All ADB tools accept an optional `device` serial. With a single device connected, it auto-selects.

| Tool | Description |
|------|-------------|
| `adb_devices` | List connected devices with model, Android version, screen size. |
| `adb_device_info` | Detailed info for a specific device. |
| `adb_screenshot` | Capture the device screen. |
| `adb_tap` / `adb_swipe` | Touch interaction at pixel coordinates. |
| `adb_type_text` | Type text (auto-escapes for `adb shell`). |
| `adb_key` | Send a key event. Aliases: `home`, `back`, `enter`, `menu`, `power`, `volume_up`, `volume_down`, `tab`, `delete`, `space`, `escape`, `app_switch`. |
| `adb_install` | Install an APK. |
| `adb_list_packages` | List installed packages (optional filter). |
| `adb_launch_app` | Launch an app by package name. |
| `adb_stop_app` | Force-stop an app. |
| `adb_push` / `adb_pull` | Push or pull files between PC and device. |

---

## Project templates (`compile_delphi_project`)

You specify components and event handlers, and the tool emits the correct DPR, PAS, and DFM files. Templates automatically pick the right uses-clause style:

- **Modern Delphi** (RAD Studio) → namespaced units (`Vcl.Forms`, `System.SysUtils`).
- **Legacy Delphi 7** → non-namespaced units (`Forms`, `SysUtils`).

Form definitions (DFM) and event wiring between DFM and PAS are generated for you.

### Example

```
compile_delphi_project(
  project_name="HelloWorld",
  form_caption="My App",
  components='[{"type": "TButton", "name": "btnHello", "caption": "Click Me",
                "left": 100, "top": 100, "width": 120, "height": 35,
                "event": "btnHelloClick"}]',
  events='[{"name": "btnHelloClick", "body": "ShowMessage(\'Hello!\');"}]',
  compiler="C:\\Path\\To\\dcc64.exe"
)
```

This produces:
- `HelloWorld.dpr` — project file with proper uses clause
- `uMain.pas` — unit with form class, component declarations, event handlers
- `uMain.dfm` — form definition with component properties

Supported component types in templates: `TButton`, `TEdit`, `TLabel`, `TMemo`.

---

## Preview bridge

The preview bridge lets Claude see and interact with running Pascal desktop apps through its web-based preview panel. It runs as a Python/Starlette HTTP server.

```
Claude preview tools (preview_start / preview_screenshot / preview_click)
        │ HTTP
        ▼
Preview Bridge Server (Python / Starlette)
   /                 → HTML page with live screenshot viewer
   /api/screenshot   → PNG of target window
   /api/controls     → enumerate child controls with positions
   /api/click        → click at coordinates or by control hwnd
   /api/type         → send keystrokes to target window
   /api/move         → move window to screen position
   /api/resize       → resize window
        │ Win32 PrintWindow API
        ▼
Running Pascal desktop application
```

### HTTP API

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | HTML page with auto-refreshing screenshot viewer. |
| `/api/screenshot` | GET | PNG screenshot of target window. |
| `/api/windows` | GET | List visible windows. |
| `/api/target` | POST | Set target window by title. |
| `/api/controls` | GET | Enumerate child controls (buttons, inputs, …). |
| `/api/click` | POST | Click by coordinates or direct control hwnd. |
| `/api/type` | POST | Send text or key combos (`ctrl+a`, `enter`, …). |
| `/api/drag` | POST | Drag from one point to another. |
| `/api/move` | POST | Move target window. |
| `/api/resize` | POST | Resize target window. |
| `/api/window-info` | GET | Window position, size, client-area offset. |
| `/api/console` | GET | Console output from launched apps. |
| `/api/launch` | POST | Launch an executable. |

### Click methods (most to least reliable)

1. **Direct control click** — `{"hwnd": "12345"}` sends `BM_CLICK` directly to a control handle. Works regardless of DPI, multiple monitors, or foreground state. Get hwnds from `/api/controls`.
2. **Client-area coordinates** — `{"x": 200, "y": 142, "client": true}` uses `ClientToScreen` for proper DPI handling.
3. **Window-relative coordinates** — `{"x": 312, "y": 261}` — raw coordinates in the screenshot image space.

---

## Workflows worth knowing

### Driving a desktop Delphi app from Claude

1. `screenshot_app` — capture the current UI.
2. Identify pixel coordinates of the target element.
3. `app_click` to click. Use `app_type` to enter text into the focused field. Use `app_key` for keyboard shortcuts.

### Driving an Android device from Claude

1. `adb_devices` — pick a device (auto-selected when only one is attached).
2. `adb_screenshot` — see the current screen.
3. `adb_tap` / `adb_swipe` / `adb_type_text` / `adb_key` — drive the UI.
4. `adb_install` / `adb_launch_app` / `adb_stop_app` — manage apps.
5. `adb_push` / `adb_pull` — transfer files in either direction.

### Diagnosing IDE compile errors

1. `observe_ide` — screenshot the IDE and scan its project files.
2. `read_ide_errors` — pull source context around each reported error.

---

## Repo layout

- `src/pascal_mcp/server.py` — MCP tool registrations.
- `src/pascal_mcp/compiler.py` — compiler detection and invocation.
- `src/pascal_mcp/preview_bridge.py` — Starlette HTTP server for the live preview.

---

## Support

- Issues: open one on the repo.
- License: MIT.
