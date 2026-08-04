"""Fail a release when local state or obvious credentials would be published."""

from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "__pycache__", ".pytest_cache", "dist", "build"}
SKIP_FILES = {"fingerprint_profiles.json", "standalone_config.json"}
TEXT_SUFFIXES = {
    ".cmd",
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".py",
    ".toml",
    ".txt",
    ".vbs",
    ".yml",
    ".yaml",
}

FORBIDDEN_TRACKED_NAMES = [
    re.compile(r"(?i)(^|/)\.env($|\.)"),
    re.compile(r"(?i)\.har$"),
    re.compile(r"(?i)\.bak$"),
    re.compile(r"(?i)(^|/)fingerprint_profiles\.json(?:\.|$)"),
    re.compile(r"(?i)(^|/)standalone_config\.json$"),
]

SECRET_PATTERNS = {
    "private key": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"
    ),
    "secret Stripe key": re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{12,}\b"),
    "GitHub token": re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}\b"),
    "GitHub fine-grained token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    "Slack token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{12,}\b"),
    "JWT-like credential": re.compile(
        r"\beyJ[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{18,}\.[A-Za-z0-9_-]{10,}\b"
    ),
    "literal bearer credential": re.compile(
        r"(?i)bearer\s+(?!\{|<|TOKEN\b|fixture-)[A-Za-z0-9._~-]{20,}"
    ),
    "credentialed proxy URL": re.compile(
        r"(?i)\b(?:https?|socks5?)://(?!\{|user:pass@|username:password@)"
        r"[^\s/@:]+:[^\s/@]+@[^\s/]+"
    ),
}


def source_files() -> list[Path]:
    files: list[Path] = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.name in SKIP_FILES or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        files.append(path)
    return sorted(files)


def main() -> int:
    files = source_files()
    failures: list[str] = []
    for path in files:
        relative = path.relative_to(ROOT).as_posix()
        if any(pattern.search(relative) for pattern in FORBIDDEN_TRACKED_NAMES):
            failures.append(f"forbidden release file: {relative}")
            continue
        content = path.read_text(encoding="utf-8-sig", errors="replace")
        for label, pattern in SECRET_PATTERNS.items():
            for match in pattern.finditer(content):
                line = content.count("\n", 0, match.start()) + 1
                failures.append(f"{relative}:{line}: possible {label}")

    if failures:
        print("RELEASE CHECK FAILED")
        print("\n".join(failures))
        return 1

    print(f"RELEASE CHECK PASS ({len(files)} public text files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
