# Domain Docs

How engineering skills should consume this repo's domain documentation when exploring the codebase.

## Before exploring, read these

- `CONTEXT.md` at the repository root; or
- `CONTEXT-MAP.md` at the repository root if it exists, then each relevant referenced context; and
- relevant ADRs under `docs/adr/`.

If any do not exist, proceed silently. The domain-modeling workflow creates them when terminology or decisions need to be recorded.

## File structure

This is a single-context repository:

```text
/
├── CONTEXT.md
├── docs/adr/
└── penhin/
```

## Use the glossary's vocabulary

When naming a domain concept in a specification, issue, test, or proposal, use the term defined in `CONTEXT.md`. If a needed concept is absent, either reconsider the terminology or record the gap for domain modeling.

## Flag ADR conflicts

Surface contradictions with an existing ADR explicitly rather than silently overriding them.
