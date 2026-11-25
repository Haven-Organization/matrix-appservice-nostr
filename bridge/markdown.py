"""A small Markdown subset, for messages from apps that write it as plain
text (Concord's chat is plain text, but people and bots there type
``**bold**`` and ``### headings``).

It works on HTML the bridge already made from the text (escaped, with its
links, pills and custom emoji as tags), and only ever adds tags around text
outside them: what ``render`` linked stays exactly as it was. The plain
body keeps the Markdown as typed, which reads fine as a fallback.

Covered: ``**bold**``/``__bold__``, ``*italic*``/``_italic_`` (an underscore
inside a word, as in ``snake_case``, isn't one), ``~~strike~~``, ```code```,
fenced code blocks, ``#`` headings, ``-``/``*``/``+`` and ``1.`` lists, and
``>`` quotes.
"""

from __future__ import annotations

import re

# A tag, or a whole link (its text isn't touched either).
_PROTECTED_RE = re.compile(r"<a\b[^>]*>.*?</a>|<[^>]+>", re.S)
_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*|(?<![\w])__(?=\S)(.+?)(?<=\S)__(?![\w])")
_ITALIC_RE = re.compile(r"(?<![\*\w])\*(?=[^\s*])(.+?)(?<=[^\s*])\*(?![\*\w])|(?<![\w_])_(?=[^\s_])(.+?)(?<=[^\s_])_(?![\w_])")
_STRIKE_RE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_BULLET_RE = re.compile(r"^\s{0,3}[-*+]\s+(.*)$")
_NUMBER_RE = re.compile(r"^\s{0,3}(\d{1,9})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^&gt;\s?(.*)$")
_FENCE_RE = re.compile(r"^\s{0,3}```")
_RULE_RE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")


def _inline_text(text: str) -> str:
    """Inline Markdown in a stretch of escaped text holding no tags."""
    codes: list[str] = []

    def code(match: re.Match[str]) -> str:
        codes.append(f"<code>{match.group(1)}</code>")
        return f"\x00{len(codes) - 1}\x00"

    text = _CODE_RE.sub(code, text)
    text = _BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)
    text = _STRIKE_RE.sub(lambda m: f"<del>{m.group(1)}</del>", text)
    return re.sub(r"\x00(\d+)\x00", lambda m: codes[int(m.group(1))], text)


def _inline(line: str) -> str:
    """Inline Markdown in one line of HTML, outside its tags and links."""
    out, position = [], 0
    for match in _PROTECTED_RE.finditer(line):
        out.append(_inline_text(line[position:match.start()]))
        out.append(match.group(0))
        position = match.end()
    out.append(_inline_text(line[position:]))
    return "".join(out)


def to_html(rich: str) -> str:
    """``rich`` (HTML whose lines are still separated by newlines) with its
    Markdown made into HTML. Lines that aren't part of a block are joined
    with ``<br>``, as the bridge's plain rendering does."""
    lines = rich.split("\n")
    out: list[str] = []  # finished blocks; plain lines are strings ending a run
    run: list[str] = []  # plain lines waiting to be joined with <br>

    def flush() -> None:
        if run:
            out.append("<br>".join(run))
            run.clear()

    i = 0
    while i < len(lines):
        line = lines[i]
        if _FENCE_RE.match(line):
            body = []
            i += 1
            while i < len(lines) and not _FENCE_RE.match(lines[i]):
                body.append(lines[i])
                i += 1
            i += 1  # the closing fence (or the end)
            flush()
            out.append("<pre><code>" + "\n".join(body) + "</code></pre>")
            continue
        heading = _HEADING_RE.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
            i += 1
            continue
        if _RULE_RE.match(line):
            flush()
            out.append("<hr>")
            i += 1
            continue
        for pattern, tag in ((_BULLET_RE, "ul"), (_NUMBER_RE, "ol"), (_QUOTE_RE, "blockquote")):
            if pattern.match(line):
                items = []
                start = None
                while i < len(lines) and (match := pattern.match(lines[i])):
                    if tag == "ol" and start is None:
                        start = int(match.group(1))
                    items.append(match.group(match.lastindex or 1))
                    i += 1
                flush()
                if tag == "blockquote":
                    out.append("<blockquote>" + "<br>".join(_inline(x) for x in items) + "</blockquote>")
                else:
                    attribute = f' start="{start}"' if tag == "ol" and start not in (None, 1) else ""
                    out.append(f"<{tag}{attribute}>" + "".join(f"<li>{_inline(x)}</li>" for x in items) + f"</{tag}>")
                break
        else:
            run.append(_inline(line))
            i += 1
            continue
    flush()
    # A block brings its own spacing, so the blank line typed after one
    # isn't a second gap.
    html_out = "".join(out)
    html_out = re.sub(r"(</h\d>|</ul>|</ol>|</blockquote>|</pre>|<hr>)<br>", r"\1", html_out)
    return re.sub(r"(<br>)+$", "", re.sub(r"^(<br>)+", "", html_out))


def has_markdown(text: str) -> bool:
    """Whether plain ``text`` looks like it uses Markdown at all."""
    return bool(re.search(r"\*\*|__|(?<!\w)[*_]\S|~~|`|^\s{0,3}(#{1,6}\s|[-*+]\s|\d+[.)]\s|>|```)", text, re.M))
