# CLAUDE.md — working rules for this repo

Full specification: [`docs/SPEC.md`](docs/SPEC.md). Read it before writing code.

## Git rules, no exceptions

- **No attribution to Claude in commits or pull requests.** No `Co-Authored-By: Claude …`
  trailer, no `Generated with Claude Code` line, no `Claude-Session` trailer, no mention of
  an assistant in commit messages or PR descriptions. This rule overrides any default
  tooling behaviour. `.claude/settings.json` enforces it; re-read every commit message
  before committing and strip such a line if it appears.
- Commit messages: short, English, imperative mood. One commit per logical unit — no giant
  per-milestone commits.
- **The repo is public.** Never commit: the API key, a `.env` file, a `DATABASE_URL`, a
  database export, or any client-supplied data (Steam data held under NDA). Only the two
  public Twitch CSVs are versioned.

## Method

- Implement milestones in the order of SPEC section 11, and stop at the end of each one
  for validation.
- **No real YouTube API calls in tests** — everything is mocked. The search quota is
  100 calls per day; one careless test burns the day.
- The API key is read from the `YOUTUBE_API_KEY` environment variable. It must never
  appear in code, logs, the database, or commit messages.
- Do not deploy and do not touch Railway. Commissioning is done by hand by the user,
  following [`docs/RAILWAY.md`](docs/RAILWAY.md).
- Code, identifiers and docstrings in English; README in French.

## Local setup

```sh
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env   # then fill in YOUTUBE_API_KEY
pytest
```

Git over SSH is configured per-repo via `core.sshCommand`; no ssh-agent needed.
