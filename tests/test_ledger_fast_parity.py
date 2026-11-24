"""Differential coverage: the fast pass against the per-source compatibility pass."""

import random

from test_ledger_support import (  # ledger_guard is autouse
    HOUR_START, append_jsonl, claude_file, claude_response, codex_count,
    codex_file, codex_meta, ledger_guard, make_roots, write_jsonl,
)
from token_tracker.ledger import Ledger


def run_parity_sequences(tmp_path, count, seed=741920):
    randomizer = random.Random(seed)
    actions = ('append', 'truncate', 'rotate', 'new', 'delete', 'partial', 'debt', 'blocked')
    for sequence in range(count):
        root = tmp_path / str(sequence)
        data, claude, codex = make_roots(root)
        clock = [float(HOUR_START + 3600)]
        fast = Ledger(data / 'fast', claude_root=claude, codex_root=codex, clock=lambda: clock[0])
        compat = Ledger(data / 'compat', claude_root=claude, codex_root=codex, clock=lambda: clock[0])
        cp = claude_file(claude, 'main')
        first = codex_file(codex, 'a-first')
        later = codex_file(codex, 'b-later')
        write_jsonl(cp, [claude_response(10, 'base', output=1)])
        write_jsonl(first, codex_meta('T', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
        for step in range(9):
            action = actions[(step + sequence) % len(actions)] if step < 8 else randomizer.choice(actions)
            target = randomizer.choice((cp, first))
            if action == 'append':
                append_jsonl(cp, [claude_response(30 + step, f'm-{sequence}-{step}', output=step + 1)])
            elif action == 'truncate':
                write_jsonl(cp, [claude_response(40 + step, f't-{sequence}-{step}', output=2)])
            elif action == 'rotate':
                replacement = cp.with_suffix('.replacement')
                write_jsonl(replacement, [claude_response(50 + step, f'r-{sequence}-{step}', output=3)])
                replacement.replace(cp)
            elif action == 'new':
                write_jsonl(claude_file(claude, f'new-{step}'),
                            [claude_response(60 + step, f'n-{sequence}-{step}', output=4)])
            elif action == 'delete':
                target.unlink(missing_ok=True)
            elif action == 'partial':
                with cp.open('ab') as handle:
                    handle.write(b'{"type":"assistant"')
            elif action == 'debt':
                if not first.exists():
                    write_jsonl(first, codex_meta('T', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
                append_jsonl(first, [codex_count(3600, input_tokens=120 + step, output_tokens=0)])
            else:
                write_jsonl(later, codex_meta('T', 7200) + [codex_count(7201, input_tokens=150 + step, output_tokens=0)])
            clock[0] += randomizer.choice((0, 1, 121, 1801))
            budget = randomizer.choice((None, 1, 128, 1024))
            compat._ingestor._conflict_streak = 3
            fast_result = fast.ingest({'Claude': True, 'Codex': True}, budget_bytes=budget)
            compat_result = compat.ingest({'Claude': True, 'Codex': True}, budget_bytes=budget)
            assert fast_result == compat_result, (sequence, step, action, fast_result, compat_result)
            tables = [row[0] for row in fast.conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            for table in tables:
                fast_rows = sorted(fast.conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                compat_rows = sorted(compat.conn.execute(f'SELECT * FROM "{table}"').fetchall(), key=repr)
                assert fast_rows == compat_rows, (sequence, step, action, table)
        fast.close()
        compat.close()


def test_fast_matches_compat_deterministic_sequences(tmp_path):
    run_parity_sequences(tmp_path, 12)
