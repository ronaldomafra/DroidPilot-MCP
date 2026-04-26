# DroidPilot MCP

DroidPilot MCP is a local MCP server for operating Android devices through ADB only. It exposes tools for screenshots, touch gestures, text input, app launch/stop, package inspection, logcat capture, and Android stability checks.

The server runs over MCP `stdio` by default and does not require a companion Android app or live mirroring service.

## Requirements

- Python 3.10+
- Android platform-tools / `adb`
- An Android device or emulator visible to `adb devices`

## Python Setup

From the DroidPilot MCP repository root:

```bash
python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python android_agent_mcp_server.py --help
```

Windows PowerShell:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe android_agent_mcp_server.py --help
```

## Local Configuration

The server reads local configuration from the project that starts the MCP process, not from the DroidPilot MCP installation directory. By default it uses:

```text
<active-project>/android-agent.config.json
```

The versioned `android-agent.config.example.json` file stays in the DroidPilot MCP repository as a template. Copy it into each project that loads the MCP, or let `android_set_adb_config` create `android-agent.config.json` in the active project.

The server tries to autodetect `adb` on startup using `PATH` and common Android SDK locations. If it cannot find `adb`, it logs a warning and the MCP tools `android_adb_autodetect` and `android_set_adb_config` can be used to inspect or set the path.

Optional local config:

```bash
cp /abs/path/DroidPilot-MCP/android-agent.config.example.json ./android-agent.config.json
```

Example:

```json
{
  "timeoutSeconds": 12,
  "adbPath": "/opt/android/platform-tools/adb",
  "adbDeviceSerial": "",
  "artifactsDir": "tests/mcp",
  "navigationMemoryPath": "tests/mcp/navigation/navigation-guide.json"
}
```

Configuration precedence:

1. CLI arguments such as `--adb-path` and `--adb-device-serial`
2. `android-agent.config.json`
3. Environment variables such as `ANDROID_AGENT_ADB_PATH` and `ANDROID_AGENT_ADB_DEVICE_SERIAL`
4. autodetection

`artifactsDir` and `navigationMemoryPath` are also relative to the active project by default. Add `android-agent.config.json` and `tests/mcp/` to the active project's `.gitignore` if that project is versioned.

## Install in Codex CLI

Recommended:

```bash
./scripts/install_codex_mcp.sh
```

Recreate an existing registration:

```bash
./scripts/install_codex_mcp.sh --force
```

Manual registration:

```bash
codex mcp add androidAgent -- /abs/path/DroidPilot-MCP/.venv/bin/python /abs/path/DroidPilot-MCP/android_agent_mcp_server.py
```

Verify:

```bash
codex mcp list
codex mcp get androidAgent
```

## Install in Cursor

Create `.cursor/mcp.json` in a project, or `~/.cursor/mcp.json` globally:

```json
{
  "mcpServers": {
    "androidAgent": {
      "type": "stdio",
      "command": "/abs/path/DroidPilot-MCP/.venv/bin/python",
      "args": [
        "/abs/path/DroidPilot-MCP/android_agent_mcp_server.py"
      ]
    }
  }
}
```

Then restart Cursor and list tools with:

```bash
cursor-agent mcp list-tools androidAgent
```

## Install in Claude Code

```bash
claude mcp add --transport stdio \
  androidAgent \
  -- /abs/path/DroidPilot-MCP/.venv/bin/python /abs/path/DroidPilot-MCP/android_agent_mcp_server.py
```

If a client does not launch MCP servers with the target project as its working directory, pass an explicit config path in the MCP args:

```json
{
  "args": [
    "/abs/path/DroidPilot-MCP/android_agent_mcp_server.py",
    "--config",
    "/abs/path/your-project/android-agent.config.json"
  ]
}
```

Verify:

```bash
claude mcp list
claude mcp get androidAgent
```

## ADB Configuration Tools

- `android_adb_config`: returns the effective ADB configuration and session paths.
- `android_adb_autodetect`: searches for `adb` in `PATH` and common SDK locations.
- `android_set_adb_config`: updates `adbPath` and `adbDeviceSerial` at runtime and persists them by default.

Example tool inputs:

```json
{
  "adb_path": "/home/user/Android/Sdk/platform-tools/adb",
  "adb_device_serial": "emulator-5554",
  "persist": true
}
```

## Tools

- `android_agent_status`
- `android_adb_config`
- `android_adb_autodetect`
- `android_set_adb_config`
- `android_navigation_guide`
- `android_save_navigation_note`
- `android_get_screen`
- `android_list_apps`
- `android_app_info`
- `android_open_app`
- `android_adb_open_app`
- `android_close_app`
- `android_tap`
- `android_swipe`
- `android_long_click`
- `android_input_text`
- `android_back`
- `android_home`
- `android_scroll`
- `android_adb_status`
- `android_clear_logcat`
- `android_get_logcat`
- `android_detect_known_issues`

## Recommended Flow

1. Run `android_adb_config`.
2. If needed, run `android_adb_autodetect` or `android_set_adb_config`.
3. Run `android_adb_status` and confirm the target device is visible.
4. Run `android_clear_logcat` before a test.
5. Open the app with `android_open_app` or `android_adb_open_app`.
6. Use `android_get_screen`, `android_tap`, `android_swipe`, `android_input_text`, `android_back`, `android_home`, and `android_scroll`.
7. Run `android_detect_known_issues` at the end.
8. Save reusable navigation notes with `android_save_navigation_note`.

`android_get_screen` writes screenshots under `<active-project>/tests/mcp/<timestamp>/artifacts`. Command logs are written under `<active-project>/tests/mcp/<timestamp>/commands`. Navigation memory is stored in `<active-project>/tests/mcp/navigation/navigation-guide.json` unless overridden.

## Logcat Stability Checks

- `android_clear_logcat`: runs `adb logcat -c`.
- `android_get_logcat`: runs `adb logcat -d`, saves `logcat.txt`, and returns a preview.
- `android_detect_known_issues`: detects common Android failure signals from logcat.

Detected patterns include `FATAL EXCEPTION`, `WindowLeaked`, `ANR`, `IllegalStateException`, `NullPointerException`, `SecurityException`, `WindowManager$BadTokenException`, fragment detached errors, and `Can not perform this action after onSaveInstanceState`.

## Troubleshooting

- If `adbAvailable` is false, install Android platform-tools or run `android_set_adb_config`.
- If more than one device is connected, set `adbDeviceSerial`.
- If screenshot or input tools fail, confirm the device is authorized and listed by `adb devices`.
- If the client does not show new tools, restart the MCP client after changing registration or dependencies.
