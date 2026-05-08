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
import re
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


def _prompt_yes_no(question: str, *, default: bool = True) -> bool:
    """Ask `question` until the user provides a recognised yes/no answer.

    Empty input picks `default`. On non-interactive stdin (EOF) the function
    also falls back to `default` so the script still works under cron / CI."""
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            answer = input(f"{question} {suffix}: ").strip().lower()
        except EOFError:
            print(f"(stdin закрыт, используется значение по умолчанию: "
                  f"{'yes' if default else 'no'})")
            return default
        if answer == "":
            return default
        if answer in ("y", "yes", "д", "да"):
            return True
        if answer in ("n", "no", "н", "нет"):
            return False
        print("  Введите yes / no (или просто Enter для значения по умолчанию).")


def prompt_use_config_paths() -> bool:
    """Ask whether to use JSON paths declared in config.toml.

    Returns True for yes (default), False for no — meaning load
    conf.json/contrib.json from per-version folders next to the script."""
    return _prompt_yes_no("Использовать пути json из config.toml?", default=True)


def prompt_add_descriptions() -> bool:
    """Ask whether to add the Description column populated from
    descriptions.toml. Returns True for yes (default), False to skip both
    loading the file and rendering the column entirely."""
    return _prompt_yes_no("Добавлять столбец Description с описаниями?", default=True)


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


# Maps internal `kind` ("patch"/"util"/"contrib") to a TOML section name.
# `descriptions.toml` uses plural section names matching the table headings.
_DESC_SECTION = {"patch": "patches", "util": "utils", "contrib": "contrib"}


def load_descriptions(path: Path) -> dict[str, dict[str, str]]:
    """Load human-readable descriptions for patches/utils/contrib from a TOML
    file. Returns a dict keyed by internal `kind` -> {name: description}.

    The file is optional. If it does not exist, an empty mapping is returned
    so the Description column is rendered as blank for every row.

    Expected layout (all sections optional):

        [patches]
        "fix/common"         = "Описание патча"
        "func/jit"           = '''Многострочное
        описание тоже работает.'''

        [utils]
        "data_generator"     = "Описание утилиты"

        [contrib]
        "pg_cron"            = "Описание расширения"
    """
    empty: dict[str, dict[str, str]] = {k: {} for k in _DESC_SECTION}
    if not path.is_file():
        print(f"описания: файл не найден ({path}), столбец Description будет пустым",
              file=sys.stderr)
        return empty

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"описания: не удалось распарсить {path}: {e} — столбец будет пустым",
              file=sys.stderr)
        return empty

    result: dict[str, dict[str, str]] = {k: {} for k in _DESC_SECTION}
    for kind, section in _DESC_SECTION.items():
        section_data = data.get(section) or {}
        if not isinstance(section_data, dict):
            print(f"описания: секция [{section}] в {path} должна быть таблицей — пропущено",
                  file=sys.stderr)
            continue
        for name, desc in section_data.items():
            if isinstance(desc, str):
                result[kind][name] = desc
            else:
                print(f"описания: [{section}].{name!r} должен быть строкой — пропущено",
                      file=sys.stderr)
    return result


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


# Shown in the Description column whenever a row has no entry in
# descriptions.toml (or its value is an empty string).
MISSING_DESCRIPTION = "Нет описания в descriptions.toml"


def _describe(descriptions: dict[str, dict[str, str]], kind: str, name: str) -> str:
    """Return the description for (kind, name) or a placeholder if it is
    missing or empty in descriptions.toml."""
    return descriptions.get(kind, {}).get(name, "") or MISSING_DESCRIPTION


def _md_escape(s: str) -> str:
    """Make a string safe for inclusion in a single Markdown table cell:
    escape pipe characters and collapse newlines to <br> so the row stays
    on one line."""
    return s.replace("|", "\\|").replace("\r\n", "\n").replace("\n", "<br>")


# Markdown → HTML helpers for the Description column.
#
# Descriptions in descriptions.toml may use a small Markdown subset:
#   * `inline code`            -> <code>...</code>
#   * [link text](https://...) -> <a href="...">link text</a>
#   * literal newlines (from   -> <br>
#     '''...''' TOML strings)
#
# The Markdown output simply re-uses the user's Markdown as-is (only `|` and
# newlines get rewritten so the table stays valid), so this conversion runs
# only when rendering comparison.html.
_MD_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")
_MD_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")


def _md_to_html(s: str) -> str:
    """Render a description (small Markdown subset) as a fragment of HTML.

    The whole input is HTML-escaped first, so any literal `<`, `>`, `&`, `"`
    in user text show up as entities. Then `[text](url)` and `` `code` ``
    are rewritten into the corresponding tags, and remaining newlines are
    turned into `<br/>` (self-closing XHTML form — required by Confluence,
    which is strict about unclosed tags) so multi-line TOML values keep
    visible line breaks in the browser.
    """
    s = escape(s)
    s = _MD_LINK_RE.sub(r'<a href="\2">\1</a>', s)
    s = _MD_INLINE_CODE_RE.sub(r"<code>\1</code>", s)
    s = s.replace("\r\n", "\n").replace("\n", "<br/>")
    return s


def render_markdown(columns, cell, patches, utils, contribs,
                    descriptions, with_descriptions: bool) -> str:
    headers = ["Feature"]
    if with_descriptions:
        headers.append("Description")
    headers += [f"{v}/{e}" for v, e in columns]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join(["---"] * len(headers)) + "|",
    ]
    # Number of empty cells in a section divider row: one for every column
    # after Feature (Description, if present, plus one per (version, edition)).
    empty_count = len(headers) - 1

    def section(title: str, kind: str, names: list[str]) -> None:
        empties = [""] * empty_count
        lines.append("| " + " | ".join([f"**{title}**", *empties]) + " |")
        for name in names:
            row = [name]
            if with_descriptions:
                row.append(_md_escape(_describe(descriptions, kind, name)))
            row += [_mark(cell[c], kind, name) for c in columns]
            lines.append("| " + " | ".join(row) + " |")

    section("patches", "patch", patches)
    section("utils", "util", utils)
    section("contrib", "contrib", contribs)

    return "\n".join(lines) + "\n"


def render_html(columns, cell, patches, utils, contribs,
                descriptions, with_descriptions: bool) -> str:
    # Group columns by version so the header has a tidy two-row span:
    #   row 1: 18 | 17 | 16 | 15 | 14   (each colspan = number of editions)
    #   row 2: be se se1c | be se se1c certified certified_2 | ...
    groups = [(v, list(g)) for v, g in groupby(columns, key=lambda c: c[0])]
    # Fixed (rowspan-2) columns: Feature, optionally Description.
    fixed_cols = 2 if with_descriptions else 1
    total_cols = len(columns) + fixed_cols

    out: list[str] = []
    out.append('<table border="1" cellpadding="4" cellspacing="0">')
    out.append("<thead>")

    out.append("<tr>")
    out.append('<th rowspan="2">Feature</th>')
    if with_descriptions:
        out.append('<th rowspan="2">Description</th>')
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
            if with_descriptions:
                desc = _describe(descriptions, kind, name)
                out.append(f"<td>{_md_to_html(desc)}</td>")
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
    parser.add_argument(
        "--descriptions", type=Path, default=Path("descriptions.toml"),
        help="path to TOML file with descriptions for the Description column "
             "(default: ./descriptions.toml; missing file is OK)",
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

    add_descriptions = prompt_add_descriptions()
    print(
        f"столбец Description: {'включён' if add_descriptions else 'отключён'}",
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
    # Load descriptions only if the column is actually going to be rendered
    # — saves a file read and avoids a misleading "file not found" warning
    # when the user explicitly opted out of the Description column.
    descriptions = load_descriptions(args.descriptions) if add_descriptions else {}

    args.md_out.write_text(
        render_markdown(columns, cell, patches, utils, contribs,
                        descriptions, add_descriptions),
        encoding="utf-8",
    )
    args.html_out.write_text(
        render_html(columns, cell, patches, utils, contribs,
                    descriptions, add_descriptions),
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
