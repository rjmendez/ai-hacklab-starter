# Contributing

Contributions are welcome — bug fixes, new agent archetypes, improved skill handlers, documentation, and tests.

## Getting Started

```bash
git clone https://github.com/rjmendez/ai-hacklab-starter.git
cd ai-hacklab-starter
make setup
# Fill in .env with your tokens
make up
make status
```

## What to Contribute

**New skill handlers** — the fastest way to add value. See [`agents/README.md`](agents/README.md) for the skills matrix and [`docs/adding-an-agent.md`](docs/adding-an-agent.md) for instructions.

**New agent archetypes** — add a directory under `agents/` with `agent_card.py`, `skill_handlers.py`, and `Dockerfile`.

**Bug fixes** — open an issue first for anything non-trivial so we can discuss approach.

**Documentation** — always appreciated. Docs live in `docs/` and each module's `README.md`.

## Code Style

- Python 3.12+
- Standard library first — avoid adding new dependencies unless necessary
- All skill handlers: `def handle_X(input_data: dict) -> dict` — always return a dict with a `"status"` key
- No secrets, internal hostnames, real tokens, or personal data in any committed file
- Run `make scan` before opening a PR

## Pull Request Checklist

- [ ] `make scan` returns 0 verified secrets
- [ ] New skills are registered in `agent_card.py` and documented in `agents/README.md`
- [ ] New dependencies added to `requirements.txt`
- [ ] New env vars documented in `.env.example`
- [ ] Tests added for new skill handlers (see `tests/`)

## Reporting Issues

Open a GitHub issue. For security issues in this repo itself, open an issue — there's no sensitive infrastructure here.
