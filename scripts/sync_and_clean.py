#!/usr/bin/env python3
"""
sync_and_clean.py

1. Reads every *.json rule file from a LOCAL directory — expected to be
   populated by the workflow beforehand via `git clone --sparse` of the
   upstream repo's `sing-box` path (see .github/workflows/sync.yml).
   No GitHub API calls are needed for the fetch step anymore.
2. Cleans blacklisted strings out of domain / domain_suffix / domain_keyword
   arrays, per the rules in config/blacklist.json.
3. Writes the cleaned files into the local output directory.
4. Produces a detailed per-file log and a release-ready summary (Markdown).
5. Emits GitHub Actions outputs so the workflow can decide whether to
   commit, tag, and cut a release.

Exit code is always 0 unless a hard error occurs; "did anything change"
is communicated via the `changed` output, not exit code.
"""
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
LOG_DIR = ROOT / "logs"

CLEAN_FIELDS = ("domain", "domain_suffix", "domain_keyword")


def load_json(path: Path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


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


def main():
    sync_cfg = load_json(CONFIG_DIR / "sync.json")
    blacklist_cfg = load_json(CONFIG_DIR / "blacklist.json")
    blacklist = blacklist_cfg.get("blacklist", [])
    blacklist_lower = [b.lower() for b in blacklist]

    # Populated by the workflow via `git clone --sparse` before this script runs.
    upstream_dir = ROOT / sync_cfg.get("upstream_checkout_dir", "upstream/sing-box")
    output_dir = ROOT / sync_cfg["local_output_dir"]

    if not upstream_dir.exists():
        print(f"ERROR: upstream checkout dir not found: {upstream_dir}\n"
              f"Did the workflow's sparse-checkout step run first?", file=sys.stderr)
        sys.exit(1)

    LOG_DIR.mkdir(exist_ok=True)
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    upstream_files = sorted(upstream_dir.rglob("*.json"))
    print(f"Found {len(upstream_files)} JSON files in local upstream checkout "
          f"({upstream_dir}).")

    existing_before = {}
    if output_dir.exists():
        for p in output_dir.rglob("*.json"):
            existing_before[str(p.relative_to(output_dir))] = p.read_bytes()

    output_dir.mkdir(parents=True, exist_ok=True)

    per_file_reports = []
    added, updated, deleted, unchanged = [], [], [], []
    seen_relpaths = set()

    for src_path in upstream_files:
        rel_path = str(src_path.relative_to(upstream_dir))
        seen_relpaths.add(rel_path)
        local_path = output_dir / rel_path

        try:
            doc = json.loads(src_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print(f"  ! Skipping {rel_path}: invalid JSON upstream ({e})", file=sys.stderr)
            continue

        cleaned_doc, stats = clean_ruleset_document(doc, blacklist_lower)

        removed_counts = {k: len(v) for k, v in stats["removed"].items()}
        total_removed = sum(removed_counts.values())

        report = {
            "file": rel_path,
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
            print(f"  {rel_path}: -{total_removed} entries "
                  f"(domain={removed_counts['domain']}, "
                  f"suffix={removed_counts['domain_suffix']}, "
                  f"keyword={removed_counts['domain_keyword']}), "
                  f"{stats['rules_discarded']} rule(s) discarded")

    for rel_path in existing_before:
        if rel_path not in seen_relpaths:
            local_path = output_dir / rel_path
            if local_path.exists():
                local_path.unlink()
            deleted.append(rel_path)

    changed = bool(added or updated or deleted)

    # ---------- Compile every kept JSON file to .srs (sing-box binary format) ----------
    sing_box_bin = sync_cfg.get("sing_box_bin", "sing-box")
    kept_relpaths = sorted(set(added) | set(updated) | set(unchanged))
    compile_results = []  # (rel_path, ok, message)

    for rel_path in kept_relpaths:
        json_path = output_dir / rel_path
        srs_path = json_path.with_suffix(".srs")
        ok, msg = compile_to_srs(sing_box_bin, json_path, srs_path)
        compile_results.append((rel_path, ok, msg))
        if not ok:
            print(f"  ! SRS compile failed for {rel_path}: {msg}", file=sys.stderr)

    compile_failures = [r for r in compile_results if not r[1]]

    # ---------- Build jsDelivr access links for every kept file (json + srs) ----------
    cdn_base_url = sync_cfg.get("cdn_base_url", "https://testingcf.jsdelivr.net/gh")
    cdn_ref_mode = sync_cfg.get("cdn_ref_mode", "tag")  # "tag" or "branch"
    tag_name = f"v{run_ts}"
    if cdn_ref_mode == "branch":
        cdn_ref = sync_cfg.get("cdn_branch", "master")
    else:
        cdn_ref = tag_name

    repo_slug = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", set by GitHub Actions
    if repo_slug and "/" in repo_slug:
        cdn_owner, cdn_repo = repo_slug.split("/", 1)
    else:
        cdn_owner = sync_cfg.get("cdn_owner", "YOUR_GITHUB_USERNAME")
        cdn_repo = sync_cfg.get("cdn_repo", "YOUR_REPO_NAME")

    output_dir_name = sync_cfg["local_output_dir"]
    access_links = []  # (rel_path_with_ext, url)
    for rel_path in kept_relpaths:
        json_rel = f"{output_dir_name}/{rel_path}"
        access_links.append((json_rel, jsdelivr_url(cdn_base_url, cdn_owner, cdn_repo, cdn_ref, json_rel)))
        ok = next((r[1] for r in compile_results if r[0] == rel_path), False)
        if ok:
            srs_rel = f"{output_dir_name}/{Path(rel_path).with_suffix('.srs')}"
            access_links.append((srs_rel, jsdelivr_url(cdn_base_url, cdn_owner, cdn_repo, cdn_ref, srs_rel)))

    access_links.sort(key=lambda x: x[0])

    # Single manifest file logging every accessed/generated file's CDN link,
    # overwritten each run so it always reflects the current file set.
    links_path = ROOT / "access-links.txt"
    with open(links_path, "w", encoding="utf-8") as f:
        f.write(f"# Access links generated {run_ts} UTC\n")
        f.write(f"# CDN ref: {cdn_ref} (mode={cdn_ref_mode})\n")
        f.write(f"# Total files: {len(access_links)} "
                f"({len(kept_relpaths)} json, {len(kept_relpaths) - len(compile_failures)} srs)\n")
        if compile_failures:
            f.write(f"# WARNING: {len(compile_failures)} file(s) failed SRS compilation "
                    f"and have no .srs link below — see logs/sync_{run_ts}.log\n")
        f.write("#\n")
        for rel_path, url in access_links:
            f.write(f"{rel_path}\t{url}\n")

    detail_log_path = LOG_DIR / f"sync_{run_ts}.log"
    with open(detail_log_path, "w", encoding="utf-8") as f:
        f.write(f"Sync run: {run_ts} UTC\n")
        f.write(f"Upstream checkout: {upstream_dir}\n")
        f.write(f"Blacklist entries: {len(blacklist)}\n")
        f.write(f"  {blacklist}\n\n")
        f.write(f"Files added:   {len(added)}\n")
        f.write(f"Files updated: {len(updated)}\n")
        f.write(f"Files deleted: {len(deleted)}\n")
        f.write(f"Files unchanged: {len(unchanged)}\n\n")
        f.write(f"SRS compiled: {len(kept_relpaths) - len(compile_failures)}/{len(kept_relpaths)}\n")
        if compile_failures:
            f.write("SRS compile failures:\n")
            for rel_path, ok, msg in compile_failures:
                f.write(f"  - {rel_path}: {msg}\n")
        f.write("\n")
        f.write("=" * 70 + "\n")
        for r in per_file_reports:
            f.write(f"\nFile: {r['file']}  [{r['action']}]\n")
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

    summary_path = LOG_DIR / f"summary_{run_ts}.md"
    total_removed_all = sum(sum(r["removed_counts"].values()) for r in per_file_reports)
    total_discarded_rules = sum(r["rules_discarded"] for r in per_file_reports)
    discarded_files = [r["file"] for r in per_file_reports
                       if r["action"] == "discarded_all_rules_empty_file"]

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"## Sync summary — {run_ts} UTC\n\n")
        f.write(f"Upstream checkout: `{sync_cfg.get('upstream_owner')}/"
                f"{sync_cfg.get('upstream_repo')}` @ `{sync_cfg.get('upstream_branch')}` "
                f"(`{sync_cfg.get('upstream_path')}`)\n\n")
        f.write(f"- Files added: **{len(added)}**\n")
        f.write(f"- Files updated: **{len(updated)}**\n")
        f.write(f"- Files deleted: **{len(deleted)}**\n")
        f.write(f"- Files unchanged: {len(unchanged)}\n")
        f.write(f"- Blacklisted entries removed: **{total_removed_all}**\n")
        f.write(f"- Empty rules discarded: **{total_discarded_rules}**\n")
        f.write(f"- Files dropped entirely (all rules emptied): **{len(discarded_files)}**\n")
        f.write(f"- SRS files compiled: **{len(kept_relpaths) - len(compile_failures)}/{len(kept_relpaths)}**\n\n")

        if compile_failures:
            f.write("### SRS compile failures\n" +
                    "\n".join(f"- `{r[0]}`: {r[2]}" for r in compile_failures) + "\n\n")

        if added:
            f.write("### Added\n" + "\n".join(f"- `{p}`" for p in sorted(added)) + "\n\n")
        if updated:
            f.write("### Updated\n" + "\n".join(f"- `{p}`" for p in sorted(updated)) + "\n\n")
        if deleted:
            f.write("### Deleted\n" + "\n".join(f"- `{p}`" for p in sorted(deleted)) + "\n\n")
        if discarded_files:
            f.write("### Dropped (all rules removed by blacklist)\n" +
                    "\n".join(f"- `{p}`" for p in sorted(discarded_files)) + "\n\n")

        f.write("Full per-file cleanup detail is attached in the workflow logs "
                f"(`logs/sync_{run_ts}.log`).\n\n")
        f.write(f"CDN access links for every file in this release are in "
                f"[`access-links.txt`](../access-links.txt) (ref: `{cdn_ref}`).\n")

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