# Changelog

All notable changes to this project will be documented in this file. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Added Pi-style `/login`, `/logout`, and `/auth` commands with account/API-key selection and hidden API-key input.
- Added system-keyring credential storage with an explicit, permission-hardened file fallback.
- Added experimental Claude Pro/Max and ChatGPT Plus/Pro OAuth with PKCE, loopback/manual callbacks, device authorization, and serialized token refresh.
- Added an isolated `openai-codex` SSE provider for ChatGPT subscription credentials.

### Security

- Removed API keys from command arguments and scrubbed credentials from tool subprocess environments, logs, and evaluation events.
- Added explicit user-env migration and source-aware authentication status without modifying project `.env` files.

## [0.1.1] - 2026-07-30

### Fixed

- Removed benchmark-specific multi-agent planning behavior and added generic DAG protocol recovery.
- Isolated default tests from developer database configuration and made PostgreSQL integration opt-in.
- Prevented Agent shell commands from traversing outside their assigned worktree.
- Added explicit DAG finalization into an isolated, verifiable integration worktree.

### Changed

- Split model-driven and fixture-driven multi-agent regression gates.
- Diversified the baseline suite across Python, JavaScript, and Go fixtures.

## [0.1.0] - 2026-07-30

### Added

- Interactive coding-agent CLI with Anthropic, OpenAI, and Gemini providers.
- Structured filesystem, shell, task, planning, and workspace tools.
- Session persistence, recovery, compaction, and transcript inspection.
- Configurable permission modes, approval rules, quality gates, and circuit breakers.
- Isolated sub-agent worktrees, persistent SQLite/PostgreSQL orchestration, and integration flows.
- Deterministic and judge-based evaluation framework with the `penhin-eval` CLI.
- Python package metadata, console entry points, and trusted PyPI publishing workflow.

### Changed

- Unified task and todo persistence so each task owns its checklist.
- Standardized project verification on pytest and removed the obsolete smoke-test runner.
- Removed superseded compatibility helpers and unused runtime APIs.

[Unreleased]: https://github.com/penhin/penhin-code/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/penhin/penhin-code/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/penhin/penhin-code/releases/tag/v0.1.0
