# Quarterly Report

A short **overview** with some _emphasis_, ~~deletions~~, and `inline code`
followed by a [link to the docs](https://example.com "Docs").

## Section 1: Numbers

Total revenue grew by **12.4%** quarter over quarter. Here are the splits:

| Region        | Q1     | Q2     | Q3     |
|:--------------|-------:|-------:|-------:|
| North America | $12.3M | $14.1M | $15.8M |
| EMEA          | $8.7M  | $9.2M  | $10.4M |
| APAC          | $5.2M  | $5.9M  | $6.7M  |

> Growth is driven by **enterprise** adoption in EMEA and renewed
> demand in the APAC mid-market segment.

## Section 2: Engineering

The platform team shipped:

- Streaming markdown renderer (issue #69)
- Multi-region failover for the lock service
- A new `ConcurrencyConfig.max_concurrent` enforcement
  - sub-bullet: documented in the migration guide
  - sub-bullet: covered by 47 new tests
- Telemetry pipeline rewrite

Ordered roadmap items:

1. Finalize the parser swap (Option B)
2. Land the test-fidelity baseline at 100%
3. Ship 0.5.0 with the new defaults

### Code samples

```python
from chat_sdk import Chat

chat = Chat(adapter=SlackAdapter())

@chat.on_mention
async def handle(event):
    await event.thread.post("hello")
```

```bash
$ uv run pytest tests/ -q
```

### Edge cases worth checking

- A `**bold` opened but not closed inside a sentence.
- An italic `*partial` that should be repaired during streaming.
- Word-internal asterisks like `5*3=15` (must not be italic).
- A bullet item: `* this is the start of a list` is a marker, not italic.

---

## Section 3: References

For background:

- [Vercel chat SDK](https://github.com/vercel/chat)
- [mdast specification](https://github.com/syntax-tree/mdast)
- [remend npm package](https://www.npmjs.com/package/remend)

An image for visual identity: ![logo](https://example.com/logo.png "Logo")

A nested blockquote with rich formatting:

> The *quick* brown **fox** jumps over the `lazy` dog.
>
> > Inside a nested quote with a [link](https://example.com).

End of report.
