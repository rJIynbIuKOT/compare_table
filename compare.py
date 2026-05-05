#!/usr/bin/env python3
"""
Build a comparison table of Tantor DB editions across versions.

Reads `config.toml` (versions, editions, paths to conf.json and contrib.json)
and emits two output files:
  - comparison.md   (Markdown, e.g. for git/Gitea/GitHub)
  - comparison.html (HTML for pasting into Confluence)

Rows  : patches (from conf.json), then utils (from conf.json),
        then contrib names (from contrib.json) — union across all editions.
Cols  : (version, edition) pairs in the order specified by config.toml.
Cells : "y" if a patch/util/contrib is present in that edition, empty otherwise.

Works on Python >= 3.11 via stdlib `tomllib`, or on 3.7+ with `tomli` installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from html import escape

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

# Resolve relative paths (config.toml, comparison.md/html) against the
# script's own directory rather than the current working directory.
# Required for "Run as program" launches from the file manager, where
# cwd is typically the home directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))

from itertools import groupby
from pathlib import Path


# conf.json uses 'se1c' / 'certified_2'; contrib.json uses 'se-1c' / 'certified-2'.
# Normalise both to a canonical form for cross-file comparisons.
def _norm(name: str) -> str:
    return name.replace("-", "").replace("_", "").lower()


class ConfigError(Exception):
    """Raised when config.toml or its referenced JSON files are invalid."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s) found in config")


def prompt_use_config_paths() -> bool:
    """Ask the user whether to use JSON paths declared in config.toml.

    Returns True for yes (default — Enter or 'yes'), False for no
    (which means: load conf.json/contrib.json from per-version folders
    next to the script). On non-interactive stdin (EOF), defaults to True.
    """
    question = "Использовать пути json из config.toml? [Y/n]: "
    while True:
        try:
            answer = input(question).strip().lower()
        except EOFError:
            print("(stdin закрыт, используется значение по умолчанию: yes)")
            return True
        if answer in ("", "y", "yes", "д", "да"):
            return True
        if answer in ("n", "no", "н", "нет"):
            return False
        print("  Введите yes / no (или просто Enter для значения по умолчанию).")


def override_paths_with_local_dirs(config: dict, base_dir: Path) -> None:
    """Rewrite each [versions.<ver>].conf/contrib so they point to
    `<base_dir>/<ver>/conf.json` and `<base_dir>/<ver>/contrib.json`.
    Mutates `config` in place."""
    for version, vcfg in (config.get("versions") or {}).items():
        if not isinstance(vcfg, dict):
            continue
        version_dir = base_dir / str(version)
        vcfg["conf"] = str(version_dir / "conf.json")
        vcfg["contrib"] = str(version_dir / "contrib.json")


def load_and_validate(config: dict) -> dict[str, tuple[dict, dict]]:
    """
    Strictly validate config.toml and load every referenced JSON file.

    Checks performed:
      * at least one [versions.*] section is present
      * every section has 'editions' (non-empty list), 'conf', 'contrib'
      * conf and contrib files exist and parse as JSON
      * every edition listed in config.toml is actually present
        in the corresponding conf.json's "editions" object

    All problems are collected and reported at once.
    Returns: {version: (conf_dict, contrib_dict)}.
    """
    errors: list[str] = []
    loaded: dict[str, tuple[dict, dict]] = {}

    versions = config.get("versions") or {}
    if not versions:
        raise ConfigError(["config.toml has no [versions.*] sections"])

    for version, vcfg in versions.items():
        prefix = f"version {version!r}"

        if not isinstance(vcfg, dict):
            errors.append(f"{prefix}: section must be a table")
            continue

        missing = [k for k in ("editions", "conf", "contrib") if k not in vcfg]
        if missing:
            errors.append(f"{prefix}: missing required key(s): {', '.join(missing)}")
            continue

        editions = vcfg["editions"]
        if not isinstance(editions, list) or not editions:
            errors.append(f"{prefix}: 'editions' must be a non-empty list")
            editions = []
        elif not all(isinstance(e, str) for e in editions):
            errors.append(f"{prefix}: 'editions' must contain only strings")
            editions = [e for e in editions if isinstance(e, str)]

        conf_path = Path(vcfg["conf"])
        contrib_path = Path(vcfg["contrib"])

        conf = _try_load_json(conf_path, prefix, "conf", errors)
        contrib = _try_load_json(contrib_path, prefix, "contrib", errors)

        if conf is not None:
            available = conf.get("editions")
            if not isinstance(available, dict):
                errors.append(
                    f"{prefix}: {conf_path} has no 'editions' object at the top level"
                )
            else:
                missing_eds = [e for e in editions if e not in available]
                if missing_eds:
                    errors.append(
                        f"{prefix}: edition(s) {missing_eds} not found in {conf_path} "
                        f"(available: {sorted(available)})"
                    )

        if conf is not None and contrib is not None and not missing and editions:
            loaded[version] = (conf, contrib)

    if errors:
        raise ConfigError(errors)

    return loaded


def _try_load_json(path: Path, prefix: str, label: str, errors: list[str]) -> dict | None:
    """Load JSON from `path`, appending a human-readable error to `errors`
    on failure. Returns the parsed object on success, None otherwise."""
    if not path.is_file():
        errors.append(f"{prefix}: {label}.json not found: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        errors.append(f"{prefix}: cannot parse {path}: {e}")
        return None
    if not isinstance(data, dict):
        errors.append(f"{prefix}: {path} must contain a JSON object at the top level")
        return None
    return data


def collect(config: dict, loaded: dict[str, tuple[dict, dict]]):
    """
    Walk every (version, edition) listed in config.toml and collect:
      columns       — list[(version, edition)] in config order
      cell_features — dict[(version, edition)] -> set of (kind, item) tuples
      patches       — alphabetically sorted list of all patch names
      utils         — alphabetically sorted list of all util names
      contribs      — alphabetically sorted list of all contrib names

    `kind` is one of: "patch", "util", "contrib".
    """
    columns: list[tuple[str, str]] = []
    cell: dict[tuple[str, str], set[tuple[str, str]]] = {}

    patches: set[str] = set()
    utils: set[str] = set()
    contribs: set[str] = set()

    for version, vcfg in config["versions"].items():
        conf, contrib = loaded[version]
        conf_editions = conf.get("editions", {}) or {}
        contrib_items = contrib.get("contrib", []) or []

        for edition in vcfg["editions"]:
            key = (version, edition)
            columns.append(key)
            features: set[tuple[str, str]] = set()

            ed_data = conf_editions.get(edition, {}) or {}

            for patch in ed_data.get("patches", []) or []:
                patches.add(patch)
                features.add(("patch", patch))

            for util_name in (ed_data.get("utils") or {}).keys():
                utils.add(util_name)
                features.add(("util", util_name))

            ed_norm = _norm(edition)
            for item in contrib_items:
                name = item.get("name")
                if not name:
                    continue
                if any(_norm(e) == ed_norm for e in item.get("editions", []) or []):
                    contribs.add(name)
                    features.add(("contrib", name))

            cell[key] = features

    return (
        columns,
        cell,
        sorted(patches, key=str.lower),
        sorted(utils, key=str.lower),
        sorted(contribs, key=str.lower),
    )


def _mark(features: set[tuple[str, str]], kind: str, name: str) -> str:
    return "y" if (kind, name) in features else ""


def render_markdown(columns, cell, patches, utils, contribs) -> str:
    headers = ["Feature"] + [f"{v}/{e}" for v, e in columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]

    def section(title: str, kind: str, names: list[str]) -> None:
        # Section divider row — only the first cell carries text, rest are empty.
        empties = [""] * len(columns)
        lines.append("| " + " | ".join([f"**{title}**", *empties]) + " |")
        for name in names:
            row = [name] + [_mark(cell[c], kind, name) for c in columns]
            lines.append("| " + " | ".join(row) + " |")

    section("patches", "patch", patches)
    section("utils", "util", utils)
    section("contrib", "contrib", contribs)

    return "\n".join(lines) + "\n"


def render_html(columns, cell, patches, utils, contribs) -> str:
    # Group columns by version so the header has a tidy two-row span:
    #   row 1: 18 | 17 | 16 | 15 | 14   (each colspan = number of editions)
    #   row 2: be se se1c | be se se1c certified certified_2 | ...
    groups = [(v, list(g)) for v, g in groupby(columns, key=lambda c: c[0])]
    total_cols = len(columns) + 1

    out: list[str] = []
    out.append('<table border="1" cellpadding="4" cellspacing="0">')
    out.append("<thead>")

    out.append("<tr>")
    out.append('<th rowspan="2">Feature</th>')
    for v, group in groups:
        out.append(f'<th colspan="{len(group)}">{escape(v)}</th>')
    out.append("</tr>")

    out.append("<tr>")
    for _, e in columns:
        out.append(f"<th>{escape(e)}</th>")
    out.append("</tr>")

    out.append("</thead>")
    out.append("<tbody>")

    def section(title: str, kind: str, names: list[str]) -> None:
        out.append(
            f'<tr><th colspan="{total_cols}" '
            f'style="text-align:left;background:#f0f0f0">{escape(title)}</th></tr>'
        )
        for name in names:
            out.append("<tr>")
            out.append(f"<td>{escape(name)}</td>")
            for col in columns:
                m = _mark(cell[col], kind, name)
                out.append(f'<td style="text-align:center">{escape(m)}</td>')
            out.append("</tr>")

    section("patches", "patch", patches)
    section("utils", "util", utils)
    section("contrib", "contrib", contribs)

    out.append("</tbody>")
    out.append("</table>")
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--md-out", type=Path, default=Path("comparison.md"),
        help="output Markdown file (default: ./comparison.md)",
    )
    parser.add_argument(
        "--html-out", type=Path, default=Path("comparison.html"),
        help="output HTML file (default: ./comparison.html)",
    )
    args = parser.parse_args(argv)

    if not args.config.is_file():
        print(f"error: config file not found: {args.config}", file=sys.stderr)
        return 2

    try:
        with args.config.open("rb") as f:
            config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"error: cannot parse {args.config}: {e}", file=sys.stderr)
        return 2

    use_config_paths = prompt_use_config_paths()
    if use_config_paths:
        print("источник json: пути из config.toml", file=sys.stderr)
    else:
        script_dir = Path(__file__).resolve().parent
        override_paths_with_local_dirs(config, script_dir)
        print(
            f"источник json: папки версий рядом со скриптом ({script_dir})",
            file=sys.stderr,
        )

    try:
        loaded = load_and_validate(config)
    except ConfigError as e:
        print(
            f"error: config validation failed ({len(e.errors)} problem(s)):",
            file=sys.stderr,
        )
        for msg in e.errors:
            print(f"  - {msg}", file=sys.stderr)
        return 2

    columns, cell, patches, utils, contribs = collect(config, loaded)

    args.md_out.write_text(
        render_markdown(columns, cell, patches, utils, contribs),
        encoding="utf-8",
    )
    args.html_out.write_text(
        render_html(columns, cell, patches, utils, contribs),
        encoding="utf-8",
    )

    print(
        f"OK: {len(columns)} columns, "
        f"{len(patches)} patches + {len(utils)} utils + {len(contribs)} contribs",
        file=sys.stderr,
    )
    print(f"  -> {args.md_out}", file=sys.stderr)
    print(f"  -> {args.html_out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    code = 0
    try:
        code = main()
    except SystemExit as e:
        code = int(e.code) if isinstance(e.code, int) else 1
    except BaseException:
        print("\nОшибка при выполнении:", file=sys.stderr)
        traceback.print_exc()
        code = 1

    try:
        input("\nНажмите Enter для закрытия терминала...")
    except EOFError:
        pass

    sys.exit(code)
