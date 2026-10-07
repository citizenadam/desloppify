"""String-aware TypeScript character scanner."""

from __future__ import annotations

from collections.abc import Generator


def scan_code(
    text: str, start: int = 0, end: int | None = None
) -> Generator[tuple[int, str, bool], None, None]:
    """Yield ``(index, char, in_string)`` tuples while handling escapes.

    ``in_string`` is True for every character that is not code: string and
    template literal contents (with their quotes) and ``//`` / ``/* */``
    comments. Comments are masked so that a quote or backtick in prose
    (``// don't``) cannot open a phantom string that swallows the code after it.
    """
    i = start
    limit = end if end is not None else len(text)
    in_str = None
    while i < limit:
        ch = text[i]
        if in_str:
            if ch == "\\" and i + 1 < limit:
                yield (i, ch, True)
                i += 1
                yield (i, text[i], True)
                i += 1
                continue
            if ch == in_str:
                in_str = None
            yield (i, ch, in_str is not None)
        elif ch == "/" and i + 1 < limit and text[i + 1] in "/*":
            close = "\n" if text[i + 1] == "/" else "*/"
            stop = text.find(close, i + 2, limit)
            # A line comment ends before its newline; a block comment includes "*/".
            stop = limit if stop == -1 else (stop if close == "\n" else stop + 2)
            while i < stop:
                yield (i, text[i], True)
                i += 1
            continue
        else:
            if ch in ("'", '"', "`"):
                in_str = ch
                yield (i, ch, True)
            else:
                yield (i, ch, False)
        i += 1
