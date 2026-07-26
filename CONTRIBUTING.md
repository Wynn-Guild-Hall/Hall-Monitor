# Contributing to Hall-Monitor

## Workflow

1. Fork or branch off `main`.
2. `pip install -e ".[dev]"` (Python 3.12+).
3. Make your changes. If you edited any model in `src/hall_monitor/db/models.py`, generate a migration: `aerich migrate -n <short-slug>`.
4. `pytest` — new logic must ship with tests.
5. Open a PR against `main`.

## Commit messages

Prefix each commit with one of `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`. Lowercase, colon-space, imperative subject.

Examples:

- `fix: revoke stale invite on re-request`
- `feat: add ~force observer subcommand`
- `docs: explain notability aggregation`

## Conventions

- **Prefix commands only.** The bot deliberately does not register slash commands — everything is `~<command>`.
- **Cogs are organised by domain**, one file per command (see `src/hall_monitor/discord_bot/cogs/`).
- Architecture, invariants, and the end-to-end join flow are in [DESIGN.md](DESIGN.md). Skim it before non-trivial changes.
