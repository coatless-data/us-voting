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

Later releases add commands for publishing, alerts and importing an archive.
