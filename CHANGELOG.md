# Changelog

## 0.0.1a7 (2026-04-03)

Fixture replay tests and coverage hardening.

- All fixture replay tests pass (46 tests, 28 JSON fixtures copied from TS SDK)
- GChat: fix float startIndex in annotation parsing
- Postgres: atomic lock upsert to eliminate TOCTOU race
- Coverage tests added for 4 low-coverage modules
- Documentation updates (TESTING.md, ARCHITECTURE.md)

## 0.0.1a6 (2026-04-07)

Systematic port fidelity scan — 10 more bugs fixed.

- Discord: card table fallback now renders correctly (was calling wrong function)
- Teams: card fallback text now includes emoji conversion
- Emoji: megaphone fixed (📢 not 📣), exclamation "!" false-match removed
- State backends: queue dequeue reconstructs Message objects (was returning raw dict)
- WhatsApp: callback data uses compact JSON (matches Telegram)
- Discord/Teams: format converter handles dataclass messages
- Emoji: from_slack strips only one colon per end (not all)
- Types: WellKnownEmoji includes all TS entries (stop, 100, lightbulb, etc.)

## 0.0.1a5 (2026-04-07)

Port fidelity release — 10 critical/high bugs fixed.

## 0.0.1a4 (2026-04-06)

Security hardening + launch documentation.

## 0.0.1a3 (2026-04-06)

Initial alpha release — 8 adapters, 3 state backends, 2,467 tests.
