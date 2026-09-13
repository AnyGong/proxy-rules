#!/usr/bin/env python3
"""
sync_and_clean.py

Supports one or more upstream SOURCES (see config/sync.json: "sources").
Each source is { owner, repo, branch, upstream_path, directory_name }.
Sources are cloned from GitHub by default (https://github.com/<owner>/<repo>.git);
an explicit "repo_url" on a source entry overrides that and clones from
wherever it points instead (GitLab, self-hosted, etc.) — "owner"/"repo" are
still required in that case because they build the on-disk namespace.

The workflow sparse-checks out each source into:

    <upstream_checkout_root>/@<owner>/<repo>/<branch>/<directory_name>/

before this script runs (no GitHub API calls needed for the fetch step).

For every source, this script applies the following per-file conversion matrix:

  Source format | json/  | conf/  | yaml/  | srs/  | mrs/
  --------------|--------|--------|--------|-------|------
  .json         |  kept  |   ✓    |   ✓    |   ✓   |   ✓
  .conf         |   ✓    |  kept  |   ✓    |   ✓   |   ✓
  .list         |   ✓    |   ✓    |   ✓    |   ✓   |   ✓
  .txt (text)   |   ✓    |   ✓    |   ✓    |   ✓   |   ✓
  .yaml/.yml    |   ✓    |   ✓    |  kept  |   ✓   |   ✓
  .srs (pre-compiled) | — | —    |   —    | copy  |   —
  .mrs (pre-compiled) | — | —    |   —    |   —   | copy
  anything else | pass-through to json/, no compilation

1. Cleans blacklisted strings out of domain / domain_suffix / domain_keyword
   arrays in every parseable file, per blacklist.json. Each entry is either
   a plain substring match (matches anywhere in the domain, as before) or,
   if it contains *, ?, or [, an fnmatch glob matched against the whole
   domain (e.g. "*.xx.com" matches "ads.xx.com" but not bare "xx.com").
2. Every parseable format (.json/.conf/.list/.txt/.yaml/.yml) is parsed into
   a single canonical sing-box rule-set document, cleaned once, then
   re-serialised into ALL THREE text trees — json/<ns>/…, conf/<ns>/… (as
   .conf), and yaml/<ns>/… (as .yaml, mihomo rule-provider payload style)
   — from that one cleaned document, so every representation of a file is
   always in sync. It's also compiled → srs/<ns>/… and mrs/<ns>/….
   Whichever tree matches the file's own source format holds its "native"
   re-serialisation (e.g. a .conf source's conf/ output is that same
   content, cleaned); the other two text trees hold format conversions of
   the identical cleaned rules. Values written into .conf/.yaml are
   escaped appropriately for that format (YAML: single-quoted with
   embedded quotes doubled) so no special character can break parsing on
   either the way in or the way out.
3. pre-compiled .srs / .mrs → copied through byte-for-byte into their own
   format tree only, current content only (no dated history kept here —
   see "Output layout" below):
       srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.srs
       mrs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.mrs
4. Any other file format is synced byte-for-byte into json/ but never
   compiled or linked.
5. Produces a combined detailed log and a release-ready summary/README.md
   for every file touched this run, across all sources.
6. Emits GitHub Actions outputs so the workflow can decide whether to
   commit, tag, and cut a release.
7. Every source's fetched files are ALSO mirrored byte-for-byte,
   unmodified, into a sibling files/ tree — no parsing, cleaning
   (blacklist or otherwise), conversion, or compilation. This runs
   unconditionally for every source, with no per-source opt-in.

Output layout: this script only ever writes this run's CURRENT content —
json/, conf/, yaml/, srs/, mrs/, and files/ hold no dated subdirectories
of their own. Historical snapshots come from the destination repo's own
commit history instead: the calling workflow pushes everything this
script writes as a full, from-scratch snapshot onto a single branch each
run (this repo's own git history over that branch IS the changelog), so
there's no need for this script to also keep per-run dated copies
on disk.

This script never touches git itself — it only writes local
directories under its own ROOT. Which repo(s) and branch(es) those
directories end up committed to is entirely the calling workflow's
concern (see .github/workflows/build.yml), not this script's.

Exit code is always 0 unless a hard error occurs; "did anything change"
is communicated via the `changed` output, not exit code.
"""
import ipaddress
import concurrent.futures
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT
LOG_DIR = ROOT / "logs"

# Per-file conversion/cleaning details (one line per file touched) are ALWAYS
# written in full to the detail log under LOG_DIR (and from there into the
# committed logs/ tree) — nothing is lost. They're only echoed to stdout too
# when SYNC_VERBOSE=1, since on a run touching thousands of files that
# per-file echo is what buries the handful of lines that actually matter
# (ERROR:/! Skipping.../compile failed) in CI's console output. Those
# warning/error lines always print regardless of this flag.
VERBOSE = os.environ.get("SYNC_VERBOSE") == "1"

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


def _is_git_internal(rel: Path) -> bool:
    """True if any path segment is '.git' — i.e. the file lives inside a
    nested clone's .git metadata, which must NEVER be treated as source
    content. If such files reach json/ or files/, git records the whole
    containing directory as a gitlink (embedded submodule reference) on
    commit, and the real files vanish from the repo."""
    return any(part == ".git" for part in rel.parts)


def _is_glob_pattern(entry: str) -> bool:
    """A blacklist entry containing any of *, ?, [ is treated as an
    fnmatch-style glob (matched against the WHOLE domain string, anchored
    both ends) rather than the plain substring match used for everything
    else — so "*.xx.com" matches "ads.xx.com" / "a.b.xx.com" but not the
    bare apex "xx.com" (add "xx.com" as its own entry too if you want that
    covered as well), while existing non-glob entries keep behaving exactly
    as before (substring match anywhere in the domain)."""
    return any(ch in entry for ch in "*?[")


def clean_rule_array(arr, blacklist_lower):
    kept, removed = [], []
    for item in arr:
        item_l = str(item).lower()
        matched = any(
            fnmatch.fnmatch(item_l, bl) if _is_glob_pattern(bl) else bl in item_l
            for bl in blacklist_lower
        )
        if matched:
            removed.append(item)
        else:
            kept.append(item)
    return kept, removed


def clean_ruleset_document(doc: dict, blacklist_lower):
    # sing-box's rule-set compiler hard-fails ("missing rule-set version") on any
    # document lacking a top-level "version". Native upstream .json files usually
    # carry this already, but hand-written custom/*.json sources easily omit it
    # (and .conf-converted docs already set it in convert_conf_to_ruleset). Default
    # to 1 — the only version sing-box currently supports — without clobbering an
    # explicit value if one is already present.
    doc.setdefault("version", 1)

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


# Reverse of CONF_TYPE_MAP — used to re-serialise a cleaned sing-box JSON
# document back into a Surge/Clash-style plain-text .conf rule list.
CONF_FIELD_TO_PREFIX = {
    "domain": "DOMAIN",
    "domain_suffix": "DOMAIN-SUFFIX",
    "domain_keyword": "DOMAIN-KEYWORD",
    "ip_cidr": "IP-CIDR",
    "process_name": "PROCESS-NAME",
}


def convert_ruleset_to_conf(doc: dict) -> str:
    """Serialise a cleaned sing-box rule-set JSON document back into a
    Surge/Clash-style plain-text .conf rule list. IPv4 / IPv6 entries in
    the `ip_cidr` field are written as `IP-CIDR` / `IP-CIDR6`
    respectively so Surge accepts the output."""
    lines = [f"{prefix},{value}" for prefix, value in iter_serialised_rule_lines(doc)]
    return "\n".join(lines) + ("\n" if lines else "")


def escape_yaml_scalar(value: str) -> str:
    """Single-quote a scalar for safe YAML output, doubling any embedded
    single quotes. This is the same escaping style mihomo's own
    rule-provider payload files use, and is what compile_to_mrs already
    does when it writes mihomo's temp payload YAML — used here too so
    every value with a comma, colon, '#', quote, or leading special
    character round-trips through .yaml without corrupting the list."""
    return "'" + str(value).replace("'", "''") + "'"

def is_ipv6_cidr(value: str) -> bool:
    """Return True if `value` is an IPv6 address or IPv6 CIDR block.

    Used to pick the correct Surge/Clash directive when re-serialising a
    sing-box rule-set's (address-family-agnostic) `ip_cidr` field back into
    a .conf / .yaml rule list: IPv6 entries must use `IP-CIDR6`, IPv4
    entries must use `IP-CIDR`. sing-box merges both families into one
    `ip_cidr` array, so the family has to be re-derived here — and getting
    it wrong is exactly why Surge rejects `IP-CIDR,<ipv6-addr>`."""
    try:
        if "/" in value:
            return ipaddress.ip_network(value, strict=False).version == 6
        return ipaddress.ip_address(value).version == 6
    except ValueError:
        return False


def iter_serialised_rule_lines(doc: dict):
    """Yield (directive_prefix, value) for every rule field in a cleaned
    sing-box rule-set document. For `ip_cidr` entries the prefix is chosen
    per value (IPv4 → IP-CIDR, IPv6 → IP-CIDR6), matching what Surge and
    mihomo actually accept; all other fields use the static CONF_FIELD_TO_
    PREFIX mapping. Unknown fields are skipped, as before."""
    for rule in doc.get("rules", []):
        if not isinstance(rule, dict):
            continue
        for field, default_prefix in CONF_FIELD_TO_PREFIX.items():
            for value in rule.get(field, []):
                if field == "ip_cidr":
                    prefix = "IP-CIDR6" if is_ipv6_cidr(str(value)) else "IP-CIDR"
                else:
                    prefix = default_prefix
                yield prefix, value

def _unescape_yaml_scalar(raw: str) -> str:
    """Inverse of escape_yaml_scalar, plus support for double-quoted and
    bare (unquoted) scalars, and stripping a trailing unquoted '# comment'.
    Scoped to single-line flow scalars only — no block scalars, no nested
    structures — matching the narrow payload-list shape this pipeline
    reads and writes."""
    raw = raw.strip()
    if raw and raw[0] not in "'\"":
        hash_idx = raw.find(" #")
        if hash_idx != -1:
            raw = raw[:hash_idx].strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1].replace("''", "'")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return raw[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return raw


def parse_yaml_payload(text: str) -> list:
    """Minimal parser for mihomo-style rule-provider YAML: a single
    top-level 'payload:' key whose value is a flat list of quoted or
    unquoted scalar strings (list items starting with '-'). This
    intentionally does NOT attempt to parse arbitrary YAML — only the
    narrow payload-list shape mihomo rule-providers (and this pipeline's
    own .yaml output) use. No external yaml library required."""
    items = []
    in_payload = False
    for raw_line in text.splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if not in_payload:
            if stripped in ("payload:", "payload:[]", "payload: []"):
                in_payload = stripped == "payload:"
            continue
        if stripped.startswith("-"):
            items.append(_unescape_yaml_scalar(stripped[1:].strip()))
        else:
            # Dedented to a new top-level key — payload list has ended.
            in_payload = False
    return items


# yaml payload lines may be either classical directive syntax identical to
# .conf ('DOMAIN-SUFFIX,example.com', reusing CONF_TYPE_MAP) or mihomo's own
# domain-behavior shorthand ('+.example.com' for a suffix, a bare domain for
# an exact match) or a bare CIDR for ip-cidr behavior — this pipeline accepts
# all three on read so it's compatible with whatever a given upstream .yaml
# actually contains, but only ever WRITES classical directive lines (see
# convert_ruleset_to_yaml) since that's the only shorthand that can carry
# every field, including domain_keyword/process_name, so writing it can
# always round-trip losslessly back through this same parser.
def yaml_payload_line_to_rule(line: str):
    """Classify one payload line into (field, value), or (None, line) if
    it doesn't match any recognized shorthand."""
    if "," in line:
        rule_type, _, value = line.partition(",")
        field = CONF_TYPE_MAP.get(rule_type.strip().upper())
        value = value.strip()
        if field and value:
            return field, value
        return None, line
    if line.startswith("+.") and len(line) > 2:
        return "domain_suffix", line[2:]
    if any(c in line for c in ("/", ":")) and not line.startswith("http"):
        return "ip_cidr", line
    if line:
        return "domain", line
    return None, line


def convert_yaml_to_ruleset(text: str):
    """Convert a mihomo-style rule-provider YAML payload into a sing-box
    rule-set JSON document. Returns (doc, stats) with the same shape as
    convert_conf_to_ruleset — same skipped-line tracking, so nothing is
    silently dropped without a trace."""
    fields = {"domain": [], "domain_suffix": [], "domain_keyword": [], "ip_cidr": [], "process_name": []}
    skipped_lines = []

    for line in parse_yaml_payload(text):
        field, value = yaml_payload_line_to_rule(line)
        if field is None:
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


def convert_ruleset_to_yaml(doc: dict) -> str:
    """Serialise a cleaned sing-box rule-set JSON document into mihomo-style
    rule-provider YAML (same classical directives .conf uses, so every
    field round-trips losslessly back through yaml_payload_line_to_rule).
    IPv4 / IPv6 `ip_cidr` entries are written as IP-CIDR / IP-CIDR6."""
    lines = [f"{prefix},{value}" for prefix, value in iter_serialised_rule_lines(doc)]
    if not lines:
        return "payload: []\n"
    out = ["payload:"]
    for line in lines:
        out.append(f"  - {escape_yaml_scalar(line)}")
    return "\n".join(out) + "\n"


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


def doc_to_mihomo_variants(doc: dict) -> dict:
    """Split a cleaned sing-box rule-set document into the payload variants
    mihomo's .mrs format can actually represent.

    mihomo's `.mrs` binary format only supports the 'domain' and 'ipcidr'
    rule-provider behaviors — never 'classical' (see mihomo's own
    rule-providers docs: "Currently, mrs behavior only supports
    domain / ipcidr."). Feeding 'classical' into `mihomo convert-ruleset`
    for an .mrs target doesn't fail cleanly; it hits an unimplemented path
    in classicalStrategy.payloadToRule and segfaults (nil pointer
    dereference). So instead of ever asking mihomo for 'classical' .mrs
    output, a mixed rule-set is split into up to two independent payloads
    here, each compiled to its own .mrs file:

      'domain' — domain / domain_suffix entries.
                 Payload: '+.example.com' (suffix) or 'example.com' (exact).
      'ipcidr' — ip_cidr entries.
                 Payload: plain CIDR notation '1.2.3.0/24'.

    domain_keyword / process_name entries have no equivalent in either
    format and can't be split into a third .mrs — they're reported back
    as dropped counts so the caller can log them (they're still preserved
    in the .srs output, since sing-box's classical behavior handles them
    fine).

    Returns {"domain": [...], "ipcidr": [...],
             "dropped_keyword": int, "dropped_process": int}.
    """
    domain_lines, ipcidr_lines = [], []
    dropped_keyword = dropped_process = 0

    for rule in doc.get("rules", []):
        if not isinstance(rule, dict):
            continue
        for value in rule.get("domain", []):
            domain_lines.append(value)
        for value in rule.get("domain_suffix", []):
            domain_lines.append(f"+.{value}")
        for value in rule.get("ip_cidr", []):
            ipcidr_lines.append(value)
        dropped_keyword += len(rule.get("domain_keyword") or [])
        dropped_process += len(rule.get("process_name") or [])

    return {
        "domain": domain_lines,
        "ipcidr": ipcidr_lines,
        "dropped_keyword": dropped_keyword,
        "dropped_process": dropped_process,
    }


def _variant_path(path: Path, variant: str) -> Path:
    """Insert '_domain' / '_ipcidr' before a path's suffix for split mrs
    output. No-op (returns path unchanged) when variant is falsy."""
    if not variant:
        return path
    return path.with_name(f"{path.stem}_{variant}{path.suffix}")


def _run_mihomo_convert(mihomo_bin: str, behavior: str, payload_lines: list, out_path: Path):
    """Invoke `mihomo convert-ruleset <behavior> yaml <tmp.yaml> <out_path>`
    for a single already-selected 'domain' or 'ipcidr' payload (never
    'classical' — see doc_to_mihomo_variants). Returns (ok: bool, message: str)."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_yaml_path = out_path.with_suffix(".mihomo_payload.tmp.yaml")
    try:
        with open(tmp_yaml_path, "w", encoding="utf-8") as f:
            f.write("payload:\n")
            for line in payload_lines:
                escaped = line.replace("'", "''")
                f.write(f"  - '{escaped}'\n")

        try:
            result = subprocess.run(
                [mihomo_bin, "convert-ruleset", behavior, "yaml", str(tmp_yaml_path), str(out_path)],
                capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError:
            return False, (f"'{mihomo_bin}' binary not found on PATH — install the mihomo "
                           f"CLI in the workflow before running this script.")
        except subprocess.TimeoutExpired:
            return False, "compile timed out after 60s"
    finally:
        tmp_yaml_path.unlink(missing_ok=True)

    if result.returncode != 0:
        return False, (result.stderr or result.stdout or "unknown compile error").strip()
    return True, f"ok (behavior={behavior})"


def compile_to_mrs(mihomo_bin: str, json_path: Path, mrs_path: Path, doc: dict = None) -> list:
    """Compile a cleaned sing-box rule-set JSON file into one or two binary
    .mrs files via mihomo's `convert-ruleset` command.

    A rule-set containing only domain/domain_suffix entries (or only
    ip_cidr entries) compiles straight to mrs_path. A rule-set that mixes
    domain and ip_cidr entries is split into two sibling files —
    '<stem>_domain.mrs' and '<stem>_ipcidr.mrs' — since mihomo's .mrs
    format can't represent 'classical'/mixed rules in one file (see
    doc_to_mihomo_variants). domain_keyword / process_name entries can't
    be represented in .mrs at all and are dropped, noted in the message.

    `doc` lets the caller pass the already-parsed, already-cleaned
    document straight from the sync stage (it was just written to
    json_path moments ago) so this doesn't re-read and re-parse the same
    JSON off disk for every single file. Falls back to reading json_path
    when doc isn't supplied, so this remains a drop-in standalone
    compiler like compile_to_srs.

    Returns a list of variant result dicts:
        [{"variant": "" | "domain" | "ipcidr", "ok": bool,
          "msg": str, "out_path": Path}, ...]
    "variant" is "" (and out_path == mrs_path) for the unsplit case.
    """
    if doc is None:
        try:
            doc = json.loads(json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            return [{"variant": "", "ok": False, "out_path": mrs_path,
                     "msg": f"could not read/parse {json_path.name} for mrs conversion: {e}"}]

    variants = doc_to_mihomo_variants(doc)
    domain_lines, ipcidr_lines = variants["domain"], variants["ipcidr"]
    dropped_note = ""
    dropped_total = variants["dropped_keyword"] + variants["dropped_process"]
    if dropped_total:
        dropped_note = (f" (dropped {dropped_total} domain_keyword/process_name "
                        f"entr{'y' if dropped_total == 1 else 'ies'} — unsupported by "
                        f".mrs domain/ipcidr formats; still present in .srs)")

    if domain_lines and not ipcidr_lines:
        ok, msg = _run_mihomo_convert(mihomo_bin, "domain", domain_lines, mrs_path)
        return [{"variant": "", "ok": ok, "msg": msg + dropped_note, "out_path": mrs_path}]

    if ipcidr_lines and not domain_lines:
        ok, msg = _run_mihomo_convert(mihomo_bin, "ipcidr", ipcidr_lines, mrs_path)
        return [{"variant": "", "ok": ok, "msg": msg + dropped_note, "out_path": mrs_path}]

    if domain_lines and ipcidr_lines:
        # Mixed rule-set — split into two independent .mrs files rather than
        # ever passing 'classical' behavior to mihomo's mrs converter. The
        # two conversions are independent subprocess calls, so run them
        # concurrently instead of waiting on one before starting the other.
        domain_path = _variant_path(mrs_path, "domain")
        ipcidr_path = _variant_path(mrs_path, "ipcidr")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            domain_future = pool.submit(_run_mihomo_convert, mihomo_bin, "domain", domain_lines, domain_path)
            ipcidr_future = pool.submit(_run_mihomo_convert, mihomo_bin, "ipcidr", ipcidr_lines, ipcidr_path)
            ok_d, msg_d = domain_future.result()
            ok_i, msg_i = ipcidr_future.result()
        return [
            {"variant": "domain", "ok": ok_d, "msg": msg_d + dropped_note, "out_path": domain_path},
            {"variant": "ipcidr", "ok": ok_i, "msg": msg_i, "out_path": ipcidr_path},
        ]

    # Neither domain nor ip_cidr entries survived — nothing left to compile
    # into .mrs (only keyword/process_name entries, or a fully empty rule-set).
    msg = "rule-set is empty after cleanup — nothing to compile into .mrs"
    if dropped_total:
        msg = (f"rule-set contains only domain_keyword/process_name entries "
               f"({dropped_total}) — unsupported by mihomo's mrs domain/ipcidr "
               f"behaviors; skipping .mrs (still covered by .srs)")
    return [{"variant": "", "ok": False, "msg": msg, "out_path": mrs_path}]


def jsdelivr_url(base_url: str, owner: str, repo: str, ref: str, rel_path: str) -> str:
    rel_path = rel_path.replace(os.sep, "/")
    return f"{base_url.rstrip('/')}/{owner}/{repo}@{ref}/{rel_path}"


def sync_local_tree(namespace: Path, upstream_dir: Path, json_output_dir: Path,
                    conf_output_dir: Path, yaml_output_dir: Path,
                    blacklist_lower: list, required: bool = True):
    """Core sync logic shared by both configured upstream sources and the
    local custom/ directory. Every file under upstream_dir is synced,
    regardless of format. Every PARSEABLE format (.json/.conf/.list/.txt/
    .yaml/.yml) is parsed into one canonical cleaned rule-set document and
    then re-serialised into ALL THREE text trees (json/conf/yaml) from that
    single cleaned document — so a file's json/conf/yaml renditions can
    never drift out of sync with each other, regardless of which format it
    originated from. Pre-compiled .srs/.mrs and any other unrecognized
    format are copied through byte-for-byte into json_output_dir (used as
    a holding area for the compile stage's copy-through into srs/mrs).
    Mirrors the (possibly multi-level) subdirectory structure. Returns a
    result dict."""
    namespace_str = str(namespace)

    if not upstream_dir.exists():
        if required:
            print(f"ERROR: source dir not found for '{namespace_str}': {upstream_dir}\n"
                  f"Did the workflow's sparse-checkout step run for this source first?", file=sys.stderr)
        return {
            "namespace": namespace_str, "namespace_path": namespace,
            "json_output_dir": json_output_dir, "conf_output_dir": conf_output_dir,
            "yaml_output_dir": yaml_output_dir, "per_file_reports": [],
            "added": [], "updated": [], "deleted": [], "unchanged": [], "error": required,
        }

    upstream_files = sorted(
        p for p in upstream_dir.rglob("*")
        if p.is_file() and not _is_git_internal(p.relative_to(upstream_dir))
    )
    kind_counts = {"json": 0, "conf": 0, "list": 0, "text": 0, "yaml": 0, "srs": 0, "mrs": 0, "other": 0}
    for p in upstream_files:
        suf = p.suffix.lower()
        kind_counts["json" if suf == ".json" else "conf" if suf == ".conf" else
        "list" if suf == ".list" else "text" if suf == ".txt" else
        "yaml" if suf in (".yaml", ".yml") else
        "srs" if suf == ".srs" else "mrs" if suf == ".mrs" else "other"] += 1
    print(f"[{namespace_str}] Found {len(upstream_files)} files ({upstream_dir}): "
          f"{kind_counts['json']} json, {kind_counts['conf']} conf, "
          f"{kind_counts['list']} list, {kind_counts['text']} txt, "
          f"{kind_counts['yaml']} yaml, "
          f"{kind_counts['srs']} srs, {kind_counts['mrs']} mrs, "
          f"{kind_counts['other']} other — json/conf/list/txt/yaml are converted, "
          f"srs/mrs are copied through, other synced as-is.")

    # Canonical diff key for every parseable file is its .json rendition's
    # content, scanned from json_output_dir alone — conf/yaml renditions are
    # always regenerated in lockstep from the same cleaned document, so they
    # never need their own independent diff. Pre-compiled srs/mrs/other
    # copy-through files also live in json_output_dir (by their own
    # extension, so no collision with a same-named .json).
    existing_before = {}
    if json_output_dir.exists():
        for p in json_output_dir.rglob("*"):
            if p.is_file():
                existing_before[str(p.relative_to(json_output_dir))] = p

    def read_old_bytes(rel: str):
        p = existing_before.get(rel)
        if p is None:
            return None
        try:
            return p.read_bytes()
        except OSError:
            return None

    json_output_dir.mkdir(parents=True, exist_ok=True)
    conf_output_dir.mkdir(parents=True, exist_ok=True)
    yaml_output_dir.mkdir(parents=True, exist_ok=True)

    per_file_reports = []
    added, updated, deleted, unchanged = [], [], [], []
    seen_relpaths = set()          # every json_output_dir rel_path written this run
    seen_parseable_json_rels = set()  # subset of the above that came from a parseable file

    PARSE_KIND_BY_SUFFIX = {
        ".json": "json", ".conf": "conf", ".list": "list", ".txt": "text",
        ".yaml": "yaml", ".yml": "yaml",
    }

    for src_path in upstream_files:
        rel_path_src = str(src_path.relative_to(upstream_dir))
        suffix = src_path.suffix.lower()
        kind = PARSE_KIND_BY_SUFFIX.get(suffix)

        if kind is not None:
            json_rel = str(Path(rel_path_src).with_suffix(".json"))
            conf_rel = str(Path(rel_path_src).with_suffix(".conf"))
            yaml_rel = str(Path(rel_path_src).with_suffix(".yaml"))
            seen_relpaths.add(json_rel)
            seen_parseable_json_rels.add(json_rel)

            if kind == "json":
                try:
                    doc = json.loads(src_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as e:
                    print(f"  [{namespace_str}] ! Skipping {rel_path_src}: invalid JSON ({e})", file=sys.stderr)
                    continue
                if not isinstance(doc, dict):
                    # A sing-box rule-set document is always a top-level JSON
                    # object ({"version": ..., "rules": [...]}) — anything
                    # else (a bare array, a string, a number) isn't a
                    # rule-set at all, just a same-extension file that
                    # happens to live in the synced tree (e.g. a plain
                    # domain/IP list in some upstream's own legacy format).
                    # Treat it like invalid JSON: skip and log, don't crash
                    # the whole sync over one unrelated file.
                    print(f"  [{namespace_str}] ! Skipping {rel_path_src}: "
                          f"top-level JSON is a {type(doc).__name__}, not an object "
                          f"— not a sing-box rule-set", file=sys.stderr)
                    continue
                conv_stats = None
            else:
                try:
                    src_text = src_path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as e:
                    print(f"  [{namespace_str}] ! Skipping {rel_path_src}: unreadable ({e})", file=sys.stderr)
                    continue
                if kind == "yaml":
                    doc, conv_stats = convert_yaml_to_ruleset(src_text)
                else:  # conf, list, text — identical directive-line syntax
                    doc, conv_stats = convert_conf_to_ruleset(src_text)

            cleaned_doc, stats = clean_ruleset_document(doc, blacklist_lower)

            removed_counts = {k: len(v) for k, v in stats["removed"].items()}
            total_removed = sum(removed_counts.values())

            report = {
                "file": json_rel, "kind": kind,
                "rules_total": stats["rules_total"],
                "rules_discarded": stats["rules_discarded"],
                "removed_counts": removed_counts,
                "removed_values": stats["removed"],
                "fields_cleared": stats["fields_cleared"],
                "action": None,
            }
            if kind != "json":
                report["source_file"] = rel_path_src
                report["conf_converted_counts"] = conv_stats["converted_counts"]
                report["conf_skipped_count"] = conv_stats["skipped_count"]
                report["conf_skipped_lines"] = conv_stats["skipped_lines"]

            old_bytes = read_old_bytes(json_rel)

            if cleaned_doc is None:
                report["action"] = "discarded_all_rules_empty_file"
                if old_bytes is not None:
                    deleted.append(json_rel)
                    (json_output_dir / json_rel).unlink(missing_ok=True)
                    (conf_output_dir / conf_rel).unlink(missing_ok=True)
                    (yaml_output_dir / yaml_rel).unlink(missing_ok=True)
                per_file_reports.append(report)
                continue

            new_bytes = canonical_dump(cleaned_doc).encode("utf-8")
            # Kept for the compile stage, so it can compile straight from
            # this already-parsed, already-cleaned document instead of
            # re-reading and re-parsing the JSON file it's about to write.
            report["_cleaned_doc"] = cleaned_doc

            if old_bytes is None:
                report["action"] = "added"
                added.append(json_rel)
            elif old_bytes != new_bytes:
                report["action"] = "updated"
                updated.append(json_rel)
            else:
                report["action"] = "unchanged"
                unchanged.append(json_rel)

            json_local_path = json_output_dir / json_rel
            json_local_path.parent.mkdir(parents=True, exist_ok=True)
            json_local_path.write_bytes(new_bytes)

            conf_local_path = conf_output_dir / conf_rel
            conf_local_path.parent.mkdir(parents=True, exist_ok=True)
            conf_local_path.write_text(convert_ruleset_to_conf(cleaned_doc), encoding="utf-8")

            yaml_local_path = yaml_output_dir / yaml_rel
            yaml_local_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_local_path.write_text(convert_ruleset_to_yaml(cleaned_doc), encoding="utf-8")

            per_file_reports.append(report)

            if kind == "json":
                if VERBOSE and (total_removed or stats["rules_discarded"]):
                    print(f"  [{namespace_str}] {json_rel}: -{total_removed} entries "
                          f"(domain={removed_counts['domain']}, "
                          f"suffix={removed_counts['domain_suffix']}, "
                          f"keyword={removed_counts['domain_keyword']}), "
                          f"{stats['rules_discarded']} rule(s) discarded")
            else:
                convc = conv_stats["converted_counts"]
                if VERBOSE:
                    print(f"  [{namespace_str}] {rel_path_src} -> {json_rel} + {conf_rel} + {yaml_rel}: "
                          f"converted (domain={convc['domain']}, suffix={convc['domain_suffix']}, "
                          f"keyword={convc['domain_keyword']}, ip_cidr={convc['ip_cidr']}, "
                          f"process_name={convc['process_name']}), "
                          f"{conv_stats['skipped_count']} line(s) unsupported/skipped"
                          + (f", -{total_removed} blacklisted after conversion" if total_removed else ""))

        else:
            # Pre-compiled .srs/.mrs (copied through json_output_dir as a
            # holding area for the compile stage's copy into srs/mrs) or any
            # other unrecognized format (synced as-is, never compiled/linked).
            rel_path = rel_path_src
            file_kind = "srs" if suffix == ".srs" else "mrs" if suffix == ".mrs" else "other"
            seen_relpaths.add(rel_path)
            local_path = json_output_dir / rel_path
            try:
                new_bytes = src_path.read_bytes()
            except OSError as e:
                print(f"  [{namespace_str}] ! Skipping {rel_path}: unreadable ({e})", file=sys.stderr)
                continue

            report = {
                "file": rel_path, "kind": file_kind,
                "rules_total": 0, "rules_discarded": 0,
                "removed_counts": {"domain": 0, "domain_suffix": 0, "domain_keyword": 0},
                "removed_values": {"domain": [], "domain_suffix": [], "domain_keyword": []},
                "fields_cleared": [], "action": None,
            }

            old_bytes = read_old_bytes(rel_path)
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

    # Delete anything left in json_output_dir that wasn't touched this run.
    for rel_path, p_path in existing_before.items():
        if rel_path not in seen_relpaths:
            if p_path.exists():
                p_path.unlink()
            deleted.append(rel_path)

    # conf/ and yaml/ renditions are derived 1:1 from json_output_dir's
    # parseable entries — sweep out any stale ones left over from a file
    # that's since been renamed/removed/emptied upstream.
    if conf_output_dir.exists():
        for p in conf_output_dir.rglob("*"):
            if p.is_file() and str(p.relative_to(conf_output_dir).with_suffix(".json")) not in seen_parseable_json_rels:
                p.unlink()
    if yaml_output_dir.exists():
        for p in yaml_output_dir.rglob("*"):
            if p.is_file() and str(p.relative_to(yaml_output_dir).with_suffix(".json")) not in seen_parseable_json_rels:
                p.unlink()

    return {
        "namespace": namespace_str, "namespace_path": namespace,
        "json_output_dir": json_output_dir, "conf_output_dir": conf_output_dir,
        "yaml_output_dir": yaml_output_dir, "per_file_reports": per_file_reports,
        "added": added, "updated": updated, "deleted": deleted, "unchanged": unchanged,
        "error": False,
    }


def process_source(source: dict, sync_cfg: dict, blacklist_lower: list):
    """Sync + clean one configured upstream source. All paths are namespaced
    under @<owner>/<repo>/<branch>/<directory_name>/... (each of which may
    itself be a multi-level path, e.g. directory_name="sing-box/Clash")."""
    namespace = source_namespace(source)
    checkout_root = ROOT / sync_cfg.get("upstream_checkout_root", "@rules")
    upstream_dir = checkout_root / namespace
    json_output_dir = ROOT / "json" / namespace
    conf_output_dir = ROOT / "conf" / namespace
    yaml_output_dir = ROOT / "yaml" / namespace
    return sync_local_tree(namespace, upstream_dir, json_output_dir, conf_output_dir,
                           yaml_output_dir, blacklist_lower, required=True)


# Dated snapshot directories (YYYYMMDD, see mirror_raw_files) are written
# nested inside the very same namespace directory as the "latest" copies —
# This pipeline no longer writes dated snapshot subdirectories under
# files/<ns>/ (the destination repo's main branch is itself a full,
# continuously-overwritten snapshot each run, so a per-run date layer
# would just be redundant). This filter is kept only so a repo that still
# has leftover dated dirs from before this change doesn't have them
# mistaken for stale "latest" files and deleted out of the diff.
_DATE_SEGMENT_RE = re.compile(r"^\d{8}$")


def mirror_raw_files(source: dict, sync_cfg: dict):
    """Mirror one source's fetched files byte-for-byte into files/, with NO
    parsing, cleaning (blacklist or otherwise), conversion, or compilation
    whatsoever. Runs unconditionally for every source — this is not an
    opt-in — as a companion tree sitting alongside json/conf/yaml/srs/mrs,
    so the exact raw upstream content is always available too.

    Layout mirrors srs/ and mrs/ exactly — always-current content only, no
    dated snapshots (the output repo's main branch is itself a full,
    continuously-overwritten snapshot each run, so a separate per-run date
    layer would just be redundant history-keeping the destination repo
    already doesn't want):

        files/@<owner>/<repo>/<branch>/<dir>/<subdir>/<file>
    """
    namespace = source_namespace(source)
    namespace_str = str(namespace)
    checkout_root = ROOT / sync_cfg.get("upstream_checkout_root", "@rules")
    upstream_dir = checkout_root / namespace
    files_root = ROOT / "files"
    latest_dir = files_root / namespace

    if not upstream_dir.exists():
        # process_source (running for the same source) already reports the
        # missing-checkout error; nothing further to log here.
        return {
            "namespace": namespace_str,
            "added": [], "updated": [], "deleted": [], "unchanged": [],
            "error": True,
        }

    upstream_files = sorted(
        p for p in upstream_dir.rglob("*")
        if p.is_file() and not _is_git_internal(p.relative_to(upstream_dir))
    )

    existing_before = {}
    if latest_dir.exists():
        for p in latest_dir.rglob("*"):
            if p.is_file():
                rel = p.relative_to(latest_dir)
                if any(_DATE_SEGMENT_RE.match(part) for part in rel.parts):
                    continue  # nested dated snapshot, not a "latest" file
                existing_before[str(rel)] = p

    added, updated, deleted, unchanged = [], [], [], []
    seen_relpaths = set()

    for src_path in upstream_files:
        rel = Path(src_path.relative_to(upstream_dir))
        rel_path = str(rel)
        seen_relpaths.add(rel_path)

        try:
            new_bytes = src_path.read_bytes()
        except OSError as e:
            print(f"  [{namespace_str}] ! files/ mirror: skipping {rel_path}: unreadable ({e})", file=sys.stderr)
            continue

        old_path = existing_before.get(rel_path)
        old_bytes = None
        if old_path is not None:
            try:
                old_bytes = old_path.read_bytes()
            except OSError:
                old_bytes = None

        if old_bytes is None:
            action = "added"
            added.append(rel_path)
        elif old_bytes != new_bytes:
            action = "updated"
            updated.append(rel_path)
        else:
            action = "unchanged"
            unchanged.append(rel_path)

        # Always-current copy, sitting at the same level srs/mrs/json/conf/
        # yaml put their files: files/<ns>/<subdir>/<file>
        latest_path = latest_dir / rel
        latest_path.parent.mkdir(parents=True, exist_ok=True)
        latest_path.write_bytes(new_bytes)

    # Delete stale files this run didn't see.
    for rel_path, p_path in existing_before.items():
        if rel_path not in seen_relpaths:
            if p_path.exists():
                p_path.unlink()
            deleted.append(rel_path)

    print(f"[{namespace_str}] files/ mirror: {len(upstream_files)} raw file(s) "
          f"({len(added)} added, {len(updated)} updated, {len(deleted)} deleted, "
          f"{len(unchanged)} unchanged).")

    return {
        "namespace": namespace_str,
        "added": added, "updated": updated, "deleted": deleted, "unchanged": unchanged,
        "error": False,
    }


def process_custom(sync_cfg: dict, blacklist_lower: list):
    """Clean the local custom/ directory (checked into this repo directly,
    not fetched from any upstream). Supports arbitrary multi-level
    subdirectories under custom/. Optional — if the directory doesn't exist
    or is empty, this is silently skipped rather than treated as an error."""
    custom_dir_name = sync_cfg.get("custom_dir_name", "custom")
    namespace = Path(custom_dir_name)
    upstream_dir = ROOT / custom_dir_name
    json_output_dir = ROOT / "json" / custom_dir_name
    conf_output_dir = ROOT / "conf" / custom_dir_name
    yaml_output_dir = ROOT / "yaml" / custom_dir_name
    return sync_local_tree(namespace, upstream_dir, json_output_dir, conf_output_dir,
                           yaml_output_dir, blacklist_lower, required=False)


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

    # logs/ otherwise accumulates one sync_*.log + summary_*.md pair per
    # run forever. Only this run's pair is meaningful going forward, so
    # every other dated log/summary file is removed up front — leaving
    # exactly one (the one about to be written) once this run completes.
    for pattern in ("sync_*.log", "summary_*.md"):
        for old_path in LOG_DIR.glob(pattern):
            if old_path.name not in (f"sync_{run_ts}.log", f"summary_{run_ts}.md"):
                old_path.unlink(missing_ok=True)

    # Each source (and custom/) reads/writes an entirely independent set of
    # directories, so there's no shared state to worry about — sync them
    # concurrently instead of one at a time. This is disk/IO-bound work
    # (lots of small file reads/writes per source), so threads are enough;
    # no need for separate processes.
    #
    # Every source runs through both process_source (parse + clean +
    # convert, feeding the later compile stage) AND mirror_raw_files (an
    # unconditional, unmodified byte-for-byte copy into files/) — the two
    # are independent reads of the same fetched directory, dispatched into
    # the same pool so neither can serialize the whole run.
    sync_workers = sync_cfg.get("sync_workers") or min(8, max(1, len(sources) * 2 + (1 if enable_custom else 0)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=sync_workers) as pool:
        source_futures = [pool.submit(process_source, src, sync_cfg, blacklist_lower) for src in sources]
        raw_mirror_futures = [pool.submit(mirror_raw_files, src, sync_cfg) for src in sources]
        custom_future = pool.submit(process_custom, sync_cfg, blacklist_lower) if enable_custom else None
        source_results = [f.result() for f in source_futures]
        raw_mirror_results = [f.result() for f in raw_mirror_futures]
        if custom_future is not None:
            source_results.append(custom_future.result())

    all_added = sum((r["added"] for r in source_results), [])
    all_updated = sum((r["updated"] for r in source_results), [])
    all_deleted = sum((r["deleted"] for r in source_results), [])
    all_unchanged = sum((r["unchanged"] for r in source_results), [])
    changed = bool(all_added or all_updated or all_deleted)

    raw_added = sum((r["added"] for r in raw_mirror_results), [])
    raw_updated = sum((r["updated"] for r in raw_mirror_results), [])
    raw_deleted = sum((r["deleted"] for r in raw_mirror_results), [])
    raw_unchanged = sum((r["unchanged"] for r in raw_mirror_results), [])
    changed = changed or bool(raw_added or raw_updated or raw_deleted)

    # ---------- CDN identity (needed for both the srs output path and the links) ----------
    cdn_base_url = sync_cfg.get("cdn_base_url", "https://testingcf.jsdelivr.net/gh")
    cdn_ref_mode = sync_cfg.get("cdn_ref_mode", "tag")  # "tag" or "branch"
    cdn_branch = sync_cfg.get("cdn_branch", "main")  # destination repo's build-output branch
    tag_name = f"v{run_ts}"
    cdn_ref = cdn_branch if cdn_ref_mode == "branch" else tag_name

    repo_slug = os.environ.get("GITHUB_REPOSITORY")  # "owner/repo", set by GitHub Actions
    if repo_slug and "/" in repo_slug:
        cdn_owner, cdn_repo = repo_slug.split("/", 1)
    else:
        cdn_owner = sync_cfg.get("cdn_owner", "YOUR_GITHUB_USERNAME")
        cdn_repo = sync_cfg.get("cdn_repo", "YOUR_REPO_NAME")

    # ---------- Compile every kept JSON file to .srs AND .mrs, per source ----------
    sing_box_bin = sync_cfg.get("sing_box_bin", "sing-box")
    mihomo_bin = sync_cfg.get("mihomo_bin", "mihomo")

    # Each file's srs/mrs compile is a separate external-process invocation
    # (`sing-box rule-set compile`, `mihomo convert-ruleset`) that spends
    # almost all of its wall-clock time waiting on that subprocess, not
    # doing Python-side work — a textbook case for thread-pool concurrency
    # (subprocess.run releases the GIL while the child runs). Running these
    # one at a time, as before, means total wall time scales linearly with
    # file count purely from per-process spawn overhead; running them
    # concurrently collapses that to roughly (file count / worker count).
    # Override via sync.json's "compile_workers"; default scales with the
    # runner's CPU count but is capped to avoid overwhelming it, since the
    # compiler binaries themselves also use CPU once running.
    compile_workers = sync_cfg.get("compile_workers") or min(16, max(4, (os.cpu_count() or 4) * 2))

    def build_work_items(precompiled_kind: str):
        items = []
        for res in source_results:
            if res["error"]:
                continue
            namespace_path = res["namespace_path"]
            json_output_dir = res["json_output_dir"]
            kept_relpaths = sorted(set(res["added"]) | set(res["updated"]) | set(res["unchanged"]))
            report_lookup = {r["file"]: r for r in res["per_file_reports"]}

            for rel_path in kept_relpaths:
                r = report_lookup.get(rel_path)
                kind = r["kind"] if r else None
                if kind == "other":
                    continue
                if kind not in ("json", "conf", "list", "text", "yaml", precompiled_kind):
                    continue
                items.append((res, namespace_path, json_output_dir, rel_path, kind, r.get("_cleaned_doc")))
        return items

    def run_compile_stage(format_name: str, binary: str, compiler_fn, precompiled_kind: str):
        format_root = ROOT / format_name
        work_items = build_work_items(precompiled_kind)
        if not work_items:
            return []

        def compile_one(item):
            res, namespace_path, json_output_dir, rel_path, kind, cleaned_doc = item
            file_results = []

            # Every parseable kind's canonical cleaned document lives in
            # json_output_dir at rel_path (always the .json rendition —
            # see sync_local_tree). Compilation always reads from there
            # regardless of the file's original source format.
            source_file_path = json_output_dir / rel_path

            rel = Path(rel_path)
            out_filename = rel.with_suffix(f".{format_name}").name
            subdir = rel.parent
            if str(subdir) in (".", ""):
                latest_rel = namespace_path / out_filename
            else:
                latest_rel = namespace_path / subdir / out_filename

            latest_path = format_root / latest_rel

            if kind in ("json", "conf", "list", "text", "yaml"):
                if format_name == "mrs":
                    raw = compiler_fn(binary, source_file_path, latest_path, cleaned_doc)
                    variant_results = raw  # already [{"variant","ok","msg","out_path"}, ...]
                else:
                    ok, msg = compiler_fn(binary, source_file_path, latest_path)
                    variant_results = [{"variant": "", "ok": ok, "msg": msg, "out_path": latest_path}]
            else:  # precompiled_kind
                try:
                    latest_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source_file_path, latest_path)
                    ok, msg = True, f"synced (pre-compiled upstream .{format_name}, not recompiled)"
                except OSError as e:
                    ok, msg = False, f"failed to copy pre-compiled .{format_name}: {e}"
                    print(f"  [{res['namespace']}] ! {msg} ({rel_path})", file=sys.stderr)
                variant_results = [{"variant": "", "ok": ok, "msg": msg, "out_path": latest_path}]

            for vr in variant_results:
                variant, ok, msg = vr["variant"], vr["ok"], vr["msg"]
                actual_latest_rel = _variant_path(latest_rel, variant)

                if not ok:
                    tag = f" ({variant})" if variant else ""
                    print(f"  [{res['namespace']}] ! {format_name.upper()}{tag} compile failed "
                          f"for {rel_path}: {msg}", file=sys.stderr)

                file_results.append((
                    format_name, res["namespace"], rel_path, ok, msg,
                    f"json/{namespace_path.as_posix()}/{rel_path}",
                    actual_latest_rel.as_posix() if ok else None,
                    actual_latest_rel.as_posix() if ok else None, variant,
                ))

            return file_results

        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=compile_workers) as pool:
            for file_results in pool.map(compile_one, work_items):
                results.extend(file_results)
        return results

    # The SRS and MRS stages are fully independent of each other (different
    # binaries, different output trees), so run them concurrently too
    # rather than one stage completing before the next starts.
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as stage_pool:
        srs_future = stage_pool.submit(run_compile_stage, "srs", sing_box_bin, compile_to_srs, "srs")
        mrs_future = stage_pool.submit(run_compile_stage, "mrs", mihomo_bin, compile_to_mrs, "mrs")
        srs_compile_results = srs_future.result()
        mrs_compile_results = mrs_future.result()
    compile_results = srs_compile_results + mrs_compile_results

    compile_failures = [r for r in compile_results if not r[3]]
    compile_ok_count = len(compile_results) - len(compile_failures)
    srs_ok_count = len(srs_compile_results) - len([r for r in srs_compile_results if not r[3]])
    mrs_ok_count = len(mrs_compile_results) - len([r for r in mrs_compile_results if not r[3]])

    # ---------- Detailed log ----------
    # kind_totals tracks the processed kinds; the files/ mirror is counted
    # separately below via the raw_mirror_results.
    kind_totals = {"json": 0, "conf": 0, "list": 0, "text": 0, "yaml": 0,
                   "srs": 0, "mrs": 0, "other": 0}
    other_files = []  # (namespace, rel_path) — synced but not eligible for conversion
    for res in source_results:
        kept = set(res["added"]) | set(res["updated"]) | set(res["unchanged"])
        for r in res["per_file_reports"]:
            if r["file"] in kept:
                k = r["kind"]
                kind_totals[k] = kind_totals.get(k, 0) + 1
                if k == "other":
                    other_files.append((res["namespace"], r["file"]))

    detail_log_path = LOG_DIR / f"sync_{run_ts}.log"
    with open(detail_log_path, "w", encoding="utf-8") as f:
        f.write(f"Sync run: {run_ts}\n")
        f.write(f"Sources ({len(sources)}{' + custom' if enable_custom else ''}):\n")
        for src in sources:
            clone_url = src.get("repo_url") or f"https://github.com/{src['owner']}/{src['repo']}.git"
            f.write(f"  - {source_namespace(src)}  <-  "
                    f"{clone_url}@{src['branch']}:{src['upstream_path']}\n")
        if enable_custom:
            f.write(f"  - {sync_cfg.get('custom_dir_name', 'custom')}  <-  local (not fetched)\n")
        f.write(f"Blacklist entries: {len(blacklist)}\n")
        f.write(f"  {blacklist}\n\n")
        f.write(f"Files added:   {len(all_added)}\n")
        f.write(f"Files updated: {len(all_updated)}\n")
        f.write(f"Files deleted: {len(all_deleted)}\n")
        f.write(f"Files unchanged: {len(all_unchanged)}\n\n")
        f.write(f"Raw files/ mirror: {len(raw_added)} added, {len(raw_updated)} updated, "
                f"{len(raw_deleted)} deleted, {len(raw_unchanged)} unchanged "
                f"(byte-for-byte, no cleaning/conversion)\n\n")
        f.write(f"File kinds synced this run: "
                f"{kind_totals['json']} json (cleaned → json+conf+yaml+srs+mrs), "
                f"{kind_totals['conf']} conf (converted → json+conf+yaml+srs+mrs), "
                f"{kind_totals['list']} list (converted → json+conf+yaml+srs+mrs), "
                f"{kind_totals['text']} txt (converted → json+conf+yaml+srs+mrs), "
                f"{kind_totals['yaml']} yaml (converted → json+conf+yaml+srs+mrs), "
                f"{kind_totals['srs']} srs (pre-compiled, copied through), "
                f"{kind_totals['mrs']} mrs (pre-compiled, copied through), "
                f"{kind_totals['other']} other (synced as-is, not converted)\n")
        if other_files:
            f.write("Synced but not converted (not json/conf/list/txt/yaml/srs/mrs):\n")
            for namespace, rel_path in other_files:
                f.write(f"  - [{namespace}] {rel_path}\n")
        f.write(f"\nSRS compiled/copied: {srs_ok_count}/{len(srs_compile_results)}\n")
        f.write(f"MRS compiled/copied: {mrs_ok_count}/{len(mrs_compile_results)}\n")
        if compile_failures:
            f.write("SRS/MRS compile/copy failures:\n")
            for format_name, namespace, rel_path, ok, msg, synced_out_rel, dated_rel, latest_rel, variant in compile_failures:
                tag = f" ({variant})" if variant else ""
                f.write(f"  - [{format_name}{tag}] [{namespace}] {rel_path}: {msg}\n")
        f.write("\n")
        f.write("=" * 70 + "\n")
        src_ext_by_kind = {"conf": ".conf", "list": ".list", "text": ".txt", "yaml": ".yaml"}
        for res in source_results:
            for r in res["per_file_reports"]:
                if "source_file" in r:
                    f.write(f"\n[{res['namespace']}] File: {r['source_file']} -> {r['file']}  "
                            f"[{r['kind']}] [{r['action']}]\n")
                else:
                    f.write(f"\n[{res['namespace']}] File: {r['file']}  [{r['kind']}] [{r['action']}]\n")

                if r["kind"] not in ("json", "conf", "list", "text", "yaml"):
                    continue

                if r["kind"] in src_ext_by_kind:
                    src_ext = src_ext_by_kind[r["kind"]]
                    cc = r["conf_converted_counts"]
                    f.write(f"  Converted from {src_ext} -> domain: {cc['domain']}, "
                            f"domain_suffix: {cc['domain_suffix']}, "
                            f"domain_keyword: {cc['domain_keyword']}, "
                            f"ip_cidr: {cc['ip_cidr']}, process_name: {cc['process_name']}\n")
                    if r["conf_skipped_count"]:
                        f.write(f"  Skipped {r['conf_skipped_count']} unsupported {src_ext} line(s):\n")
                        for line in r["conf_skipped_lines"]:
                            f.write(f"    - {line}\n")

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

    # Per-source added/updated/deleted breakdown, in source order (sources
    # first, custom/ last — matches how source_results was built above).
    # Every source also mirrors its raw files into files/ unconditionally.
    source_entries = []
    for src, res in zip(sources, source_results):
        upstream_cell = f"`{src['owner']}/{src['repo']}` @ `{src['branch']}` (`{src['upstream_path']}`)"
        source_entries.append((
            f"`{source_namespace(src)}`",
            upstream_cell,
            res,
        ))
    if enable_custom and len(source_results) > len(sources):
        custom_res = source_results[len(sources)]
        source_entries.append((
            f"`{sync_cfg.get('custom_dir_name', 'custom')}`", "local files (not fetched)", custom_res,
        ))

    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"## 🔄 Sync summary — `{run_ts}`\n\n")

        f.write("### 📦 Sources\n\n")
        f.write("| Namespace | Upstream | Added | Updated | Deleted |\n")
        f.write("|:----------|:---------|------:|--------:|--------:|\n")
        for namespace_cell, upstream_cell, res in source_entries:
            f.write(f"| {namespace_cell} | {upstream_cell} "
                    f"| {len(res['added'])} | {len(res['updated'])} | {len(res['deleted'])} |\n")
        f.write("\n")

        f.write("### 📊 Overview\n\n")
        f.write("| Metric | Count |\n")
        f.write("|:-------|------:|\n")
        f.write(f"| Files added | **{len(all_added)}** |\n")
        f.write(f"| Files updated | **{len(all_updated)}** |\n")
        f.write(f"| Files deleted | **{len(all_deleted)}** |\n")
        f.write(f"| Files unchanged | {len(all_unchanged)} |\n")
        f.write(f"| Blacklisted entries removed | **{total_removed_all}** |\n")
        f.write(f"| Empty rules discarded | **{total_discarded_rules}** |\n")
        f.write(f"| Files dropped entirely (all rules emptied) | **{len(discarded_files)}** |\n")
        f.write(f"| SRS files compiled/copied | **{srs_ok_count}/{len(srs_compile_results)}** |\n")
        f.write(f"| MRS files compiled/copied | **{mrs_ok_count}/{len(mrs_compile_results)}** |\n")
        f.write(f"| Raw files/ added/updated/deleted | **{len(raw_added)}/{len(raw_updated)}/{len(raw_deleted)}** |\n\n")

        if compile_failures:
            f.write("### ⚠️ Compile failures\n\n")
            f.write("| Format | Source | File | Reason |\n")
            f.write("|:-------|:-------|:-----|:-------|\n")
            for r in compile_failures:
                f.write(f"| `{r[0]}` | `{r[1]}` | `{r[2]}` | {r[4]} |\n")
            f.write("\n")

        def write_file_list(title: str, emoji: str, paths: list):
            if not paths:
                return
            f.write(f"<details>\n<summary>{emoji} <strong>{title}</strong> ({len(paths)})</summary>\n\n")
            f.write("\n".join(f"- `{p}`" for p in sorted(paths)))
            f.write("\n\n</details>\n\n")

        write_file_list("Added", "🆕", added_ns)
        write_file_list("Updated", "✏️", updated_ns)
        write_file_list("Deleted", "🗑️", deleted_ns)
        write_file_list("Dropped (all rules removed by blacklist)", "🚫", discarded_files)

        f.write("---\n\n")
        f.write(f"📄 Full per-file cleanup detail: [`logs/sync_{run_ts}.log`](../logs/sync_{run_ts}.log)\n\n")
        f.write(f"🔗 CDN ref for this release: `{cdn_ref}` (see [`README.md`](../README.md) "
                f"for the latest-run summary)\n")

    # ---------- Build README.md (overwrite-only, one dated entry) ----------
    # Overwritten in full every run — it reflects only the latest run's
    # state and is NOT an appended changelog, so the file can never grow
    # unbounded no matter how many runs execute over time.
    readme_path = ROOT / "README.md"

    if changed:
        # The README entry is built directly from the same data used for
        # summary.md — not by text-filtering summary.md — so it can't
        # drift out of sync with what that data actually contains. It
        # keeps the per-source stats table (added/updated/deleted per
        # repo) and the file-level Updated/Deleted detail, but always
        # drops "Compile failures" (an internal build diagnostic with no
        # reader value here) as well as the Added/Dropped file lists (too
        # noisy; the per-source counts above already cover "how much
        # changed").
        entry_lines = [f"## 🚀 `{tag_name}`", f"*{run_ts.replace('_', ' ')} (UTC+8)*", ""]

        entry_lines += ["### 📦 Sources", "", "| Namespace | Upstream | Added | Updated | Deleted |",
                        "|:----------|:---------|------:|--------:|--------:|"]
        for namespace_cell, upstream_cell, res in source_entries:
            entry_lines.append(f"| {namespace_cell} | {upstream_cell} "
                               f"| {len(res['added'])} | {len(res['updated'])} | {len(res['deleted'])} |")
        entry_lines.append("")

        entry_lines += ["### 📊 Overview", "", "| Metric | Count |", "|:-------|------:|",
                        f"| Files added | **{len(all_added)}** |",
                        f"| Files updated | **{len(all_updated)}** |",
                        f"| Files deleted | **{len(all_deleted)}** |",
                        f"| Files unchanged | {len(all_unchanged)} |",
                        f"| Blacklisted entries removed | **{total_removed_all}** |",
                        f"| Empty rules discarded | **{total_discarded_rules}** |",
                        f"| Files dropped entirely (all rules emptied) | **{len(discarded_files)}** |",
                        f"| SRS files compiled/copied | **{srs_ok_count}/{len(srs_compile_results)}** |",
                        f"| MRS files compiled/copied | **{mrs_ok_count}/{len(mrs_compile_results)}** |",
                        f"| Raw files/ added/updated/deleted | **{len(raw_added)}/{len(raw_updated)}/{len(raw_deleted)}** |", ""]

        if updated_ns:
            entry_lines += [f"<details>\n<summary>✏️ <strong>Updated</strong> ({len(updated_ns)})</summary>", ""]
            entry_lines += [f"- `{p}`" for p in sorted(updated_ns)]
            entry_lines += ["", "</details>", ""]
        if deleted_ns:
            entry_lines += [f"<details>\n<summary>🗑️ <strong>Deleted</strong> ({len(deleted_ns)})</summary>", ""]
            entry_lines += [f"- `{p}`" for p in sorted(deleted_ns)]
            entry_lines += ["", "</details>", ""]

        entry_lines += [f"🔗 CDN ref for this release: `{cdn_ref}`"]

        entry = "\n".join(entry_lines).rstrip("\n")
        new_readme = f"# Latest sync\n\n{entry}\n"
        readme_path.write_text(new_readme, encoding="utf-8")

    # No CHANGELOG.md file is created anymore.

    gha_output = os.environ.get("GITHUB_OUTPUT")
    if gha_output:
        with open(gha_output, "a", encoding="utf-8") as f:
            f.write(f"changed={'true' if changed else 'false'}\n")
            f.write(f"tag={tag_name}\n")
            f.write(f"summary_path={summary_path.relative_to(ROOT)}\n")
            f.write(f"links_path={readme_path.relative_to(ROOT)}\n")

    print(f"\nDone. changed={changed}")
    print(f"Detail log:   {detail_log_path}")
    print(f"Summary:      {summary_path}")
    print(f"README (latest run): {readme_path}")


if __name__ == "__main__":
    main()