# sing-box Rule Sync, Cleanup & SRS Compile

Mirrors sing-box JSON rule-sets from one or more upstream GitHub repos (plus
any hand-maintained JSON of your own), strips blacklisted domains, compiles
everything to sing-box's binary `.srs` format, publishes jsDelivr CDN links,
and cuts a timestamped release whenever anything actually changes.

## Layout

```
sync.json               # sources, output paths, CDN settings
blacklist.json          # strings to strip — edit anytime, no code changes
sync_and_clean.py        # the whole pipeline
custom/                  # optional: your own hand-maintained JSON rule-sets
rules/                   # generated: cleaned JSON, one tree per source
srs/                     # generated: compiled .srs, one tree per source
logs/                    # generated: per-run detail log + release summary
CHANGELOG.md             # generated: running history
ACCESS_LINKS.md          # generated: jsDelivr link for every file, this run
.github/workflows/sync.yml
```

## How it works

1. **`.github/workflows/sync.yml`** fetches every configured source with a
   shallow, blobless, sparse `git clone` — no GitHub API calls, no token
   needed for public upstreams, no rate limits. Each source lands at
   `upstream/@<owner>/<repo>/<branch>/<directory_name>/`.
2. **`sync_and_clean.py`** then, for every source *and* the local `custom/`
   directory:
    - Removes any entry in `domain`, `domain_suffix`, or `domain_keyword`
      whose value **contains** a blacklisted substring (case-insensitive).
    - Drops a field entirely if it becomes empty after cleanup.
    - Drops a rule entirely if *all* its fields were removed.
    - Drops a file entirely if *all* its rules were discarded.
    - Writes the cleaned JSON to `rules/<namespace>/<...>.json`.
    - Compiles it to sing-box's `.srs` binary format, written two ways:
        - `srs/<namespace>/<...>/<date>/<file>.srs` — dated snapshot, kept forever
        - `srs/<namespace>/<...>/<file>.srs` — "latest", always current, no date
3. Every file touched this run gets a jsDelivr link in **`ACCESS_LINKS.md`**
   (path, then a fenced code block with the URL).
4. A detailed log (`logs/sync_<ts>.log`) and release summary
   (`logs/summary_<ts>.md`) are generated; the summary is appended to
   `CHANGELOG.md`.
5. The workflow runs every 12 hours (or on manual dispatch), and only if
   something changed does it commit, tag `vYYYYMMDD_HHMMSS`, and publish a
   GitHub Release using the summary as release notes.

If nothing changed, no commit/tag/release happens.

## Namespacing

Both `rules/` and `srs/` use the identical prefix, so multiple sources never
collide even if they happen to produce a same-named file:

```
rules/@<owner>/<repo>/<branch>/<directory_name>/<...>.json
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<date>/<file>.srs
srs/@<owner>/<repo>/<branch>/<directory_name>/<...>/<file>.srs
```

`upstream_path` and `directory_name` both support multi-level paths (e.g.
`"sing-box/Clash"`) — everything is built with `pathlib`, and Git's
sparse-checkout (cone mode, default since Git 2.25) accepts nested
directories directly.

`custom/` follows the same pattern but with a fixed, ownerless namespace —
`rules/custom/<...>.json` and `srs/custom/<...>.srs` — and also supports
arbitrary multi-level subdirectories underneath it (e.g.
`custom/mygroup/ads/blocklist.json`). Files here aren't fetched from
anywhere; you maintain them directly in this repo, and every run they go
through the exact same cleanup + compile pipeline as any other source. If
`custom/` doesn't exist or is empty, it's silently skipped.

## Configuration

### `sync.json`
```jsonc
{
  "sources": [
    {
      "owner": "SukkaLab",
      "repo": "ruleset.skk.moe",
      "branch": "master",
      "upstream_path": "sing-box",
      "directory_name": "sing-box"   // optional — defaults to the last segment of upstream_path
    }
    // add more entries here for additional upstream repos/directories
  ],

  "local_output_root": "rules",
  "upstream_checkout_root": "upstream",

  "enable_custom": true,
  "custom_dir_name": "custom",

  "sing_box_bin": "sing-box",

  "cdn_base_url": "https://testingcf.jsdelivr.net/gh",
  "cdn_ref_mode": "tag",      // "tag" (dated files) or "branch" (always-current files)
  "cdn_branch": "master"
}
```

### `blacklist.json`
```jsonc
{
  "blacklist": [
    "example-bad-domain.com",
    "ads.example.net"
  ]
}
```
Matching is a case-insensitive substring check against each array element.

## Manual run

```bash
# populate upstream/ yourself (the workflow does this automatically per source)
git clone --depth 1 --filter=blob:none --sparse \
  --branch master https://github.com/SukkaLab/ruleset.skk.moe.git tmp_clone
(cd tmp_clone && git sparse-checkout set sing-box)
mkdir -p "upstream/@SukkaLab/ruleset.skk.moe/master/sing-box"
cp -r tmp_clone/sing-box/. "upstream/@SukkaLab/ruleset.skk.moe/master/sing-box/"
rm -rf tmp_clone

python3 sync_and_clean.py
```

`sing-box` must be on `PATH` for `.srs` compilation to succeed — if it's
missing, the script logs the failure per-file and continues (JSON output and
links still get produced, just no `.srs`/link for that file).

## Notes

- `permissions: contents: write` is required in the workflow for it to push
  commits/tags and create releases with the default token.
- The "latest" `.srs` catalog is always linked against `cdn_branch`
  regardless of `cdn_ref_mode`, since its contents change every run — a
  tag-pinned link to a file that mutates after the tag exists would be
  misleading.
- "Empty after cleanup" only strips `invert` as a non-matching key — other
  selector keys (`ip_cidr`, `process_name`, etc.) are preserved and keep a
  rule alive even when its domain fields are gone.