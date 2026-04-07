# Changelog

## 0.0.1a5 (2026-04-07)

Port fidelity release — 10 critical/high bugs fixed from systematic TS comparison.

### Critical fixes
- Slack: multi-workspace token now persists into async tasks (ContextVar fix)
- Discord: slash command deferred responses now resolve correctly
- Discord: file attachments no longer silently dropped
- WhatsApp: media downloads work again (auth header restored)
- Chat: `on_lock_conflict="force"` now works

### High fixes
- Discord: emoji normalized through resolver (reaction matching works)
- Teams: webhook options passed to reaction events
- Google Chat: subscription errors propagate to concurrent waiters
- Linear: fetch_thread metadata includes both key casings
- Streaming markdown `_remend` rewritten with proper delimiter tracking

### Other improvements
- CLAUDE.md agent guidance file
- Parser: spaced thematic breaks (`* * *`) and trailing `#` stripping
- BaseFormatConverter: card fallback text generation
- All PR review comments addressed

## 0.0.1a4 (2026-04-06)

Security hardening + launch documentation.

## 0.0.1a3 (2026-04-06)

Initial alpha release — 8 adapters, 3 state backends, 2,467 tests.
