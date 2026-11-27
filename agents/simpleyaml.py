"""the slice of yaml that config files actually use, read without a dependency.

pyyaml is an optional extra here (`pip install crittertown[dify-yaml]`), and crewai
configs are yaml. that left the crewai import path refusing every real file on a default
install with "install pyyaml", which is a strange thing for an importer to say: the file
is right there, and the part of yaml it uses is small.

so this reads the subset: block mappings by indentation, block lists, inline lists, block
scalars (`|` and `>`), comments, quoted and plain scalars, booleans and null. it refuses
the rest by line number rather than guessing, because a config reader that quietly turns
an anchor into a string is worse than one that says it cannot read line 12.

deliberately not supported: anchors and aliases, flow mappings (`{a: 1}`), multiple
documents, complex keys, and tabs for indentation. a file that needs those gets a refusal
naming the line, and `pyyaml` is one pip install away for whoever needs them.
"""

from __future__ import annotations

from core.errors import ValidationError

TRUE = {"true", "yes", "on"}
FALSE = {"false", "no", "off"}
NULL = {"", "~", "null"}


def _refuse(line_no: int, why: str, text: str = "") -> ValidationError:
    detail = f"line {line_no}: {why}"
    if text:
        detail += f" ({text.strip()[:60]})"
    return ValidationError(f"this yaml cannot be read: {detail}",
                           reason="yaml_line_unreadable", line=line_no)


def _strip_comment(text: str) -> str:
    """a # outside quotes starts a comment. inside them it is a character."""
    quote = ""
    for position, char in enumerate(text):
        if quote:
            if char == quote:
                quote = ""
            continue
        if char in "'\"":
            quote = char
        elif char == "#" and (position == 0 or text[position - 1] in " \t"):
            return text[:position]
    return text


def _scalar(text: str):
    raw = text.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
        return raw[1:-1]
    lowered = raw.lower()
    if lowered in NULL:
        return None
    if lowered in TRUE:
        return True
    if lowered in FALSE:
        return False
    if raw.startswith(("&", "*", "!", "{")):
        raise ValueError(f"unsupported yaml construct: {raw[:20]}")
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _inline_list(text: str) -> list:
    body = text.strip()[1:-1].strip()
    if not body:
        return []
    return [_scalar(part) for part in body.split(",")]


def _rows(text: str) -> list[tuple[int, int, str]]:
    """(line number, indent, content) for every line that carries anything."""
    rows: list[tuple[int, int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        if "\t" in raw[: len(raw) - len(raw.lstrip())]:
            raise _refuse(number, "indentation must be spaces, not tabs")
        line = _strip_comment(raw).rstrip()
        if not line.strip():
            continue
        if line.strip() in ("---", "..."):
            continue
        indent = len(line) - len(line.lstrip(" "))
        rows.append((number, indent, line.strip()))
    return rows


def _block_scalar(rows: list, start: int, style: str, parent_indent: int) -> tuple[str, int]:
    """`|` keeps newlines, `>` folds them. both end at the first shallower line."""
    collected: list[str] = []
    index = start
    while index < len(rows):
        number, indent, content = rows[index]
        if indent <= parent_indent:
            break
        collected.append(content)
        index += 1
    joiner = "\n" if style.startswith("|") else " "
    return joiner.join(collected), index


def _parse_block(rows: list, start: int, indent: int):
    if rows[start][2].startswith("- "):
        return _parse_list(rows, start, indent)
    return _parse_map(rows, start, indent)


def _parse_map(rows: list, start: int, indent: int) -> tuple[dict, int]:
    result: dict = {}
    index = start
    while index < len(rows):
        number, row_indent, content = rows[index]
        if row_indent < indent:
            break
        if row_indent > indent:
            raise _refuse(number, "unexpected indentation", content)
        if content.startswith("- "):
            break
        if ":" not in content:
            raise _refuse(number, "a mapping line needs a colon", content)
        key, _, rest = content.partition(":")
        key = key.strip().strip("'\"")
        if not key:
            raise _refuse(number, "a mapping line needs a key", content)
        if key in result:
            raise _refuse(number, f"duplicate key '{key}'", content)
        rest = rest.strip()
        if rest.startswith(("|", ">")):
            value, index = _block_scalar(rows, index + 1, rest, row_indent)
            result[key] = value
            continue
        if rest:
            if rest.startswith("[") and rest.endswith("]"):
                result[key] = _inline_list(rest)
            elif rest.startswith("{"):
                raise _refuse(number, "flow mappings are not supported", content)
            else:
                try:
                    result[key] = _scalar(rest)
                except ValueError as exc:
                    raise _refuse(number, str(exc), content) from exc
            index += 1
            continue
        # an empty value: the next deeper block belongs to this key
        if index + 1 < len(rows) and rows[index + 1][1] > row_indent:
            child_indent = rows[index + 1][1]
            child, index = _parse_block(rows, index + 1, child_indent)
            result[key] = child
            continue
        result[key] = None
        index += 1
    return result, index


def _parse_list(rows: list, start: int, indent: int) -> tuple[list, int]:
    result: list = []
    index = start
    while index < len(rows):
        number, row_indent, content = rows[index]
        if row_indent < indent:
            break
        if row_indent > indent:
            raise _refuse(number, "unexpected indentation", content)
        if not content.startswith("- "):
            break
        item = content[2:].strip()
        if not item:
            if index + 1 < len(rows) and rows[index + 1][1] > row_indent:
                child, index = _parse_block(rows, index + 1, rows[index + 1][1])
                result.append(child)
                continue
            result.append(None)
            index += 1
            continue
        if ":" in item and not item.startswith(("'", '"')):
            # "- key: value" opens a mapping item indented two past the dash
            key, _, rest = item.partition(":")
            child_indent = row_indent + 2
            rebuilt = [(number, child_indent, item)]
            cursor = index + 1
            while cursor < len(rows) and rows[cursor][1] > row_indent:
                rebuilt.append(rows[cursor])
                cursor += 1
            mapping, _ = _parse_map(rebuilt, 0, child_indent)
            result.append(mapping)
            index = cursor
            continue
        if item.startswith("[") and item.endswith("]"):
            result.append(_inline_list(item))
        else:
            try:
                result.append(_scalar(item))
            except ValueError as exc:
                raise _refuse(number, str(exc), content) from exc
        index += 1
    return result, index


def loads(text: str):
    """read a yaml document, or refuse it by line."""
    if not isinstance(text, str):
        raise ValidationError("yaml has to be text", reason="yaml_not_text")
    rows = _rows(text)
    if not rows:
        return {}
    value, index = _parse_block(rows, 0, rows[0][1])
    if index < len(rows):
        number, _, content = rows[index]
        raise _refuse(number, "this line does not belong to anything above it", content)
    return value
