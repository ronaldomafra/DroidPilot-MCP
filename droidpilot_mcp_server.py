#!/usr/bin/env python3
"""DroidPilot MCP server backed only by local ADB."""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


PROJECT_DIR = Path.cwd().resolve()
DEFAULT_ARTIFACTS_DIR = PROJECT_DIR / "tests" / "mcp"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "android-agent.config.json"
DEFAULT_NAVIGATION_MEMORY_PATH = DEFAULT_ARTIFACTS_DIR / "navigation" / "navigation-guide.json"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_SCREENSHOT_TIMEOUT_SECONDS = 30.0

try:
    from mcp.server.fastmcp import FastMCP
    from mcp.types import ToolAnnotations
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "Nao foi possivel importar o SDK MCP. Instale as dependencias com "
        "`python -m pip install -r requirements.txt` e execute o servidor com o Python desse ambiente."
    ) from exc


LOGGER = logging.getLogger("droidpilot-mcp")
NAVIGATION_EVENT_ACTIONS = {
    "adb_open_app",
    "open_app",
    "close_app",
    "get_screen",
    "tap",
    "swipe",
    "long_click",
    "input_text",
    "back",
    "home",
    "scroll",
}
LOGCAT_ISSUE_PATTERNS = [
    {
        "key": "hasCrash",
        "label": "FATAL EXCEPTION",
        "summary": "FATAL EXCEPTION detected",
        "pattern": re.compile(r"FATAL EXCEPTION", re.IGNORECASE),
    },
    {
        "key": "hasWindowLeak",
        "label": "WindowLeaked",
        "summary": "WindowLeaked detected",
        "pattern": re.compile(r"android\.view\.WindowLeaked|Activity has leaked window", re.IGNORECASE),
    },
    {
        "key": "hasANR",
        "label": "ANR",
        "summary": "ANR detected",
        "pattern": re.compile(r"\bANR\b|Application Not Responding", re.IGNORECASE),
    },
    {
        "key": "hasIllegalState",
        "label": "IllegalStateException",
        "summary": "IllegalStateException detected",
        "pattern": re.compile(r"IllegalStateException", re.IGNORECASE),
    },
    {
        "key": "hasNPE",
        "label": "NullPointerException",
        "summary": "NullPointerException detected",
        "pattern": re.compile(r"NullPointerException", re.IGNORECASE),
    },
    {
        "key": "hasSecurityException",
        "label": "SecurityException",
        "summary": "SecurityException detected",
        "pattern": re.compile(r"SecurityException", re.IGNORECASE),
    },
    {
        "key": "hasBadToken",
        "label": "WindowManager$BadTokenException",
        "summary": "WindowManager$BadTokenException detected",
        "pattern": re.compile(r"WindowManager\$BadTokenException", re.IGNORECASE),
    },
    {
        "key": "hasFragmentDetached",
        "label": "Fragment not attached",
        "summary": "Fragment not attached detected",
        "pattern": re.compile(r"Fragment (?:\S+ )?not attached|not attached to (?:an )?Activity", re.IGNORECASE),
    },
    {
        "key": "hasOnSaveInstanceStateIssue",
        "label": "Can not perform this action after onSaveInstanceState",
        "summary": "Can not perform this action after onSaveInstanceState detected",
        "pattern": re.compile(r"Can not perform this action after onSaveInstanceState", re.IGNORECASE),
    },
]


@dataclass
class SessionPaths:
    root: Path
    commands_dir: Path
    artifacts_dir: Path
    session_log: Path


class SessionRecorder:
    def __init__(self, base_dir: Path) -> None:
        session_name = time.strftime("%Y%m%d-%H%M%S")
        self.paths = SessionPaths(
            root=base_dir / session_name,
            commands_dir=base_dir / session_name / "commands",
            artifacts_dir=base_dir / session_name / "artifacts",
            session_log=base_dir / session_name / "session.jsonl",
        )
        self.paths.commands_dir.mkdir(parents=True, exist_ok=True)
        self.paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._command_counter = 0
        self._lock = threading.Lock()

    def record_event(self, event_type: str, payload: dict[str, Any]) -> None:
        event = {"eventType": event_type, "timestamp": time.time(), "payload": payload}
        with self._lock:
            with self.paths.session_log.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def record_command(
        self,
        command_name: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any] | None = None,
        error_message: str | None = None,
        artifact_path: Path | None = None,
    ) -> Path:
        with self._lock:
            self._command_counter += 1
            filename = f"{self._command_counter:03d}-{sanitize_filename(command_name)}.json"
            output_path = self.paths.commands_dir / filename

        payload = {
            "commandName": command_name,
            "request": request_payload,
            "response": response_payload,
            "error": error_message,
            "artifactPath": str(artifact_path) if artifact_path else None,
            "recordedAt": time.time(),
        }
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        return output_path


class NavigationMemory:
    def __init__(self, path: Path, recorder: SessionRecorder) -> None:
        self.path = path
        self.recorder = recorder
        self._lock = threading.Lock()

    def read(self) -> dict[str, Any]:
        with self._lock:
            return self._read_unlocked()

    def save_note(
        self,
        *,
        app_package: str,
        screen_name: str,
        description: str = "",
        how_to_reach: str = "",
        visual_cues: str = "",
        useful_actions: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        package_name = app_package.strip()
        display_screen_name = screen_name.strip()
        if not package_name:
            raise ValueError("app_package eh obrigatorio para salvar memoria de navegacao")
        if not display_screen_name:
            raise ValueError("screen_name eh obrigatorio para salvar memoria de navegacao")

        now = int(time.time() * 1000)
        with self._lock:
            guide = self._read_unlocked()
            apps = guide.setdefault("apps", {})
            app = apps.setdefault(
                package_name,
                {"packageName": package_name, "screens": {}, "createdAt": now, "updatedAt": now},
            )
            app.setdefault("screens", {})
            screen_id = sanitize_filename(display_screen_name)
            screen = app["screens"].setdefault(
                screen_id,
                {
                    "screenName": display_screen_name,
                    "description": "",
                    "howToReach": "",
                    "visualCues": "",
                    "usefulActions": [],
                    "notes": [],
                    "createdAt": now,
                    "updatedAt": now,
                },
            )
            self._replace_if_present(screen, "screenName", display_screen_name)
            self._replace_if_present(screen, "description", description)
            self._replace_if_present(screen, "howToReach", how_to_reach)
            self._replace_if_present(screen, "visualCues", visual_cues)
            screen["usefulActions"] = merge_unique_strings(screen.get("usefulActions"), useful_actions or [])
            note_text = notes.strip()
            if note_text:
                screen.setdefault("notes", []).append({"timestamp": now, "text": note_text})
            screen["updatedAt"] = now
            app["updatedAt"] = now
            guide["updatedAt"] = now
            self._write_unlocked(guide)

        command_path = self.recorder.record_command(
            command_name="save_navigation_note",
            request_payload={
                "appPackage": package_name,
                "screenName": display_screen_name,
                "description": description,
                "howToReach": how_to_reach,
                "visualCues": visual_cues,
                "usefulActions": useful_actions or [],
                "notes": notes,
            },
            response_payload={"success": True, "navigationMemoryPath": str(self.path.resolve()), "screenId": screen_id},
        )
        return {
            "success": True,
            "message": "Navigation note saved",
            "navigationMemoryPath": str(self.path.resolve()),
            "commandLogPath": str(command_path.resolve()),
            "appPackage": package_name,
            "screenId": screen_id,
            "screen": screen,
        }

    def record_automatic_event(self, *, action: str, result: dict[str, Any]) -> None:
        if action not in NAVIGATION_EVENT_ACTIONS:
            return
        now = int(time.time() * 1000)
        request = result.get("request")
        if not isinstance(request, dict):
            request = {"action": action}
        event = {
            "timestamp": now,
            "action": action,
            "success": result.get("success"),
            "request": request,
            "artifactPath": result.get("artifactPath"),
            "commandLogPath": result.get("commandLogPath"),
        }
        with self._lock:
            guide = self._read_unlocked()
            events = guide.setdefault("recentEvents", [])
            events.append(event)
            guide["recentEvents"] = events[-200:]
            guide["updatedAt"] = now
            self._write_unlocked(guide)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "updatedAt": None, "apps": {}, "recentEvents": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Memoria de navegacao invalida: {self.path}") from exc
        if not isinstance(payload, dict):
            raise RuntimeError(f"Memoria de navegacao deve ser um objeto JSON: {self.path}")
        payload.setdefault("version", 1)
        payload.setdefault("updatedAt", None)
        payload.setdefault("apps", {})
        payload.setdefault("recentEvents", [])
        return payload

    def _write_unlocked(self, payload: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _replace_if_present(target: dict[str, Any], key: str, value: str) -> None:
        normalized = value.strip()
        if normalized:
            target[key] = normalized


def merge_unique_strings(existing: Any, incoming: list[str]) -> list[str]:
    values: list[str] = []
    for item in existing or []:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    for item in incoming:
        if isinstance(item, str) and item.strip() and item.strip() not in values:
            values.append(item.strip())
    return values


def sanitize_filename(value: str) -> str:
    filtered = [char if char.isalnum() or char in {"-", "_"} else "-" for char in value.lower()]
    collapsed = "".join(filtered).strip("-")
    return collapsed or "command"


def safe_artifact_filename(filename: str, fallback_stem: str, suffix: str = ".png") -> str:
    candidate = Path(filename).name
    candidate_suffix = Path(candidate).suffix or suffix
    stem = sanitize_filename(Path(candidate).stem or fallback_stem)
    return f"{stem}{candidate_suffix}"


def tail_preview(content: str, max_lines: int = 80, max_chars: int = 8000) -> str:
    lines = content.splitlines()
    preview = "\n".join(lines[-max_lines:])
    return preview[-max_chars:] if len(preview) > max_chars else preview


def find_issue_context(logcat_content: str, pattern: re.Pattern[str], context_lines: int = 2) -> list[str]:
    lines = logcat_content.splitlines()
    snippets: list[str] = []
    seen: set[str] = set()
    for index, line in enumerate(lines):
        if not pattern.search(line):
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        snippet = "\n".join(lines[start:end]).strip()
        if snippet and snippet not in seen:
            snippets.append(snippet)
            seen.add(snippet)
        if len(snippets) >= 3:
            break
    return snippets


def detect_logcat_issues(logcat_content: str) -> dict[str, Any]:
    result: dict[str, Any] = {
        "hasCrash": False,
        "hasWindowLeak": False,
        "hasANR": False,
        "hasIllegalState": False,
        "hasNPE": False,
        "hasSecurityException": False,
        "hasBadToken": False,
        "hasFragmentDetached": False,
        "hasOnSaveInstanceStateIssue": False,
        "summary": [],
        "issueDetails": [],
    }
    summary: list[str] = []
    issue_details: list[dict[str, Any]] = []
    for issue in LOGCAT_ISSUE_PATTERNS:
        pattern = issue["pattern"]
        matched = bool(pattern.search(logcat_content))
        result[str(issue["key"])] = matched
        if matched:
            summary.append(str(issue["summary"]))
            issue_details.append(
                {
                    "key": issue["key"],
                    "label": issue["label"],
                    "summary": issue["summary"],
                    "matches": find_issue_context(logcat_content, pattern),
                }
            )
    result["summary"] = summary
    result["issueDetails"] = issue_details
    return result


def parse_wm_size(output: str) -> tuple[int, int] | None:
    matches = re.findall(r"(\d+)x(\d+)", output)
    if not matches:
        return None
    width, height = matches[-1]
    return int(width), int(height)


def adb_escape_text(text: str) -> str:
    return text.replace("%", "%s").replace(" ", "%s")


@dataclass
class ServerRuntime:
    timeout_seconds: float
    recorder: SessionRecorder
    navigation_memory: NavigationMemory
    config_path: Path
    adb_path: str | None
    adb_device_serial: str | None

    def adb_config(self) -> dict[str, Any]:
        detected = self.detect_adb_path()
        configured_exists = bool(self.adb_path and Path(self.adb_path).expanduser().exists())
        return {
            "success": True,
            "transport": "adb",
            "configPath": str(self.config_path.resolve()),
            "timeoutSeconds": self.timeout_seconds,
            "adbPath": self.adb_path,
            "effectiveAdbPath": self.adb_path or detected,
            "adbPathConfigured": bool(self.adb_path),
            "adbPathExists": configured_exists,
            "adbDetectedPath": detected,
            "adbAvailable": bool(self.adb_path or detected),
            "adbDeviceSerial": self.adb_device_serial,
            "artifactsRoot": str(self.recorder.paths.root.resolve()),
            "sessionLogPath": str(self.recorder.paths.session_log.resolve()),
            "navigationMemoryPath": str(self.navigation_memory.path.resolve()),
        }

    def adb_autodetect(self) -> dict[str, Any]:
        detected = self.detect_adb_path()
        return {
            "success": bool(detected),
            "message": "ADB detected" if detected else "ADB not found",
            "adbDetectedPath": detected,
            "candidates": self.adb_candidates(),
            "hint": (
                "Use android_set_adb_config to persist adbPath."
                if detected
                else "Install Android platform-tools or pass adbPath with android_set_adb_config."
            ),
        }

    def set_adb_config(self, adb_path: str = "", adb_device_serial: str = "", persist: bool = True) -> dict[str, Any]:
        self.adb_path = adb_path.strip() or None
        self.adb_device_serial = adb_device_serial.strip() or None
        if persist:
            self.persist_config()
        return {"success": True, "message": "ADB configuration updated", "persisted": persist, **self.adb_config()}

    def persist_config(self) -> None:
        payload = load_json_config(self.config_path)
        payload["timeoutSeconds"] = self.timeout_seconds
        payload["adbPath"] = self.adb_path or ""
        payload["adbDeviceSerial"] = self.adb_device_serial or ""
        payload.setdefault("artifactsDir", relative_or_absolute_path(DEFAULT_ARTIFACTS_DIR))
        payload.setdefault("navigationMemoryPath", relative_or_absolute_path(DEFAULT_NAVIGATION_MEMORY_PATH))
        legacy_key = "end" + "point"
        payload.pop(legacy_key, None)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def navigation_guide(self) -> dict[str, Any]:
        return {"success": True, "navigationMemoryPath": str(self.navigation_memory.path.resolve()), "guide": self.navigation_memory.read()}

    def save_navigation_note(
        self,
        *,
        app_package: str,
        screen_name: str,
        description: str = "",
        how_to_reach: str = "",
        visual_cues: str = "",
        useful_actions: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        return self.navigation_memory.save_note(
            app_package=app_package,
            screen_name=screen_name,
            description=description,
            how_to_reach=how_to_reach,
            visual_cues=visual_cues,
            useful_actions=useful_actions,
            notes=notes,
        )

    def adb_status(self) -> dict[str, Any]:
        payload = self.run_adb_command(command_name="adb_status", adb_args=["devices", "-l"])
        payload["adbConfig"] = self.adb_config()
        return payload

    def get_screen(self, include_base64: bool = False, filename: str | None = None) -> dict[str, Any]:
        artifact_name = safe_artifact_filename(filename, "screen", ".png") if filename else "screen.png"
        artifact_path = self.recorder.paths.artifacts_dir / artifact_name
        result = self.run_adb_command(
            command_name="get_screen",
            adb_args=["exec-out", "screencap", "-p"],
            timeout_seconds=max(self.timeout_seconds, DEFAULT_SCREENSHOT_TIMEOUT_SECONDS),
            decode_stdout=False,
            artifact_path=artifact_path,
        )
        if result["success"]:
            result["message"] = "Screenshot captured successfully"
            result["artifactPath"] = str(artifact_path.resolve())
            result["path"] = str(artifact_path.resolve())
        else:
            result["message"] = "Failed to capture screenshot"
        if not include_base64:
            result.pop("stdoutBase64", None)
        self.navigation_memory.record_automatic_event(action="get_screen", result=result)
        return result

    def list_apps(self, query: str = "") -> dict[str, Any]:
        result = self.run_adb_command(
            command_name="list_apps",
            adb_args=["shell", "pm", "list", "packages"],
            request_payload={"query": query},
            timeout_seconds=max(self.timeout_seconds, 30.0),
        )
        packages = []
        query_normalized = query.strip().lower()
        for line in str(result.get("stdout") or "").splitlines():
            package_name = line.removeprefix("package:").strip()
            if package_name and (not query_normalized or query_normalized in package_name.lower()):
                packages.append(package_name)
        result["packages"] = packages
        result["count"] = len(packages)
        return result

    def app_info(self, package_name: str) -> dict[str, Any]:
        package_name = package_name.strip()
        if not package_name:
            raise ValueError("package_name eh obrigatorio")
        result = self.run_adb_command(
            command_name="app_info",
            adb_args=["shell", "dumpsys", "package", package_name],
            request_payload={"packageName": package_name},
            timeout_seconds=max(self.timeout_seconds, 30.0),
            record_stdout_max_chars=20000,
        )
        stdout = str(result.get("stdout") or "")
        result["packageName"] = package_name
        result["installed"] = package_name in stdout
        result["launcherActivities"] = sorted(set(re.findall(rf"{re.escape(package_name)}/[A-Za-z0-9_.$]+", stdout)))
        result["content_preview"] = tail_preview(stdout, max_lines=120, max_chars=12000)
        result["contentPreview"] = result["content_preview"]
        return result

    def open_app(self, package_name: str, activity_name: str | None = None) -> dict[str, Any]:
        package_name = package_name.strip()
        if not package_name:
            raise ValueError("package_name eh obrigatorio")
        if activity_name and "/" in activity_name:
            component = activity_name
        elif activity_name:
            component = f"{package_name}/{activity_name}"
        else:
            component = ""
        if component:
            adb_args = ["shell", "am", "start", "-n", component]
            payload = {"packageName": package_name, "activityName": activity_name, "component": component}
        else:
            adb_args = ["shell", "monkey", "-p", package_name, "-c", "android.intent.category.LAUNCHER", "1"]
            payload = {"packageName": package_name}
        result = self.run_adb_command(command_name="adb_open_app", adb_args=adb_args, request_payload=payload)
        result["packageName"] = package_name
        if activity_name:
            result["activityName"] = activity_name
        self.navigation_memory.record_automatic_event(action="adb_open_app", result=result)
        return result

    def close_app(self, package_name: str) -> dict[str, Any]:
        package_name = package_name.strip()
        if not package_name:
            raise ValueError("package_name eh obrigatorio")
        result = self.run_adb_command(command_name="close_app", adb_args=["shell", "am", "force-stop", package_name], request_payload={"packageName": package_name})
        self.navigation_memory.record_automatic_event(action="close_app", result=result)
        return result

    def tap(self, x: int, y: int) -> dict[str, Any]:
        result = self.run_adb_command(command_name="tap", adb_args=["shell", "input", "tap", str(x), str(y)], request_payload={"x": x, "y": y})
        self.navigation_memory.record_automatic_event(action="tap", result=result)
        return result

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 350) -> dict[str, Any]:
        result = self.run_adb_command(
            command_name="swipe",
            adb_args=["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)],
            request_payload={"x1": x1, "y1": y1, "x2": x2, "y2": y2, "durationMs": duration_ms},
        )
        self.navigation_memory.record_automatic_event(action="swipe", result=result)
        return result

    def long_click(self, x: int, y: int, duration_ms: int = 800) -> dict[str, Any]:
        result = self.swipe(x, y, x, y, duration_ms)
        result["request"]["action"] = "long_click"
        self.navigation_memory.record_automatic_event(action="long_click", result=result)
        return result

    def input_text(self, text: str) -> dict[str, Any]:
        escaped = adb_escape_text(text)
        result = self.run_adb_command(command_name="input_text", adb_args=["shell", "input", "text", escaped], request_payload={"text": text, "adbEscapedText": escaped})
        self.navigation_memory.record_automatic_event(action="input_text", result=result)
        return result

    def keyevent(self, command_name: str, key: str) -> dict[str, Any]:
        result = self.run_adb_command(command_name=command_name, adb_args=["shell", "input", "keyevent", key])
        self.navigation_memory.record_automatic_event(action=command_name, result=result)
        return result

    def scroll(self, direction: Literal["up", "down", "left", "right"] = "down") -> dict[str, Any]:
        width, height = self.screen_size()
        center_x = width // 2
        center_y = height // 2
        delta_y = max(height // 3, 1)
        delta_x = max(width // 3, 1)
        if direction == "up":
            coords = (center_x, center_y - delta_y, center_x, center_y + delta_y)
        elif direction == "down":
            coords = (center_x, center_y + delta_y, center_x, center_y - delta_y)
        elif direction == "left":
            coords = (center_x - delta_x, center_y, center_x + delta_x, center_y)
        else:
            coords = (center_x + delta_x, center_y, center_x - delta_x, center_y)
        result = self.swipe(*coords, duration_ms=400)
        result["scrollDirection"] = direction
        self.navigation_memory.record_automatic_event(action="scroll", result=result)
        return result

    def screen_size(self) -> tuple[int, int]:
        result = self.run_adb_command(command_name="screen_size", adb_args=["shell", "wm", "size"], record_command=False)
        parsed = parse_wm_size(str(result.get("stdout") or ""))
        if not parsed:
            raise RuntimeError(f"Nao foi possivel detectar tamanho da tela: {result.get('stdout')!r}")
        return parsed

    def clear_logcat(self) -> dict[str, Any]:
        result = self.run_adb_command(command_name="clear_logcat", adb_args=["logcat", "-c"])
        result["message"] = "Logcat cleared successfully" if result["success"] else "Failed to clear logcat"
        return result

    def get_logcat(self) -> dict[str, Any]:
        result = self.run_adb_command(command_name="get_logcat", adb_args=["logcat", "-d"], timeout_seconds=max(self.timeout_seconds, 30.0), record_stdout_max_chars=12000)
        logcat_content = str(result.get("stdout") or "")
        logcat_path = self.save_logcat_artifact(logcat_content)
        result.update(
            {
                "path": str(logcat_path.resolve()),
                "logcatPath": str(logcat_path.resolve()),
                "content_preview": tail_preview(logcat_content),
                "contentPreview": tail_preview(logcat_content),
                "message": "Logcat captured successfully" if result["success"] else "Failed to capture logcat",
            }
        )
        return result

    def detect_known_issues(self) -> dict[str, Any]:
        result = self.run_adb_command(command_name="detect_known_issues", adb_args=["logcat", "-d"], timeout_seconds=max(self.timeout_seconds, 30.0), record_stdout_max_chars=12000)
        logcat_content = str(result.get("stdout") or "")
        logcat_path = self.save_logcat_artifact(logcat_content)
        detection = detect_logcat_issues(logcat_content)
        detection.update(
            {
                "success": result["success"],
                "message": "Known issue detection completed" if result["success"] else "Failed to capture logcat for known issue detection",
                "path": str(logcat_path.resolve()),
                "logcatPath": str(logcat_path.resolve()),
                "content_preview": tail_preview(logcat_content),
                "contentPreview": tail_preview(logcat_content),
                "commandLogPath": result.get("commandLogPath"),
                "returnCode": result.get("returnCode"),
                "stderr": result.get("stderr"),
                "adbPath": result.get("adbPath"),
                "deviceSerial": result.get("deviceSerial"),
            }
        )
        return detection

    def save_logcat_artifact(self, content: str) -> Path:
        logcat_path = self.recorder.paths.artifacts_dir / "logcat.txt"
        logcat_path.write_text(content, encoding="utf-8", errors="replace")
        return logcat_path

    def detect_adb_path(self) -> str | None:
        if self.adb_path:
            candidate = Path(self.adb_path).expanduser()
            if candidate.exists():
                return str(candidate)
        detected = shutil.which("adb")
        if detected:
            return detected
        for candidate in self.adb_candidates():
            path = Path(candidate).expanduser()
            if path.exists():
                return str(path)
        return None

    def adb_candidates(self) -> list[str]:
        home = Path.home()
        candidates = [
            str(home / "Android" / "Sdk" / "platform-tools" / "adb"),
            str(home / "Library" / "Android" / "sdk" / "platform-tools" / "adb"),
            "/opt/android/platform-tools/adb",
            "/usr/local/bin/adb",
            "/usr/bin/adb",
        ]
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(str(Path(local_app_data) / "Android" / "Sdk" / "platform-tools" / "adb.exe"))
        return candidates

    def require_adb_path(self) -> str:
        detected = self.detect_adb_path()
        if detected:
            return detected
        raise RuntimeError(
            f"adb nao encontrado. Defina adbPath em {self.config_path}, use android_set_adb_config, "
            "ou instale Android platform-tools no PATH."
        )

    def run_adb_command(
        self,
        *,
        command_name: str,
        adb_args: list[str],
        request_payload: dict[str, Any] | None = None,
        timeout_seconds: float | None = None,
        record_stdout_max_chars: int | None = None,
        decode_stdout: bool = True,
        artifact_path: Path | None = None,
        record_command: bool = True,
    ) -> dict[str, Any]:
        adb_path = self.require_adb_path()
        command = [adb_path]
        if self.adb_device_serial:
            command.extend(["-s", self.adb_device_serial])
        command.extend(adb_args)
        request = {
            "transport": "adb",
            "adbPath": adb_path,
            "deviceSerial": self.adb_device_serial,
            "argv": command,
            "shellCommand": " ".join(shlex.quote(part) for part in command),
            **(request_payload or {}),
        }
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=decode_stdout,
                encoding="utf-8" if decode_stdout else None,
                errors="replace" if decode_stdout else None,
                timeout=timeout_seconds or self.timeout_seconds,
                check=False,
            )
            stdout: str
            stdout_base64: str | None = None
            if decode_stdout:
                stdout = str(completed.stdout).strip()
            else:
                stdout_bytes = bytes(completed.stdout)
                artifact_path = artifact_path or self.recorder.paths.artifacts_dir / f"{sanitize_filename(command_name)}.bin"
                if completed.returncode == 0:
                    artifact_path.write_bytes(stdout_bytes)
                stdout = f"<{len(stdout_bytes)} bytes>"
                if len(stdout_bytes) <= 2_000_000:
                    import base64

                    stdout_base64 = base64.b64encode(stdout_bytes).decode("ascii")
            stderr_value = completed.stderr.strip() if isinstance(completed.stderr, str) else completed.stderr.decode("utf-8", errors="replace").strip()
            response: dict[str, Any] = {
                "transport": "adb",
                "success": completed.returncode == 0,
                "returnCode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr_value,
                "adbPath": adb_path,
                "deviceSerial": self.adb_device_serial,
                "argv": command,
                "timestamp": int(time.time() * 1000),
            }
            if stdout_base64:
                response["stdoutBase64"] = stdout_base64
            if artifact_path and completed.returncode == 0:
                response["artifactPath"] = str(artifact_path.resolve())
            response_for_log = dict(response)
            if record_stdout_max_chars is not None and len(response_for_log["stdout"]) > record_stdout_max_chars:
                response_for_log["stdout"] = response_for_log["stdout"][-record_stdout_max_chars:]
                response_for_log["stdoutTruncated"] = True
                response_for_log["stdoutOriginalLength"] = len(response["stdout"])
            if not decode_stdout:
                response_for_log.pop("stdoutBase64", None)
            command_path: Path | None = None
            if record_command:
                command_path = self.recorder.record_command(
                    command_name=command_name,
                    request_payload=request,
                    response_payload=response_for_log,
                    artifact_path=artifact_path if completed.returncode == 0 else None,
                )
            return {
                "success": response["success"],
                "message": "ADB command executed" if response["success"] else "ADB command failed",
                "commandLogPath": str(command_path.resolve()) if command_path else None,
                "request": {"action": command_name, **request_payload} if request_payload else {"action": command_name},
                **response,
            }
        except Exception as exc:
            command_path = self.recorder.record_command(command_name=command_name, request_payload=request, error_message=str(exc))
            raise RuntimeError(f"{exc} (detalhes: {command_path})") from exc


def build_server(runtime: ServerRuntime) -> FastMCP:
    mcp = FastMCP(
        name="DroidPilot MCP",
        instructions=(
            "Use estas tools para operar Android via ADB local. "
            "Consulte android_adb_config para confirmar o adb e android_get_screen para validar a tela atual."
        ),
        json_response=True,
        log_level="WARNING",
    )
    read_only = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
    stateful = ToolAnnotations(readOnlyHint=False, idempotentHint=False, openWorldHint=True)

    @mcp.tool(name="android_agent_status", description="Retorna a configuracao local do DroidPilot MCP e paths da sessao atual.", annotations=read_only)
    def android_agent_status() -> dict[str, Any]:
        return runtime.adb_config()

    @mcp.tool(name="android_adb_config", description="Retorna a configuracao ADB efetiva, autodeteccao e paths de artifacts.", annotations=read_only)
    def android_adb_config() -> dict[str, Any]:
        return runtime.adb_config()

    @mcp.tool(name="android_adb_autodetect", description="Tenta localizar o executavel adb no PATH e em locais comuns do Android SDK.", annotations=read_only)
    def android_adb_autodetect() -> dict[str, Any]:
        return runtime.adb_autodetect()

    @mcp.tool(name="android_set_adb_config", description="Atualiza adbPath e adbDeviceSerial em runtime e, por padrao, persiste no config local.", annotations=stateful)
    def android_set_adb_config(adb_path: str = "", adb_device_serial: str = "", persist: bool = True) -> dict[str, Any]:
        return runtime.set_adb_config(adb_path=adb_path, adb_device_serial=adb_device_serial, persist=persist)

    @mcp.tool(name="android_navigation_guide", description="Retorna a memoria consolidada de navegacao salva por testes anteriores.", annotations=read_only)
    def android_navigation_guide() -> dict[str, Any]:
        return runtime.navigation_guide()

    @mcp.tool(name="android_save_navigation_note", description="Salva uma orientacao de menu/tela para facilitar testes futuros do app.", annotations=stateful)
    def android_save_navigation_note(
        app_package: str,
        screen_name: str,
        description: str = "",
        how_to_reach: str = "",
        visual_cues: str = "",
        useful_actions: list[str] | None = None,
        notes: str = "",
    ) -> dict[str, Any]:
        return runtime.save_navigation_note(
            app_package=app_package,
            screen_name=screen_name,
            description=description,
            how_to_reach=how_to_reach,
            visual_cues=visual_cues,
            useful_actions=useful_actions,
            notes=notes,
        )

    @mcp.tool(name="android_get_screen", description="Captura a tela atual via ADB screencap e salva a imagem em tests/mcp.", annotations=read_only)
    def android_get_screen(include_base64: bool = False, filename: str | None = None) -> dict[str, Any]:
        return runtime.get_screen(include_base64=include_base64, filename=filename)

    @mcp.tool(name="android_list_apps", description="Lista packages instalados via ADB, com filtro opcional por package.", annotations=read_only)
    def android_list_apps(query: str = "") -> dict[str, Any]:
        return runtime.list_apps(query=query)

    @mcp.tool(name="android_app_info", description="Retorna diagnostico de instalacao e possiveis launchers para um package name.", annotations=read_only)
    def android_app_info(package_name: str) -> dict[str, Any]:
        return runtime.app_info(package_name=package_name)

    @mcp.tool(name="android_open_app", description="Abre um app via ADB usando package name e, opcionalmente, uma activity explicita.", annotations=stateful)
    def android_open_app(package_name: str, activity_name: str = "") -> dict[str, Any]:
        return runtime.open_app(package_name=package_name, activity_name=activity_name.strip() or None)

    @mcp.tool(name="android_adb_open_app", description="Alias explicito de ADB para abrir um app por package/activity.", annotations=stateful)
    def android_adb_open_app(package_name: str, activity_name: str = "") -> dict[str, Any]:
        return runtime.open_app(package_name=package_name, activity_name=activity_name.strip() or None)

    @mcp.tool(name="android_close_app", description="Forca parada de um app via ADB am force-stop.", annotations=stateful)
    def android_close_app(package_name: str) -> dict[str, Any]:
        return runtime.close_app(package_name=package_name)

    @mcp.tool(name="android_tap", description="Executa um tap absoluto via ADB input tap.", annotations=stateful)
    def android_tap(x: int, y: int) -> dict[str, Any]:
        return runtime.tap(x=x, y=y)

    @mcp.tool(name="android_swipe", description="Executa um swipe absoluto via ADB input swipe.", annotations=stateful)
    def android_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 350) -> dict[str, Any]:
        return runtime.swipe(x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)

    @mcp.tool(name="android_long_click", description="Executa long click via ADB input swipe no mesmo ponto.", annotations=stateful)
    def android_long_click(x: int, y: int, duration_ms: int = 800) -> dict[str, Any]:
        return runtime.long_click(x=x, y=y, duration_ms=duration_ms)

    @mcp.tool(name="android_input_text", description="Envia texto para o campo focado via ADB input text.", annotations=stateful)
    def android_input_text(text: str) -> dict[str, Any]:
        return runtime.input_text(text=text)

    @mcp.tool(name="android_back", description="Executa o botao Back via ADB keyevent.", annotations=stateful)
    def android_back() -> dict[str, Any]:
        return runtime.keyevent("back", "KEYCODE_BACK")

    @mcp.tool(name="android_home", description="Executa o botao Home via ADB keyevent.", annotations=stateful)
    def android_home() -> dict[str, Any]:
        return runtime.keyevent("home", "KEYCODE_HOME")

    @mcp.tool(name="android_scroll", description="Executa scroll via ADB calculando coordenadas a partir do tamanho da tela.", annotations=stateful)
    def android_scroll(direction: Literal["up", "down", "left", "right"] = "down") -> dict[str, Any]:
        return runtime.scroll(direction=direction)

    @mcp.tool(name="android_adb_status", description="Retorna o status local do ADB e a lista de devices visiveis para o servidor MCP.", annotations=read_only)
    def android_adb_status() -> dict[str, Any]:
        return runtime.adb_status()

    @mcp.tool(name="android_clear_logcat", description="Limpa o logcat via ADB antes de iniciar um teste de UI.", annotations=stateful)
    def android_clear_logcat() -> dict[str, Any]:
        return runtime.clear_logcat()

    @mcp.tool(name="android_get_logcat", description="Captura o dump atual do logcat via ADB e salva em tests/mcp/<timestamp>/artifacts/logcat.txt.", annotations=read_only)
    def android_get_logcat() -> dict[str, Any]:
        return runtime.get_logcat()

    @mcp.tool(name="android_detect_known_issues", description="Captura logcat via ADB e detecta crashes, ANR, WindowLeaked e excecoes Android conhecidas.", annotations=read_only)
    def android_detect_known_issues() -> dict[str, Any]:
        return runtime.detect_known_issues()

    return mcp


def load_json_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Arquivo de configuracao JSON invalido: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Arquivo de configuracao deve conter um objeto JSON: {path}")
    return payload


def first_text(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def first_float(*values: Any, default: float) -> float:
    for value in values:
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Valor numerico invalido: {value!r}") from exc
    return default


def relative_or_absolute_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_DIR))
    except ValueError:
        return str(path)


def resolve_project_path(value: str | None, default: Path) -> Path:
    if not value:
        return default
    candidate = Path(value).expanduser()
    if candidate.is_absolute():
        return candidate
    return PROJECT_DIR / candidate


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Servidor MCP DroidPilot ADB-only.")
    parser.add_argument(
        "--config",
        default=os.environ.get("ANDROID_AGENT_CONFIG", "").strip() or str(DEFAULT_CONFIG_PATH),
        help=f"Arquivo JSON de configuracao local. Padrao: {DEFAULT_CONFIG_PATH}",
    )
    parser.add_argument("--timeout", type=float, default=None, help=f"Timeout por comando em segundos. Padrao: {DEFAULT_TIMEOUT_SECONDS}")
    parser.add_argument("--artifacts-dir", default=None, help="Diretorio base para logs e artifacts das chamadas MCP. Sobrescreve artifactsDir do config.")
    parser.add_argument("--navigation-memory-path", default=None, help="Arquivo JSON para memoria consolidada de navegacao. Sobrescreve navigationMemoryPath do config.")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio", help="Transporte MCP exposto pelo servidor. Padrao: stdio")
    parser.add_argument("--host", default="127.0.0.1", help="Host para streamable-http")
    parser.add_argument("--port", type=int, default=8000, help="Porta para streamable-http")
    parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="WARNING", help="Nivel de log do processo local.")
    parser.add_argument("--adb-path", default=None, help="Caminho para o executavel adb. Sobrescreve adbPath do config.")
    parser.add_argument("--adb-device-serial", default=None, help="Serial do device ADB para usar quando houver mais de um dispositivo conectado.")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = Path.cwd() / config_path
    try:
        config = load_json_config(config_path)
        args.timeout = first_float(args.timeout, config.get("timeoutSeconds"), os.environ.get("ANDROID_AGENT_TIMEOUT"), default=DEFAULT_TIMEOUT_SECONDS)
        args.artifacts_dir = str(
            resolve_project_path(
                first_text(args.artifacts_dir, config.get("artifactsDir"), os.environ.get("ANDROID_AGENT_ARTIFACTS_DIR")),
                DEFAULT_ARTIFACTS_DIR,
            )
        )
        args.navigation_memory_path = str(
            resolve_project_path(
                first_text(args.navigation_memory_path, config.get("navigationMemoryPath"), os.environ.get("ANDROID_AGENT_NAVIGATION_MEMORY_PATH")),
                DEFAULT_NAVIGATION_MEMORY_PATH,
            )
        )
        args.adb_path = first_text(args.adb_path, config.get("adbPath"), os.environ.get("ANDROID_AGENT_ADB_PATH"))
        args.adb_device_serial = first_text(args.adb_device_serial, config.get("adbDeviceSerial"), os.environ.get("ANDROID_AGENT_ADB_DEVICE_SERIAL"))
        args.config_path = config_path
    except ValueError as exc:
        parser.error(str(exc))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), stream=sys.stderr)
    recorder = SessionRecorder(Path(args.artifacts_dir))
    navigation_memory = NavigationMemory(Path(args.navigation_memory_path), recorder=recorder)
    runtime = ServerRuntime(
        timeout_seconds=args.timeout,
        recorder=recorder,
        navigation_memory=navigation_memory,
        config_path=Path(args.config_path),
        adb_path=args.adb_path,
        adb_device_serial=args.adb_device_serial,
    )
    server = build_server(runtime)
    LOGGER.info("Sessao MCP iniciada em %s", recorder.paths.root)
    if not runtime.detect_adb_path():
        LOGGER.warning("adb nao encontrado no startup. Use android_adb_autodetect ou android_set_adb_config.")
    if args.transport == "streamable-http":
        server.settings.host = args.host
        server.settings.port = args.port
    server.run(transport=args.transport)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
