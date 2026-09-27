# Contributing

- Run `make test` (needs `docker compose -f docker-compose.dev.yml up -d`) and `ruff check src tests tools scripts` before pushing.
- **Every new route needs a `@policy`**, or the app will not boot, and every new API operation
  must be classified in `tests/api/test_matrix.py`, or the tests fail. That is on purpose.
- Read protected data (reviews, scores, votes, comparisons) only through `quorum/policy/repos.py`
  or a service that enforces the rules; a lint test enforces this.
- State changes go through a service function that writes an audit event in the same transaction.
- Maths changes go in `src/engine/` with a test, and a note in JUDGING.md. Bump `ENGINE_VERSION`
  when outputs change: stored runs must stay reproducible.
- Commit messages: imperative, present tense, one idea per commit.
