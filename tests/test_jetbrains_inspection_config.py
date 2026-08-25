from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import parse


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_PROFILE = ROOT / "config" / "jetbrains" / "Mediaforce.xml"
GENERATED_PROFILE = ROOT / ".idea" / "inspectionProfiles" / "Mediaforce.xml"


def _matches(path: str, pattern: str) -> bool:
    path_segments = path.split("/")
    pattern_segments = pattern.split("/")
    memo: dict[tuple[int, int], bool] = {}

    def match(pattern_index: int, path_index: int) -> bool:
        key = (pattern_index, path_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_segments):
            result = path_index == len(path_segments)
        elif pattern_segments[pattern_index] == "**":
            result = match(pattern_index + 1, path_index) or (
                path_index < len(path_segments) and match(pattern_index, path_index + 1)
            )
        else:
            result = (
                path_index < len(path_segments)
                and fnmatch.fnmatchcase(path_segments[path_index], pattern_segments[pattern_index])
                and match(pattern_index + 1, path_index + 1)
            )
        memo[key] = result
        return result

    return match(0, 0)


def _inspection_config() -> dict[str, Any]:
    github = json.loads((ROOT / ".github" / "github.json").read_text())
    return github["qualityGate"]["inspection"]


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    return [path.decode() for path in result.stdout.split(b"\0") if path]


def test_jetbrains_inspection_lanes_are_language_owned() -> None:
    inspection = _inspection_config()

    assert inspection["profile"] == "Mediaforce"
    assert inspection["prepare"] == "bash scripts/prepare-jetbrains-inspection.sh"
    assert inspection["requiredGeneratedState"] == [
        ".venv",
        ".idea/inspectionProfiles/Mediaforce.xml",
        "frontend/.idea/inspectionProfiles/Mediaforce.xml",
        "frontend/node_modules",
        "frontend/.svelte-kit",
    ]

    lanes = {lane["id"]: lane for lane in inspection["lanes"]}
    assert set(lanes) == {"python", "frontend"}
    assert lanes["python"] == {
        "id": "python",
        "ide": "PyCharm",
        "required": True,
        "include": [
            "*.py",
            "bin/**/*.py",
            "mediaforce/**/*.py",
            "scripts/**/*.py",
            "tests/**/*.py",
        ],
    }
    assert lanes["frontend"] == {
        "id": "frontend",
        "ide": "WebStorm",
        "required": False,
        "projectPath": "frontend",
        "include": [
            "frontend/**/*.svelte",
            "frontend/**/*.ts",
            "frontend/**/*.js",
            "frontend/**/*.mjs",
            "frontend/**/*.cjs",
            "frontend/**/*.css",
            "frontend/**/*.html",
            "frontend/package.json",
            "frontend/tsconfig.json",
        ],
        "exclude": [
            "frontend/build/**",
            "frontend/.svelte-kit/**",
            "frontend/node_modules/**",
        ],
    }


def test_all_tracked_source_files_match_exactly_one_lane() -> None:
    lanes = _inspection_config()["lanes"]
    frontend_suffixes = {".svelte", ".ts", ".js", ".mjs", ".cjs", ".css", ".html"}

    source_files = [
        path
        for path in _tracked_files()
        if path.endswith(".py")
        or (
            path.startswith("frontend/")
            and (
                Path(path).suffix in frontend_suffixes
                or path in {"frontend/package.json", "frontend/tsconfig.json"}
            )
        )
    ]

    for path in source_files:
        matching_lanes = [
            lane["id"]
            for lane in lanes
            if any(_matches(path, pattern) for pattern in lane["include"])
            and not any(_matches(path, pattern) for pattern in lane.get("exclude", []))
        ]
        assert matching_lanes == (["frontend"] if path.startswith("frontend/") else ["python"]), path


def test_shared_profile_is_bounded_and_generated_state_is_ignored() -> None:
    profile = parse(CANONICAL_PROFILE).getroot().find("profile")
    assert profile is not None
    assert profile.find("option[@name='myName']").attrib["value"] == "Mediaforce"

    tools = {tool.attrib["class"]: tool.attrib for tool in profile.findall("inspection_tool")}
    assert {name for name, attributes in tools.items() if attributes["enabled"] == "false"} == {
        "ES6ConvertLetToConst",
        "ExceptionCaughtLocallyJS",
        "HtmlUnknownAttribute",
        "JSRemoveUnnecessaryParentheses",
        "SpellCheckingInspection",
    }
    assert {name for name, attributes in tools.items() if attributes["enabled"] == "true"} == {
        "Eslint",
        "HtmlRequiredAltAttribute",
        "JSUnusedGlobalSymbols",
    }

    assert "/inspectionProfiles/Mediaforce.xml" in (ROOT / ".idea" / ".gitignore").read_text().splitlines()
    assert not subprocess.run(
        ["git", "ls-files", "--error-unmatch", GENERATED_PROFILE.relative_to(ROOT)],
        cwd=ROOT,
        capture_output=True,
    ).returncode == 0
    if GENERATED_PROFILE.exists():
        assert GENERATED_PROFILE.read_bytes() == CANONICAL_PROFILE.read_bytes()

    generated_frontend_profile = ROOT / "frontend" / ".idea" / "inspectionProfiles" / "Mediaforce.xml"
    assert "/.idea/" in (ROOT / "frontend" / ".gitignore").read_text().splitlines()
    if generated_frontend_profile.exists():
        assert generated_frontend_profile.read_bytes() == CANONICAL_PROFILE.read_bytes()

    prepare_script = ROOT / "scripts" / "prepare-jetbrains-inspection.sh"
    assert os.access(prepare_script, os.X_OK)


def test_native_frontend_checks_remain_required() -> None:
    quality_gate = json.loads((ROOT / ".github" / "github.json").read_text())["qualityGate"]

    assert quality_gate["typecheck"]["frontend"] == "npm --prefix frontend run check"
    assert quality_gate["lint"]["frontend"] == "npm --prefix frontend run lint"
    assert quality_gate["test"]["frontend"] == "npm --prefix frontend test"
