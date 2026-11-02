# us-voting

Collects official election files from state election offices on a schedule and publishes them as
GitHub Releases. Each file is saved unchanged, and only files that changed since the last capture
are kept.

Count-only files (results, turnout totals) are published in the public repository
`coatless-data/us-voting`. Files that identify voters are kept in a private repository, and only
counts derived from them are published.

## Commands

- `us-voting --version` prints the package version.
- `us-voting check-config` validates the files in the config directory.
- `us-voting capture --out DIR` fetches the sources that are due and writes public and private
  bundles and a status file under `DIR/capture`. `--only ids` runs the named sources regardless of
  schedule, `--status PATH` names the status file to start from, and `--config DIR` names the
  config directory.
- `us-voting publish DIR` uploads the bundles in `DIR/public` and `DIR/private` to their stores, one
  monthly `raw-YYYY-MM` release each, and updates every release's `_manifest.json`. Each public
  bundle is checked before it is uploaded, and one that holds a voter-level file is refused, which
  stops the publish. `--public-store SPEC` and `--private-store SPEC` (`local:DIR` or
  `github:OWNER/REPO`) override the configured stores, which must not be the same. Writing to
  GitHub needs `US_VOTING_WRITER=1`.

- `us-voting alerts STATUS_JSON --publish-outcome X` opens, refreshes and closes GitHub issues from
  a status file: one per failing or blocked source, one per public source with voter-level files,
  and one for publish failures. `--capture-outcome X` also opens or closes one for a capture step
  that failed or timed out; with it, `STATUS_JSON` may be left out. It reads `GITHUB_REPOSITORY`
  and `GITHUB_TOKEN`; without both it prints what it would do. It always exits 0.
- `us-voting hold-fetch-state PREVIOUS CURRENT` runs after a failed publish. It gives each source
  in the status file `CURRENT` the fetch state (validators, hashes, version) it had in `PREVIOUS`
  and keeps `CURRENT`'s schedule state, so unpublished files are fetched again when their sources
  are next due.
- `us-voting import-archive ARCHIVE --out DIR` imports a local archive of raw captures into
  deterministic `DIR/public` and `DIR/private` bundles, one per privacy class per UTC day, ready for
  `publish`. `--through YYYY-MM-DD` is the last day imported (default: yesterday, UTC). A file that
  no single source owns or whose hash does not verify stops the import with nothing written, and
  an existing non-empty `DIR/public` or `DIR/private` is never overwritten.
