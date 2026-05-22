# Handoff: <feature name>

> Filled in by the implementing AI. Read by the reviewers (Claude + Codex).
> Commit this file on the feature branch at:
>     .ai-collab/handoffs/<feature>.md
> where <feature> is the `feature` label sent to the review server. A
> per-feature path prevents a stale handoff from a previous feature being
> inherited via a reused branch. The reviewers pick it up automatically.
> (Legacy `HANDOFF.md` / `.ai-collab/HANDOFF.md` are still recognized as a
> fallback, but the per-feature path is preferred.)

## Goal

One or two sentences. What is this branch meant to accomplish? Link the ticket / spec if applicable.

## In scope

Bullet-list of things this branch actually implements. Be concrete — file or feature names, not vague verbs.

- ...
- ...

## Deliberately out of scope

Things that could be related but were **not** addressed here. Helps reviewers stop chasing missing pieces that were intentional.

- ...

## Design decisions

Non-obvious choices and why. Especially: places where you picked X over Y, places where the spec was ambiguous and you resolved it, and any new patterns you introduced.

- **Decision:** ...
  **Why:** ...
- **Decision:** ...
  **Why:** ...

## Implementation notes — where to look hard

Point reviewers at the parts of the diff that deserve the most scrutiny. Examples: migrations, auth checks, concurrency, transactions, anything touching money/user data/permissions.

- `path/to/file.cs` — ...
- `path/to/migration.cs` — ...

## Risks & known limitations

What you (the implementing AI) are uncertain about, or know is imperfect but shipped anyway. Don't hide these — reviewers cannot help if they don't know.

- ...
- ...

## Testing done

- **Unit / integration:** what was added, where it lives, what it covers
- **Manual verification:** what you actually ran end-to-end
- **Not yet tested:** be honest about gaps

## Open questions for reviewers

Specific things you want a second opinion on. Frame each as a question.

1. Should X be handled by A or B?
2. Is the migration in `...` safe to run against existing prod data?
3. ...

## Out-of-tree context (optional)

Anything the reviewers cannot see by reading the diff alone:
- Runtime / framework versions
- External services this depends on
- Feature flags that gate this code
- Data assumptions (size, distribution, NULL-ness)
