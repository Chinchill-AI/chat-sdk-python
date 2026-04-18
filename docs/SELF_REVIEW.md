# Self-Review Discipline

Automated reviewers (Codex, CodeRabbit, CI linters) catch bugs not because
they're smarter than humans or agents, but because they apply adversarial
checks consistently on every diff. Self-review tends to verify happy paths
and ship. This doc is the set of adversarial checks to run against your own
code *before* declaring a change ready.

## When to apply

Any change that introduces novel logic — especially:

- New regex, substitution, or tokenization pass
- New emit/parse code path (serialization, format conversion)
- New transformation pass inserted into an existing pipeline
- Divergence from upstream (anything that lands in the non-parity table
  in `docs/UPSTREAM_SYNC.md`)
- Any time you're about to say "this is ready" or "ship it"

Ports of straightforward upstream code that upstream has already reviewed
need less adversarial attention. The risk scales with how much *new*
Python-specific logic you're introducing.

## The principles

### 1. Adversarial input sweep

For every regex, substitution, or tokenization pass, enumerate inputs that
could break it:

- Empty input, single-character input
- Input containing the marker/sentinel your code produces (forgery)
- Input containing each delimiter your pattern uses
- The pattern appearing in unintended contexts (code spans, quoted
  strings, comments, nested escapes)
- Very long input, unusual whitespace, private-use (PUA) code points,
  newlines, control characters

If any of these would break your code, fix or guard against it before
shipping.

### 2. Emit/parse symmetry

If you add `X → Y` (emit), verify your `Y → X` (parse) accepts every `X`
the emit might produce:

- Every scheme/format your emit outputs
- Every edge-case shape (empty, relative, None, whitespace-only,
  malformed)
- Every encoding, escape, or quoting your emit might apply

Asymmetry between emit and parse is a common source of silent data loss —
the emit produces something the parse doesn't recognize, and the
round-trip quietly drops or corrupts data.

### 3. Pass-interaction check

When inserting a new transformation pass into a pipeline (Markdown
parsing, plain-text stripping, AST walking, card rendering), walk through
every existing pass and ask:

- Does my new pass break any existing pass?
- Does any existing pass break my new pass?
- Does the ORDER of passes matter? Am I in the right slot?

A pass that works in isolation can produce wrong output when composed
with neighbors, especially when both operate on overlapping patterns.

### 4. Unforgeable sentinels

Any placeholder or sentinel token that later code maps back to structured
data must carry a per-call nonce (`secrets.token_hex(n)`). Fixed tokens
can be forged by user-supplied input that happens to match the pattern,
turning user content into structured data or crashing the parser.

Also: tolerate out-of-range or unrecognized sentinels without raising.
Crafted input that guesses the nonce should fall back to literal text,
not propagate exceptions up through the stack.

### 5. Divergence budget

`docs/UPSTREAM_SYNC.md` sets a max of **2 divergences per sync PR**.
Enforce this against yourself, not just externally. A 3rd divergence =
stop and ask whether this is still a sync or becoming a fork.

Every divergence must have:
- A row in the non-parity table (`docs/UPSTREAM_SYNC.md`)
- A breadcrumb at the code site: `# Divergence from upstream — see docs/UPSTREAM_SYNC.md`
- A regression test that would fail if someone "fixes" the divergence
  back to upstream's behavior
- A CHANGELOG entry under "Python-specific (divergence from upstream)"

### 6. Rebind / idempotent-path state coherence

When a constructor or factory accepts an already-constructed instance
and then "rebinds" or "updates" it (common in `from_json`-style
idempotent APIs, dependency-injection helpers, or adapter-swapping code),
every cached or derived piece of state must be re-resolved against the
new binding. Common leak paths:

- Cached downstream instances (a `_channel_cache` on a thread, a state
  adapter pointer, a resolved HTTP client).
- Cached lookup keys (a name copied from the old binding and not
  re-synced to the new one).
- Lazy-resolution placeholders that were already resolved under the
  old binding and now hold stale references.

Walk every instance attribute and ask: "does this value come from the
previous binding, and would it route operations to the wrong context
after the rebind?"

### 7. The pre-ship question

Before declaring any change ready, ask yourself:

> *What would an adversarial reviewer find in this code right now?*

Be honest. If the answer is "probably nothing," ship. If the answer is
"maybe X or Y," test X and Y first — don't wait for a bot to file them.

This is the highest-leverage single check. Five minutes of honest
adversarial self-review catches most bot findings.

## Maintenance

If future PRs accumulate high bot-finding counts relative to self-surfaced
findings, revisit this doc and add a new principle or sharpen an existing
one. Each principle should describe a *class* of bug, not a specific
instance — specifics belong in commit messages and PR descriptions.
