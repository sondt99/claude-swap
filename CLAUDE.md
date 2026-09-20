# claude-swap (web fork)

## Style

**No em-dashes anywhere this fork writes.** Use `--`, a comma, a colon, or
parentheses instead. This covers code comments, docstrings, commit messages,
UI strings, and README prose.

Upstream (`origin`, github.com/realiti4/claude-swap) uses em-dashes heavily:
1756 of them in `src/` and `tests/` as of v0.27.0b1. Leave those alone. Every
one rewritten in a line upstream also touches becomes a merge conflict on the
next `git merge origin/main`, forever, and this fork carries ~30 commits that
already have to survive those merges.

So the rule applies to fork-authored content only:

- `deploy/**` and `src/claude_swap/web/**` are fork-only. No em-dashes at all.
- Shared files (`switcher.py`, `poll_policy.py`, `oauth.py`, `autoswitch.py`,
  `tests/**`, ...) are mixed. Lines this fork added carry no em-dash; lines
  upstream wrote stay exactly as upstream wrote them.

Check what a change adds, not what the file contains:

```
git diff origin/main -- <path> | grep '^+' | grep -P '\x{2014}'
```

(The pattern is written as an escape on purpose, so this file itself stays
clean and so grepping the repo for offenders does not match these rules.)

## Layout

The always-on Docker stack lives in `deploy/`. The code runs from the image,
built from the repo root, so a source edit does nothing until
`cd deploy && docker compose build && docker compose up -d`.

`main` tracks `origin/main` (upstream, not ours). Push to `fork`
(github.com/sondt99/claude-swap), never to `origin`.
