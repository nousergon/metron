## What and why

<!-- What does this change and why? Link any related issue. -->

## Checklist

- [ ] Tests added/updated for the behavior change
- [ ] `pytest` passes locally — the coverage floor (`--cov-fail-under=95` in `.github/workflows/ci.yml`) is a ratchet, raised as coverage improves and never lowered to make a change pass
- [ ] `ruff check api portfolio_analytics tests` is clean for files I touched
- [ ] No secrets, proprietary prompt templates, or private signal feeds committed
- [ ] Fail-loud preserved — no new silent `except: pass` swallows

## Test plan

<!-- How you verified this works. -->

---

**Prepared by:** <!-- model name from the session prompt, e.g. claude-opus-5, claude-sonnet-5, claude-haiku-4-5 -->
