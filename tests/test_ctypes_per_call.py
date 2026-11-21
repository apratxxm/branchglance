"""Windows regressions for ctypes pointer-cache growth in hot/native helpers."""

import ctypes
import os
import uuid
from datetime import date

import pytest


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ctypes APIs")


def _assert_pointer_cache_stable(call):
    # Count product struct types handed to ctypes.POINTER rather than the size of the
    # global cache, which moved onto each type in Python 3.14 (Sol C-801).
    real_pointer = ctypes.POINTER
    seen = set()

    def recording_pointer(cls):
        if str(getattr(cls, "__module__", "")).startswith("token_tracker"):
            seen.add(cls)
        return real_pointer(cls)

    ctypes.POINTER = recording_pointer
    try:
        call()
        baseline = set(seen)
        for _ in range(3):
            call()
    finally:
        ctypes.POINTER = real_pointer
    assert seen == baseline


class _FunctionProxy:
    def __init__(self, function, recorder, name, override=None):
        object.__setattr__(self, "_function", function)
        object.__setattr__(self, "_recorder", recorder)
        object.__setattr__(self, "_name", name)
        object.__setattr__(self, "_override", override)

    @property
    def argtypes(self):
        return self._function.argtypes

    @argtypes.setter
    def argtypes(self, value):
        self._recorder.signature_writes += 1
        self._function.argtypes = value

    @property
    def restype(self):
        return self._function.restype

    @restype.setter
    def restype(self, value):
        self._recorder.signature_writes += 1
        self._function.restype = value

    def __call__(self, *args):
        self._recorder.calls[self._name] = self._recorder.calls.get(self._name, 0) + 1
        if self._override is not None:
            return self._override(*args)
        return self._function(*args)


class _DllProxy:
    def __init__(self, library, recorder, overrides):
        self._library = library
        self._recorder = recorder
        self._overrides = overrides
        self._functions = {}

    def __getattr__(self, name):
        if name not in self._functions:
            self._functions[name] = _FunctionProxy(
                getattr(self._library, name), self._recorder, name, self._overrides.get(name))
        return self._functions[name]


class _CountingWinDLL:
    def __init__(self, overrides=None):
        self._real = ctypes.WinDLL
        self._overrides = overrides or {}
        self.constructions = []
        self.signature_writes = 0
        self.calls = {}

    def __call__(self, name, **kwargs):
        self.constructions.append(name)
        library = self._real(name, **kwargs)
        return _DllProxy(library, self, self._overrides.get(name, {}))


def _assert_bindings_cached(monkeypatch, factory, call):
    with monkeypatch.context() as context:
        context.setattr(ctypes, "WinDLL", factory)
        call()
        warm_counts = (len(factory.constructions), factory.signature_writes)
        assert all(warm_counts), "the first call must construct and bind the native API"
        _assert_pointer_cache_stable(call)
        assert (len(factory.constructions), factory.signature_writes) == warm_counts


def test_agent_links_windows_receipt_types_do_not_accumulate(tmp_path, monkeypatch):
    from token_tracker import agent_links, product

    file = tmp_path / "identity"
    fd = os.open(file, os.O_CREAT | os.O_RDWR, 0o600)
    inbox = tmp_path / "inbox"
    receipt = inbox / "receipts.jsonl"

    class StubFunction:
        def __init__(self, function):
            self.function = function
            self.argtypes = self.restype = None

        def __call__(self, *args):
            return self.function(*args)

    folder_buffer = ctypes.create_unicode_buffer(r"C:\Users\stub\AppData\Local")

    def known_folder(_folder, _flags, _token, out):
        out._obj.value = ctypes.addressof(folder_buffer)
        return 0

    def file_information(_handle, out):
        out._obj.index_low = 1
        return 1

    def convert_descriptor(_sddl, _revision, out, _size):
        out._obj.value = 1234
        return 1

    shell = type("Shell", (), {"SHGetKnownFolderPath": StubFunction(known_folder)})()
    ole = type("Ole", (), {"CoTaskMemFree": StubFunction(lambda _ptr: None)})()
    kernel = type("Kernel", (), {
        "GetFileInformationByHandle": StubFunction(file_information),
        "CreateDirectoryW": StubFunction(lambda *_: 1),
        "CreateFileW": StubFunction(lambda *_: 1),
        "CloseHandle": StubFunction(lambda *_: 1),
        "LocalFree": StubFunction(lambda *_: 0),
    })()
    advapi = type("Advapi", (), {
        "ConvertStringSecurityDescriptorToSecurityDescriptorW": StubFunction(convert_descriptor),
        "SetFileSecurityW": StubFunction(lambda *_: 1),
    })()

    class Factory:
        def __init__(self):
            self.loaded = []

        def __call__(self, name, **_kwargs):
            self.loaded.append(name)
            return {"shell32": shell, "ole32": ole, "kernel32": kernel, "advapi32": advapi}[name]

    factory = Factory()
    try:
        monkeypatch.setattr(agent_links, "_plain_chain", lambda _path: True)
        monkeypatch.setattr(product, "current_user_sid", lambda: "S-1-5-21-1-2-3-1000")
        monkeypatch.setattr(type(inbox), "is_dir", lambda _path: True)
        monkeypatch.setattr(type(receipt), "is_file", lambda _path: True)
        with monkeypatch.context() as context:
            context.setattr(ctypes, "WinDLL", factory)

            def call():
                assert str(agent_links._known_local_app_data()) == r"C:\Users\stub\AppData\Local"
                assert agent_links._file_identity(fd)
                agent_links._create_private_inbox(inbox, receipt)

            _assert_pointer_cache_stable(call)
            assert set(factory.loaded) == {"shell32", "ole32", "kernel32", "advapi32"}
    finally:
        os.close(fd)

    assert str(agent_links._known_local_app_data()) != r"C:\Users\stub\AppData\Local"


def test_claude_hook_windows_types_and_bindings_do_not_accumulate(tmp_path):
    from token_tracker import claude_hook

    append_path = str(tmp_path / "events.jsonl")
    empty_retention_dir = tmp_path / "retention"
    empty_retention_dir.mkdir()

    def call():
        claude_hook._process_table()
        assert claude_hook.probe_process(os.getpid())[0] == "alive"
        assert claude_hook._append_windows(append_path, b"{}\n", 1024) == "ok"
        claude_hook._retention_windows(str(empty_retention_dir), date.today())

    _assert_pointer_cache_stable(call)


def test_claude_hook_append_bindings_are_built_once(tmp_path, monkeypatch):
    from token_tracker import claude_hook

    factory = _CountingWinDLL()
    path = str(tmp_path / "append.jsonl")
    _assert_bindings_cached(
        monkeypatch, factory, lambda: claude_hook._append_windows(path, b"{}\n", 1024))


def test_codex_windows_job_types_do_not_accumulate(monkeypatch):
    from token_tracker import codex_client

    startup_types = []

    def refuse_process(*args):
        startup_types.append(type(args[8]._obj))
        return False

    factory = _CountingWinDLL({"kernel32": {"CreateProcessW": refuse_process}})
    resolved = codex_client.CodexExecutable("codex.exe")

    def call():
        with pytest.raises(OSError, match="could not create contained process"):
            codex_client._run_contained_codex(
                resolved, ["--version"], timeout=1, cwd=None, env=None, mark_started=lambda: None)

    _assert_bindings_cached(monkeypatch, factory, call)
    assert startup_types == [codex_client._windows_codex_types()[3]] * 5


def test_product_windows_mutex_security_attributes_do_not_accumulate():
    from token_tracker import product

    name = f"Global\\Branchglance-{product.current_user_sid()}-Instance.Test.{uuid.uuid4().hex}"

    def call():
        handle = product.create_secured_mutex(name)
        assert handle
        ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(handle)

    _assert_pointer_cache_stable(call)


def test_widget_monitor_structures_do_not_accumulate(monkeypatch):
    from token_tracker import widget

    monitor_types = []

    class StubFunction:
        def __init__(self, function):
            self.function = function
            self.argtypes = self.restype = None

        def __call__(self, *args):
            return self.function(*args)

    def get_monitor_info(_handle, info_ptr):
        info = info_ptr._obj
        monitor_types.append(type(info))
        info.flags = 1
        info.work.left, info.work.top, info.work.right, info.work.bottom = (10, 20, 810, 620)
        if hasattr(info, "device"):
            info.device = "DISPLAY1"
        return 1

    def enumerate_monitors(_dc, _clip, callback, _data):
        for handle in (101, 102):
            callback(ctypes.c_void_p(handle), None, ctypes.POINTER(widget._Rect)(), 0)
        return 1

    def scale_factor(_monitor, factor):
        factor._obj.value = 100
        return 0

    user32 = type("User32", (), {
        "SetThreadDpiAwarenessContext": StubFunction(lambda *_: None),
        "MonitorFromWindow": StubFunction(lambda *_: 99),
        "GetMonitorInfoW": StubFunction(get_monitor_info),
        "EnumDisplayMonitors": StubFunction(enumerate_monitors),
        "GetDpiForSystem": StubFunction(lambda: 96),
    })()
    shcore = type("Shcore", (), {"GetScaleFactorForMonitor": StubFunction(scale_factor)})()
    monkeypatch.setattr(widget.ctypes, "windll", type("Dlls", (), {"user32": user32, "shcore": shcore})())
    monkeypatch.setattr(widget, "_display_source_targets", lambda: {})
    monkeypatch.setattr(widget, "_display_kinds_by_device", lambda _targets: {})
    monkeypatch.setattr(widget, "_display_stable_ids_by_source", lambda _targets: {})

    def call():
        start = len(monitor_types)
        assert widget._work_area(hwnd=123) == (10, 20, 810, 620)
        assert len(widget._monitor_work_areas(physical=True)) == 2
        assert len(widget._monitor_descriptors()) == 2
        current = monitor_types[start:]
        assert len(current) == 5
        assert current[:3] == [widget._monitor_info_type()] * 3
        assert current[3:] == [widget._monitor_info_type(device=True)] * 2

    _assert_pointer_cache_stable(call)
