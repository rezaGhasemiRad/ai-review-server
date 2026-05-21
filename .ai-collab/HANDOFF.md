# Handoff: demo-smoke-test

## Goal

Smoke-test the end-to-end review pipeline by introducing a small, harmless change with one deliberate review-bait so we can verify both reviewers actually engage with the diff.

## In scope

- Add a `__version__ = "0.2.0"` constant near the top of `ai_chat.py`
- Add a tiny helper `_redact_secrets(text)` that strips obvious `sk-...` / `ghp_...` tokens from a string, intended for future log scrubbing
- No callers added yet — the helper is dormant

## Out of scope

- Wiring the redactor into actual logging (a follow-up branch)
- Any change to the server, prompts, or tests
- Bumping anything in `requirements.txt`

## Design decisions

- **Decision:** chose module-level constant `__version__` over reading `pyproject.toml`/setup metadata.
  **Why:** the project has no `pyproject.toml` yet; constant is the smallest correct change. Will revisit when packaging.
- **Decision:** `_redact_secrets` does substring replace via regex.
  **Why:** good enough for the two prefixes we use; full key-shape parsing isn't needed.

## Implementation notes — where to look hard

- `ai_chat.py` — confirm `_redact_secrets` won't false-positive on legitimate strings that happen to contain `sk-` (e.g. a German word in a comment). The 20+ character length guard is meant to prevent that.

## Risks & known limitations

- Imports `re` inside the function rather than at module top. Slight performance cost; conscious choice to keep the existing import block clean for a dormant helper.
- No tests yet.

## Testing done

- **Unit / integration:** none added.
- **Manual verification:** ran `python -c "import ai_chat; print(ai_chat.__version__)"` locally — prints `0.2.0`.
- **Not yet tested:** the redactor on actual log strings; should be tested when it's wired in.

## Open questions for reviewers

1. Should `__version__` live in a separate `_version.py` to avoid editing the main file on every release?
2. Is regex-based redaction sufficient, or do we want a dedicated `secrets` parsing library before this gets wired into logs?
