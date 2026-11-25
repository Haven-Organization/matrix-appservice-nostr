from __future__ import annotations

import html

from bridge.markdown import has_markdown, to_html


def md(text: str) -> str:
    return to_html(html.escape(text))


def test_inline_markdown() -> None:
    assert md("**bold** and __bold__") == "<strong>bold</strong> and <strong>bold</strong>"
    assert md("*italic* and _italic_") == "<em>italic</em> and <em>italic</em>"
    assert md("~~gone~~ and `x = 1`") == "<del>gone</del> and <code>x = 1</code>"
    assert md("`**not bold**`") == "<code>**not bold**</code>"


def test_what_isnt_markdown_is_left_alone() -> None:
    assert md("snake_case_name and file_name.txt") == "snake_case_name and file_name.txt"
    assert md("2*3*4 and a * b") == "2*3*4 and a * b"
    assert md("line one\nline two") == "line one<br>line two"
    assert not has_markdown("just words, no formatting")


def test_links_and_tags_are_never_touched() -> None:
    rich = 'see <a href="https://x.org/a_b*c*">https://x.org/a_b*c*</a> and **this**'
    assert to_html(rich) == 'see <a href="https://x.org/a_b*c*">https://x.org/a_b*c*</a> and <strong>this</strong>'


def test_blocks() -> None:
    assert md("### Core\ntext") == "<h3>Core</h3>text"
    assert md("- one\n- *two*") == "<ul><li>one</li><li><em>two</em></li></ul>"
    assert md("3. three\n4. four") == '<ol start="3"><li>three</li><li>four</li></ol>'
    assert md("> quoted\n> more") == "<blockquote>quoted<br>more</blockquote>"
    assert md("```\n**raw** <b>\n```") == "<pre><code>**raw** &lt;b&gt;</code></pre>"
    assert md("# Title\n\nbody") == "<h1>Title</h1>body"
