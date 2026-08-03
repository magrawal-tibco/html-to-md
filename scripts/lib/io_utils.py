"""
io_utils.py — Shared I/O helpers for the html-to-md pipeline.

Canonical implementations of:
  load_settings    — read config/settings.yaml
  load_manifest    — read manifests/manifest_<phase>.json
  parse_frontmatter  — parse YAML frontmatter from a string
  read_frontmatter   — read .md file and return (fm_dict, body)
  format_frontmatter — serialize (fm, body) back to a string
  write_frontmatter  — serialize and write frontmatter to a .md file in-place

Usage:
  from scripts.lib.io_utils import load_settings, load_manifest
  from scripts.lib.io_utils import parse_frontmatter, read_frontmatter, write_frontmatter
"""

import json
from pathlib import Path

import yaml


def load_settings(config_path: str) -> dict:
    try:
        return yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise FileNotFoundError(f"Settings file not found: {config_path}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"Malformed settings file {config_path}: {exc}") from exc


def load_manifest(phase: str, settings: dict) -> list[dict]:
    manifests_dir = Path(settings.get("manifests_dir", "manifests"))
    path = manifests_dir / f"manifest_{phase}.json"
    if not path.exists():
        raise FileNotFoundError(f"Manifest not found: {path}. Run Step 1 first.")
    return json.loads(path.read_text(encoding="utf-8"))


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML frontmatter from a string. Returns (fm_dict, body_text)."""
    if not content.startswith("---"):
        return {}, content
    end = content.find("\n---\n", 3)
    if end == -1:
        return {}, content
    try:
        fm = yaml.safe_load(content[3:end]) or {}
    except yaml.YAMLError:
        fm = {}
    return fm, content[end + 5:]


def read_frontmatter(md_path: Path) -> tuple[dict, str]:
    """Read a .md file and return (fm_dict, body_text). Returns ({}, "") on I/O error."""
    try:
        content = md_path.read_text(encoding="utf-8")
    except Exception:
        return {}, ""
    return parse_frontmatter(content)


def format_frontmatter(fm: dict, body: str) -> str:
    """Serialize (fm_dict, body) to a YAML-frontmatter string without writing to disk."""
    fm_text = yaml.dump(fm, allow_unicode=True, default_flow_style=False)
    return f"---\n{fm_text}---\n\n{body.lstrip()}"


def write_frontmatter(md_path: Path, fm: dict, body: str) -> None:
    """Write updated frontmatter + body back to a .md file in-place."""
    md_path.write_text(format_frontmatter(fm, body), encoding="utf-8")
