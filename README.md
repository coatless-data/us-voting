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
  monthly `raw-YYYY-MM` release each, and updates every release's `_manifest.json`. A public
  bundle that holds a voter-level file is refused before anything is uploaded.
  `--public-store SPEC` and `--private-store SPEC` (`local:DIR` or `github:OWNER/REPO`) override
  the configured stores. Writing to GitHub needs `US_VOTING_WRITER=1`.

- `us-voting alerts STATUS_JSON --publish-outcome X` opens, refreshes and closes GitHub issues from
  a status file: one per failing or blocked source, one per public source with voter-level files,
  and one for publish failures. It reads `GITHUB_REPOSITORY` and `GITHUB_TOKEN`; without both it
  prints what it would do. It always exits 0.

Later releases add a command for importing an archive.
