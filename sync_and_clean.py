#!/usr/bin/env python3
"""
sync_and_clean.py

Supports one or more upstream SOURCES (see config/sync.json: "sources").
Each source is { owner, repo, branch, upstream_path, directory_name }.
The workflow sparse-checks out each source into:

    <upstream_checkout_root>/@<owner>/<repo>/<branch>/<directory_name>/

before this script runs (no GitHub API calls needed for the fetch step).

For every source, this script:
1. Syncs EVERY file from its local checkout, regardless of format — but
   only *.json files are parsed and cleaned. Everything else (including
   pre-compiled .srs) is copied through byte-for-byte, unmodified.
2. Cleans blacklisted strings out of domain / domain_suffix / domain_keyword
   arrays in every *.json file, per config/blacklist.json.
3. Writes every synced file (cleaned json, or as-is for everything else) under:
       <local_output_root>/@<owner>/<repo>/<branch>/<directory_name>/<...>
4. Compiles each kept *.json file to sing-box's binary .srs format. Any
   file that was ALREADY a pre-compiled .srs upstream is copied through
   rather than recompiled. Both cases land under the SAME
   @<owner>/<repo>/<branch>/<directory_name> namespace, two ways:
       srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<date>/<file>.srs  (dated snapshot)
       srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.srs         (always-current "latest")
   Owner+repo+branch is already globally unique, so using it as the shared
   prefix for both trees means multiple sources never collide, and the srs
   tree's top-level naming always matches the synced-file tree's. Any other
   file format is synced but never compiled or linked.
5. Produces a combined detailed log, a release-ready summary, and a single
   ACCESS_LINKS.md manifest of jsDelivr CDN links for every .srs file
   (compiled or pre-compiled-and-passed-through) touched this run, across
   all sources.
6. Emits GitHub Actions outputs so the workflow can decide whether to
   commit, tag, and cut a release.

Exit code is always 0 unless a hard error occurs; "did anything change"
is communicated via the `changed` output, not exit code.
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT
LOG_DIR = ROOT / "logs"

CLEAN_FIELDS = ("domain", "domain_suffix", "domain_keyword")

# All generated timestamps use this fixed UTC+8 offset — never labeled "UTC"
# in any output, since the offset is the implicit default throughout.
DISPLAY_TZ = timezone(timedelta(hours=8))


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def source_namespace(source: dict) -> Path:
    """The shared prefix used for both the synced-file tree and the srs tree:
    @<owner>/<repo>/<branch>/<directory_name>"""
    directory_name = source.get("directory_name") or Path(source["upstream_path"]).name
    return Path(f"@{source['owner']}") / source["repo"] / source["branch"] / directory_name


def clean_rule_array(arr, blacklist_lower):
    kept, removed = [], []
    for item in arr:
        item_l = str(item).lower()
        if any(bl in item_l for bl in blacklist_lower):
            removed.append(item)
        else:
            kept.append(item)
    return kept, removed


def clean_ruleset_document(doc: dict, blacklist_lower):
    stats = {
        "rules_total": 0,
        "rules_discarded": 0,
        "removed": {"domain": [], "domain_suffix": [], "domain_keyword": []},
        "fields_cleared": [],
    }

    rules = doc.get("rules", [])
    stats["rules_total"] = len(rules)
    new_rules = []

    for idx, rule in enumerate(rules):
        if not isinstance(rule, dict):
            new_rules.append(rule)
            continue

        for field in CLEAN_FIELDS:
            if field in rule and isinstance(rule[field], list):
                new_arr, removed = clean_rule_array(rule[field], blacklist_lower)
                if removed:
                    stats["removed"][field].extend(removed)
                if new_arr:
                    rule[field] = new_arr
                else:
                    if field in rule:
                        del rule[field]
                        if removed:
                            stats["fields_cleared"].append((idx, field))

        remaining_keys = [k for k in rule.keys() if k not in ("invert",)]
        if not remaining_keys:
            stats["rules_discarded"] += 1
            continue

        new_rules.append(rule)

    if not new_rules:
        return None, stats

    doc["rules"] = new_rules
    return doc, stats


def canonical_dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


# Maps Surge/Clash-style .conf directive names to sing-box rule-set JSON fields.
# Anything not in this map (URL-REGEX, USER-AGENT, DOMAIN-SET, AND/OR logic
# blocks, GEOIP, etc.) is left untouched — logged as skipped, not guessed at.
CONF_TYPE_MAP = {
    "DOMAIN": "domain",
    "HOST": "domain",
    "DOMAIN-SUFFIX": "domain_suffix",
    "HOST-SUFFIX": "domain_suffix",
    "DOMAIN-KEYWORD": "domain_keyword",
    "HOST-KEYWORD": "domain_keyword",
    "IP-CIDR": "ip_cidr",
    "IP-CIDR6": "ip_cidr",
    "IP6-CIDR": "ip_cidr",
    "PROCESS-NAME": "process_name",
}


def convert_conf_to_ruleset(text: str):
    """Convert a Surge/Clash-style plain-text .conf rule list (lines like
    `DOMAIN-SUFFIX,example.com`) into a sing-box rule-set JSON document.
    Comments (#, //, ;) and blank lines are ignored. Returns (doc, stats);
    stats records per-field converted counts and every line that didn't map
    to a supported directive, so nothing is silently dropped without a trace."""
    fields = {"domain": [], "domain_suffix": [], "domain_keyword": [], "ip_cidr": [], "process_name": []}
    skipped_lines = []

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith("//") or line.startswith(";"):
            continue
        parts = [p.strip() for p in line.split(",")]
        rule_type = parts[0].upper() if parts else ""
        value = parts[1] if len(parts) > 1 else ""
        field = CONF_TYPE_MAP.get(rule_type)
        if field is None or not value:
            skipped_lines.append(line)
            continue
        fields[field].append(value)

    rule_obj = {k: v for k, v in fields.items() if v}
    doc = {"version": 1, "rules": [rule_obj] if rule_obj else []}
    stats = {
        "converted_counts": {k: len(v) for k, v in fields.items()},
        "skipped_count": len(skipped_lines),
        "skipped_lines": skipped_lines,
    }
    return doc, stats


def compile_to_srs(sing_box_bin: str, json_path: Path, srs_path: Path):
    """Compile a sing-box rule-set JSON file into binary .srs format.
    Returns (ok: bool, message: str)."""
    srs_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [sing_box_bin, "rule-set", "compile", str(json_path), "-o", str(srs_path)],
            capture_output=True, text=True, timeout=60,
        )
    except FileNotFoundError:
        return False, (f"'{sing_box_bin}' binary not found on PATH — install the sing-box "
                       f"CLI in the workflow before running this script.")
    except subprocess.TimeoutExpired:
        return False, "compile timed out after 60s"

    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "unknown compile error").strip()
    return True, "ok"


def jsdelivr_url(base_url: str, owner: str, repo: str, ref: str, rel_path: str) -> str:
    rel_path = rel_path.replace(os.sep, "/")
    return f"{base_url.rstrip('/')}/{owner}/{repo}@{ref}/{rel_path}"


def sync_local_tree(namespace: Path, upstream_dir: Path, output_dir: Path,
                    blacklist_lower: list, required: bool = True):
    """Core sync logic shared by both configured upstream sources and the
    local custom/ directory. Every file under upstream_dir is synced,
    regardless of format — but only *.json files are parsed and cleaned;
    everything else (including pre-compiled .srs) is copied through as-is.
    Mirrors the (possibly multi-level) subdirectory structure. Returns a
    result dict."""
    namespace_str = str(namespace)

    if not upstream_dir.exists():
        if required:
            print(f"ERROR: source dir not found for '{namespace_str}': {upstream_dir}\n"
                  f"Did the workflow's sparse-checkout step run for this source first?", file=sys.stderr)
        return {
            "namespace": namespace_str, "namespace_path": namespace,
            "output_dir": output_dir, "per_file_reports": [],
            "added": [], "updated": [], "deleted": [], "unchanged": [], "error": required,
        }

    upstream_files = sorted(p for p in upstream_dir.rglob("*") if p.is_file())
    kind_counts = {"json": 0, "conf": 0, "srs": 0, "other": 0}
    for p in upstream_files:
        suf = p.suffix.lower()
        kind_counts["json" if suf == ".json" else "conf" if suf == ".conf" else
        "srs" if suf == ".srs" else "other"] += 1
    print(f"[{namespace_str}] Found {len(upstream_files)} files ({upstream_dir}): "
          f"{kind_counts['json']} json, {kind_counts['conf']} conf, {kind_counts['srs']} srs, "
          f"{kind_counts['other']} other — only json and conf are cleaned/converted, "
          f"the rest sync through as-is.")

    existing_before = {}
    if output_dir.exists():
        for p in output_dir.rglob("*"):
            if p.is_file():
                existing_before[str(p.relative_to(output_dir))] = p.read_bytes()

    output_dir.mkdir(parents=True, exist_ok=True)

    per_file_reports = []
    added, updated, deleted, unchanged = [], [], [], []
    seen_relpaths = set()

    for src_path in upstream_files:
        rel_path_src = str(src_path.relative_to(upstream_dir))
        suffix = src_path.suffix.lower()

        if suffix == ".json":
            rel_path = rel_path_src  # output extension unchanged
            seen_relpaths.add(rel_path)
            local_path = output_dir / rel_path

            try:
                doc = json.loads(src_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                print(f"  [{namespace_str}] ! Skipping {rel_path}: invalid JSON ({e})", file=sys.stderr)
                continue

            cleaned_doc, stats = clean_ruleset_document(doc, blacklist_lower)

            removed_counts = {k: len(v) for k, v in stats["removed"].items()}
            total_removed = sum(removed_counts.values())

            report = {
                "file": rel_path, "kind": "json",
                "rules_total": stats["rules_total"],
                "rules_discarded": stats["rules_discarded"],
                "removed_counts": removed_counts,
                "removed_values": stats["removed"],
                "fields_cleared": stats["fields_cleared"],
                "action": None,
            }

            old_bytes = existing_before.get(rel_path)

            if cleaned_doc is None:
                report["action"] = "discarded_all_rules_empty_file"
                if old_bytes is not None:
                    deleted.append(rel_path)
                    local_path.unlink(missing_ok=True)
                per_file_reports.append(report)
                continue

            new_bytes = canonical_dump(cleaned_doc).encode("utf-8")

            if old_bytes is None:
                report["action"] = "added"
                added.append(rel_path)
            elif old_bytes != new_bytes:
                report["action"] = "updated"
                updated.append(rel_path)
            else:
                report["action"] = "unchanged"
                unchanged.append(rel_path)

            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(new_bytes)
            per_file_reports.append(report)

            if total_removed or stats["rules_discarded"]:
                print(f"  [{namespace_str}] {rel_path}: -{total_removed} entries "
                      f"(domain={removed_counts['domain']}, "
                      f"suffix={removed_counts['domain_suffix']}, "
                      f"keyword={removed_counts['domain_keyword']}), "
                      f"{stats['rules_discarded']} rule(s) discarded")

        elif suffix == ".conf":
            # Surge/Clash-style plain-text ruleset -> converted to a sing-box
            # rule-set JSON document, then cleaned exactly like a native .json.
            # Output filename swaps .conf -> .json since the content format
            # itself has changed, not just been copied through.
            rel_path = str(Path(rel_path_src).with_suffix(".json"))
            seen_relpaths.add(rel_path)
            local_path = output_dir / rel_path

            try:
                conf_text = src_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as e:
                print(f"  [{namespace_str}] ! Skipping {rel_path_src}: unreadable .conf ({e})", file=sys.stderr)
                continue

            doc, conv_stats = convert_conf_to_ruleset(conf_text)
            cleaned_doc, stats = clean_ruleset_document(doc, blacklist_lower)

            removed_counts = {k: len(v) for k, v in stats["removed"].items()}
            total_removed = sum(removed_counts.values())

            report = {
                "file": rel_path, "kind": "conf", "source_file": rel_path_src,
                "rules_total": stats["rules_total"],
                "rules_discarded": stats["rules_discarded"],
                "removed_counts": removed_counts,
                "removed_values": stats["removed"],
                "fields_cleared": stats["fields_cleared"],
                "action": None,
                "conf_converted_counts": conv_stats["converted_counts"],
                "conf_skipped_count": conv_stats["skipped_count"],
                "conf_skipped_lines": conv_stats["skipped_lines"],
            }

            old_bytes = existing_before.get(rel_path)

            if cleaned_doc is None:
                report["action"] = "discarded_all_rules_empty_file"
                if old_bytes is not None:
                    deleted.append(rel_path)
                    local_path.unlink(missing_ok=True)
                per_file_reports.append(report)
                continue

            new_bytes = canonical_dump(cleaned_doc).encode("utf-8")

            if old_bytes is None:
                report["action"] = "added"
                added.append(rel_path)
            elif old_bytes != new_bytes:
                report["action"] = "updated"
                updated.append(rel_path)
            else:
                report["action"] = "unchanged"
                unchanged.append(rel_path)

            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(new_bytes)
            per_file_reports.append(report)

            convc = conv_stats["converted_counts"]
            print(f"  [{namespace_str}] {rel_path_src} -> {rel_path}: converted "
                  f"(domain={convc['domain']}, suffix={convc['domain_suffix']}, "
                  f"keyword={convc['domain_keyword']}, ip_cidr={convc['ip_cidr']}, "
                  f"process_name={convc['process_name']}), "
                  f"{conv_stats['skipped_count']} line(s) unsupported/skipped"
                  + (f", -{total_removed} blacklisted after conversion" if total_removed else ""))

        else:
            # Non-JSON, non-conf file (including pre-compiled .srs): synced through
            # verbatim, never parsed or cleaned. .srs files still get picked up for
            # SRS compile-step handling (as a direct copy, not a fresh compile) and
            # still get an ACCESS_LINKS.md entry; any other format is synced to
            # rules/ but not touched further.
            rel_path = rel_path_src
            seen_relpaths.add(rel_path)
            local_path = output_dir / rel_path
            kind = "srs" if suffix == ".srs" else "other"
            try:
                new_bytes = src_path.read_bytes()
            except OSError as e:
                print(f"  [{namespace_str}] ! Skipping {rel_path}: unreadable ({e})", file=sys.stderr)
                continue

            report = {
                "file": rel_path, "kind": kind,
                "rules_total": 0, "rules_discarded": 0,
                "removed_counts": {"domain": 0, "domain_suffix": 0, "domain_keyword": 0},
                "removed_values": {"domain": [], "domain_suffix": [], "domain_keyword": []},
                "fields_cleared": [], "action": None,
            }

            old_bytes = existing_before.get(rel_path)
            if old_bytes is None:
                report["action"] = "added"
                added.append(rel_path)
            elif old_bytes != new_bytes:
                report["action"] = "updated"
                updated.append(rel_path)
            else:
                report["action"] = "unchanged"
                unchanged.append(rel_path)

            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(new_bytes)
            per_file_reports.append(report)

    for rel_path in existing_before:
        if rel_path not in seen_relpaths:
            local_path = output_dir / rel_path
            if local_path.exists():
                local_path.unlink()
            deleted.append(rel_path)

    return {
        "namespace": namespace_str, "namespace_path": namespace,
        "output_dir": output_dir, "per_file_reports": per_file_reports,
        "added": added, "updated": updated, "deleted": deleted, "unchanged": unchanged,
        "error": False,
    }


def process_source(source: dict, sync_cfg: dict, blacklist_lower: list):
    """Sync + clean one configured upstream source. All paths are namespaced
    under @<owner>/<repo>/<branch>/<directory_name>/... (each of which may
    itself be a multi-level path, e.g. directory_name="sing-box/Clash")."""
    namespace = source_namespace(source)
    checkout_root = ROOT / sync_cfg.get("upstream_checkout_root", "upstream")
    upstream_dir = checkout_root / namespace
    output_dir = ROOT / sync_cfg["local_output_root"] / namespace
    return sync_local_tree(namespace, upstream_dir, output_dir, blacklist_lower, required=True)


def process_custom(sync_cfg: dict, blacklist_lower: list):
    """Clean the local custom/ directory (checked into this repo directly,
    not fetched from any upstream). Supports arbitrary multi-level
    subdirectories under custom/. Optional — if the directory doesn't exist
    or is empty, this is silently skipped rather than treated as an error."""
    custom_dir_name = sync_cfg.get("custom_dir_name", "custom")
    namespace = Path(custom_dir_name)
    upstream_dir = ROOT / custom_dir_name
    output_dir = ROOT / sync_cfg["local_output_root"] / custom_dir_name
    return sync_local_tree(namespace, upstream_dir, output_dir, blacklist_lower, required=False)


def main():
    sync_cfg = load_json(CONFIG_DIR / "sync.json")
    blacklist_cfg = load_json(CONFIG_DIR / "blacklist.json")
    blacklist = blacklist_cfg.get("blacklist", [])
    blacklist_lower = [b.lower() for b in blacklist]

    sources = sync_cfg.get("sources", [])
    enable_custom = sync_cfg.get("enable_custom", True)
    if not sources and not enable_custom:
        print("ERROR: sync.json has no 'sources' configured and custom/ is disabled.", file=sys.stderr)
        sys.exit(1)

    LOG_DIR.mkdir(exist_ok=True)
    run_ts = datetime.now(DISPLAY_TZ).strftime("%Y%m%d_%H%M%S")
    date_str = run_ts.split("_")[0]  # YYYYMMDD

    source_results = [process_source(src, sync_cfg, blacklist_lower) for src in sources]
    if enable_custom:
        source_results.append(process_custom(sync_cfg, blacklist_lower))

    all_added = sum((r["added"] for r in source_results), [])
    all_updated = sum((r["updated"] for r in source_results), [])
    all_deleted = sum((r["deleted"] for r in source_results), [])
    all_unchanged = sum((r["unchanged"] for r in source_results), [])
    changed = bool(all_added or all_updated or all_deleted)

    # ---------- CDN identity (needed for both the srs output path and the links) ----------
    cdn_base_url = sync_cfg.get("cdn_base_url", "https://testingcf.jsdelivr.net/gh")
    cdn_ref_mode = sync_cfg.get("cdn_ref_mode", "tag")  # "tag" or "branch"
    cdn_branch = sync_cfg.get("cdn_branch", "master")
    tag_name = f"v{run_ts}"
    cdn_ref = cdn_branch if cdn_ref_mode == "branch" else tag_name

    repo_slug = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", set by GitHub Actions
    if repo_slug and "/" in repo_slug:
        cdn_owner, cdn_repo = repo_slug.split("/", 1)
    else:
        cdn_owner = sync_cfg.get("cdn_owner", "YOUR_GITHUB_USERNAME")
        cdn_repo = sync_cfg.get("cdn_repo", "YOUR_REPO_NAME")

    # ---------- Compile every kept JSON file to .srs, per source ----------
    # Stored under the SAME @<owner>/<repo>/<branch>/<directory_name> namespace as the synced JSON:
    #   dated snapshot: srs/@<owner>/<repo>/<branch>/<directory_name>/<subdir>/<date>/<file>.srs
    #   latest catalog: srs/@<owner>/<repo>/<branch>/<directory_name>/<subdir>/<file>.srs (always current)
    sing_box_bin = sync_cfg.get("sing_box_bin", "sing-box")
    srs_root = ROOT / "srs"
    output_root_name = sync_cfg["local_output_root"]

    compile_results = []  # (namespace, rel_path, ok, message, synced_out_rel, dated_srs_rel, latest_srs_rel)

    for res in source_results:
        if res["error"]:
            continue
        namespace_path = res["namespace_path"]
        output_dir = res["output_dir"]
        kept_relpaths = sorted(set(res["added"]) | set(res["updated"]) | set(res["unchanged"]))
        kind_lookup = {r["file"]: r["kind"] for r in res["per_file_reports"]}

        for rel_path in kept_relpaths:
            kind = kind_lookup.get(rel_path)
            if kind == "other":
                # Synced to rules/ as-is, but not json and not a pre-compiled .srs —
                # nothing to compile or copy into srs/, and no ACCESS_LINKS.md entry.
                continue

            synced_out_rel = f"{output_root_name}/{namespace_path.as_posix()}/{rel_path}"

            rel = Path(rel_path)
            srs_filename = rel.with_suffix(".srs").name
            subdir = rel.parent
            if str(subdir) in (".", ""):
                dated_rel = namespace_path / date_str / srs_filename
                latest_rel = namespace_path / srs_filename
            else:
                dated_rel = namespace_path / subdir / date_str / srs_filename
                latest_rel = namespace_path / subdir / srs_filename

            dated_path = srs_root / dated_rel
            latest_path = srs_root / latest_rel

            if kind == "json":
                json_path = output_dir / rel_path
                ok, msg = compile_to_srs(sing_box_bin, json_path, dated_path)
                if ok:
                    latest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(dated_path, latest_path)
                else:
                    print(f"  [{res['namespace']}] ! SRS compile failed for {rel_path}: {msg}", file=sys.stderr)
            else:  # kind == "srs": already compiled upstream — copy through, don't recompile
                srs_source_path = output_dir / rel_path
                try:
                    dated_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(srs_source_path, dated_path)
                    latest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(srs_source_path, latest_path)
                    ok, msg = True, "synced (pre-compiled upstream .srs, not recompiled)"
                except OSError as e:
                    ok, msg = False, f"failed to copy pre-compiled .srs: {e}"
                    print(f"  [{res['namespace']}] ! {msg} ({rel_path})", file=sys.stderr)

            compile_results.append((
                res["namespace"], rel_path, ok, msg, synced_out_rel,
                dated_rel.as_posix() if ok else None, latest_rel.as_posix() if ok else None,
            ))

    compile_failures = [r for r in compile_results if not r[2]]
    compile_ok_count = len(compile_results) - len(compile_failures)

    # ---------- Build the "latest" SRS link list (json links and dated snapshots
    # are intentionally excluded from the public manifest — see ACCESS_LINKS.md below) ----------
    latest_entries = []  # (filename, url)
    for namespace, rel_path, ok, msg, synced_out_rel, dated_rel, latest_rel in compile_results:
        if not ok:
            continue
        latest_full = f"srs/{latest_rel}"
        url = jsdelivr_url(cdn_base_url, cdn_owner, cdn_repo, cdn_branch, latest_full)
        filename = Path(latest_rel).name
        latest_entries.append((filename, url))

    latest_entries.sort(key=lambda x: (x[0].lower(), x[0]))

    groups = {}
    for filename, url in latest_entries:
        first_char = filename[0].upper() if filename else "0-9"
        if not first_char.isalpha():
            first_char = "0-9"
        groups.setdefault(first_char, []).append((filename, url))
    letters_sorted = sorted(groups.keys(), key=lambda k: (k == "0-9", k))

    # ---------- Single manifest: ACCESS_LINKS.md ----------
    # SRS-only navigation index, grouped alphabetically by filename. No JSON
    # links and no source-path text are shown — only the compiled .srs CDN
    # link for each file, always resolved against the branch (these links are
    # meant to be permanent, not tied to any one dated snapshot).
    # Overwritten every run so it always reflects the current file set.
    links_path = ROOT / "ACCESS_LINKS.md"
    active_namespaces = sorted(set(r[0] for r in compile_results)) or \
                        [str(source_namespace(s)) for s in sources]
    with open(links_path, "w", encoding="utf-8") as f:
        f.write("# Access Links\n\n")
        f.write(f"Generated {run_ts}\n\n")

        f.write("## Summary\n\n")
        f.write(f"- Total SRS files: {len(latest_entries)}\n")
        f.write(f"- Sources: {len(active_namespaces)}\n")
        if compile_failures:
            f.write(f"- Compile failures this run: {len(compile_failures)} "
                    f"(see `logs/sync_{run_ts}.log`)\n")
        f.write("\n---\n\n")

        f.write("## Navigation\n\n")
        f.write(" · ".join(f"[{letter}](#{letter.lower()})" for letter in letters_sorted) + "\n\n")
        f.write("---\n\n")

        for letter in letters_sorted:
            f.write(f"## {letter}\n\n")
            for filename, url in groups[letter]:
                f.write(f"### {filename}\n")
                f.write("```\n")
                f.write(f"{url}\n")
                f.write("```\n\n")
            f.write("---\n\n")

        f.write("## Related\n\n")
        f.write("- [Changelog](CHANGELOG.md)\n")
        f.write(f"- [Sync log](logs/sync_{run_ts}.log)\n")
        f.write(f"- [Release summary](logs/summary_{run_ts}.md)\n")

    # ---------- Detailed log ----------
    kind_totals = {"json": 0, "srs": 0, "other": 0}
    other_files = []  # (namespace, rel_path) — synced but not eligible for conversion
    for res in source_results:
        kept = set(res["added"]) | set(res["updated"]) | set(res["unchanged"])
        for r in res["per_file_reports"]:
            if r["file"] in kept:
                kind_totals[r["kind"]] += 1
                if r["kind"] == "other":
                    other_files.append((res["namespace"], r["file"]))

    detail_log_path = LOG_DIR / f"sync_{run_ts}.log"
    with open(detail_log_path, "w", encoding="utf-8") as f:
        f.write(f"Sync run: {run_ts}\n")
        f.write(f"Sources ({len(sources)}{' + custom' if enable_custom else ''}):\n")
        for src in sources:
            f.write(f"  - {source_namespace(src)}  <-  "
                    f"{src['owner']}/{src['repo']}@{src['branch']}:{src['upstream_path']}\n")
        if enable_custom:
            f.write(f"  - {sync_cfg.get('custom_dir_name', 'custom')}  <-  local (not fetched)\n")
        f.write(f"Blacklist entries: {len(blacklist)}\n")
        f.write(f"  {blacklist}\n\n")
        f.write(f"Files added:   {len(all_added)}\n")
        f.write(f"Files updated: {len(all_updated)}\n")
        f.write(f"Files deleted: {len(all_deleted)}\n")
        f.write(f"Files unchanged: {len(all_unchanged)}\n\n")
        f.write(f"File kinds synced this run: {kind_totals['json']} json (cleaned+compiled), "
                f"{kind_totals['srs']} srs (pre-compiled, copied through), "
                f"{kind_totals['other']} other (synced as-is, not converted)\n")
        if other_files:
            f.write("Synced but not converted (not json, not srs):\n")
            for namespace, rel_path in other_files:
                f.write(f"  - [{namespace}] {rel_path}\n")
        f.write(f"\nSRS compiled/copied: {compile_ok_count}/{len(compile_results)}\n")
        if compile_failures:
            f.write("SRS compile/copy failures:\n")
            for namespace, rel_path, ok, msg, *_ in compile_failures:
                f.write(f"  - [{namespace}] {rel_path}: {msg}\n")
        f.write("\n")
        f.write("=" * 70 + "\n")
        for res in source_results:
            for r in res["per_file_reports"]:
                f.write(f"\n[{res['namespace']}] File: {r['file']}  [{r['kind']}] [{r['action']}]\n")
                if r["kind"] != "json":
                    continue
                f.write(f"  Total rules: {r['rules_total']}, discarded rules: {r['rules_discarded']}\n")
                rc = r["removed_counts"]
                f.write(f"  Removed -> domain: {rc['domain']}, "
                        f"domain_suffix: {rc['domain_suffix']}, "
                        f"domain_keyword: {rc['domain_keyword']}\n")
                if r["fields_cleared"]:
                    f.write("  Fields fully cleared (rule_index, field):\n")
                    for idx, field in r["fields_cleared"]:
                        f.write(f"    - rule[{idx}].{field}\n")
                for field, values in r["removed_values"].items():
                    if values:
                        f.write(f"  Removed {field} entries: {values}\n")

    # ---------- Release-ready summary (Markdown) ----------
    summary_path = LOG_DIR / f"summary_{run_ts}.md"
    total_removed_all = sum(
        sum(r["removed_counts"].values())
        for res in source_results for r in res["per_file_reports"]
    )
    total_discarded_rules = sum(
        r["rules_discarded"] for res in source_results for r in res["per_file_reports"]
    )
    discarded_files = [
        f"{res['namespace']}/{r['file']}"
        for res in source_results for r in res["per_file_reports"]
        if r["action"] == "discarded_all_rules_empty_file"
    ]
    added_ns = [f"{res['namespace']}/{p}" for res in source_results for p in res["added"]]
    updated_ns = [f"{res['namespace']}/{p}" for res in source_results for p in res["updated"]]
    deleted_ns = [f"{res['namespace']}/{p}" for res in source_results for p in res["deleted"]]

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"## Sync summary — {run_ts}\n\n")
        f.write("Sources:\n")
        for src in sources:
            f.write(f"- `{source_namespace(src)}` ← "
                    f"`{src['owner']}/{src['repo']}` @ `{src['branch']}` (`{src['upstream_path']}`)\n")
        if enable_custom:
            f.write(f"- `{sync_cfg.get('custom_dir_name', 'custom')}` ← local files (not fetched)\n")
        f.write("\n")
        f.write(f"- Files added: **{len(all_added)}**\n")
        f.write(f"- Files updated: **{len(all_updated)}**\n")
        f.write(f"- Files deleted: **{len(all_deleted)}**\n")
        f.write(f"- Files unchanged: {len(all_unchanged)}\n")
        f.write(f"- Blacklisted entries removed: **{total_removed_all}**\n")
        f.write(f"- Empty rules discarded: **{total_discarded_rules}**\n")
        f.write(f"- Files dropped entirely (all rules emptied): **{len(discarded_files)}**\n")
        f.write(f"- SRS files compiled/copied: **{compile_ok_count}/{len(compile_results)}**\n\n")

        if compile_failures:
            f.write("### SRS compile failures\n" +
                    "\n".join(f"- `{r[0]}/{r[1]}`: {r[3]}" for r in compile_failures) + "\n\n")

        if added_ns:
            f.write("### Added\n" + "\n".join(f"- `{p}`" for p in sorted(added_ns)) + "\n\n")
        if updated_ns:
            f.write("### Updated\n" + "\n".join(f"- `{p}`" for p in sorted(updated_ns)) + "\n\n")
        if deleted_ns:
            f.write("### Deleted\n" + "\n".join(f"- `{p}`" for p in sorted(deleted_ns)) + "\n\n")
        if discarded_files:
            f.write("### Dropped (all rules removed by blacklist)\n" +
                    "\n".join(f"- `{p}`" for p in sorted(discarded_files)) + "\n\n")

        f.write("Full per-file cleanup detail is attached in the workflow logs "
                f"(`logs/sync_{run_ts}.log`).\n\n")
        f.write("CDN access links for every file in this release are in "
                f"[`ACCESS_LINKS.md`](../ACCESS_LINKS.md) (ref: "
                f"`{cdn_ref}`).\n")

    changelog_path = ROOT / "CHANGELOG.md"
    body = summary_path.read_text(encoding="utf-8")
    existing = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else "# Changelog\n\n"
    if changed:
        changelog_path.write_text(existing + "\n" + body + "\n---\n\n", encoding="utf-8")

    gha_output = os.environ.get("GITHUB_OUTPUT")
    if gha_output:
        with open(gha_output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"tag={tag_name}\n")
            f.write(f"summary_path={summary_path.relative_to(ROOT)}\n")
            f.write(f"links_path={links_path.relative_to(ROOT)}\n")

    print(f"\nDone. changed={changed}")
    print(f"Detail log:   {detail_log_path}")
    print(f"Summary:      {summary_path}")
    print(f"Access links: {links_path}")


if __name__ == "__main__":
    main()