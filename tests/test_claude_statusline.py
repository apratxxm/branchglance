import json

from token_tracker import claude_statusline as statusline


def sample(used, reset=2000):
    return {"rate_limits": {"five_hour": {"used_percentage": used, "resets_at": reset}}}


def test_parser_rejects_invalid_values():
    for value in (None, True, "20", -1, 101, float("nan")):
        assert statusline.parse(sample(value), now=1000) == {}


def test_unchanged_or_lower_usage_does_not_refresh(tmp_path):
    statusline.write(statusline.parse(sample(40), now=1000), tmp_path, now=1000)
    statusline.write(statusline.parse(sample(40), now=1100), tmp_path, now=1100)
    statusline.write(statusline.parse(sample(20), now=1200), tmp_path, now=1200)
    actual = statusline.read(tmp_path / statusline.FILENAME, now=1200)["five_hour"]
    assert (actual["used_percentage"], actual["receivedAt"]) == (40, 1000)


def test_writer_projects_only_limits(tmp_path):
    payload = sample(22)
    payload.update({"session_id": "secret", "cwd": "secret"})
    statusline.write(statusline.parse(payload, now=1000), tmp_path, now=1000)
    saved = json.loads((tmp_path / statusline.FILENAME).read_text())
    assert set(saved) == {"source", "five_hour"}


def empty_desktop():
    return {"fiveHour": None, "weekly": None, "fiveHourPercent": None,
            "weeklyPercent": None, "observedAt": None}


def test_cli_store_is_bound_to_desktop_org(tmp_path):
    windows = statusline.parse(sample(22), now=1000)
    statusline.write(windows, tmp_path, now=1000, filename=statusline.CLI_FILENAME,
                     source="claude-cli", org_id="org-a")
    cli_path = tmp_path / statusline.CLI_FILENAME
    assert statusline.merge_usage(empty_desktop(), [], now=1000, org_id="org-a", cli_path=cli_path)["fiveHourPercent"] == 22
    assert statusline.merge_usage(empty_desktop(), [], now=1000, org_id="org-b", cli_path=cli_path)["fiveHourPercent"] is None
    assert statusline.merge_usage(empty_desktop(), [], now=1000, cli_path=cli_path)["fiveHourPercent"] == 22


def test_parser_schema_and_reset_edges():
    for value in (None, [], {}, {"rate_limits": None}, {"rate_limits": []}):
        assert statusline.parse(value, now=1000) == {}
    for reset in (None, True, [], {}, "2000", "2026-01-01T00:00:00", -1):
        assert statusline.parse(sample(5, reset), now=1000) == {}
    assert statusline.parse(sample(0, "1970-01-01T00:33:20Z"), now=1000)["five_hour"]["resets_at"] == 2000
    assert statusline.parse(sample(100), now=1000)["five_hour"]["used_percentage"] == 100


def test_bounded_file_and_future_and_stale(tmp_path):
    path = tmp_path / statusline.FILENAME
    path.write_bytes(b" " * (statusline.MAX_BYTES + 1))
    assert not statusline.read(path, now=1000)
    path.write_text("{")
    assert not statusline.read(path, now=1000)
    statusline.write(statusline.parse(sample(20), now=1061), tmp_path, now=1061)
    assert not statusline.read(path, now=1000)
    assert not statusline.read(path, now=1700, max_age=600)


def test_atomic_replace_and_projection(tmp_path, monkeypatch):
    calls = []
    replace = statusline.os.replace
    def record(src, dst):
        calls.append((src, dst))
        assert json.loads(open(src, encoding="utf-8").read())["five_hour"]["used_percentage"] == 33
        replace(src, dst)
    monkeypatch.setattr(statusline.os, "replace", record)
    statusline.write(statusline.parse(sample(33), now=1000), tmp_path, now=1000)
    assert len(calls) == 1 and not list(tmp_path.glob('.claude-statusline-*'))


def test_roots_and_window_merge(tmp_path):
    local = tmp_path / 'Local'
    root = local / 'Branchglance' / 'state'
    paths = statusline.sample_paths(root, {'LOCALAPPDATA': str(local)})
    assert paths[1] == local / 'Packages' / 'Claude_pzs8sxrjxfjjc' / 'LocalCache' / 'Local' / 'Branchglance' / 'state' / statusline.FILENAME
    statusline.write(statusline.parse(sample(40), now=1000), paths[0].parent, now=1000)
    statusline.write(statusline.parse(sample(20), now=1100), paths[1].parent, now=1100)
    desktop = {**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 30, 'observedAt': 1200}
    merged = statusline.merge_usage(desktop, paths, now=1200)
    assert merged['fiveHourPercent'] == 40 and merged['observedAt'] == 1000
    statusline.write(statusline.parse(sample(5, 3000), now=1200), paths[1].parent, now=1200)
    merged = statusline.merge_usage(desktop, paths, now=1200)
    assert (merged['fiveHourPercent'], merged['fiveHour']) == (5, 3000)
    statusline.write(statusline.parse(sample(99, 2000), now=1300), paths[1].parent, now=1300)
    assert statusline.read(paths[1], now=1300)['five_hour']['used_percentage'] == 5
    expired = statusline.merge_usage(empty_desktop(), paths, now=3100)
    assert expired['fiveHour'] is None and expired['fiveHourPercent'] is None
    assert expired['lastKnownFiveHour'] == 3000


def test_large_and_bad_stdin_are_quiet_and_bounded(tmp_path):
    import os
    import subprocess
    import sys
    env = os.environ.copy()
    env.update(LOCALAPPDATA=str(tmp_path), APPDATA=str(tmp_path), USERPROFILE=str(tmp_path),
               HOME=str(tmp_path), BRANCHGLANCE_STATE_ROOT=str(tmp_path))
    for payload in (b' ' * (statusline.MAX_BYTES + 1), b'{invalid', b'null'):
        proc = subprocess.run([sys.executable, '-m', 'token_tracker.claude_statusline'],
                              input=payload, capture_output=True, env=env, timeout=10)
        assert proc.returncode == 0 and proc.stdout.strip() == b'' and proc.stderr == b''
        assert not (tmp_path / statusline.FILENAME).exists()


def test_stale_winning_window_is_unknown(tmp_path):
    statusline.write(statusline.parse(sample(60, 5000), now=1000), tmp_path, now=1000)
    desktop = {**empty_desktop(), 'fiveHour': 5000, 'fiveHourPercent': 20, 'observedAt': 1700}
    merged = statusline.merge_usage(desktop, [tmp_path / statusline.FILENAME], now=1700)
    assert merged['fiveHourPercent'] is None and merged['_fiveHourSelected']


def test_expired_statusline_cannot_fall_back_to_history(tmp_path, monkeypatch):
    from token_tracker import providers
    root = tmp_path / 'state'
    statusline.write(statusline.parse(sample(80, 1050), now=1000), root, now=1000)
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [root / statusline.FILENAME])
    monkeypatch.setattr(statusline.time, 'time', lambda: 1100)
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {'u': {'fh': 80}}))
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_reset_at', lambda self: 5000)
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **k: empty_desktop())
    snapshot = providers.ClaudeLimitsProvider(str(tmp_path)).snapshot()
    assert snapshot['fiveHour']['percent'] is None
    assert snapshot['fiveHour']['resetAt'] is None


def test_deeply_nested_file_is_rejected(tmp_path):
    path = tmp_path / statusline.FILENAME
    path.write_text('[' * 2000 + '0' + ']' * 2000)
    assert statusline.read(path, now=1000) == {}


def test_impossible_deadlines_rejected_by_writer_and_reader(tmp_path):
    assert not statusline.parse(sample(1, 1e308), now=1000)
    path = tmp_path / statusline.FILENAME
    path.write_text(json.dumps({'source': 'claude-statusline', 'five_hour':
                              {'used_percentage': 1, 'resets_at': 1e308, 'receivedAt': 1000}}))
    assert not statusline.read(path, now=1000)
    statusline.write(statusline.parse(sample(40), now=1000), tmp_path, now=1000)
    assert statusline.read(path, now=1000)['five_hour']['used_percentage'] == 40


def test_later_desktop_deadline_with_unknown_usage_wins(tmp_path):
    statusline.write(statusline.parse(sample(80, 2000), now=1000), tmp_path, now=1000)
    desktop = {**empty_desktop(), 'fiveHour': 3000, 'fiveHourPercent': None, 'observedAt': 1000}
    merged = statusline.merge_usage(desktop, [tmp_path / statusline.FILENAME], now=1100)
    assert merged['fiveHour'] == 3000 and merged['fiveHourPercent'] is None


def test_snapshot_preserves_desktop_cache_anti_rollback(tmp_path, monkeypatch):
    from token_tracker import providers
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, '_last_live', {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    items = iter([{**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 70, 'observedAt': 1050},
                  {**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 30, 'observedAt': 1000}])
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: next(items))
    provider = providers.ClaudeLimitsProvider(str(tmp_path))
    assert [provider.snapshot()['fiveHour']['percent'] for _ in range(2)] == [70, 70]


def test_blocked_stdin_exits_with_deadline(tmp_path):
    import os, subprocess, sys, time
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), APPDATA=str(tmp_path), USERPROFILE=str(tmp_path),
               HOME=str(tmp_path), BRANCHGLANCE_STATE_ROOT=str(tmp_path))
    proc = subprocess.Popen([sys.executable, '-m', 'token_tracker.claude_statusline'], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    try:
        proc.stdin.write(b'{}'); proc.stdin.flush()
        proc.wait(timeout=3.5)
        assert proc.returncode == 0 and proc.stdout.read() == b'' and proc.stderr.read() == b''
    finally:
        if proc.poll() is None:
            proc.kill(); proc.wait()
        proc.stdin.close()


def test_reparse_ancestor_is_rejected(tmp_path, monkeypatch):
    import os, stat
    root = tmp_path / 'redirected' / 'state'
    root.mkdir(parents=True)
    original = os.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(root.parent):
            class Fake:
                st_mode = info.st_mode
                st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
            return Fake()
        return info
    monkeypatch.setattr(os, 'lstat', lstat)
    try:
        statusline.write(statusline.parse(sample(20), now=1000), root, now=1000)
    except OSError:
        pass
    assert not (root / statusline.FILENAME).exists()


def test_reparse_file_targets_are_rejected(tmp_path, monkeypatch):
    import os, stat
    root = tmp_path / 'state'
    root.mkdir()
    target = root / 'claude-statusline.lock'
    target.write_bytes(b'')
    original = os.lstat
    def lstat(path, *args, **kwargs):
        info = original(path, *args, **kwargs)
        if os.fspath(path) == os.fspath(target):
            class Fake:
                st_mode = info.st_mode
                st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT
            return Fake()
        return info
    monkeypatch.setattr(os, 'lstat', lstat)
    try:
        statusline.write(statusline.parse(sample(20), now=1000), root, now=1000)
    except OSError:
        pass
    assert target.read_bytes() == b'' and not (root / statusline.FILENAME).exists()


def test_direct_writer_rejects_poisoned_deadline(tmp_path):
    statusline.write({'five_hour': {'used_percentage': 1, 'resets_at': 1e308, 'receivedAt': 1000}}, tmp_path, now=1000)
    assert not (tmp_path / statusline.FILENAME).exists()


def test_hardlinked_lock_and_final_are_never_modified(tmp_path):
    import os
    import pytest
    outside = tmp_path / 'outside'
    outside.write_bytes(b'sentinel')
    root = tmp_path / 'state'
    root.mkdir()
    lock = root / 'claude-statusline.lock'
    try:
        os.link(outside, lock)
    except OSError:
        pytest.skip('hard links unavailable')
    with pytest.raises(OSError):
        statusline.write(statusline.parse(sample(20), now=1000), root, now=1000)
    assert outside.read_bytes() == b'sentinel'
    lock.unlink()
    os.link(outside, root / statusline.FILENAME)
    with pytest.raises(OSError):
        statusline.write(statusline.parse(sample(20), now=1000), root, now=1000)
    assert outside.read_bytes() == b'sentinel'
    assert statusline.read(root / statusline.FILENAME, now=1000) == {}


def test_junction_ancestor_refused_for_reader_writer_and_msix(tmp_path):
    import os, subprocess, pytest
    if os.name != 'nt':
        pytest.skip('Windows junction regression')
    local = tmp_path / 'Local'
    root = local / 'Branchglance' / 'state'
    paths = statusline.sample_paths(root, {'LOCALAPPDATA': str(local)})
    target = tmp_path / 'target'
    target.mkdir()
    redirected_state = target / 'Branchglance' / 'state'
    redirected_state.mkdir(parents=True)
    (redirected_state / statusline.FILENAME).write_text(json.dumps({'source': 'claude-statusline', 'five_hour':
        {'used_percentage': 90, 'resets_at': 2000, 'receivedAt': 1000}}))
    package = paths[1].parents[3]
    package.mkdir(parents=True)
    junction = package / 'Local'
    made = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(junction), str(target)], capture_output=True)
    if made.returncode:
        pytest.skip('junction creation unavailable')
    assert statusline.read(paths[1], now=1000) == {}
    assert statusline.merge_usage(empty_desktop(), paths, now=1000)['fiveHourPercent'] is None
    with pytest.raises(OSError):
        statusline.write(statusline.parse(sample(20), now=1000), paths[1].parent, now=1000)
    assert json.loads((redirected_state / statusline.FILENAME).read_text())['five_hour']['used_percentage'] == 90


def test_pinned_directory_cannot_be_renamed(tmp_path):
    import os, pytest
    if os.name != 'nt':
        pytest.skip('Windows handle sharing regression')
    root = tmp_path / 'state'
    root.mkdir()
    with statusline._guard_directories(root):
        with pytest.raises(OSError):
            root.rename(tmp_path / 'moved')


def test_contended_lock_respects_command_deadline(tmp_path):
    import os, subprocess, sys, time, pytest
    if os.name != 'nt':
        pytest.skip('Windows locking regression')
    import msvcrt
    root = tmp_path / 'state'
    root.mkdir()
    env = dict(os.environ, LOCALAPPDATA=str(tmp_path), APPDATA=str(tmp_path), USERPROFILE=str(tmp_path),
               HOME=str(tmp_path), BRANCHGLANCE_STATE_ROOT=str(root))
    payload = json.dumps(sample(30, time.time() + 1000)).encode()
    with statusline._open_checked(root / 'claude-statusline.lock', write=True, create=True) as lock:
        msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        try:
            start = time.monotonic()
            result = subprocess.run([sys.executable, '-m', 'token_tracker.claude_statusline'], input=payload,
                                    capture_output=True, env=env, timeout=4)
            assert time.monotonic() - start < 3.5
            assert result.returncode == 0 and result.stdout == b'' and not result.stderr
            assert not (root / statusline.FILENAME).exists()
        finally:
            lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)


def test_snapshot_keeps_later_unknown_window_through_rollback_guard(tmp_path, monkeypatch):
    from token_tracker import providers
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, '_last_live', {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    items = iter([{**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 70, 'observedAt': 1050},
                  {**empty_desktop(), 'fiveHour': 3000, 'fiveHourPercent': None, 'observedAt': 1060},
                  {**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 80, 'observedAt': 1090}])
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: next(items))
    provider = providers.ClaudeLimitsProvider(str(tmp_path))
    assert provider.snapshot()['fiveHour']['percent'] == 70
    later = provider.snapshot()['fiveHour']
    assert later['resetAt'] == 3000 and later['percent'] is None
    replay = provider.snapshot()['fiveHour']
    assert replay['resetAt'] == 3000 and replay['percent'] is None


def test_snapshot_never_lowers_same_window_on_newer_desktop_response(tmp_path, monkeypatch):
    from token_tracker import providers
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, '_last_live', {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    items = iter([{**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 70, 'observedAt': 1000},
                  {**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 30, 'observedAt': 1090}])
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: next(items))
    provider = providers.ClaudeLimitsProvider(str(tmp_path))
    assert [provider.snapshot()['fiveHour']['percent'] for _ in range(2)] == [70, 70]


def test_lock_substitution_after_path_check_is_not_followed(tmp_path, monkeypatch):
    import os, pytest
    root = tmp_path / 'state'
    root.mkdir()
    outside = tmp_path / 'outside'
    outside.write_bytes(b'sentinel')
    target = root / 'claude-statusline.lock'
    target.write_bytes(b'')
    original = statusline._check_path
    swapped = False
    def check(path, **kwargs):
        nonlocal swapped
        result = original(path, **kwargs)
        if path == target and not swapped:
            swapped = True
            target.unlink()
            os.link(outside, target)
        return result
    monkeypatch.setattr(statusline, '_check_path', check)
    with pytest.raises(OSError):
        statusline.write(statusline.parse(sample(20), now=1000), root, now=1000)
    assert swapped and outside.read_bytes() == b'sentinel'
    assert not (root / statusline.FILENAME).exists()


def test_both_window_deadline_bounds_are_enforced(tmp_path):
    for name, duration in statusline.DURATIONS.items():
        def data(reset):
            return {'rate_limits': {name: {'used_percentage': 15, 'resets_at': reset}}}
        assert name in statusline.parse(data(1000 + duration + statusline.SKEW), now=1000)
        assert not statusline.parse(data(1001 + duration + statusline.SKEW), now=1000)
        assert not statusline.parse(data(999 - statusline.SKEW), now=1000)
        path = tmp_path / statusline.FILENAME
        path.write_text(json.dumps({'source': 'claude-statusline', name: {'used_percentage': 15,
                                   'resets_at': 1001 + duration + statusline.SKEW, 'receivedAt': 1000}}))
        assert not statusline.read(path, now=1000)


def test_initial_unknown_window_blocks_older_replay(tmp_path, monkeypatch):
    from token_tracker import providers
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, '_last_live', {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    values = iter([{**empty_desktop(), 'fiveHour': 3000, 'fiveHourPercent': None, 'observedAt': 1000},
                   {**empty_desktop(), 'fiveHour': 2000, 'fiveHourPercent': 80, 'observedAt': 1090}])
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: next(values))
    provider = providers.ClaudeLimitsProvider(str(tmp_path))
    first, second = [provider.snapshot()['fiveHour'] for _ in range(2)]
    assert first['resetAt'] == second['resetAt'] == 3000
    assert first['percent'] is second['percent'] is None


def test_weekly_refresh_cannot_erase_stale_five_hour_watermark(tmp_path, monkeypatch):
    from token_tracker import providers
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [])
    monkeypatch.setattr(providers.ClaudeLimitsProvider, '_last_live', {})
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    now = [1000]
    monkeypatch.setattr(providers.time, 'time', lambda: now[0])
    values = iter([
        {**empty_desktop(), 'fiveHour': 5000, 'fiveHourPercent': 70, 'weekly': 90000, 'weeklyPercent': 10, 'observedAt': 1000},
        {**empty_desktop(), 'fiveHour': 5000, 'fiveHourPercent': None, 'weekly': 90000, 'weeklyPercent': 20, 'observedAt': 1601},
        {**empty_desktop(), 'fiveHour': 5000, 'fiveHourPercent': 30, 'weekly': 90000, 'weeklyPercent': 30, 'observedAt': 1602},
    ])
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: next(values))
    provider = providers.ClaudeLimitsProvider(str(tmp_path))
    got = []
    for moment in (1000, 1601, 1602):
        now[0] = moment
        got.append(provider.snapshot()['fiveHour']['percent'])
    assert got == [70, None, None]


def test_full_provider_timestamps_merged_usage_not_history(tmp_path, monkeypatch):
    from token_tracker import providers
    root = tmp_path / 'state'
    statusline.write(statusline.parse(sample(70, 5000), now=1100), root, now=1100)
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [root / statusline.FILENAME])
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {}))
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: empty_desktop())
    provider = providers.ClaudeDesktopProvider(str(tmp_path))
    monkeypatch.setattr(provider, '_transcript_usage', lambda: {'totals': dict.fromkeys(('input_tokens','cached_tokens','cache_5m_tokens','cache_1h_tokens','output_tokens','total_tokens'), 0), 'models': {}, 'daily': {}, 'sessions': []})
    monkeypatch.setattr(provider, '_chats', lambda sessions: [])
    snap = provider.snapshot()
    assert snap['fiveHour']['percent'] == 70
    assert snap['limitsUpdatedAt'] == '1970-01-01T00:18:20+00:00'


def test_full_provider_mixed_history_and_live_uses_older_actual_timestamp(tmp_path, monkeypatch):
    from token_tracker import providers
    root = tmp_path / 'state'
    statusline.write(statusline.parse(sample(70, 5000), now=1100), root, now=1100)
    monkeypatch.setattr(statusline, 'sample_paths', lambda: [root / statusline.FILENAME])
    monkeypatch.setattr(providers.time, 'time', lambda: 1100)
    monkeypatch.setattr(providers.ClaudeDesktopProvider, '_usage', lambda self: ([], {'u': {'sd': 20}, 't': 1000000}))
    monkeypatch.setattr(providers, 'usage_info', lambda *a, **kw: empty_desktop())
    provider = providers.ClaudeDesktopProvider(str(tmp_path))
    monkeypatch.setattr(provider, '_transcript_usage', lambda: {'totals': dict.fromkeys(('input_tokens','cached_tokens','cache_5m_tokens','cache_1h_tokens','output_tokens','total_tokens'), 0), 'models': {}, 'daily': {}, 'sessions': []})
    monkeypatch.setattr(provider, '_chats', lambda sessions: [])
    snap = provider.snapshot()
    assert snap['fiveHour']['percent'] == 70 and snap['weekly']['percent'] == 20
    assert snap['limitsUpdatedAt'] == '1970-01-01T00:16:40+00:00'
