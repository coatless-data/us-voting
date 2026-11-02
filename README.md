# us-voting

Collects official election files from state election offices on a schedule and publishes them as
GitHub Releases. Each file is saved unchanged, and only files that changed since the last capture
are kept.

Count-only files (results, turnout totals) are published in the public repository
`coatless-data/us-voting`. Files that identify voters are kept in a private repository, and only
counts derived from them are published.

## Commands

No commands exist yet beyond the global options:

- `us-voting --version` prints the package version.

Later releases add commands for checking configuration, capturing, publishing, alerts and importing
an archive.
