# sing-box Rule Sync & Cleanup

Automatically mirrors `sing-box` JSON rule-set files from an upstream GitHub
repository, strips blacklisted domains, and cuts a timestamped release
whenever anything actually changes.

## How it works

The fetch and the cleanup are now split cleanly between the workflow and the script:

1. **`.github/workflows/sync.yml`** does the fetching with plain `git`:
   a shallow, blobless, sparse `git clone` that pulls down only the
   upstream repo's `sing-box` directory (`--filter=blob:none --sparse`).
   No GitHub API calls, no token needed for public upstream repos, no
   rate limits. The result lands at `upstream/sing-box/` in the workspace.
2. **`scripts/sync_and_clean.py`** then reads those local files and:
   - Removes any entry in `domain`, `domain_suffix`, or `domain_keyword`
     whose value **contains** a blacklisted substring (case-insensitive).
   - Drops a field entirely if it becomes empty after cleanup.
   - Drops a rule entirely if *all* its fields were removed.
   - Drops a file entirely if *all* its rules were discarded.
3. Cleaned files are written into `sing-box/` (configurable) in this repo.
4. A detailed log (`logs/sync_<timestamp>.log`) and a release-ready summary
   (`logs/summary_<timestamp>.md`) are generated, and the summary is
   appended to `CHANGELOG.md`.
5. The workflow runs every 12 hours (or on manual dispatch), and only if
   something changed does it:
   - commit the updated files,
   - create a tag `vYYYYMMDD_HHMMSS`,
   - publish a GitHub Release using the generated summary as release notes.

If nothing changed upstream, no commit/tag/release is created.

## Configuration

### `config/sync.json`
```jsonc
{
  "upstream_owner": "SagerNet",              // <- set to the real upstream owner
  "upstream_repo": "sing-geosite",           // <- set to the real upstream repo
  "upstream_branch": "rule-set",
  "upstream_path": "sing-box",               // directory inside upstream to sync
  "upstream_checkout_dir": "upstream/sing-box", // where the workflow's sparse-checkout lands
  "local_output_dir": "sing-box"             // where cleaned files land locally
}
```
**Edit the placeholder `upstream_owner`/`upstream_repo`/`upstream_branch`
to point at your actual source repository before enabling the workflow —
and update the matching `env:` block at the top of
`.github/workflows/sync.yml` (`UPSTREAM_OWNER`/`UPSTREAM_REPO`/
`UPSTREAM_BRANCH`/`UPSTREAM_PATH`) so both stay in sync.**

### `config/blacklist.json`
```jsonc
{
  "blacklist": [
    "example-bad-domain.com",
    "ads.example.net"
  ]
}
```
Add or remove strings any time — no code changes required. Matching is a
case-insensitive substring check against each array element.

## Manual run

The script no longer talks to the GitHub API — it just reads local files —
so you need to populate `upstream/sing-box/` yourself first (the workflow
does this automatically):

```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch rule-set https://github.com/SagerNet/sing-geosite.git upstream_repo
cd upstream_repo && git sparse-checkout set sing-box && cd ..
mkdir -p upstream && cp -r upstream_repo/sing-box upstream/sing-box

python3 scripts/sync_and_clean.py
```

Outputs:
- `sing-box/**/*.json` — cleaned rule files
- `logs/sync_<ts>.log` — full per-file detail (counts + exact removed values)
- `logs/summary_<ts>.md` — condensed summary suitable for release notes
- `CHANGELOG.md` — running history, newest entries appended

## Notes / things to double check before first real run

- Fetching now uses `git clone --filter=blob:none --sparse`, so only the
  `sing-box` directory's blobs are ever downloaded — fast, no GitHub API
  rate limits, no token required for a public upstream repo. If your
  upstream is private, add auth to the clone URL or an SSH deploy key.
- `permissions: contents: write` is required in the workflow for it to
  push commits/tags and create releases with the default token.
- Keep `config/sync.json` and the `env:` block in `.github/workflows/sync.yml`
  pointed at the same upstream — the workflow does the fetching, the
  script only reads what's already on disk.
- "Empty after cleanup" only strips `invert` as a non-matching key — if the
  upstream rule-set format includes other selector keys you want preserved
  even when domain fields are gone (e.g. `ip_cidr`, `process_name`), that's
  already handled correctly since those keys simply remain and keep the
  rule alive.
