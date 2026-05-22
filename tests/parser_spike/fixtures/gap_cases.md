Setext H1 underline
===================

Setext H2 underline
-------------------

Indented code block (4-space):

    def hello():
        return "world"

A paragraph with escaped \*asterisks\* and escaped \[brackets\] and a
literal backslash \\ in it.

A footnote reference[^1] in running text.

[^1]: This is the footnote body.

Multi-backtick inline code: ``some `quoted` code`` and triple ```backticks
with ``double`` inside```.

Raw HTML block:

<div class="callout">
  <p>This is HTML, not markdown.</p>
</div>

Inline HTML: <span style="color:red">red text</span> and a self-closing
<br/> mid-sentence.

Word-internal asterisks: `5*3=15`, paths like `lib/*.so`, and
glob*patterns*everywhere.

Math: a single dollar $a^2 + b^2 = c^2$ and a display block:

$$
\int_0^\infty e^{-x^2} dx = \frac{\sqrt{\pi}}{2}
$$

A task list (GFM):

- [ ] Pending item
- [x] Completed item
- [ ] Another pending one

An autolink: <https://example.com> and an email <user@example.com>.

A definition list (some flavors):

term1
:   definition for term 1

term2
:   definition for term 2
