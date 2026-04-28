#!/usr/bin/env python3
"""DroidPilot MCP server backed only by local ADB."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal


PROJECT_DIR = Path.cwd().resolve()
SERVER_DIR = Path(__file__).resolve().parent
DEFAULT_ARTIFACTS_DIR = PROJECT_DIR / "tests" / "mcp"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "android-agent.config.json"
DEFAULT_NAVIGATION_MEMORY_PATH = DEFAULT_ARTIFACTS_DIR / "navigation" / "navigation-guide.json"
DEFAULT_TIMEOUT_SECONDS = 12.0
DEFAULT_SCREENSHOT_TIMEOUT_SECONDS = 30.0
DEFAULT_UPDATE_REPO_URL = "https://github.com/ronaldomafra/DroidPilot-MCP.git"
DEFAULT_UPDATE_CHANNEL = "main"
DEFAULT_SQLITE_MAX_ROWS = 200
DEFAULT_TOOL_MAX_ITEMS = 20
UPDATE_STATE_FILENAME = ".droidpilot-update.json"

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
    "ui_context",
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

    def save_learning(
        self,
        *,
        screen_name: str = "",
        goal: str = "",
        route: list[str] | None = None,
        visual_cues: list[str] | None = None,
        useful_actions: list[str] | None = None,
        assertions: list[str] | None = None,
        blockers: list[str] | None = None,
        notes: str = "",
        confidence: float = 0.7,
        app_package: str = "",
        source_type: str = "",
        navigation_mode: str = "",
        screen_fingerprint: str = "",
        key_texts: list[str] | None = None,
        key_resource_ids: list[str] | None = None,
        key_content_descs: list[str] | None = None,
        current_activity: str = "",
        focused_window: str = "",
    ) -> dict[str, Any]:
        display_screen_name = screen_name.strip()
        display_goal = goal.strip()
        if not display_screen_name and not display_goal:
            raise ValueError("screen_name ou goal eh obrigatorio para salvar aprendizado de navegacao")

        now = int(time.time() * 1000)
        safe_confidence = max(0.0, min(1.0, float(confidence)))
        screen_id = sanitize_filename(display_screen_name or display_goal)
        route_steps = normalize_string_list(route)
        cues = normalize_string_list(visual_cues)
        actions = normalize_string_list(useful_actions)
        checks = normalize_string_list(assertions)
        blocker_items = normalize_string_list(blockers)
        source_type_value = normalize_ui_text(source_type)
        navigation_mode_value = normalize_ui_text(navigation_mode)
        fingerprint_value = normalize_ui_text(screen_fingerprint)
        key_text_items = normalize_string_list(key_texts)
        key_resource_id_items = normalize_string_list(key_resource_ids)
        key_content_desc_items = normalize_string_list(key_content_descs)
        current_activity_value = normalize_activity_name(current_activity)
        focused_window_value = normalize_ui_text(focused_window)
        note_text = notes.strip()
        package_name = app_package.strip()

        with self._lock:
            guide = self._read_unlocked()
            project_navigation = self._project_navigation(guide)
            screens = project_navigation.setdefault("screens", {})
            screen = screens.setdefault(
                screen_id,
                {
                    "screenName": display_screen_name or display_goal,
                    "goals": [],
                    "visualCues": [],
                    "usefulActions": [],
                    "assertions": [],
                    "blockers": [],
                    "notes": [],
                    "packages": [],
                    "sourceTypes": [],
                    "navigationModes": [],
                    "screenFingerprints": [],
                    "keyTexts": [],
                    "keyResourceIds": [],
                    "keyContentDescs": [],
                    "activities": [],
                    "focusedWindows": [],
                    "createdAt": now,
                    "updatedAt": now,
                    "confidence": safe_confidence,
                },
            )

            self._replace_if_present(screen, "screenName", display_screen_name or screen.get("screenName", ""))
            screen["goals"] = merge_unique_strings(screen.get("goals"), [display_goal] if display_goal else [])
            screen["visualCues"] = merge_unique_strings(screen.get("visualCues"), cues)
            screen["usefulActions"] = merge_unique_strings(screen.get("usefulActions"), actions)
            screen["assertions"] = merge_unique_strings(screen.get("assertions"), checks)
            screen["blockers"] = merge_unique_strings(screen.get("blockers"), blocker_items)
            screen["packages"] = merge_unique_strings(screen.get("packages"), [package_name] if package_name else [])
            screen["sourceTypes"] = merge_unique_strings(screen.get("sourceTypes"), [source_type_value] if source_type_value else [])
            screen["navigationModes"] = merge_unique_strings(screen.get("navigationModes"), [navigation_mode_value] if navigation_mode_value else [])
            screen["screenFingerprints"] = merge_unique_strings(screen.get("screenFingerprints"), [fingerprint_value] if fingerprint_value else [])
            screen["keyTexts"] = merge_unique_strings(screen.get("keyTexts"), key_text_items)
            screen["keyResourceIds"] = merge_unique_strings(screen.get("keyResourceIds"), key_resource_id_items)
            screen["keyContentDescs"] = merge_unique_strings(screen.get("keyContentDescs"), key_content_desc_items)
            screen["activities"] = merge_unique_strings(screen.get("activities"), [current_activity_value] if current_activity_value else [])
            screen["focusedWindows"] = merge_unique_strings(screen.get("focusedWindows"), [focused_window_value] if focused_window_value else [])
            if note_text:
                screen.setdefault("notes", []).append({"timestamp": now, "text": note_text})
            screen["confidence"] = max(float(screen.get("confidence") or 0.0), safe_confidence)
            screen["updatedAt"] = now

            if route_steps:
                route_id = sanitize_filename(f"{display_goal or display_screen_name}-{screen_id}")
                routes = project_navigation.setdefault("routes", {})
                route_payload = routes.setdefault(
                    route_id,
                    {
                        "goal": display_goal,
                        "targetScreen": display_screen_name,
                        "steps": [],
                        "packages": [],
                        "createdAt": now,
                        "updatedAt": now,
                        "confidence": safe_confidence,
                    },
                )
                self._replace_if_present(route_payload, "goal", display_goal)
                self._replace_if_present(route_payload, "targetScreen", display_screen_name)
                route_payload["steps"] = merge_unique_strings(route_payload.get("steps"), route_steps)
                route_payload["packages"] = merge_unique_strings(route_payload.get("packages"), [package_name] if package_name else [])
                route_payload["confidence"] = max(float(route_payload.get("confidence") or 0.0), safe_confidence)
                route_payload["updatedAt"] = now

            project_navigation["knownActions"] = merge_unique_strings(project_navigation.get("knownActions"), actions)
            project_navigation["blockers"] = merge_unique_strings(project_navigation.get("blockers"), blocker_items)
            project_navigation["updatedAt"] = now
            guide["version"] = 2
            guide["updatedAt"] = now
            self._write_unlocked(guide)

        command_path = self.recorder.record_command(
            command_name="save_navigation_learning",
            request_payload={
                "appPackage": package_name,
                "screenName": display_screen_name,
                "goal": display_goal,
                "route": route_steps,
                "visualCues": cues,
                "usefulActions": actions,
                "assertions": checks,
                "blockers": blocker_items,
                "notes": notes,
                "confidence": safe_confidence,
                "sourceType": source_type_value,
                "navigationMode": navigation_mode_value,
                "screenFingerprint": fingerprint_value,
                "keyTexts": key_text_items,
                "keyResourceIds": key_resource_id_items,
                "keyContentDescs": key_content_desc_items,
                "currentActivity": current_activity_value,
                "focusedWindow": focused_window_value,
            },
            response_payload={"success": True, "navigationMemoryPath": str(self.path.resolve()), "screenId": screen_id},
        )
        return {
            "success": True,
            "message": "Navigation learning saved",
            "navigationMemoryPath": str(self.path.resolve()),
            "commandLogPath": str(command_path.resolve()),
            "screenId": screen_id,
            "screenName": display_screen_name,
            "goal": display_goal,
        }

    def context(
        self,
        *,
        goal: str = "",
        max_items: int = 8,
        screen_fingerprint: str = "",
        current_activity: str = "",
        source_type: str = "",
        navigation_mode: str = "",
    ) -> dict[str, Any]:
        limit = max(1, min(int(max_items or 8), 25))
        goal_text = goal.strip()
        fingerprint_value = normalize_ui_text(screen_fingerprint)
        current_activity_value = normalize_activity_name(current_activity)
        source_type_value = normalize_ui_text(source_type)
        navigation_mode_value = normalize_ui_text(navigation_mode)
        with self._lock:
            guide = self._read_unlocked()

        project_navigation = self._project_navigation(guide)
        screens = list(project_navigation.get("screens", {}).values())
        routes = list(project_navigation.get("routes", {}).values())
        old_screens = self._legacy_screens(guide)
        raw_events = list(project_navigation.get("recentEvents") or guide.get("recentEvents") or [])[-limit:]
        events = [
            {
                "timestamp": event.get("timestamp"),
                "action": event.get("action"),
                "success": event.get("success"),
                "sourceType": event.get("sourceType"),
                "navigationMode": event.get("navigationMode"),
                "screenFingerprint": event.get("screenFingerprint"),
                "currentActivity": event.get("currentActivity"),
                "fallbackRecommended": event.get("fallbackRecommended"),
            }
            for event in raw_events
            if isinstance(event, dict)
        ]

        all_screens = screens + old_screens
        fingerprint_matches: list[dict[str, Any]] = []
        if fingerprint_value:
            for screen in all_screens:
                if fingerprint_value in normalize_string_list(screen.get("screenFingerprints")):
                    fingerprint_matches.append(screen)
        activity_matches: list[dict[str, Any]] = []
        if current_activity_value:
            for screen in all_screens:
                if current_activity_value in normalize_string_list(screen.get("activities")) and screen not in fingerprint_matches:
                    activity_matches.append(screen)
        filtered_screens = []
        for screen in fingerprint_matches + activity_matches + rank_navigation_items(all_screens, goal_text):
            if screen not in filtered_screens:
                filtered_screens.append(screen)
            if len(filtered_screens) >= limit:
                break
        filtered_routes = rank_navigation_items(routes, goal_text)[:limit]
        recommended_steps: list[str] = []
        for route_payload in filtered_routes:
            steps = normalize_string_list(route_payload.get("steps"))
            if route_payload.get("goal"):
                recommended_steps.append(f"Objetivo: {route_payload.get('goal')}")
            recommended_steps.extend(steps)
            if len(recommended_steps) >= limit:
                break

        useful_actions: list[str] = normalize_string_list(project_navigation.get("knownActions"))
        warnings: list[str] = normalize_string_list(project_navigation.get("blockers"))[:limit]
        known_screens: list[dict[str, Any]] = []
        for screen in filtered_screens:
            useful_actions = merge_unique_strings(useful_actions, normalize_string_list(screen.get("usefulActions")))
            warnings = merge_unique_strings(warnings, normalize_string_list(screen.get("blockers")))
            known_screens.append(
                {
                    "screenName": screen.get("screenName"),
                    "goals": normalize_string_list(screen.get("goals"))[:3],
                    "visualCues": normalize_string_list(screen.get("visualCues"))[:3],
                    "sourceTypes": normalize_string_list(screen.get("sourceTypes"))[:3],
                    "navigationModes": normalize_string_list(screen.get("navigationModes"))[:3],
                    "screenFingerprints": normalize_string_list(screen.get("screenFingerprints"))[:3],
                    "activities": normalize_string_list(screen.get("activities"))[:3],
                    "assertions": normalize_string_list(screen.get("assertions"))[:3],
                    "confidence": screen.get("confidence"),
                }
            )

        matched_by_fingerprint = bool(fingerprint_matches)
        matched_by_activity = bool(activity_matches)
        preferred_navigation_mode = navigation_mode_value or ""
        if fingerprint_matches and normalize_string_list(fingerprint_matches[0].get("navigationModes")):
            preferred_navigation_mode = normalize_string_list(fingerprint_matches[0].get("navigationModes"))[0]
        elif activity_matches and normalize_string_list(activity_matches[0].get("navigationModes")):
            preferred_navigation_mode = normalize_string_list(activity_matches[0].get("navigationModes"))[0]
        elif source_type_value == "webview":
            preferred_navigation_mode = "visual"
        elif not preferred_navigation_mode:
            preferred_navigation_mode = "structured"

        visual_fallback_recommended = preferred_navigation_mode in {"visual", "hybrid"} or source_type_value == "webview"

        summary = "Nenhum aprendizado de navegacao salvo ainda."
        if known_screens or recommended_steps or useful_actions:
            summary_parts = []
            if goal_text:
                summary_parts.append(f"Contexto para objetivo: {goal_text}.")
            if matched_by_fingerprint:
                summary_parts.append("Tela atual reconhecida por fingerprint.")
            elif matched_by_activity:
                summary_parts.append("Tela atual aproximada por activity.")
            summary_parts.append(f"{len(known_screens)} tela(s) conhecida(s), {len(filtered_routes)} rota(s) relevante(s).")
            summary = " ".join(summary_parts)

        return {
            "success": True,
            "summary": summary,
            "goal": goal_text,
            "recommendedSteps": recommended_steps[:limit],
            "knownScreens": known_screens[:limit],
            "usefulActions": useful_actions[:limit],
            "warnings": warnings[:limit],
            "recentEvents": events,
            "matchedByFingerprint": matched_by_fingerprint,
            "matchedByActivity": matched_by_activity,
            "preferredNavigationMode": preferred_navigation_mode,
            "visualFallbackRecommended": visual_fallback_recommended,
            "screenFingerprint": fingerprint_value,
            "currentActivity": current_activity_value,
            "sourcePath": str(self.path.resolve()),
            "navigationMemoryPath": str(self.path.resolve()),
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
            "sourceType": result.get("sourceType"),
            "navigationMode": result.get("navigationMode"),
            "screenFingerprint": result.get("screenFingerprint"),
            "currentActivity": result.get("currentActivity"),
            "matchedElements": result.get("matchedElements"),
            "fallbackUsed": result.get("fallbackUsed"),
            "fallbackRecommended": result.get("fallbackRecommended"),
        }
        with self._lock:
            guide = self._read_unlocked()
            events = guide.setdefault("recentEvents", [])
            events.append(event)
            guide["recentEvents"] = events[-200:]
            project_navigation = self._project_navigation(guide)
            project_events = project_navigation.setdefault("recentEvents", [])
            project_events.append(event)
            project_navigation["recentEvents"] = project_events[-200:]
            project_navigation["updatedAt"] = now
            guide["updatedAt"] = now
            self._write_unlocked(guide)

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty_guide()
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
        self._project_navigation(payload)
        return payload

    @staticmethod
    def _empty_guide() -> dict[str, Any]:
        return {
            "version": 2,
            "updatedAt": None,
            "apps": {},
            "recentEvents": [],
            "projectNavigation": {
                "screens": {},
                "routes": {},
                "knownActions": [],
                "blockers": [],
                "recentEvents": [],
                "updatedAt": None,
            },
        }

    @staticmethod
    def _project_navigation(guide: dict[str, Any]) -> dict[str, Any]:
        project_navigation = guide.setdefault("projectNavigation", {})
        if not isinstance(project_navigation, dict):
            project_navigation = {}
            guide["projectNavigation"] = project_navigation
        for key in ("screens", "routes"):
            if not isinstance(project_navigation.get(key), dict):
                project_navigation[key] = {}
        for key in ("knownActions", "blockers", "recentEvents"):
            if not isinstance(project_navigation.get(key), list):
                project_navigation[key] = []
        project_navigation.setdefault("updatedAt", None)
        return project_navigation

    @staticmethod
    def _legacy_screens(guide: dict[str, Any]) -> list[dict[str, Any]]:
        screens: list[dict[str, Any]] = []
        apps = guide.get("apps")
        if not isinstance(apps, dict):
            return screens
        for app_package, app_payload in apps.items():
            if not isinstance(app_payload, dict):
                continue
            app_screens = app_payload.get("screens")
            if not isinstance(app_screens, dict):
                continue
            for screen in app_screens.values():
                if not isinstance(screen, dict):
                    continue
                copied = dict(screen)
                copied["packages"] = merge_unique_strings(copied.get("packages"), [str(app_package)])
                screens.append(copied)
        return screens

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


def normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        values = [str(value)]
    normalized: list[str] = []
    for item in values:
        if not isinstance(item, str):
            item = str(item)
        text = item.strip()
        if text and text not in normalized:
            normalized.append(text)
    return normalized


def navigation_search_blob(item: dict[str, Any]) -> str:
    parts: list[str] = []
    for value in item.values():
        if isinstance(value, str):
            parts.append(value)
        elif isinstance(value, list):
            parts.extend(str(entry) for entry in value)
    return " ".join(parts).lower()


def rank_navigation_items(items: list[dict[str, Any]], goal: str) -> list[dict[str, Any]]:
    if not goal.strip():
        return sorted(items, key=lambda item: int(item.get("updatedAt") or 0), reverse=True)
    terms = [term for term in re.split(r"\s+", goal.lower()) if term]

    def score(item: dict[str, Any]) -> tuple[int, int]:
        blob = navigation_search_blob(item)
        matches = sum(1 for term in terms if term in blob)
        return matches, int(item.get("updatedAt") or 0)

    ranked = [item for item in items if score(item)[0] > 0]
    return sorted(ranked, key=score, reverse=True)


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


def normalize_verbosity(value: str = "summary") -> Literal["summary", "focused", "full"]:
    normalized = normalize_ui_text(value).lower() or "summary"
    if normalized not in {"summary", "focused", "full"}:
        raise ValueError("verbosity deve ser summary, focused ou full")
    return normalized  # type: ignore[return-value]


def normalize_max_items(value: int = DEFAULT_TOOL_MAX_ITEMS, upper_bound: int = 100) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_items deve ser numerico") from exc
    return max(1, min(parsed, upper_bound))


def summarize_text(content: str, max_lines: int = 20, max_chars: int = 2000) -> dict[str, Any]:
    return {
        "originalLength": len(content),
        "lineCount": len(content.splitlines()),
        "preview": tail_preview(content, max_lines=max_lines, max_chars=max_chars),
    }


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


def normalize_ui_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return re.sub(r"\s+", " ", text)


def parse_android_bool(value: Any) -> bool:
    return str(value or "").strip().lower() == "true"


def parse_android_bounds(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", value.strip())
    if not match:
        return None
    return tuple(int(group) for group in match.groups())


def bounds_payload(bounds: tuple[int, int, int, int] | None) -> dict[str, int] | None:
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return {
        "left": left,
        "top": top,
        "right": right,
        "bottom": bottom,
        "width": max(0, right - left),
        "height": max(0, bottom - top),
    }


def bounds_center(bounds: tuple[int, int, int, int] | None) -> tuple[int, int] | None:
    if not bounds:
        return None
    left, top, right, bottom = bounds
    return ((left + right) // 2, (top + bottom) // 2)


def text_preview_items(items: list[str], limit: int = 8) -> list[str]:
    values: list[str] = []
    for item in items:
        text = normalize_ui_text(item)
        if not text or text in values:
            continue
        values.append(text)
        if len(values) >= limit:
            break
    return values


def build_screen_fingerprint(
    *,
    package_name: str,
    current_activity: str,
    focused_window: str,
    source_type: str,
    key_texts: list[str],
    key_resource_ids: list[str],
    key_content_descs: list[str],
    key_classes: list[str],
) -> tuple[str, dict[str, Any]]:
    payload = {
        "packageName": normalize_ui_text(package_name),
        "currentActivity": normalize_ui_text(current_activity),
        "focusedWindow": normalize_ui_text(focused_window),
        "sourceType": normalize_ui_text(source_type),
        "keyTexts": text_preview_items(key_texts, limit=6),
        "keyResourceIds": text_preview_items(key_resource_ids, limit=6),
        "keyContentDescs": text_preview_items(key_content_descs, limit=6),
        "keyClasses": text_preview_items(key_classes, limit=6),
    }
    digest = hashlib.sha1(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return f"screen-{digest}", payload


def detect_webview_from_text(*values: str) -> bool:
    return any("webview" in str(value or "").lower() for value in values)


def normalize_activity_name(value: str) -> str:
    return normalize_ui_text(value)


def normalize_bool(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    raise ValueError(f"Valor booleano invalido: {value!r}")


def normalize_package_name(value: str) -> str | None:
    candidate = value.strip()
    if not candidate:
        return None
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)+", candidate):
        return None
    return candidate


def parse_adb_devices_output(output: str) -> list[dict[str, Any]]:
    devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.lower().startswith("list of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        details: dict[str, str] = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                details[key] = value
        devices.append(
            {
                "serial": parts[0],
                "state": parts[1],
                "online": parts[1] == "device",
                "details": details,
            }
        )
    return devices


def is_external_sqlite_root(value: str | None) -> bool:
    normalized = str(value or "").replace("\\", "/").strip()
    return normalized.startswith("/sdcard/") or normalized == "/sdcard" or normalized.startswith("/storage/emulated/0/")


def normalize_external_sqlite_root(value: str) -> str:
    normalized = value.replace("\\", "/").strip().rstrip("/")
    if not is_external_sqlite_root(normalized):
        raise ValueError("sqlite_root_path externo deve iniciar com /sdcard ou /storage/emulated/0")
    if "/../" in f"{normalized}/" or normalized.endswith("/.."):
        raise ValueError("sqlite_root_path externo invalido")
    return normalized or "/sdcard"


def read_text_if_exists(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def collect_android_package_candidates(project_dir: Path) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_paths: set[Path] = set()

    def add_candidate(package_name: str, score: int, source: str, path: Path) -> None:
        normalized = normalize_package_name(package_name)
        if not normalized:
            return
        candidates.append(
            {
                "packageName": normalized,
                "score": score,
                "source": source,
                "path": str(path),
            }
        )

    def inspect_file(path: Path) -> None:
        resolved = path.resolve()
        if resolved in seen_paths or not path.is_file():
            return
        seen_paths.add(resolved)
        text = read_text_if_exists(path)
        if not text:
            return
        path_str = str(path).replace("\\", "/").lower()
        if path.name in {"build.gradle", "build.gradle.kts"}:
            base_score = 100 if "/app/" in path_str else 80
            for match in re.finditer(r"\bapplicationId\s*(?:=)?\s*[\"']([A-Za-z0-9_.]+)[\"']", text):
                add_candidate(match.group(1), base_score, "applicationId", path)
            for match in re.finditer(r"\bnamespace\s*(?:=)?\s*[\"']([A-Za-z0-9_.]+)[\"']", text):
                add_candidate(match.group(1), base_score - 20, "namespace", path)
        elif path.name == "AndroidManifest.xml":
            match = re.search(r"<manifest[^>]*\bpackage\s*=\s*[\"']([A-Za-z0-9_.]+)[\"']", text)
            if match:
                score = 70 if "/src/main/" in path_str else 60
                add_candidate(match.group(1), score, "manifest", path)

    preferred_files = [
        project_dir / "app" / "build.gradle",
        project_dir / "app" / "build.gradle.kts",
        project_dir / "app" / "src" / "main" / "AndroidManifest.xml",
        project_dir / "build.gradle",
        project_dir / "build.gradle.kts",
    ]
    for path in preferred_files:
        inspect_file(path)

    if not candidates:
        for pattern in ("build.gradle", "build.gradle.kts", "AndroidManifest.xml"):
            for path in sorted(project_dir.rglob(pattern))[:24]:
                inspect_file(path)
                if len(candidates) >= 12:
                    break
            if len(candidates) >= 12:
                break

    return sorted(candidates, key=lambda item: (int(item["score"]), item["path"]), reverse=True)


def infer_android_package_name(project_dir: Path) -> dict[str, Any]:
    candidates = collect_android_package_candidates(project_dir)
    best = candidates[0] if candidates else None
    return {
        "packageName": best["packageName"] if best else None,
        "source": best["source"] if best else None,
        "sourcePath": best["path"] if best else None,
        "candidates": candidates[:8],
    }


def ensure_safe_database_name(database_name: str) -> str:
    candidate = Path(database_name.strip()).name
    if not candidate or candidate in {".", ".."} or "/" in database_name or "\\" in database_name:
        raise ValueError("database_name deve ser apenas o nome do arquivo do banco")
    return candidate


def classify_sql_statement(sql: str) -> Literal["read", "write"]:
    normalized = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    normalized = re.sub(r"--.*?$", " ", normalized, flags=re.MULTILINE).strip()
    keyword = normalized.split(None, 1)[0].upper() if normalized else ""
    if keyword in {"SELECT", "PRAGMA", "EXPLAIN"}:
        return "read"
    if keyword == "WITH" and not re.search(r"\b(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER)\b", normalized, re.IGNORECASE):
        return "read"
    return "write"


def json_safe_sqlite_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return {"type": "bytes", "hex": value.hex()}
    return value


def should_preserve_update_path(relative: Path) -> bool:
    if not relative.parts:
        return True
    if relative.parts[0] in {".git", ".venv", "__pycache__"}:
        return True
    if relative.parts[0] == "tests" and len(relative.parts) > 1 and relative.parts[1] == "mcp":
        return True
    if relative.name in {UPDATE_STATE_FILENAME, "android-agent.config.json"}:
        return True
    return False


def iter_fingerprint_files(root: Path) -> list[Path]:
    excluded_names = {".git", ".venv", "__pycache__", UPDATE_STATE_FILENAME}
    files: list[Path] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        if any(part in excluded_names for part in relative.parts):
            continue
        if relative.parts[:2] == ("tests", "mcp"):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return files


def compute_tree_fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    for path in iter_fingerprint_files(root):
        relative = path.relative_to(root).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def update_repo_archive_url(repo_url: str, channel: str) -> str | None:
    normalized = repo_url.strip().removesuffix(".git").rstrip("/")
    if normalized.startswith("https://github.com/"):
        return f"{normalized}/archive/refs/heads/{channel}.zip"
    return None


def first_int_match(pattern: str, content: str) -> int | None:
    match = re.search(pattern, content)
    if not match:
        return None
    return int(match.group(1))


@dataclass
class ServerRuntime:
    timeout_seconds: float
    recorder: SessionRecorder
    navigation_memory: NavigationMemory
    config_path: Path
    adb_path: str | None
    adb_device_serial: str | None
    install_dir: Path
    configured_package_name: str | None
    sqlite_root_path: str | None
    sqlite_root_access_policy: str
    sqlite_default_database_name: str | None
    auto_update_enabled: bool
    update_repo_url: str
    update_channel: str

    def __post_init__(self) -> None:
        self._package_resolution_cache: dict[str, Any] | None = None
        self._package_meta_cache: dict[str, dict[str, Any]] = {}
        self._update_status_cache: dict[str, Any] | None = None
        self._sqlite_bundle_cache: dict[str, dict[str, Any]] = {}

    def adb_config(self) -> dict[str, Any]:
        detected = self.detect_adb_path()
        configured_exists = bool(self.adb_path and Path(self.adb_path).expanduser().exists())
        payload = {
            "success": True,
            "transport": "adb",
            "configPath": str(self.config_path.resolve()),
            "installDir": str(self.install_dir.resolve()),
            "timeoutSeconds": self.timeout_seconds,
            "adbPath": self.adb_path,
            "effectiveAdbPath": self.adb_path or detected,
            "adbPathConfigured": bool(self.adb_path),
            "adbPathExists": configured_exists,
            "adbDetectedPath": detected,
            "adbAvailable": bool(self.adb_path or detected),
            "adbDeviceSerial": self.adb_device_serial,
            "selectedDeviceSerial": self.select_adb_device_serial(detected, fail_on_missing=False),
            "artifactsRoot": str(self.recorder.paths.root.resolve()),
            "sessionLogPath": str(self.recorder.paths.session_log.resolve()),
            "navigationMemoryPath": str(self.navigation_memory.path.resolve()),
        }
        payload["packageResolution"] = self.package_resolution()
        payload["sqlite"] = self.sqlite_status(include_databases=False, fail_silently=True)
        payload["autoUpdate"] = self.current_update_status()
        return payload

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
        if adb_path.strip():
            self.adb_path = adb_path.strip()
        requested_serial = adb_device_serial.strip()
        if requested_serial:
            devices = self.list_adb_devices(self.require_adb_path())
            known_serials = {str(device.get("serial")) for device in devices}
            if requested_serial not in known_serials:
                raise ValueError(f"Device ADB nao encontrado: {requested_serial}")
        if requested_serial:
            self.adb_device_serial = requested_serial
        if persist:
            self.persist_config()
        return {"success": True, "message": "ADB configuration updated", "persisted": persist, **self.adb_config()}

    def set_sqlite_config(
        self,
        sqlite_root_path: str = "",
        sqlite_root_access_policy: str = "",
        default_database_name: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        root_path = sqlite_root_path.strip()
        access_policy = sqlite_root_access_policy.strip() or self.sqlite_root_access_policy or "run-as-then-root"
        database_name = default_database_name.strip()
        normalized_root_for_db = root_path.replace("\\", "/")
        if normalized_root_for_db.lower().endswith(".db"):
            inferred_database = Path(normalized_root_for_db).name
            root_path = normalized_root_for_db.rsplit("/", 1)[0] if "/" in normalized_root_for_db else ""
            if not database_name:
                database_name = inferred_database
        if access_policy == "auto":
            access_policy = "external" if is_external_sqlite_root(root_path or self.sqlite_root_path) else "run-as-then-root"
        if access_policy not in {"run-as-only", "root-only", "run-as-then-root", "external"}:
            raise ValueError("sqlite_root_access_policy deve ser auto, run-as-only, root-only, run-as-then-root ou external")
        if root_path and is_external_sqlite_root(root_path):
            root_path = normalize_external_sqlite_root(root_path)
            if not sqlite_root_access_policy.strip():
                access_policy = "external"
        if database_name:
            database_name = ensure_safe_database_name(database_name)
        self.sqlite_root_path = root_path or self.sqlite_root_path
        self.sqlite_root_access_policy = access_policy
        self.sqlite_default_database_name = database_name or self.sqlite_default_database_name
        self._sqlite_bundle_cache.clear()
        if persist:
            self.persist_config()
        return {"success": True, "message": "SQLite configuration updated", "persisted": persist, "sqlite": self.sqlite_config_status()}

    def persist_config(self) -> None:
        payload = load_json_config(self.config_path)
        payload["timeoutSeconds"] = self.timeout_seconds
        payload["adbPath"] = self.adb_path or ""
        payload["adbDeviceSerial"] = self.adb_device_serial or ""
        payload["packageName"] = self.configured_package_name or ""
        payload["sqliteRootPath"] = self.sqlite_root_path or ""
        payload["sqliteRootAccessPolicy"] = self.sqlite_root_access_policy
        payload["sqliteDefaultDatabaseName"] = self.sqlite_default_database_name or ""
        payload["autoUpdateEnabled"] = self.auto_update_enabled
        payload["updateRepoUrl"] = self.update_repo_url
        payload["updateChannel"] = self.update_channel
        payload.setdefault("artifactsDir", relative_or_absolute_path(DEFAULT_ARTIFACTS_DIR))
        payload.setdefault("navigationMemoryPath", relative_or_absolute_path(DEFAULT_NAVIGATION_MEMORY_PATH))
        legacy_key = "end" + "point"
        payload.pop(legacy_key, None)
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def package_resolution(self, force_refresh: bool = False) -> dict[str, Any]:
        if self._package_resolution_cache and not force_refresh:
            return dict(self._package_resolution_cache)
        inferred = infer_android_package_name(PROJECT_DIR)
        configured = normalize_package_name(self.configured_package_name or "")
        package_name = configured or inferred.get("packageName")
        payload = {
            "packageName": package_name,
            "configuredPackageName": configured,
            "inferredPackageName": inferred.get("packageName"),
            "source": "config" if configured else inferred.get("source"),
            "sourcePath": str(self.config_path.resolve()) if configured else inferred.get("sourcePath"),
            "candidates": inferred.get("candidates", []),
        }
        self._package_resolution_cache = payload
        return dict(payload)

    def resolve_package_name(self) -> str:
        package_name = self.package_resolution().get("packageName")
        if package_name:
            return str(package_name)
        raise RuntimeError(
            "Nao foi possivel resolver o packageName do projeto ativo. "
            "Defina packageName em android-agent.config.json ou use um projeto Android com applicationId detectavel."
        )

    def sqlite_root_relative_path(self) -> str:
        value = (self.sqlite_root_path or "databases").replace("\\", "/").strip().strip("/")
        if not value:
            return "databases"
        parts = [part for part in value.split("/") if part]
        if any(part == ".." for part in parts):
            raise ValueError("sqliteRootPath invalido")
        return "/".join(parts)

    def sqlite_config_status(self) -> dict[str, Any]:
        root_path = self.sqlite_root_path or "databases"
        access_policy = self.sqlite_root_access_policy
        is_external = access_policy == "external" or is_external_sqlite_root(root_path)
        return {
            "sqliteRootPath": root_path,
            "sqliteRootAccessPolicy": access_policy,
            "sqliteDefaultDatabaseName": self.sqlite_default_database_name,
            "isExternal": is_external,
            "mode": "external" if is_external else "app-internal",
        }

    def resolve_sqlite_database_name(self, database_name: str = "") -> str:
        candidate = database_name.strip() or (self.sqlite_default_database_name or "")
        if not candidate:
            raise ValueError("database_name eh obrigatorio ou configure sqliteDefaultDatabaseName com android_set_sqlite_config")
        return ensure_safe_database_name(candidate)

    def package_meta(self, package_name: str) -> dict[str, Any]:
        if package_name in self._package_meta_cache:
            return dict(self._package_meta_cache[package_name])
        result = self.run_adb_command(
            command_name="package_meta",
            adb_args=["shell", "dumpsys", "package", package_name],
            request_payload={"packageName": package_name},
            timeout_seconds=max(self.timeout_seconds, 30.0),
            record_stdout_max_chars=20000,
        )
        stdout = str(result.get("stdout") or "")
        meta = {
            "packageName": package_name,
            "installed": package_name in stdout,
            "debuggable": "DEBUGGABLE" in stdout,
            "appId": first_int_match(r"\bappId=(\d+)", stdout),
            "dataDir": None,
            "commandLogPath": result.get("commandLogPath"),
        }
        data_dir_match = re.search(r"\bdataDir=([^\s]+)", stdout)
        if data_dir_match:
            meta["dataDir"] = data_dir_match.group(1).strip()
        self._package_meta_cache[package_name] = meta
        return dict(meta)

    def navigation_guide(self, verbosity: str = "summary", goal: str = "", screen_fingerprint: str = "", max_items: int = DEFAULT_TOOL_MAX_ITEMS) -> dict[str, Any]:
        mode = normalize_verbosity(verbosity)
        if mode != "full":
            return self.navigation_context(goal=goal, max_items=max_items, screen_fingerprint=screen_fingerprint)
        return {"success": True, "verbosity": mode, "navigationMemoryPath": str(self.navigation_memory.path.resolve()), "guide": self.navigation_memory.read()}

    def navigation_context(
        self,
        goal: str = "",
        max_items: int = 8,
        screen_fingerprint: str = "",
        current_activity: str = "",
        source_type: str = "",
        navigation_mode: str = "",
    ) -> dict[str, Any]:
        return self.navigation_memory.context(
            goal=goal,
            max_items=max_items,
            screen_fingerprint=screen_fingerprint,
            current_activity=current_activity,
            source_type=source_type,
            navigation_mode=navigation_mode,
        )

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

    def save_navigation_learning(
        self,
        *,
        screen_name: str = "",
        goal: str = "",
        route: list[str] | None = None,
        visual_cues: list[str] | None = None,
        useful_actions: list[str] | None = None,
        assertions: list[str] | None = None,
        blockers: list[str] | None = None,
        notes: str = "",
        confidence: float = 0.7,
        app_package: str = "",
        source_type: str = "",
        navigation_mode: str = "",
        screen_fingerprint: str = "",
        key_texts: list[str] | None = None,
        key_resource_ids: list[str] | None = None,
        key_content_descs: list[str] | None = None,
        current_activity: str = "",
        focused_window: str = "",
    ) -> dict[str, Any]:
        return self.navigation_memory.save_learning(
            screen_name=screen_name,
            goal=goal,
            route=route,
            visual_cues=visual_cues,
            useful_actions=useful_actions,
            assertions=assertions,
            blockers=blockers,
            notes=notes,
            confidence=confidence,
            app_package=app_package,
            source_type=source_type,
            navigation_mode=navigation_mode,
            screen_fingerprint=screen_fingerprint,
            key_texts=key_texts,
            key_resource_ids=key_resource_ids,
            key_content_descs=key_content_descs,
            current_activity=current_activity,
            focused_window=focused_window,
        )

    def adb_status(self) -> dict[str, Any]:
        payload = self.run_adb_command(command_name="adb_status", adb_args=["devices", "-l"])
        devices = parse_adb_devices_output(str(payload.get("stdout") or ""))
        payload["devices"] = devices
        payload["onlineDevices"] = [device for device in devices if device.get("online")]
        payload["selectedDeviceSerial"] = self.select_adb_device_serial(payload.get("adbPath"), fail_on_missing=False)
        payload["adbConfig"] = self.adb_config()
        return payload

    def sqlite_status(self, include_databases: bool = True, fail_silently: bool = False) -> dict[str, Any]:
        try:
            package_name = self.resolve_package_name()
            package_meta = self.package_meta(package_name)
            configured_root = self.sqlite_root_path or "databases"
            external_root = self.sqlite_root_access_policy == "external" or is_external_sqlite_root(configured_root)
            if external_root:
                remote_root = normalize_external_sqlite_root(configured_root)
                access = {
                    "accessMode": "external",
                    "canRead": True,
                    "canWrite": False,
                    "message": "SQLite directory configured on shared storage",
                    "attempts": [],
                }
                payload = {
                    "success": True,
                    "packageName": package_name,
                    "rootRelativePath": None,
                    "remoteRootPath": remote_root,
                    "accessPolicy": self.sqlite_root_access_policy,
                    "accessMode": "external",
                    "canRead": True,
                    "canWrite": False,
                    "readOnly": True,
                    "defaultDatabaseName": self.sqlite_default_database_name,
                    "installed": package_meta.get("installed"),
                    "debuggable": package_meta.get("debuggable"),
                    "appId": package_meta.get("appId"),
                    "message": access["message"],
                    "attempts": access["attempts"],
                }
                if include_databases:
                    listing = self.list_remote_sqlite_entries(package_name, "external", "", remote_root)
                    payload.update(listing)
                return payload
            root_relative = self.sqlite_root_relative_path()
            data_dir = package_meta.get("dataDir") or f"/data/user/0/{package_name}"
            remote_root = f"{data_dir.rstrip('/')}/{root_relative}"
            access = self.detect_sqlite_access(package_name, root_relative, remote_root, package_meta=package_meta)
            payload = {
                "success": access["canRead"],
                "packageName": package_name,
                "rootRelativePath": root_relative,
                "remoteRootPath": remote_root,
                "accessPolicy": self.sqlite_root_access_policy,
                "accessMode": access["accessMode"],
                "canRead": access["canRead"],
                "canWrite": access["canWrite"],
                "readOnly": not access["canWrite"],
                "defaultDatabaseName": self.sqlite_default_database_name,
                "installed": package_meta.get("installed"),
                "debuggable": package_meta.get("debuggable"),
                "appId": package_meta.get("appId"),
                "message": access["message"],
                "attempts": access["attempts"],
            }
            if include_databases and access["canRead"]:
                listing = self.list_remote_sqlite_entries(package_name, access["accessMode"], root_relative, remote_root)
                payload["entries"] = listing["entries"]
                payload["databases"] = listing["databases"]
                payload["companionFiles"] = listing["companionFiles"]
            return payload
        except Exception as exc:
            payload = {
                "success": False,
                "message": str(exc),
                "accessPolicy": self.sqlite_root_access_policy,
                "accessMode": "unavailable",
                "canRead": False,
                "canWrite": False,
            }
            if fail_silently:
                return payload
            raise RuntimeError(str(exc)) from exc

    def detect_sqlite_access(
        self,
        package_name: str,
        root_relative: str,
        remote_root: str,
        *,
        package_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        attempts: list[dict[str, Any]] = []
        policy = self.sqlite_root_access_policy
        if policy in {"run-as-only", "run-as-then-root"}:
            result = self.run_adb_command(
                command_name="sqlite_probe_run_as",
                adb_args=["shell", "run-as", package_name, "ls", root_relative],
                request_payload={"packageName": package_name, "rootRelativePath": root_relative, "probeMode": "run-as"},
                timeout_seconds=max(self.timeout_seconds, 20.0),
                record_stdout_max_chars=4000,
            )
            attempts.append({"mode": "run-as", "success": result["success"], "stderr": result.get("stderr"), "stdout": tail_preview(str(result.get("stdout") or ""), max_lines=20, max_chars=1000)})
            if result["success"]:
                return {
                    "accessMode": "run-as",
                    "canRead": True,
                    "canWrite": True,
                    "message": "SQLite directory accessible via run-as",
                    "attempts": attempts,
                }
        if policy in {"root-only", "run-as-then-root"}:
            result = self.run_adb_command(
                command_name="sqlite_probe_root",
                adb_args=["shell", "su", "-c", f"ls {shlex.quote(remote_root)}"],
                request_payload={"packageName": package_name, "remoteRootPath": remote_root, "probeMode": "root"},
                timeout_seconds=max(self.timeout_seconds, 20.0),
                record_stdout_max_chars=4000,
            )
            attempts.append({"mode": "root", "success": result["success"], "stderr": result.get("stderr"), "stdout": tail_preview(str(result.get("stdout") or ""), max_lines=20, max_chars=1000)})
            if result["success"]:
                can_write = bool((package_meta or {}).get("appId"))
                return {
                    "accessMode": "root",
                    "canRead": True,
                    "canWrite": can_write,
                    "message": "SQLite directory accessible via root",
                    "attempts": attempts,
                }
        return {
            "accessMode": "unavailable",
            "canRead": False,
            "canWrite": False,
            "message": "Nao foi possivel acessar o diretorio SQLite do app via run-as/root",
            "attempts": attempts,
        }

    def list_remote_sqlite_entries(self, package_name: str, access_mode: str, root_relative: str, remote_root: str) -> dict[str, Any]:
        result = self.run_remote_listing_command(package_name, access_mode, root_relative, remote_root)
        lines = [line.strip() for line in str(result.get("stdout") or "").splitlines() if line.strip() and not line.lower().startswith("total")]
        entries = sorted(set(lines))
        companion_suffixes = ("-wal", "-shm", "-journal")
        databases = [entry for entry in entries if not entry.endswith(companion_suffixes) and ".bak-" not in entry]
        companion_files = [entry for entry in entries if entry.endswith(companion_suffixes)]
        backup_files = [entry for entry in entries if ".bak-" in entry]
        visible_entries = entries[:80]
        return {
            "entries": visible_entries,
            "allEntries": entries,
            "entryCount": len(entries),
            "databases": databases,
            "companionFiles": companion_files,
            "backupCount": len(backup_files),
            "commandLogPath": result.get("commandLogPath"),
            "truncated": len(visible_entries) < len(entries),
        }

    def sqlite_list_databases(self) -> dict[str, Any]:
        status = self.sqlite_status(include_databases=True)
        status.pop("allEntries", None)
        status["message"] = "SQLite databases listed successfully" if status["success"] else status["message"]
        return status

    def sqlite_pull_database(self, database_name: str = "", refresh: bool = False) -> dict[str, Any]:
        database_name = self.resolve_sqlite_database_name(database_name)
        status = self.sqlite_status(include_databases=False)
        if not status["canRead"]:
            raise RuntimeError(status["message"])
        bundle = self.pull_remote_sqlite_bundle(
            package_name=str(status["packageName"]),
            access_mode=str(status["accessMode"]),
            root_relative=str(status.get("rootRelativePath") or ""),
            remote_root=str(status["remoteRootPath"]),
            database_name=database_name,
            refresh=refresh,
        )
        return {
            "success": True,
            "message": "SQLite database pulled successfully",
            "packageName": status["packageName"],
            "accessMode": status["accessMode"],
            "databaseName": database_name,
            "artifactDir": str(bundle["artifactDir"].resolve()),
            "databasePath": str(bundle["databasePath"].resolve()),
            "paths": [str(path.resolve()) for path in bundle["files"].values()],
            "files": sorted(bundle["files"].keys()),
            "remoteRootPath": status["remoteRootPath"],
        }

    def sqlite_query(self, database_name: str = "", sql: str = "", parameters: list[Any] | None = None, max_rows: int = DEFAULT_SQLITE_MAX_ROWS) -> dict[str, Any]:
        database_name = self.resolve_sqlite_database_name(database_name)
        sql = sql.strip()
        if not sql:
            raise ValueError("sql eh obrigatorio")
        if max_rows < 1:
            raise ValueError("max_rows deve ser >= 1")
        status = self.sqlite_status(include_databases=False)
        if not status["canRead"]:
            raise RuntimeError(status["message"])
        statement_kind = classify_sql_statement(sql)
        if statement_kind == "write" and not status["canWrite"]:
            raise RuntimeError("O acesso SQLite atual nao permite escrita no app alvo")
        bundle = self.pull_remote_sqlite_bundle(
            package_name=str(status["packageName"]),
            access_mode=str(status["accessMode"]),
            root_relative=str(status.get("rootRelativePath") or ""),
            remote_root=str(status["remoteRootPath"]),
            database_name=database_name,
            refresh=statement_kind == "write",
        )
        local_backup_dir: Path | None = None
        if statement_kind == "write":
            local_backup_dir = self.recorder.paths.artifacts_dir / "sqlite" / f"backup-{int(time.time() * 1000)}"
            local_backup_dir.mkdir(parents=True, exist_ok=True)
            for filename, path in bundle["files"].items():
                shutil.copy2(path, local_backup_dir / filename)
        connection = sqlite3.connect(str(bundle["databasePath"]))
        write_back: dict[str, Any] | None = None
        try:
            cursor = connection.cursor()
            cursor.execute(sql, list(parameters or []))
            columns = [str(item[0]) for item in cursor.description] if cursor.description else []
            rows_raw = cursor.fetchmany(max_rows + 1) if cursor.description else []
            rows = [[json_safe_sqlite_value(value) for value in row] for row in rows_raw[:max_rows]]
            truncated = len(rows_raw) > max_rows
            affected_rows = cursor.rowcount if cursor.rowcount != -1 else len(rows)
            if statement_kind == "write":
                connection.commit()
                try:
                    connection.execute("PRAGMA wal_checkpoint(FULL)")
                except sqlite3.DatabaseError:
                    pass
            else:
                connection.rollback()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        if statement_kind == "write":
            write_back = self.push_local_sqlite_bundle(
                package_name=str(status["packageName"]),
                access_mode=str(status["accessMode"]),
                root_relative=str(status["rootRelativePath"]),
                remote_root=str(status["remoteRootPath"]),
                database_name=database_name,
                local_dir=bundle["artifactDir"],
                package_meta=self.package_meta(str(status["packageName"])),
            )
        return {
            "success": True,
            "message": "SQLite query executed successfully",
            "packageName": status["packageName"],
            "databaseName": database_name,
            "statementKind": statement_kind,
            "columns": columns,
            "rows": rows,
            "rowCount": len(rows),
            "affectedRows": affected_rows,
            "truncated": truncated,
            "maxRows": max_rows,
            "artifactDir": str(bundle["artifactDir"].resolve()),
            "databasePath": str(bundle["databasePath"].resolve()),
            "localBackupDir": str(local_backup_dir.resolve()) if local_backup_dir else None,
            "writeBack": write_back,
        }

    def ui_context(
        self,
        verbosity: str = "summary",
        max_items: int = DEFAULT_TOOL_MAX_ITEMS,
        text_filter: str = "",
        resource_id_filter: str = "",
        package_filter: str = "",
        include_xml: bool = False,
    ) -> dict[str, Any]:
        mode = normalize_verbosity(verbosity)
        limit = normalize_max_items(max_items)
        xml_path = self.dump_ui_hierarchy()
        xml_content = xml_path.read_text(encoding="utf-8", errors="replace")
        nodes = self.parse_ui_hierarchy(xml_content)
        window_info = self.window_focus_info()
        ui_package = self.infer_ui_package(nodes)
        window_package = normalize_ui_text(str(window_info.get("currentPackage") or ""))
        current_package = ui_package or window_package or self.resolve_package_name()
        window_activity = normalize_activity_name(str(window_info.get("currentActivity") or ""))
        effective_activity = window_activity if window_activity.startswith(f"{current_package}/") else normalize_activity_name(str(window_info.get("visibleActivity") or window_activity))
        focus_mismatch = bool(ui_package and window_package and ui_package != window_package)
        visible_texts = text_preview_items(
            [node.get("text", "") for node in nodes] + [node.get("contentDesc", "") for node in nodes],
            limit=limit,
        )
        clickable_elements = [node for node in nodes if bool(node.get("clickable")) and node.get("bounds")]
        editable_elements = [node for node in nodes if bool(node.get("editable")) and node.get("bounds")]
        scrollable_elements = [node for node in nodes if bool(node.get("scrollable")) and node.get("bounds")]
        key_resource_ids = text_preview_items([str(node.get("resourceId") or "") for node in clickable_elements + editable_elements], limit=10)
        key_content_descs = text_preview_items([str(node.get("contentDesc") or "") for node in clickable_elements], limit=10)
        key_classes = text_preview_items([str(node.get("className") or "") for node in clickable_elements + editable_elements + scrollable_elements], limit=10)
        focused_window = normalize_ui_text(str(window_info.get("focusedWindow") or ""))
        source_type = self.classify_ui_source(nodes, window_info)
        navigation_mode, fallback_recommended, fallback_reason, confidence = self.evaluate_navigation_mode(source_type, nodes, visible_texts, clickable_elements)
        fallback_method = "screenshot" if fallback_recommended else ""
        if fallback_recommended:
            agent_hint = "Use android_get_screen before deciding the next tap."
        else:
            agent_hint = "Use clickableElements and bounds from this response for navigation."
        screen_fingerprint, fingerprint_parts = build_screen_fingerprint(
            package_name=current_package,
            current_activity=effective_activity,
            focused_window=focused_window,
            source_type=source_type,
            key_texts=visible_texts,
            key_resource_ids=key_resource_ids,
            key_content_descs=key_content_descs,
            key_classes=key_classes,
        )
        screen_bounds = self.estimate_screen_bounds(nodes)
        matching_nodes = self.filter_ui_nodes(
            nodes,
            text_filter=text_filter,
            resource_id_filter=resource_id_filter,
            package_filter=package_filter,
        )
        payload = {
            "success": True,
            "message": "UI context captured successfully",
            "verbosity": mode,
            "sourceType": source_type,
            "navigationMode": navigation_mode,
            "fallbackRecommended": fallback_recommended,
            "fallbackMethod": fallback_method,
            "fallbackReason": fallback_reason,
            "agentHint": agent_hint,
            "confidence": confidence,
            "currentPackage": current_package,
            "effectivePackage": current_package,
            "uiPackage": ui_package,
            "windowPackage": window_package,
            "focusMismatch": focus_mismatch,
            "currentActivity": effective_activity,
            "windowActivity": window_activity,
            "focusedWindow": focused_window,
            "screenBounds": screen_bounds,
            "screenFingerprint": screen_fingerprint,
            "visibleTexts": visible_texts,
            "clickableElements": clickable_elements[:limit],
            "editableElements": editable_elements[:limit],
            "scrollableElements": scrollable_elements[:limit],
            "matches": matching_nodes[:limit],
            "found": bool(matching_nodes),
            "matchedElements": {
                "clickable": len(clickable_elements),
                "editable": len(editable_elements),
                "scrollable": len(scrollable_elements),
                "visibleTexts": len(visible_texts),
                "matches": len(matching_nodes),
                "nodes": len(nodes),
            },
            "xmlArtifactPath": str(xml_path.resolve()),
            "artifactPath": str(xml_path.resolve()),
            "path": str(xml_path.resolve()),
            "xmlOriginalLength": len(xml_content),
            "fallbackUsed": False,
        }
        if mode == "full":
            payload["screenFingerprintParts"] = fingerprint_parts
            payload["allClickableElements"] = clickable_elements
            payload["allEditableElements"] = editable_elements
            payload["allScrollableElements"] = scrollable_elements
        if include_xml:
            payload["xml"] = xml_content
        self.navigation_memory.record_automatic_event(action="ui_context", result=payload)
        return payload

    def dump_ui_hierarchy(self) -> Path:
        xml_path = self.recorder.paths.artifacts_dir / "window_dump.xml"
        dump_result = self.run_adb_command(
            command_name="ui_dump",
            adb_args=["shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
            timeout_seconds=max(self.timeout_seconds, 20.0),
            record_stdout_max_chars=4000,
        )
        if not dump_result["success"]:
            raise RuntimeError(dump_result.get("stderr") or dump_result.get("stdout") or "Falha ao gerar uiautomator dump")
        pull_result = self.run_adb_command(
            command_name="ui_dump_pull",
            adb_args=["exec-out", "cat", "/sdcard/window_dump.xml"],
            timeout_seconds=max(self.timeout_seconds, 20.0),
            decode_stdout=False,
            artifact_path=xml_path,
        )
        if not pull_result["success"]:
            raise RuntimeError(pull_result.get("stderr") or pull_result.get("stdout") or "Falha ao ler XML da UI")
        return xml_path

    def parse_ui_hierarchy(self, xml_content: str) -> list[dict[str, Any]]:
        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as exc:
            raise RuntimeError("Falha ao fazer parse do XML de UI do Android") from exc
        nodes: list[dict[str, Any]] = []

        def walk(element: ET.Element, depth: int) -> None:
            if element.tag == "node":
                attrs = element.attrib
                bounds = parse_android_bounds(attrs.get("bounds", ""))
                center = bounds_center(bounds)
                payload = bounds_payload(bounds)
                node = {
                    "text": normalize_ui_text(attrs.get("text")),
                    "contentDesc": normalize_ui_text(attrs.get("content-desc")),
                    "resourceId": normalize_ui_text(attrs.get("resource-id")),
                    "className": normalize_ui_text(attrs.get("class")),
                    "packageName": normalize_ui_text(attrs.get("package")),
                    "clickable": parse_android_bool(attrs.get("clickable")),
                    "enabled": parse_android_bool(attrs.get("enabled")),
                    "scrollable": parse_android_bool(attrs.get("scrollable")),
                    "focused": parse_android_bool(attrs.get("focused")),
                    "editable": "edittext" in normalize_ui_text(attrs.get("class")).lower(),
                    "checkable": parse_android_bool(attrs.get("checkable")),
                    "checked": parse_android_bool(attrs.get("checked")),
                    "bounds": payload,
                    "centerX": center[0] if center else None,
                    "centerY": center[1] if center else None,
                    "depth": depth,
                }
                if any(
                    [
                        node["text"],
                        node["contentDesc"],
                        node["resourceId"],
                        node["clickable"],
                        node["editable"],
                        node["scrollable"],
                    ]
                ):
                    nodes.append(node)
            for child in element:
                walk(child, depth + 1)

        walk(root, 0)
        return nodes

    def infer_ui_package(self, nodes: list[dict[str, Any]]) -> str:
        counts: dict[str, int] = {}
        for node in nodes:
            package_name = normalize_ui_text(node.get("packageName"))
            if not package_name:
                continue
            counts[package_name] = counts.get(package_name, 0) + 1
        if not counts:
            return ""
        return sorted(counts.items(), key=lambda item: item[1], reverse=True)[0][0]

    def filter_ui_nodes(
        self,
        nodes: list[dict[str, Any]],
        *,
        text_filter: str = "",
        resource_id_filter: str = "",
        package_filter: str = "",
    ) -> list[dict[str, Any]]:
        text_value = normalize_ui_text(text_filter).lower()
        resource_value = normalize_ui_text(resource_id_filter).lower()
        package_value = normalize_ui_text(package_filter).lower()
        if not text_value and not resource_value and not package_value:
            return []
        matches: list[dict[str, Any]] = []
        for node in nodes:
            text_blob = " ".join([str(node.get("text") or ""), str(node.get("contentDesc") or "")]).lower()
            resource_id = str(node.get("resourceId") or "").lower()
            package_name = str(node.get("packageName") or "").lower()
            text_matches = bool(text_value and text_value in text_blob)
            resource_matches = bool(resource_value and resource_value in resource_id)
            if (text_value or resource_value) and not (text_matches or resource_matches):
                continue
            if package_value and package_value not in package_name:
                continue
            matches.append(node)
        return matches

    def window_focus_info(self) -> dict[str, Any]:
        window_result = self.run_adb_command(
            command_name="window_focus_info",
            adb_args=["shell", "dumpsys", "window", "windows"],
            timeout_seconds=max(self.timeout_seconds, 20.0),
            record_stdout_max_chars=16000,
        )
        activity_result = self.run_adb_command(
            command_name="activity_top_info",
            adb_args=["shell", "dumpsys", "activity", "top"],
            timeout_seconds=max(self.timeout_seconds, 20.0),
            record_stdout_max_chars=16000,
        )
        window_stdout = str(window_result.get("stdout") or "")
        activity_stdout = str(activity_result.get("stdout") or "")
        focused_window = ""
        current_activity = ""
        current_package = ""
        visible_activity = ""
        for pattern in (
            r"mCurrentFocus=Window\{[^\}]+\s([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)\}",
            r"mFocusedApp=.*? ([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)",
        ):
            match = re.search(pattern, window_stdout)
            if match:
                focused_window = normalize_ui_text(match.group(1))
                break
        activity_match = re.search(r"\bACTIVITY\s+([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)", activity_stdout)
        if activity_match:
            current_activity = normalize_activity_name(activity_match.group(1))
        visible_matches = re.findall(
            r"Window #\d+ Window\{[^\n]+ ([A-Za-z0-9_.$]+/[A-Za-z0-9_.$]+)\}:[\s\S]{0,1800}?isOnScreen=true[\s\S]{0,200}?isVisible=true",
            window_stdout,
        )
        if visible_matches:
            visible_activity = normalize_activity_name(visible_matches[0])
        if current_activity:
            current_package = current_activity.split("/", 1)[0]
        elif focused_window:
            current_activity = focused_window
            current_package = focused_window.split("/", 1)[0]
        if visible_activity and (not current_activity or "launcher" in current_activity.lower()):
            current_activity = visible_activity
            current_package = current_activity.split("/", 1)[0]
        return {
            "focusedWindow": focused_window,
            "currentActivity": current_activity,
            "currentPackage": current_package,
            "visibleActivity": visible_activity,
            "windowDumpArtifact": window_result.get("commandLogPath"),
            "activityDumpArtifact": activity_result.get("commandLogPath"),
        }

    def classify_ui_source(self, nodes: list[dict[str, Any]], window_info: dict[str, Any]) -> str:
        class_names = " ".join(str(node.get("className") or "") for node in nodes).lower()
        resource_ids = " ".join(str(node.get("resourceId") or "") for node in nodes).lower()
        focused_window = str(window_info.get("focusedWindow") or "")
        current_activity = str(window_info.get("currentActivity") or "")
        has_webview = detect_webview_from_text(class_names, resource_ids, focused_window, current_activity)
        has_compose = "compose" in class_names or "compose" in current_activity.lower()
        clickable_count = sum(1 for node in nodes if node.get("clickable"))
        text_count = sum(1 for node in nodes if node.get("text") or node.get("contentDesc"))
        if has_webview and (clickable_count > 0 or text_count > 0):
            return "hybrid"
        if has_webview:
            return "webview"
        if has_compose:
            return "compose"
        if clickable_count > 0 or text_count > 0:
            return "views"
        return "unknown"

    def evaluate_navigation_mode(
        self,
        source_type: str,
        nodes: list[dict[str, Any]],
        visible_texts: list[str],
        clickable_elements: list[dict[str, Any]],
    ) -> tuple[str, bool, str, float]:
        clickable_count = len(clickable_elements)
        useful_count = clickable_count + len(visible_texts)
        generic_node_count = sum(1 for node in nodes if not node.get("text") and not node.get("contentDesc") and not node.get("resourceId"))
        weak_tree = useful_count < 4 or (nodes and generic_node_count / max(len(nodes), 1) > 0.65)
        if source_type == "webview":
            return "visual", True, "UI tree insuficiente para WebView", 0.35
        if source_type == "hybrid":
            return "hybrid", True, "Tela hibrida com area estruturada e area visual", 0.55
        if source_type == "compose":
            if weak_tree:
                return "hybrid", True, "Compose com arvore de acessibilidade fraca", 0.45
            return "structured", False, "", 0.75
        if source_type == "views":
            if weak_tree:
                return "hybrid", True, "Arvore de UI com poucos elementos confiaveis", 0.6
            return "structured", False, "", 0.9
        if weak_tree:
            return "visual", True, "Arvore de UI vazia ou ambigua", 0.25
        return "structured", False, "", 0.65

    def estimate_screen_bounds(self, nodes: list[dict[str, Any]]) -> dict[str, int] | None:
        right = 0
        bottom = 0
        for node in nodes:
            bounds = node.get("bounds")
            if not isinstance(bounds, dict):
                continue
            right = max(right, int(bounds.get("right") or 0))
            bottom = max(bottom, int(bounds.get("bottom") or 0))
        if right <= 0 or bottom <= 0:
            return None
        return {"left": 0, "top": 0, "right": right, "bottom": bottom, "width": right, "height": bottom}

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

    def app_info(self, package_name: str, verbosity: str = "summary", include_raw_preview: bool = False) -> dict[str, Any]:
        mode = normalize_verbosity(verbosity)
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
        launcher_activities = self.extract_launcher_activities(package_name, stdout)
        requested_permissions = re.findall(r"^\s+(android\.permission\.[A-Z0-9_]+|[A-Za-z0-9_.]+\.permission\.[A-Za-z0-9_]+)$", stdout, flags=re.MULTILINE)
        version_code = first_int_match(r"\bversionCode=(\d+)", stdout)
        version_name_match = re.search(r"\bversionName=([^\s]+)", stdout)
        summary = {
            "success": result["success"],
            "message": "App info captured successfully" if result["success"] else "Failed to capture app info",
            "verbosity": mode,
            "packageName": package_name,
            "installed": package_name in stdout,
            "versionCode": version_code,
            "versionName": version_name_match.group(1) if version_name_match else None,
            "debuggable": "DEBUGGABLE" in stdout,
            "launcherActivity": launcher_activities[0] if launcher_activities else None,
            "launcherActivities": launcher_activities,
            "criticalPermissions": [permission for permission in requested_permissions if permission in {
                "android.permission.MANAGE_EXTERNAL_STORAGE",
                "android.permission.SYSTEM_ALERT_WINDOW",
                "android.permission.QUERY_ALL_PACKAGES",
                "android.permission.ACCESS_FINE_LOCATION",
                "android.permission.ACCESS_BACKGROUND_LOCATION",
                "android.permission.READ_EXTERNAL_STORAGE",
                "android.permission.WRITE_EXTERNAL_STORAGE",
            }],
            "requestedPermissionCount": len(requested_permissions),
            "stdoutOriginalLength": len(stdout),
            "commandLogPath": result.get("commandLogPath"),
            "returnCode": result.get("returnCode"),
            "stderr": result.get("stderr"),
            "adbPath": result.get("adbPath"),
            "deviceSerial": result.get("deviceSerial"),
        }
        if include_raw_preview or mode == "full":
            summary["content_preview"] = tail_preview(stdout, max_lines=120, max_chars=12000)
            summary["contentPreview"] = summary["content_preview"]
        if mode == "full":
            summary["stdout"] = stdout
            summary["argv"] = result.get("argv")
        return summary

    def extract_launcher_activities(self, package_name: str, dumpsys_output: str) -> list[str]:
        launchers: list[str] = []
        for block in re.findall(r"([A-Fa-f0-9]+\s+" + re.escape(package_name) + r"/[A-Za-z0-9_.$]+[\s\S]{0,260}?Category: \"android.intent.category.LAUNCHER\")", dumpsys_output):
            match = re.search(rf"{re.escape(package_name)}/[A-Za-z0-9_.$]+", block)
            if match and match.group(0) not in launchers:
                launchers.append(match.group(0))
        return launchers

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

    def get_logcat(self, include_preview: bool = True, max_preview_chars: int = 8000) -> dict[str, Any]:
        result = self.run_adb_command(command_name="get_logcat", adb_args=["logcat", "-d"], timeout_seconds=max(self.timeout_seconds, 30.0), record_stdout_max_chars=12000)
        logcat_content = str(result.get("stdout") or "")
        logcat_path = self.save_logcat_artifact(logcat_content)
        result.update(
            {
                "path": str(logcat_path.resolve()),
                "logcatPath": str(logcat_path.resolve()),
                "stdoutOriginalLength": len(logcat_content),
                "message": "Logcat captured successfully" if result["success"] else "Failed to capture logcat",
            }
        )
        result.pop("stdout", None)
        if include_preview:
            result["content_preview"] = tail_preview(logcat_content, max_chars=max_preview_chars)
            result["contentPreview"] = result["content_preview"]
        return result

    def detect_known_issues(self, include_preview: bool = False, preview_on_issue: bool = True, max_preview_chars: int = 8000) -> dict[str, Any]:
        result = self.run_adb_command(command_name="detect_known_issues", adb_args=["logcat", "-d"], timeout_seconds=max(self.timeout_seconds, 30.0), record_stdout_max_chars=12000)
        logcat_content = str(result.get("stdout") or "")
        logcat_path = self.save_logcat_artifact(logcat_content)
        detection = detect_logcat_issues(logcat_content)
        has_issue = bool(detection.get("summary"))
        detection.update(
            {
                "success": result["success"],
                "message": "Known issue detection completed" if result["success"] else "Failed to capture logcat for known issue detection",
                "path": str(logcat_path.resolve()),
                "logcatPath": str(logcat_path.resolve()),
                "stdoutOriginalLength": len(logcat_content),
                "lineCount": len(logcat_content.splitlines()),
                "commandLogPath": result.get("commandLogPath"),
                "returnCode": result.get("returnCode"),
                "stderr": result.get("stderr"),
                "adbPath": result.get("adbPath"),
                "deviceSerial": result.get("deviceSerial"),
            }
        )
        if include_preview or (preview_on_issue and has_issue):
            detection["content_preview"] = tail_preview(logcat_content, max_chars=max_preview_chars)
            detection["contentPreview"] = detection["content_preview"]
        return detection

    def save_logcat_artifact(self, content: str) -> Path:
        logcat_path = self.recorder.paths.artifacts_dir / "logcat.txt"
        logcat_path.write_text(content, encoding="utf-8", errors="replace")
        return logcat_path

    def current_update_status(self) -> dict[str, Any]:
        if self._update_status_cache is not None:
            return dict(self._update_status_cache)
        install_mode = self.detect_install_mode()
        state = self.load_update_state()
        payload = {
            "enabled": self.auto_update_enabled,
            "installMode": install_mode,
            "repoUrl": self.update_repo_url,
            "channel": self.update_channel,
            "checkedAt": state.get("checkedAt"),
            "currentRevision": state.get("currentRevision"),
            "targetRevision": state.get("targetRevision"),
            "updateApplied": bool(state.get("updateApplied", False)),
            "restartRequired": bool(state.get("restartRequired", False)),
            "message": state.get("message", "Auto-update not checked in this session"),
            "lastError": state.get("lastError"),
        }
        self._update_status_cache = payload
        return dict(payload)

    def load_update_state(self) -> dict[str, Any]:
        path = self.install_dir / UPDATE_STATE_FILENAME
        if not path.exists():
            return {}
        return load_json_config(path)

    def save_update_state(self, payload: dict[str, Any]) -> None:
        path = self.install_dir / UPDATE_STATE_FILENAME
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._update_status_cache = dict(payload)

    def detect_install_mode(self) -> str:
        return "git" if (self.install_dir / ".git").exists() else "archive"

    def update_venv_python(self) -> Path:
        if os.name == "nt":
            return self.install_dir / ".venv" / "Scripts" / "python.exe"
        return self.install_dir / ".venv" / "bin" / "python"

    def startup_auto_update(self) -> dict[str, Any]:
        install_mode = self.detect_install_mode()
        base_status = {
            "enabled": self.auto_update_enabled,
            "installMode": install_mode,
            "repoUrl": self.update_repo_url,
            "channel": self.update_channel,
            "checkedAt": int(time.time() * 1000),
            "currentRevision": None,
            "targetRevision": None,
            "updateApplied": False,
            "restartRequired": False,
            "message": "Auto-update disabled",
            "lastError": None,
        }
        if not self.auto_update_enabled:
            base_status["currentRevision"] = self.local_install_revision(install_mode)
            self.save_update_state(base_status)
            return base_status
        try:
            if install_mode == "git":
                status = self.update_git_install()
            else:
                status = self.update_archive_install()
        except Exception as exc:
            base_status["message"] = "Auto-update failed"
            base_status["lastError"] = str(exc)
            base_status["currentRevision"] = self.local_install_revision(install_mode)
            self.save_update_state(base_status)
            LOGGER.warning("Auto-update failed: %s", exc)
            return base_status
        self.save_update_state(status)
        if status.get("updateApplied"):
            LOGGER.warning("DroidPilot MCP updated on disk. Restart the MCP client to load the new version.")
        return status

    def local_install_revision(self, install_mode: str | None = None) -> str | None:
        mode = install_mode or self.detect_install_mode()
        if mode == "git":
            try:
                result = self.run_host_command("update_git_head", ["git", "-C", str(self.install_dir), "rev-parse", "HEAD"], timeout_seconds=max(self.timeout_seconds, 30.0))
            except Exception:
                return None
            if result["success"]:
                return str(result.get("stdout") or "").strip() or None
            return None
        return compute_tree_fingerprint(self.install_dir)

    def update_git_install(self) -> dict[str, Any]:
        before = self.local_install_revision("git")
        dirty = self.run_host_command("update_git_status", ["git", "-C", str(self.install_dir), "status", "--porcelain"], timeout_seconds=max(self.timeout_seconds, 30.0))
        if str(dirty.get("stdout") or "").strip():
            return {
                "enabled": True,
                "installMode": "git",
                "repoUrl": self.update_repo_url,
                "channel": self.update_channel,
                "checkedAt": int(time.time() * 1000),
                "currentRevision": before,
                "targetRevision": before,
                "updateApplied": False,
                "restartRequired": False,
                "message": "Auto-update skipped because the installation has local changes",
                "lastError": None,
            }
        self.run_host_command("update_git_remote", ["git", "-C", str(self.install_dir), "remote", "set-url", "origin", self.update_repo_url], timeout_seconds=max(self.timeout_seconds, 30.0))
        fetch = self.run_host_command("update_git_fetch", ["git", "-C", str(self.install_dir), "fetch", "origin", self.update_channel], timeout_seconds=max(self.timeout_seconds, 90.0))
        if not fetch["success"]:
            raise RuntimeError(fetch.get("stderr") or fetch.get("stdout") or "git fetch failed")
        target = self.run_host_command(
            "update_git_target",
            ["git", "-C", str(self.install_dir), "rev-parse", f"origin/{self.update_channel}"],
            timeout_seconds=max(self.timeout_seconds, 30.0),
        )
        target_revision = str(target.get("stdout") or "").strip() or None
        if before and target_revision and before == target_revision:
            return {
                "enabled": True,
                "installMode": "git",
                "repoUrl": self.update_repo_url,
                "channel": self.update_channel,
                "checkedAt": int(time.time() * 1000),
                "currentRevision": before,
                "targetRevision": target_revision,
                "updateApplied": False,
                "restartRequired": False,
                "message": "Installation already up to date",
                "lastError": None,
            }
        checkout = self.run_host_command(
            "update_git_checkout",
            ["git", "-C", str(self.install_dir), "checkout", self.update_channel],
            timeout_seconds=max(self.timeout_seconds, 60.0),
        )
        if not checkout["success"]:
            fallback_checkout = self.run_host_command(
                "update_git_checkout_branch",
                ["git", "-C", str(self.install_dir), "checkout", "-B", self.update_channel, f"origin/{self.update_channel}"],
                timeout_seconds=max(self.timeout_seconds, 60.0),
            )
            if not fallback_checkout["success"]:
                raise RuntimeError(fallback_checkout.get("stderr") or fallback_checkout.get("stdout") or "git checkout failed")
        pull = self.run_host_command(
            "update_git_pull",
            ["git", "-C", str(self.install_dir), "pull", "--ff-only", "origin", self.update_channel],
            timeout_seconds=max(self.timeout_seconds, 120.0),
        )
        if not pull["success"]:
            raise RuntimeError(pull.get("stderr") or pull.get("stdout") or "git pull failed")
        after = self.local_install_revision("git")
        changed = bool(before != after)
        dependency_status = self.reinstall_requirements_if_needed(changed)
        return {
            "enabled": True,
            "installMode": "git",
            "repoUrl": self.update_repo_url,
            "channel": self.update_channel,
            "checkedAt": int(time.time() * 1000),
            "currentRevision": after,
            "targetRevision": target_revision or after,
            "updateApplied": changed,
            "restartRequired": changed,
            "message": "Git installation updated" if changed else "Installation already up to date",
            "lastError": None,
            "dependencyUpdate": dependency_status,
        }

    def update_archive_install(self) -> dict[str, Any]:
        archive_url = update_repo_archive_url(self.update_repo_url, self.update_channel)
        if not archive_url:
            raise RuntimeError("Auto-update por archive atualmente suporta apenas repositorios GitHub")
        local_revision = self.local_install_revision("archive")
        with tempfile.TemporaryDirectory(prefix="droidpilot-update-") as temp_dir_value:
            temp_dir = Path(temp_dir_value)
            archive_path = temp_dir / "droidpilot.zip"
            with urllib.request.urlopen(archive_url, timeout=max(int(self.timeout_seconds), 20)) as response:
                archive_path.write_bytes(response.read())
            with zipfile.ZipFile(archive_path) as archive:
                archive.extractall(temp_dir)
            extracted_dirs = [path for path in temp_dir.iterdir() if path.is_dir()]
            if not extracted_dirs:
                raise RuntimeError("Arquivo de update nao contem um diretorio de projeto valido")
            source_dir = extracted_dirs[0]
            target_revision = compute_tree_fingerprint(source_dir)
            if local_revision == target_revision:
                return {
                    "enabled": True,
                    "installMode": "archive",
                    "repoUrl": self.update_repo_url,
                    "channel": self.update_channel,
                    "checkedAt": int(time.time() * 1000),
                    "currentRevision": local_revision,
                    "targetRevision": target_revision,
                    "updateApplied": False,
                    "restartRequired": False,
                    "message": "Installation already up to date",
                    "lastError": None,
                }
            self.sync_install_tree(source_dir, self.install_dir)
        current_revision = self.local_install_revision("archive")
        dependency_status = self.reinstall_requirements_if_needed(True)
        return {
            "enabled": True,
            "installMode": "archive",
            "repoUrl": self.update_repo_url,
            "channel": self.update_channel,
            "checkedAt": int(time.time() * 1000),
            "currentRevision": current_revision,
            "targetRevision": current_revision,
            "updateApplied": True,
            "restartRequired": True,
            "message": "Archive installation updated",
            "lastError": None,
            "dependencyUpdate": dependency_status,
        }

    def sync_install_tree(self, source_dir: Path, target_dir: Path) -> None:
        for target_path in sorted(target_dir.rglob("*"), reverse=True):
            relative = target_path.relative_to(target_dir)
            if should_preserve_update_path(relative):
                continue
            if not (source_dir / relative).exists():
                if target_path.is_file():
                    target_path.unlink()
                elif target_path.is_dir():
                    try:
                        target_path.rmdir()
                    except OSError:
                        pass
        for source_path in sorted(source_dir.rglob("*")):
            relative = source_path.relative_to(source_dir)
            if should_preserve_update_path(relative):
                continue
            destination = target_dir / relative
            if source_path.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, destination)

    def reinstall_requirements_if_needed(self, installation_changed: bool) -> dict[str, Any]:
        if not installation_changed:
            return {"success": True, "skipped": True, "message": "Dependency refresh not required"}
        venv_python = self.update_venv_python()
        requirements_file = self.install_dir / "requirements.txt"
        if not venv_python.exists() or not requirements_file.exists():
            return {"success": False, "skipped": True, "message": "Venv or requirements not found"}
        result = self.run_host_command(
            "update_requirements",
            [str(venv_python), "-m", "pip", "install", "-r", str(requirements_file)],
            timeout_seconds=max(self.timeout_seconds, 180.0),
        )
        return {
            "success": result["success"],
            "skipped": False,
            "message": "Dependencies updated" if result["success"] else "Dependency update failed",
            "commandLogPath": result.get("commandLogPath"),
            "stderr": result.get("stderr"),
        }

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

    def list_adb_devices(self, adb_path: str | None = None) -> list[dict[str, Any]]:
        path = adb_path or self.require_adb_path()
        completed = subprocess.run(
            [path, "devices", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(self.timeout_seconds, 10.0),
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or "Falha ao listar devices ADB")
        return parse_adb_devices_output(str(completed.stdout))

    def select_adb_device_serial(self, adb_path: str | None = None, fail_on_missing: bool = True) -> str | None:
        if self.adb_device_serial:
            return self.adb_device_serial
        try:
            devices = self.list_adb_devices(adb_path)
        except Exception:
            if fail_on_missing:
                raise
            return None
        online = [device for device in devices if device.get("online")]
        if len(online) == 1:
            return str(online[0]["serial"])
        if not online and devices and fail_on_missing:
            states = ", ".join(f"{device.get('serial')}={device.get('state')}" for device in devices)
            raise RuntimeError(f"Nenhum device ADB online ({states}). Conecte um device ou ajuste adbDeviceSerial.")
        if len(online) > 1 and fail_on_missing:
            serials = ", ".join(str(device["serial"]) for device in online)
            raise RuntimeError(f"Mais de um device ADB online ({serials}). Defina adbDeviceSerial com android_set_adb_config.")
        return None

    def run_host_command(
        self,
        command_name: str,
        argv: list[str],
        *,
        timeout_seconds: float | None = None,
        cwd: Path | None = None,
    ) -> dict[str, Any]:
        request = {
            "transport": "local-process",
            "argv": argv,
            "shellCommand": " ".join(shlex.quote(part) for part in argv),
            "cwd": str((cwd or self.install_dir).resolve()),
        }
        try:
            completed = subprocess.run(
                argv,
                cwd=str((cwd or self.install_dir).resolve()),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds or self.timeout_seconds,
                check=False,
            )
            response = {
                "transport": "local-process",
                "success": completed.returncode == 0,
                "returnCode": completed.returncode,
                "stdout": str(completed.stdout).strip(),
                "stderr": str(completed.stderr).strip(),
                "argv": argv,
                "timestamp": int(time.time() * 1000),
            }
            command_path = self.recorder.record_command(command_name=command_name, request_payload=request, response_payload=response)
            return {
                "success": response["success"],
                "message": "Local command executed" if response["success"] else "Local command failed",
                "commandLogPath": str(command_path.resolve()),
                "request": {"action": command_name},
                **response,
            }
        except Exception as exc:
            command_path = self.recorder.record_command(command_name=command_name, request_payload=request, error_message=str(exc))
            raise RuntimeError(f"{exc} (detalhes: {command_path})") from exc

    def run_remote_listing_command(self, package_name: str, access_mode: str, root_relative: str, remote_root: str) -> dict[str, Any]:
        if access_mode == "run-as":
            return self.run_adb_command(
                command_name="sqlite_list_dir_run_as",
                adb_args=["shell", "run-as", package_name, "ls", "-1", root_relative],
                request_payload={"packageName": package_name, "rootRelativePath": root_relative, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 20.0),
                record_stdout_max_chars=8000,
            )
        if access_mode == "root":
            return self.run_adb_command(
                command_name="sqlite_list_dir_root",
                adb_args=["shell", "su", "-c", f"ls -1 {shlex.quote(remote_root)}"],
                request_payload={"packageName": package_name, "remoteRootPath": remote_root, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 20.0),
                record_stdout_max_chars=8000,
            )
        if access_mode == "external":
            return self.run_adb_command(
                command_name="sqlite_list_dir_external",
                adb_args=["shell", "ls", "-1", remote_root],
                request_payload={"packageName": package_name, "remoteRootPath": remote_root, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 20.0),
                record_stdout_max_chars=8000,
            )
        raise RuntimeError("SQLite access mode indisponivel")

    def pull_remote_sqlite_bundle(
        self,
        *,
        package_name: str,
        access_mode: str,
        root_relative: str,
        remote_root: str,
        database_name: str,
        refresh: bool = False,
    ) -> dict[str, Any]:
        cache_key = f"{access_mode}|{remote_root}|{database_name}"
        if not refresh and cache_key in self._sqlite_bundle_cache:
            cached = self._sqlite_bundle_cache[cache_key]
            database_path = cached.get("databasePath")
            if isinstance(database_path, Path) and database_path.exists():
                return cached
        listing = self.list_remote_sqlite_entries(package_name, access_mode, root_relative, remote_root)
        all_entries = listing.get("allEntries") or listing["entries"]
        if database_name not in listing["databases"] and database_name not in all_entries:
            raise RuntimeError(f"Banco SQLite nao encontrado: {database_name}")
        artifact_dir = self.recorder.paths.artifacts_dir / "sqlite" / f"{sanitize_filename(database_name)}-{int(time.time() * 1000)}"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, Path] = {}
        for filename in [database_name, f"{database_name}-wal", f"{database_name}-shm", f"{database_name}-journal"]:
            if filename not in all_entries:
                continue
            local_path = artifact_dir / filename
            self.copy_remote_sqlite_file(
                package_name=package_name,
                access_mode=access_mode,
                root_relative=root_relative,
                remote_root=remote_root,
                filename=filename,
                local_path=local_path,
            )
            files[filename] = local_path
        if database_name not in files:
            raise RuntimeError(f"Nao foi possivel copiar o banco SQLite {database_name}")
        bundle = {
            "artifactDir": artifact_dir,
            "databasePath": files[database_name],
            "files": files,
        }
        self._sqlite_bundle_cache[cache_key] = bundle
        return bundle

    def copy_remote_sqlite_file(
        self,
        *,
        package_name: str,
        access_mode: str,
        root_relative: str,
        remote_root: str,
        filename: str,
        local_path: Path,
    ) -> None:
        if access_mode == "run-as":
            result = self.run_adb_command(
                command_name="sqlite_pull_file_run_as",
                adb_args=["exec-out", "run-as", package_name, "cat", f"{root_relative}/{filename}"],
                request_payload={"packageName": package_name, "filename": filename, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 30.0),
                decode_stdout=False,
                artifact_path=local_path,
            )
        elif access_mode == "root":
            result = self.run_adb_command(
                command_name="sqlite_pull_file_root",
                adb_args=["exec-out", "su", "-c", f"cat {shlex.quote(f'{remote_root}/{filename}')}"],
                request_payload={"packageName": package_name, "filename": filename, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 30.0),
                decode_stdout=False,
                artifact_path=local_path,
            )
        elif access_mode == "external":
            result = self.run_adb_command(
                command_name="sqlite_pull_file_external",
                adb_args=["exec-out", "cat", f"{remote_root}/{filename}"],
                request_payload={"packageName": package_name, "filename": filename, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 30.0),
                decode_stdout=False,
                artifact_path=local_path,
            )
        else:
            raise RuntimeError("SQLite access mode indisponivel")
        if not result["success"]:
            raise RuntimeError(result.get("stderr") or f"Falha ao copiar {filename} do device")

    def push_local_sqlite_bundle(
        self,
        *,
        package_name: str,
        access_mode: str,
        root_relative: str,
        remote_root: str,
        database_name: str,
        local_dir: Path,
        package_meta: dict[str, Any],
    ) -> dict[str, Any]:
        listing = self.list_remote_sqlite_entries(package_name, access_mode, root_relative, remote_root)
        local_filenames = [name for name in [database_name, f"{database_name}-wal", f"{database_name}-shm", f"{database_name}-journal"] if (local_dir / name).exists()]
        if database_name not in local_filenames:
            raise RuntimeError("Arquivo principal do banco nao existe localmente para write-back")
        timestamp = int(time.time() * 1000)
        backed_up: list[str] = []
        pushed: list[str] = []
        removed_remote: list[str] = []
        existing_remote = set(listing["entries"])
        for filename in [name for name in [database_name, f"{database_name}-wal", f"{database_name}-shm", f"{database_name}-journal"] if name in existing_remote]:
            self.backup_remote_sqlite_file(package_name, access_mode, root_relative, remote_root, filename, timestamp)
            backed_up.append(filename)
        for filename in local_filenames:
            local_path = local_dir / filename
            if access_mode == "run-as":
                self.install_remote_sqlite_file(
                    package_name=package_name,
                    access_mode=access_mode,
                    root_relative=root_relative,
                    remote_root=remote_root,
                    filename=filename,
                    temp_remote="",
                    package_meta=package_meta,
                    local_path=local_path,
                )
                pushed.append(filename)
                continue
            temp_remote = f"/data/local/tmp/droidpilot-{timestamp}-{sanitize_filename(filename)}"
            push_result = self.run_adb_command(
                command_name="sqlite_push_tmp",
                adb_args=["push", str(local_path), temp_remote],
                request_payload={"packageName": package_name, "filename": filename, "tempRemotePath": temp_remote},
                timeout_seconds=max(self.timeout_seconds, 60.0),
            )
            if not push_result["success"]:
                raise RuntimeError(push_result.get("stderr") or push_result.get("stdout") or f"Falha ao subir {filename} para /data/local/tmp")
            try:
                self.install_remote_sqlite_file(
                    package_name=package_name,
                    access_mode=access_mode,
                    root_relative=root_relative,
                    remote_root=remote_root,
                    filename=filename,
                    temp_remote=temp_remote,
                    package_meta=package_meta,
                )
                pushed.append(filename)
            finally:
                self.remove_temp_remote_file(temp_remote)
        for filename in [f"{database_name}-wal", f"{database_name}-shm", f"{database_name}-journal"]:
            if filename in existing_remote and filename not in local_filenames:
                self.remove_remote_sqlite_file(package_name, access_mode, root_relative, remote_root, filename)
                removed_remote.append(filename)
        return {
            "packageName": package_name,
            "databaseName": database_name,
            "backedUpFiles": backed_up,
            "pushedFiles": pushed,
            "removedRemoteFiles": removed_remote,
        }

    def backup_remote_sqlite_file(self, package_name: str, access_mode: str, root_relative: str, remote_root: str, filename: str, timestamp: int) -> None:
        backup_name = f"{filename}.bak-{timestamp}"
        if access_mode == "run-as":
            result = self.run_adb_command(
                command_name="sqlite_backup_remote_run_as",
                adb_args=["shell", "run-as", package_name, "cp", f"{root_relative}/{filename}", f"{root_relative}/{backup_name}"],
                request_payload={"packageName": package_name, "filename": filename, "backupName": backup_name},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )
        else:
            source_path = f"{remote_root}/{filename}"
            backup_path = f"{remote_root}/{backup_name}"
            result = self.run_adb_command(
                command_name="sqlite_backup_remote_root",
                adb_args=["shell", "su", "-c", f"cp {shlex.quote(source_path)} {shlex.quote(backup_path)}"],
                request_payload={"packageName": package_name, "filename": filename, "backupName": backup_name},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )
        if not result["success"]:
            raise RuntimeError(result.get("stderr") or f"Falha ao criar backup remoto de {filename}")

    def install_remote_sqlite_file(
        self,
        *,
        package_name: str,
        access_mode: str,
        root_relative: str,
        remote_root: str,
        filename: str,
        temp_remote: str,
        package_meta: dict[str, Any],
        local_path: Path | None = None,
    ) -> None:
        if access_mode == "run-as":
            if local_path is None:
                raise RuntimeError("local_path eh obrigatorio para write-back via run-as")
            result = self.run_adb_command(
                command_name="sqlite_install_remote_run_as",
                adb_args=["exec-in", "run-as", package_name, "sh", "-c", f"cat > {shlex.quote(f'{root_relative}/{filename}')}"],
                request_payload={"packageName": package_name, "filename": filename, "accessMode": access_mode},
                timeout_seconds=max(self.timeout_seconds, 30.0),
                decode_stdout=False,
                input_bytes=local_path.read_bytes(),
            )
            if not result["success"]:
                raise RuntimeError(result.get("stderr") or f"Falha ao copiar {filename} de volta para o app")
            return
        destination = f"{remote_root}/{filename}"
        result = self.run_adb_command(
            command_name="sqlite_install_remote_root",
            adb_args=["shell", "su", "-c", f"cp {shlex.quote(temp_remote)} {shlex.quote(destination)}"],
            request_payload={"packageName": package_name, "filename": filename, "tempRemotePath": temp_remote},
            timeout_seconds=max(self.timeout_seconds, 30.0),
        )
        if not result["success"]:
            raise RuntimeError(result.get("stderr") or f"Falha ao copiar {filename} de volta para o app")
        app_id = package_meta.get("appId")
        if app_id:
            self.run_adb_command(
                command_name="sqlite_chown_remote_root",
                adb_args=["shell", "su", "-c", f"chown {app_id}:{app_id} {shlex.quote(destination)}"],
                request_payload={"packageName": package_name, "filename": filename, "appId": app_id},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )
            self.run_adb_command(
                command_name="sqlite_chmod_remote_root",
                adb_args=["shell", "su", "-c", f"chmod 600 {shlex.quote(destination)}"],
                request_payload={"packageName": package_name, "filename": filename},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )

    def remove_remote_sqlite_file(self, package_name: str, access_mode: str, root_relative: str, remote_root: str, filename: str) -> None:
        if access_mode == "run-as":
            result = self.run_adb_command(
                command_name="sqlite_remove_remote_run_as",
                adb_args=["shell", "run-as", package_name, "rm", "-f", f"{root_relative}/{filename}"],
                request_payload={"packageName": package_name, "filename": filename},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )
        else:
            destination = f"{remote_root}/{filename}"
            result = self.run_adb_command(
                command_name="sqlite_remove_remote_root",
                adb_args=["shell", "su", "-c", f"rm -f {shlex.quote(destination)}"],
                request_payload={"packageName": package_name, "filename": filename},
                timeout_seconds=max(self.timeout_seconds, 30.0),
            )
        if not result["success"]:
            raise RuntimeError(result.get("stderr") or f"Falha ao remover arquivo remoto {filename}")

    def remove_temp_remote_file(self, temp_remote: str) -> None:
        self.run_adb_command(
            command_name="sqlite_remove_tmp",
            adb_args=["shell", "rm", "-f", temp_remote],
            request_payload={"tempRemotePath": temp_remote},
            timeout_seconds=max(self.timeout_seconds, 20.0),
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
        input_bytes: bytes | None = None,
    ) -> dict[str, Any]:
        adb_path = self.require_adb_path()
        command = [adb_path]
        selected_serial = None if adb_args[:2] == ["devices", "-l"] else self.select_adb_device_serial(adb_path, fail_on_missing=True)
        if selected_serial:
            command.extend(["-s", selected_serial])
        command.extend(adb_args)
        request = {
            "transport": "adb",
            "adbPath": adb_path,
            "deviceSerial": selected_serial,
            "argv": command,
            "shellCommand": " ".join(shlex.quote(part) for part in command),
            **(request_payload or {}),
        }
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                input=input_bytes,
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
                "deviceSerial": selected_serial,
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
            "Use estas tools para operar Android via ADB local, inspecionar SQLite do app alvo, navegar por UI tree quando possivel e recorrer a screenshot quando a tela exigir fallback visual. "
            "Consulte android_ui_context para decidir entre navegacao estruturada e visual."
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

    @mcp.tool(name="android_set_sqlite_config", description="Configura root/politica/default de SQLite em runtime e, por padrao, persiste no config local.", annotations=stateful)
    def android_set_sqlite_config(
        sqlite_root_path: str = "",
        sqlite_root_access_policy: str = "",
        default_database_name: str = "",
        persist: bool = True,
    ) -> dict[str, Any]:
        return runtime.set_sqlite_config(
            sqlite_root_path=sqlite_root_path,
            sqlite_root_access_policy=sqlite_root_access_policy,
            default_database_name=default_database_name,
            persist=persist,
        )

    @mcp.tool(name="android_ui_context", description="Captura a hierarquia atual da UI via uiautomator dump, classifica a tela e indica se o agente deve usar navegacao estruturada ou screenshot.", annotations=read_only)
    def android_ui_context(
        verbosity: str = "summary",
        max_items: int = DEFAULT_TOOL_MAX_ITEMS,
        text_filter: str = "",
        resource_id_filter: str = "",
        package_filter: str = "",
        include_xml: bool = False,
    ) -> dict[str, Any]:
        return runtime.ui_context(
            verbosity=verbosity,
            max_items=max_items,
            text_filter=text_filter,
            resource_id_filter=resource_id_filter,
            package_filter=package_filter,
            include_xml=include_xml,
        )

    @mcp.tool(name="android_sqlite_status", description="Resolve o packageName do projeto ativo e diagnostica o acesso SQLite via run-as/root.", annotations=read_only)
    def android_sqlite_status() -> dict[str, Any]:
        return runtime.sqlite_status(include_databases=False)

    @mcp.tool(name="android_sqlite_list_databases", description="Lista os bancos e arquivos companheiros em databases/ do app alvo.", annotations=read_only)
    def android_sqlite_list_databases() -> dict[str, Any]:
        return runtime.sqlite_list_databases()

    @mcp.tool(name="android_sqlite_pull_database", description="Copia um banco SQLite do app alvo para tests/mcp/<timestamp>/artifacts/sqlite.", annotations=read_only)
    def android_sqlite_pull_database(database_name: str = "", refresh: bool = False) -> dict[str, Any]:
        return runtime.sqlite_pull_database(database_name=database_name, refresh=refresh)

    @mcp.tool(name="android_sqlite_query", description="Executa SQL bruto localmente sobre uma copia do banco e, em caso de mutacao, grava o resultado de volta no app.", annotations=stateful)
    def android_sqlite_query(database_name: str = "", sql: str = "", parameters: list[Any] | None = None, max_rows: int = DEFAULT_SQLITE_MAX_ROWS) -> dict[str, Any]:
        return runtime.sqlite_query(database_name=database_name, sql=sql, parameters=parameters, max_rows=max_rows)

    @mcp.tool(name="android_navigation_guide", description="Retorna a memoria consolidada de navegacao salva por testes anteriores.", annotations=read_only)
    def android_navigation_guide(verbosity: str = "summary", goal: str = "", screen_fingerprint: str = "", max_items: int = DEFAULT_TOOL_MAX_ITEMS) -> dict[str, Any]:
        return runtime.navigation_guide(verbosity=verbosity, goal=goal, screen_fingerprint=screen_fingerprint, max_items=max_items)

    @mcp.tool(name="android_navigation_context", description="Retorna um contexto curto e acionavel para orientar o CLI durante testes, incluindo match por fingerprint e modo de navegacao preferido.", annotations=read_only)
    def android_navigation_context(
        goal: str = "",
        max_items: int = 8,
        screen_fingerprint: str = "",
        current_activity: str = "",
        source_type: str = "",
        navigation_mode: str = "",
    ) -> dict[str, Any]:
        return runtime.navigation_context(
            goal=goal,
            max_items=max_items,
            screen_fingerprint=screen_fingerprint,
            current_activity=current_activity,
            source_type=source_type,
            navigation_mode=navigation_mode,
        )

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

    @mcp.tool(name="android_save_navigation_learning", description="Salva aprendizado estruturado de navegacao do projeto ativo.", annotations=stateful)
    def android_save_navigation_learning(
        screen_name: str = "",
        goal: str = "",
        route: list[str] | None = None,
        visual_cues: list[str] | None = None,
        useful_actions: list[str] | None = None,
        assertions: list[str] | None = None,
        blockers: list[str] | None = None,
        notes: str = "",
        confidence: float = 0.7,
        app_package: str = "",
        source_type: str = "",
        navigation_mode: str = "",
        screen_fingerprint: str = "",
        key_texts: list[str] | None = None,
        key_resource_ids: list[str] | None = None,
        key_content_descs: list[str] | None = None,
        current_activity: str = "",
        focused_window: str = "",
    ) -> dict[str, Any]:
        return runtime.save_navigation_learning(
            screen_name=screen_name,
            goal=goal,
            route=route,
            visual_cues=visual_cues,
            useful_actions=useful_actions,
            assertions=assertions,
            blockers=blockers,
            notes=notes,
            confidence=confidence,
            app_package=app_package,
            source_type=source_type,
            navigation_mode=navigation_mode,
            screen_fingerprint=screen_fingerprint,
            key_texts=key_texts,
            key_resource_ids=key_resource_ids,
            key_content_descs=key_content_descs,
            current_activity=current_activity,
            focused_window=focused_window,
        )

    @mcp.tool(name="android_get_screen", description="Captura a tela atual via ADB screencap e salva a imagem em tests/mcp.", annotations=read_only)
    def android_get_screen(include_base64: bool = False, filename: str | None = None) -> dict[str, Any]:
        return runtime.get_screen(include_base64=include_base64, filename=filename)

    @mcp.tool(name="android_list_apps", description="Lista packages instalados via ADB, com filtro opcional por package.", annotations=read_only)
    def android_list_apps(query: str = "") -> dict[str, Any]:
        return runtime.list_apps(query=query)

    @mcp.tool(name="android_app_info", description="Retorna diagnostico de instalacao e possiveis launchers para um package name.", annotations=read_only)
    def android_app_info(package_name: str, verbosity: str = "summary", include_raw_preview: bool = False) -> dict[str, Any]:
        return runtime.app_info(package_name=package_name, verbosity=verbosity, include_raw_preview=include_raw_preview)

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
    def android_get_logcat(include_preview: bool = True, max_preview_chars: int = 8000) -> dict[str, Any]:
        return runtime.get_logcat(include_preview=include_preview, max_preview_chars=max_preview_chars)

    @mcp.tool(name="android_detect_known_issues", description="Captura logcat via ADB e detecta crashes, ANR, WindowLeaked e excecoes Android conhecidas.", annotations=read_only)
    def android_detect_known_issues(include_preview: bool = False, preview_on_issue: bool = True, max_preview_chars: int = 8000) -> dict[str, Any]:
        return runtime.detect_known_issues(include_preview=include_preview, preview_on_issue=preview_on_issue, max_preview_chars=max_preview_chars)

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
        args.package_name = first_text(config.get("packageName"), os.environ.get("ANDROID_AGENT_PACKAGE_NAME"))
        args.sqlite_root_path = first_text(config.get("sqliteRootPath"), os.environ.get("ANDROID_AGENT_SQLITE_ROOT_PATH"))
        args.sqlite_root_access_policy = first_text(
            config.get("sqliteRootAccessPolicy"),
            os.environ.get("ANDROID_AGENT_SQLITE_ROOT_ACCESS_POLICY"),
        ) or "run-as-then-root"
        args.sqlite_default_database_name = first_text(
            config.get("sqliteDefaultDatabaseName"),
            os.environ.get("ANDROID_AGENT_SQLITE_DEFAULT_DATABASE_NAME"),
        )
        args.auto_update_enabled = normalize_bool(
            first_text(config.get("autoUpdateEnabled"), os.environ.get("ANDROID_AGENT_AUTO_UPDATE_ENABLED")),
            default=False,
        )
        args.update_repo_url = first_text(config.get("updateRepoUrl"), os.environ.get("ANDROID_AGENT_UPDATE_REPO_URL")) or DEFAULT_UPDATE_REPO_URL
        args.update_channel = first_text(config.get("updateChannel"), os.environ.get("ANDROID_AGENT_UPDATE_CHANNEL")) or DEFAULT_UPDATE_CHANNEL
        args.config_path = config_path
    except ValueError as exc:
        parser.error(str(exc))
    if args.package_name and not normalize_package_name(str(args.package_name)):
        parser.error(f"packageName invalido: {args.package_name!r}")
    if args.sqlite_root_access_policy == "auto":
        args.sqlite_root_access_policy = "external" if is_external_sqlite_root(args.sqlite_root_path) else "run-as-then-root"
    if args.sqlite_root_access_policy not in {"run-as-only", "root-only", "run-as-then-root", "external"}:
        parser.error("sqliteRootAccessPolicy deve ser auto, run-as-only, root-only, run-as-then-root ou external")
    if args.sqlite_default_database_name:
        args.sqlite_default_database_name = ensure_safe_database_name(str(args.sqlite_default_database_name))
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
        install_dir=SERVER_DIR,
        configured_package_name=args.package_name,
        sqlite_root_path=args.sqlite_root_path,
        sqlite_root_access_policy=args.sqlite_root_access_policy,
        sqlite_default_database_name=args.sqlite_default_database_name,
        auto_update_enabled=args.auto_update_enabled,
        update_repo_url=args.update_repo_url,
        update_channel=args.update_channel,
    )
    runtime.startup_auto_update()
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
