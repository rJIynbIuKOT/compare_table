#!/usr/bin/env python3
"""
Build an Example.json-style features matrix from descriptions.json + per-version conf.json.

Reads:
  - config.toml          — versions, editions, paths to conf.json/contrib.json,
                           plus [edition_display_names] (short → long mapping)
  - descriptions.json    — groups → features (name, tech, description, doc_url)

Emits:
  - Example.json         — same shape: {versions, editions, groups[*].features[*].matrix}

For every feature, `matrix` is computed by walking every (version, edition) pair
declared in config.toml and checking whether the feature's `tech` is present in
that cell's patches (conf.json), utils (conf.json) or contribs (contrib.json).

Differences vs compare.py:
  - the output groups features by descriptions.json semantics (no separate
    "patches"/"utils"/"contrib" sections);
  - descriptions.json (not descriptions.toml) is the source of human-readable
    text and is REQUIRED — the script fails if it is missing.

Works on Python >= 3.11 via stdlib tomllib, or on 3.7+ with `tomli` installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections import OrderedDict
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore[no-redef]

# Resolve relative paths against the script's own directory rather than the
# current working directory — required for "Run as program" launches from the
# file manager, where cwd is typically the home directory.
os.chdir(os.path.dirname(os.path.abspath(__file__)))


# conf.json uses 'se1c' / 'certified_2'; contrib.json uses 'se-1c' / 'certified-2'.
# Normalise both to a canonical form for cross-file comparisons.
def _norm(name: str) -> str:
    return name.replace("-", "").replace("_", "").lower()


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"{len(errors)} problem(s) found in config")


def _prompt_yes_no(question: str, *, default: bool = True) -> bool:
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
    return _prompt_yes_no("Использовать пути json из config.toml?", default=True)


def override_paths_with_local_dirs(config: dict, base_dir: Path) -> None:
    """Rewrite each [versions.<ver>].conf/contrib to point at the per-version
    folder next to the script (<base_dir>/<ver>/...). Mutates `config`."""
    for version, vcfg in (config.get("versions") or {}).items():
        if not isinstance(vcfg, dict):
            continue
        version_dir = base_dir / str(version)
        vcfg["conf"] = str(version_dir / "conf.json")
        vcfg["contrib"] = str(version_dir / "contrib.json")


def _try_load_json(path: Path, prefix: str, label: str, errors: list[str]) -> dict | None:
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


def load_and_validate(config: dict) -> dict[str, tuple[dict, dict]]:
    """Validate config.toml structure and load every referenced JSON file.
    Returns {version: (conf_dict, contrib_dict)}."""
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


def _get_aliases(config: dict) -> dict[str, str]:
    """Read [tech_aliases] from config.toml. Returns {alias: canonical}."""
    raw = config.get("tech_aliases") or {}
    if not isinstance(raw, dict):
        return {}
    aliases: dict[str, str] = {}
    for alias, canonical in raw.items():
        if isinstance(alias, str) and isinstance(canonical, str) and alias and canonical:
            aliases[alias] = canonical
    return aliases


def _apply_alias(name: str, aliases: dict[str, str]) -> str:
    return aliases.get(name, name)


def _get_ignored_tech(config: dict, aliases: dict[str, str]) -> set[str]:
    """Read [ignored_tech] from config.toml and return the set of tech
    names that the script should treat as 'not a real feature'.
    Names are normalised through `aliases` so the user can list either
    the raw name from conf.json or its canonical alias target."""
    raw = config.get("ignored_tech") or {}
    if not isinstance(raw, dict):
        return set()
    return {_apply_alias(name, aliases) for name in raw if isinstance(name, str) and name}


def collect_cell_tech(config: dict,
                      loaded: dict[str, tuple[dict, dict]]) -> dict[tuple[str, str], set[str]]:
    """For every (version, edition) pair declared in config.toml, build the
    set of tech names available there — union of:
      * patches  (conf.json:editions[ed].patches)
      * utils    (conf.json:editions[ed].utils keys)
      * contribs (contrib.json:contrib[*].name where editions match)
    Names are normalised via [tech_aliases] from config.toml so that aliased
    names (e.g. func/xid64mark1) collapse onto their canonical form
    (e.g. func/xid64) before matrix lookup.
    """
    aliases = _get_aliases(config)
    cell_tech: dict[tuple[str, str], set[str]] = {}
    for version, vcfg in config["versions"].items():
        conf, contrib = loaded[version]
        conf_editions = conf.get("editions", {}) or {}
        contrib_items = contrib.get("contrib", []) or []
        for edition in vcfg["editions"]:
            tech: set[str] = set()
            ed_data = conf_editions.get(edition, {}) or {}

            for p in ed_data.get("patches", []) or []:
                tech.add(_apply_alias(p, aliases))
            for u in (ed_data.get("utils") or {}).keys():
                tech.add(_apply_alias(u, aliases))

            ed_norm = _norm(edition)
            for item in contrib_items:
                name = item.get("name")
                if not name:
                    continue
                if any(_norm(e) == ed_norm for e in item.get("editions", []) or []):
                    tech.add(_apply_alias(name, aliases))

            cell_tech[(version, edition)] = tech
    return cell_tech


def _pg_sort_key(k: str) -> int:
    """Sort `pg-<N>` keys by descending major version. Non-numeric tail → 0."""
    tail = k.split("-", 1)[1] if "-" in k else ""
    try:
        return -int(tail)
    except ValueError:
        return 0


def build_output(config: dict,
                 loaded: dict[str, tuple[dict, dict]],
                 descriptions: dict) -> dict:
    """Assemble the Example.json-shaped output: versions, editions, groups[*]
    with `matrix` per feature.

    Per-feature `matrix` is composed of two layers:
      1) computed `"<version>-<edition_long_name>"` keys based on
         (version, edition) cells from conf.json/contrib.json AND on
         `"pg-NN"` markers in the source matrix (see below);
      2) any extra keys preserved from descriptions.json's existing
         `matrix` (notably `"pg-NN"` flags themselves). Such keys are
         never overwritten or dropped — the script only reshapes (1).

    Computed keys come first (config order: newest version → oldest,
    editions in their order under the version). Preserved keys come
    after: `pg-*` sorted by descending version, then anything else in
    its original order.

    `pg-NN: true` semantics: "feature is already in upstream PostgreSQL
    NN", therefore every Tantor edition of major version NN automatically
    has the feature. Concretely, when the source matrix has `pg-NN: true`
    and NN is a version declared in config.toml, the script marks every
    `<NN>-<edition>` cell as `true` regardless of what conf.json says.
    """
    cell_tech = collect_cell_tech(config, loaded)
    display: dict[str, str] = config.get("edition_display_names") or {}

    versions = list(config["versions"].keys())
    known_versions = set(versions)

    editions_out: "OrderedDict[str, list[str]]" = OrderedDict()
    for v in versions:
        vcfg = config["versions"][v]
        editions_out[v] = [display.get(ed, ed) for ed in vcfg["editions"]]

    def _is_recomputed_key(k: str) -> bool:
        """`<known_version>-<anything>` — то, что мы пересчитываем сами."""
        if not isinstance(k, str) or "-" not in k:
            return False
        ver, _, rest = k.partition("-")
        return ver in known_versions and bool(rest)

    def _pg_promoted_versions(source_matrix: dict) -> set[str]:
        """Versions N for which `pg-N: true` in source matrix means
        'feature is in upstream PG N, so every Tantor N edition has it'.
        Only versions declared in config.toml count; foreign `pg-N`
        markers (e.g. future `pg-19`) are silently kept as preserved
        but don't expand into edition cells."""
        out: set[str] = set()
        for k, v in source_matrix.items():
            if not v or not isinstance(k, str) or not k.startswith("pg-"):
                continue
            tail = k.split("-", 1)[1]
            if tail in known_versions:
                out.add(tail)
        return out

    groups_out: list[OrderedDict] = []
    for g in descriptions.get("groups", []) or []:
        new_g: OrderedDict = OrderedDict()
        new_g["name"] = g["name"]
        features_out: list[OrderedDict] = []
        for f in g.get("features", []) or []:
            tech = f.get("tech", "") or ""
            source_matrix = f.get("matrix") or {}
            pg_versions = _pg_promoted_versions(source_matrix)

            # 1. Compute fresh (version, edition) keys: a cell is set to
            #    true if the feature's tech is present there OR if the
            #    whole version was promoted via `pg-<N>: true`.
            computed: "OrderedDict[str, bool]" = OrderedDict()
            for v in versions:
                promoted = v in pg_versions
                for ed_short in config["versions"][v]["editions"]:
                    in_tech = bool(tech) and tech in cell_tech.get((v, ed_short), set())
                    if in_tech or promoted:
                        long_name = display.get(ed_short, ed_short)
                        computed[f"{v}-{long_name}"] = True

            # 2. Carry forward anything else the source matrix has.
            preserved_items = [
                (k, v) for k, v in source_matrix.items()
                if not _is_recomputed_key(k)
            ]
            pg_items = sorted(
                (kv for kv in preserved_items if kv[0].startswith("pg-")),
                key=lambda kv: _pg_sort_key(kv[0]),
            )
            other_items = [kv for kv in preserved_items if not kv[0].startswith("pg-")]

            matrix: "OrderedDict[str, bool]" = OrderedDict()
            matrix.update(computed)
            matrix.update(pg_items)
            matrix.update(other_items)

            nf: OrderedDict = OrderedDict()
            nf["name"] = f["name"]
            nf["tech"] = tech
            nf["description"] = f.get("description", "")
            nf["matrix"] = matrix
            if f.get("doc_url"):
                nf["doc_url"] = f["doc_url"]
            features_out.append(nf)
        new_g["features"] = features_out
        groups_out.append(new_g)

    result: OrderedDict = OrderedDict()
    result["versions"] = versions
    result["editions"] = editions_out
    result["groups"] = groups_out
    return result


def _collect_referenced_tech(descriptions: dict) -> set[str]:
    """All non-empty feature.tech strings from descriptions.json."""
    referenced: set[str] = set()
    for g in descriptions.get("groups", []) or []:
        for f in g.get("features", []) or []:
            t = f.get("tech")
            if isinstance(t, str) and t:
                referenced.add(t)
    return referenced


def _iter_tech_names_for_kind(conf: dict, contrib: dict, kind: str):
    """Yield tech names for the requested source kind:
      * "patches"  — every entry of editions[*].patches in conf.json
      * "utils"    — every key of editions[*].utils in conf.json
      * "contribs" — every contrib[*].name in contrib.json
    """
    if kind == "patches":
        for ed in (conf.get("editions", {}) or {}).values():
            for p in (ed.get("patches", []) or []):
                yield p
    elif kind == "utils":
        for ed in (conf.get("editions", {}) or {}).values():
            for u in (ed.get("utils") or {}).keys():
                yield u
    elif kind == "contribs":
        for item in (contrib.get("contrib", []) or []):
            name = item.get("name")
            if name:
                yield name
    else:
        raise ValueError(f"unknown tech kind: {kind!r}")


def list_tech_missing_from_descriptions(
    config: dict,
    loaded: dict[str, tuple[dict, dict]],
    descriptions: dict,
    *,
    kind: str,
    version_keys: list[str] | None = None,
) -> list[tuple[str, list[str]]]:
    """Across the requested versions, return every tech name of the given
    `kind` (patches / utils / contribs) that is NOT referenced by any
    feature.tech in descriptions.json.

    Aliased names from [tech_aliases] are collapsed onto their canonical
    form before comparison; names listed in [ignored_tech] are dropped
    entirely (treated as 'not a real feature').

    Returns a sorted list of (tech_name, [versions_where_present]).
    The version list inside each tuple follows config.toml's order.
    If `version_keys` is None, every loaded version is scanned.
    """
    aliases = _get_aliases(config)
    ignored = _get_ignored_tech(config, aliases)
    referenced = _collect_referenced_tech(descriptions)

    config_order = list(config["versions"].keys())
    if version_keys is None:
        version_keys = [v for v in config_order if v in loaded]
    else:
        version_keys = [v for v in version_keys if v in loaded]

    # tech_name -> ordered list of versions it appears in (within scope)
    per_tech_versions: dict[str, list[str]] = {}
    for v in version_keys:
        conf, contrib = loaded[v]
        seen_here: set[str] = set()
        for name in _iter_tech_names_for_kind(conf, contrib, kind):
            canon = _apply_alias(name, aliases)
            if canon in referenced or canon in ignored or canon in seen_here:
                continue
            seen_here.add(canon)
            per_tech_versions.setdefault(canon, []).append(v)

    return sorted(per_tech_versions.items(), key=lambda kv: kv[0])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument(
        "--config", type=Path, default=Path("config.toml"),
        help="path to config.toml (default: ./config.toml)",
    )
    parser.add_argument(
        "--descriptions", type=Path, default=Path("descriptions.json"),
        help="path to descriptions.json (default: ./descriptions.json)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("Example.json"),
        help="output JSON file (default: ./Example.json)",
    )
    parser.add_argument(
        "--missing-from", type=str, default="",
        help="comma-separated version keys to report missing patches for "
             "(default: all versions from config.toml)",
    )
    args = parser.parse_args(argv)

    if not args.config.is_file():
        print(f"error: config file not found: {args.config}", file=sys.stderr)
        return 2
    if not args.descriptions.is_file():
        print(f"error: descriptions.json not found: {args.descriptions}", file=sys.stderr)
        return 2

    try:
        with args.config.open("rb") as f:
            config = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"error: cannot parse {args.config}: {e}", file=sys.stderr)
        return 2

    try:
        descriptions = json.loads(args.descriptions.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"error: cannot parse {args.descriptions}: {e}", file=sys.stderr)
        return 2
    if not isinstance(descriptions, dict):
        print(f"error: {args.descriptions} must contain a JSON object", file=sys.stderr)
        return 2

    use_config_paths = prompt_use_config_paths()
    if use_config_paths:
        print("источник json: пути из config.toml", file=sys.stderr)
    else:
        script_dir = Path(__file__).resolve().parent
        override_paths_with_local_dirs(config, script_dir)
        print(f"источник json: папки версий рядом со скриптом ({script_dir})",
              file=sys.stderr)

    try:
        loaded = load_and_validate(config)
    except ConfigError as e:
        print(f"error: config validation failed ({len(e.errors)} problem(s)):",
              file=sys.stderr)
        for msg in e.errors:
            print(f"  - {msg}", file=sys.stderr)
        return 2

    output = build_output(config, loaded, descriptions)
    args.out.write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    # Required by spec: across every requested version, list tech names
    # present in conf.json / contrib.json that are NOT referenced by any
    # feature.tech in descriptions.json. Reported per kind so it's clear
    # what exactly is missing.
    requested = [v.strip() for v in args.missing_from.split(",") if v.strip()] or None
    scope = ", ".join(requested) if requested else "все версии из config.toml"
    ignored_count = len(_get_ignored_tech(config, _get_aliases(config)))
    suffix = f", игнорируется по [ignored_tech]: {ignored_count}" if ignored_count else ""

    # (kind, label, source file shown in header, message when nothing is missing)
    report_kinds = [
        ("patches",  "Патчи",      "conf.json",    "(все патчи описаны)"),
        ("utils",    "Утилиты",    "conf.json",    "(все утилиты описаны)"),
        ("contribs", "Расширения", "contrib.json", "(все расширения описаны)"),
    ]
    for kind, label, source, empty_msg in report_kinds:
        missing = list_tech_missing_from_descriptions(
            config, loaded, descriptions, kind=kind, version_keys=requested,
        )
        print(file=sys.stderr)
        print(f"{label} из {source} без записи в {args.descriptions} "
              f"({scope}) — {len(missing)}{suffix}:", file=sys.stderr)
        if missing:
            name_width = max(len(name) for name, _ in missing)
            for name, versions in missing:
                print(f"  - {name.ljust(name_width)}   "
                      f"(in: {', '.join(versions)})", file=sys.stderr)
        else:
            print(f"  {empty_msg}", file=sys.stderr)

    # Sanity: tell the user about features whose tech wasn't matched anywhere
    # — those will have an empty matrix in the output, which is usually a
    # symptom of a typo in `tech` (or just data that's not present in the
    # versions you've enabled in config.toml).
    empty_matrix: list[tuple[str, str, str]] = []
    for g in output["groups"]:
        for f in g["features"]:
            if not f["matrix"]:
                empty_matrix.append((g["name"], f["name"], f["tech"]))
    if empty_matrix:
        print(file=sys.stderr)
        print(f"Фичи с пустой matrix (tech не найден ни в одной "
              f"(версия, издание)) ({len(empty_matrix)}):", file=sys.stderr)
        for g_name, f_name, tech in empty_matrix:
            print(f"  - [{g_name}] «{f_name}»  tech={tech!r}", file=sys.stderr)

    total = sum(len(g["features"]) for g in output["groups"])
    print(file=sys.stderr)
    print(f"OK: {len(output['versions'])} версий, "
          f"{len(output['groups'])} групп, {total} фич",
          file=sys.stderr)
    print(f"  -> {args.out}", file=sys.stderr)
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
