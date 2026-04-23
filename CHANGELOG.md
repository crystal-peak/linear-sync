# Changelog

All notable changes to linear-sync are documented here.

## [0.0.21-alpha] - 2026-03-26

### Added
- Sub-issue support: session digest shows parent/child hierarchy with indented display
- Sub-issue creation: API agent can create child issues via `parentId` in mutations
- Issue summary and "Fetch My Issues" queries now include `parent` and `children` fields

## [0.0.20-alpha] - 2026-03-26

### Added
- Config sanity checks in session-start hook: detects legacy key formats and github_org drift
- Tests for session-start config sanity checks
- `STATE_FILE_OVERRIDE` env var support in session-start for test isolation
- CHANGELOG.md

### Changed
- `load_repo_config` now returns the full config dict instead of cherry-picking fields
- Schema allows `additionalProperties` for forward compatibility

### Removed
- Stale pre-plugin artifacts: `settings.json` and `scripts/linear-repo-links.json`

## [0.0.19-alpha] - 2026-03-13

### Added
- Auto-approve standalone `linear-api.sh` path resolution (`VAR=$(ls ...linear-api.sh...)`)
- Auto-approve indirect variable API calls (`bash "$VAR"` where VAR was assigned from a `linear-api.sh` path)
- `echo` added as safe shell builtin in API allow hook
- `STATE_FILE_OVERRIDE` env var support in commit guard for test isolation
- 11 new hook tests covering path resolution and indirect variable patterns

### Fixed
- Commit guard tests now use isolated temp fixtures instead of depending on host state file — all 96 tests pass regardless of local Linear workspace config
- Use POSIX character classes in sed for macOS compatibility

## [0.0.17-alpha] - 2026-03-04

### Changed
- Setup docs updated to use env var for Linear API key

## [0.0.16-alpha] - 2026-03-02

### Added
- Nest repo labels under group label in Linear UI
- Auto-approve curl to Linear API and multiline GraphQL queries
- Auto-approve multiline single-quoted GraphQL queries
- Auto-approve read-only shell utilities in commit guard
- Sonnet subagent (switched from haiku)
- Skill trigger expanded to match setup directives
- Plugin update workflow documented in README and SETUP

### Fixed
- Always fetch fresh digest, fix subagent MCP assumption
- Drop label filter from session digest query
- Workspace isolation and auto-approve gaps
- Subagent defaulting to wrong workspace

## [0.0.10-alpha] - 2026-03-02

### Added
- Subagent type resolution and auto-approve hook
- Session-start hook rewritten as single Python script
- Parallelized sequential API calls in sync-github-issues.sh
- Marketplace moved to dedicated claude-plugins repo

### Fixed
- Self-heal missing issue titles in session-start hook
- Show issue title in session kickoff resume option

## [0.0.1-alpha] - 2026-02-27

### Added
- Initial release
- Linear MCP tool integration for native issue management
- Commit guard hook enforcing issue IDs on commits, branches, and PRs
- API allow hook for auto-approving trusted linear-api.sh calls
- State allow hook for auto-approving state file access
- Session-start hook with issue digest and workspace detection
- GitHub issue sync script
- Comprehensive test suite
