# Contributing to Hall-Monitor

## Workflow

1. Fork or branch off `main`.
2. `pip install -e ".[dev]"` (Python 3.12+).
3. Make your changes. If you edited any model in `src/hall_monitor/db/models.py`, generate a migration: `aerich migrate --name <short-slug>`, and commit the generated file with the model change. The bot applies outstanding migrations at boot — see [DESIGN.md §10](DESIGN.md#10-schema-migrations).
   - `aerich` reads its config from `pyproject.toml` and its database URL from `DATA_DIR`, so point that at a scratch directory rather than running against anything you care about: `DATA_DIR=/tmp/hm aerich migrate --name add_thing`.
   - A scratch directory starts empty, and `migrate` diffs against *applied* state — so run `DATA_DIR=/tmp/hm aerich upgrade` first to build the history, or it dies with `TypeError: 'NoneType' object is not iterable`.
   - Aerich writes each model's docstring into the SQL as a comment and escapes `/` as `\/`, which Python 3.12 flags as an invalid escape sequence in the generated file. If your new model's docstring has a slash in it, fix the two characters by hand — otherwise every boot logs a `SyntaxWarning`.
4. `pytest` — new logic must ship with tests.
5. Open a PR against `main`.

## Commit messages

Prefix each commit with one of `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`, `test:`. Lowercase, colon-space, imperative subject.

Examples:

- `fix: revoke stale invite on re-request`
- `feat: add ~force observer subcommand`
- `docs: explain major-guild status aggregation`

## Conventions

- **Prefix commands only.** The bot deliberately does not register slash commands — everything is `~<command>`.
- **Cogs are organised by domain**, one file per command (see `src/hall_monitor/discord_bot/cogs/`).
- Architecture, invariants, and the end-to-end join flow are in [DESIGN.md](DESIGN.md). Skim it before non-trivial changes.
