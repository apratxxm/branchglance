"""Differential regression for the literal patch-call scanner."""

import json
import random
import re

from token_tracker import providers


def _baseline(value):
    parsed = providers._parse_tool_arguments(value)
    if isinstance(parsed, dict):
        for key in ("code", "source", "script"):
            nested = parsed.get(key)
            if isinstance(nested, str):
                literal = _baseline(nested)
                if literal is not None:
                    return literal
    if not isinstance(value, str):
        return None
    i, n = 0, len(value)
    while i < n:
        if value.startswith("//", i):
            end = value.find("\n", i + 2)
            i = n if end < 0 else end + 1
            continue
        if value.startswith("/*", i):
            end = value.find("*/", i + 2)
            i = n if end < 0 else end + 2
            continue
        if value[i] in "'\"`":
            quote = value[i]
            i += 1
            while i < n:
                if value[i] == "\\":
                    i += 2
                elif value[i] == quote:
                    i += 1
                    break
                else:
                    i += 1
            continue
        call = re.match(r"(?<![\w$])(?:tools\s*\.\s*)?apply_patch\s*\(\s*", value[i:])
        if call:
            j = i + call.end()
            if j < n and value[j] in "'\"`":
                quote = value[j]
                start = j + 1
                j = start
                while j < n:
                    if value[j] == "\\":
                        j += 2
                    elif value[j] == quote:
                        literal = value[start:j]
                        return literal if "*** Begin Patch" in literal else None
                    else:
                        j += 1
            i += call.end()
            continue
        i += 1
    return None


def test_literal_patch_scanner_matches_baseline_on_adversarial_records():
    records = [
        None, 1, {}, {"code": 'tools.apply_patch("*** Begin Patch\\n*** End Patch")'},
        {"source": "ordinary source without a call"},
        '{"code":"tools.apply_patch(\\"*** Begin Patch\\")"}',
        "x" * 200_000, '// apply_patch("*** Begin Patch")\nnoop()',
        '"apply_patch(\\"*** Begin Patch\\")"',
        'tools.apply_patch(`*** Begin Patch\n*** End Patch`)',
        'apply_patch("*** Begin Patch")', 'apply_patch("*** Begin Patch',
        'fooapply_patch("*** Begin Patch")', '$apply_patch("*** Begin Patch")',
        '"unterminated\\', 'apply_patch("*** Begin Patch\\',
        'tools.apply_patch("*** Begin Patch\\\"still quoted")',
    ]
    rng = random.Random(4321)
    parts = ["apply_patch", "exec_command", "tools.", '"', "'", "`", "\\", "\n", "\r",
             "/*", "*/", "//", "(", ")", "*** Begin Patch", "{}", "x" * 100]
    records.extend("".join(rng.choice(parts) for _ in range(rng.randint(0, 20))) for _ in range(2000))
    for raw in (b'{"arguments":"bad\\u0000"}\r\n',
                b'{"arguments":"a","arguments":"b"}\n',
                b'{"type":"unknown","arguments":"apply_patch"}\n',
                b'{"arguments":"\xff"}\n', b'{"arguments":"unterminated'):
        try:
            decoded = raw.decode("utf-8", errors="ignore")
            record = json.loads(decoded)
            records.extend((record, record.get("arguments")))
        except ValueError:
            records.append(decoded)
    for record in records:
        assert providers._literal_apply_patch_argument(record) == _baseline(record)
    for prefix in ("foo", "$"):
        assert providers._literal_apply_patch_argument(prefix + 'apply_patch("*** Begin Patch")') == "*** Begin Patch"


def _baseline_exec(source):
    if "exec_command" not in source:
        return []
    commands = []
    i, length = 0, len(source)
    while i < length:
        if source.startswith("//", i):
            end = source.find("\n", i + 2)
            i = length if end < 0 else end + 1
            continue
        if source.startswith("/*", i):
            end = source.find("*/", i + 2)
            i = length if end < 0 else end + 2
            continue
        if source[i] in {"'", '"', "`"}:
            quote = source[i]
            cursor = i + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                elif source[cursor] == quote:
                    break
                else:
                    cursor += 1
            i = min(length, cursor + 1)
            continue
        header = providers._EXEC_COMMAND_HEADER.match(source, i)
        if header:
            quote_at = header.end()
            if quote_at < length and source[quote_at] in {"'", '"'}:
                quote = source[quote_at]
                cursor = quote_at + 1
                while cursor < length:
                    if source[cursor] == "\\":
                        cursor += 2
                    elif source[cursor] == quote or source[cursor] in "\r\n":
                        break
                    else:
                        cursor += 1
                if cursor < length and source[cursor] == quote:
                    command = providers._decode_js_string(source[quote_at + 1:cursor], quote)
                    if command is None:
                        return []
                    delimiter = cursor + 1
                    while delimiter < length and source[delimiter].isspace():
                        delimiter += 1
                    if delimiter >= length or source[delimiter] not in {",", "}"}:
                        return []
                    commands.append(command)
                    i = delimiter + 1
                    continue
            return []
        i += 1
    return commands


def test_exec_scanner_matches_baseline_on_adversarial_sources():
    rng = random.Random(919)
    parts = ["exec_command", "tools.", '"', "'", "`", "\\", "\n", "\r", "/*", "*/", "//",
             "(", ")", "{cmd:", "}", ",", "&&", "a", "0", " ", "ç", "_", "$"]
    sources = ["x" * 300_000, 'exec_command({cmd:"hello"})',
               '// exec_command({cmd:"fake"})\nexec_command({cmd:"real"})',
               'exec_command({cmd:"x"+y})', 'exec_command({cmd:"x"})\r\n',
               '"unfinished\\', 'exec_command({cmd:"unfinished\\',
               'exec_command({cmd:"escaped\\\"quote"})', 'exec_command({cmd:"line\nend"})']
    sources.extend("".join(rng.choice(parts) for _ in range(rng.randint(0, 60))) for _ in range(5000))
    for source in sources:
        assert providers._scan_literal_exec_commands(source) == _baseline_exec(source)
