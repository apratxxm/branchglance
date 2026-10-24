import json
from pathlib import Path
import threading
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from token_tracker import server
from token_tracker import widget


def test_display_identity_failure_and_unknown_technology_stay_neutral(monkeypatch):
    monkeypatch.setattr(widget.ctypes, "WinDLL",
                        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("display API unavailable")),
                        raising=False)
    assert widget._display_source_output_technologies() == {}
    assert widget._display_kinds_by_device({"\\\\.\\DISPLAY1": {0xFFFFFFFF},
                                            "\\\\.\\DISPLAY2": {0xFFFFFFFE}}) == {}


def test_display_identity_uses_source_mapping_not_display_number():
    kinds = widget._display_kinds_by_device({
        "\\\\.\\DISPLAY2": {11},  # Windows may enumerate the laptop panel second.
        "\\\\.\\DISPLAY1": {10},
    })
    assert kinds == {"\\\\.\\DISPLAY2": "internal", "\\\\.\\DISPLAY1": "external"}


def test_cloned_source_with_conflicting_targets_stays_neutral():
    assert widget._display_kinds_by_device({"\\\\.\\DISPLAY1": {11, 10}}) == {}


def test_display_config_structures_match_win32_abi():
    assert widget.ctypes.sizeof(widget._DisplayConfigPathSourceInfo) == 20
    assert widget.ctypes.sizeof(widget._DisplayConfigPathTargetInfo) == 48
    assert widget.ctypes.sizeof(widget._DisplayConfigPathInfo) == 72
    assert widget.ctypes.sizeof(widget._DisplayConfigModeInfo) == 64
    assert widget.ctypes.sizeof(widget._DisplayConfigTargetNameInfo) == 420


def test_lvds_is_classified_as_internal_panel():
    assert widget._display_kinds_by_device({"\\\\.\\DISPLAY2": {6}}) == {
        "\\\\.\\DISPLAY2": "internal"}


def test_target_paths_are_stable_only_for_unambiguous_sources():
    paths = widget._display_stable_ids_by_source({
        "\\\\.\\DISPLAY2": [{"device_path": "\\\\?\\DISPLAY#PANEL"}],
        "\\\\.\\DISPLAY1": [{"device_path": "\\\\?\\DISPLAY#EXTERNAL"}],
        "\\\\.\\DISPLAY3": [{"device_path": "\\\\?\\DISPLAY#CLONE-A"},
                                {"device_path": "\\\\?\\DISPLAY#CLONE-B"}],
        "\\\\.\\DISPLAY4": [{"device_path": "\\\\?\\DISPLAY#SHARED"}],
        "\\\\.\\DISPLAY5": [{"device_path": "\\\\?\\DISPLAY#SHARED"}],
        "\\\\.\\DISPLAY6": [{"device_path": None}],
    })
    assert paths == {"\\\\.\\DISPLAY2": "\\\\?\\DISPLAY#PANEL",
                     "\\\\.\\DISPLAY1": "\\\\?\\DISPLAY#EXTERNAL"}


def test_legacy_monitor_id_migrates_using_unique_manual_area(native_notch):
    app, monitors, _rect, _calls = native_notch
    panel = "\\\\?\\DISPLAY#PANEL"
    external = "\\\\?\\DISPLAY#EXTERNAL"
    monitors[0].update(id=panel, stableId=panel, gdiId="display-one")
    monitors[1].update(id=external, stableId=external, gdiId="display-two")
    app.selected_monitor_id = "display-one"
    app._notch_manual_position = True
    app._notch_position = (-2200, 220)

    selected = app._selected_monitor(monitors)

    assert selected["id"] == external
    assert app.selected_monitor_id == external
    assert app._notch_manual_position is True
    assert app._notch_position == (-2200, 220)


def test_stable_target_keeps_manual_anchor_when_area_shrinks(native_notch):
    app, monitors, _rect, _calls = native_notch
    panel = "\\\\?\\DISPLAY#PANEL"
    monitors[0].update(id=panel, stableId=panel, gdiId="display-one", area=(0, 0, 1600, 900))
    app.selected_monitor_id = panel
    app._notch_manual_position = True
    app._notch_position = (1850, 1000)

    assert app._notch_target() == (680, 148, 920, 752)
    assert app.selected_monitor_id == panel
    assert app._notch_manual_position is True
    assert app._notch_position == (920, 752)


def test_expansion_keeps_right_edge_anchor_and_clips_height_downward(native_notch):
    app, monitors, _rect, _calls = native_notch
    panel = "\\\\?\\DISPLAY#PANEL"
    monitors[0].update(id=panel, stableId=panel, gdiId="display-one")
    app.selected_monitor_id = panel
    app._notch_manual_position = True
    app._notch_position = (1520, 800)

    collapsed = app._notch_target()
    app.notch_expanded = True
    app.notch_height = 900
    expanded = app._notch_target()

    assert collapsed == (680, 148, 1240, 800)
    assert expanded == (680, 280, 1240, 800)
    assert app._notch_position == (1240, 800)


def test_legacy_manual_anchor_near_bottom_keeps_full_collapsed_rail(native_notch):
    app, monitors, _rect, _calls = native_notch
    monitor = monitors[0]
    assert monitor.get("stableId") is None
    app.selected_monitor_id = monitor["id"]
    app._notch_manual_position = True
    app._notch_position = (1500, 1000)  # Valid legacy anchor, 80px above work-area bottom.

    collapsed = app._notch_target()
    app.notch_expanded = True
    app.notch_height = 900
    expanded = app._notch_target()

    assert collapsed == (680, 148, 1240, 932)
    assert expanded == (680, 148, 1240, 932)


def test_reordered_monitors_keep_selected_stable_target_and_manual_anchor(native_notch):
    app, monitors, _rect, _calls = native_notch
    panel = "\\\\?\\DISPLAY#PANEL"
    external = "\\\\?\\DISPLAY#EXTERNAL"
    monitors[0].update(id=panel, stableId=panel, gdiId="display-one")
    monitors[1].update(id=external, stableId=external, gdiId="display-two")
    app.selected_monitor_id = external
    app._notch_manual_position = True
    app._notch_position = (-2200, 220)

    monitors.reverse()

    selected = app._selected_monitor(monitors)
    assert selected["id"] == external
    assert selected["area"] == (-2560, 40, 0, 1440)
    assert app.selected_monitor_id == external
    assert app._notch_manual_position is True
    assert app._notch_position == (-2200, 220)


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_profile_sizes_modes_and_validates_before_mutating(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    assert widget.WidgetApp.__init__.__kwdefaults__["compact_profile"] is True
    assert app.compact_profile is True
    assert app._compact_notch_mode == "controls", "new compact profiles start with persistent toolbar controls"
    assert widget.COMPACT_NOTCH_TRANSITION_SECONDS == 0.22
    assert app.initial_bounds() == (420, 110, 750, 0)
    assert rect == [750, 0, 420, 110]
    assert rect[0] + rect[2] / 2 == 960

    for mode, expected in (("idle", (420, 100)), ("rest", (420, 100)),
                           ("controls", (420, 110)), ("inspect", (560, 114))):
        app.set_notch(False, None, False, mode)
        assert (rect[2], rect[3]) == expected
    app.set_notch(True, 80, False, "inspect")
    assert (rect[2], rect[3]) == (560, 114)
    assert rect[0] + rect[2] / 2 == 960
    app.set_notch(True, 500, False, "inspect")
    assert (rect[2], rect[3]) == (560, 420)
    assert app.select_monitor("display-two")[1]["selected"] is True
    app.set_notch(True, 200, False, "inspect")
    assert rect == [-1630, 40, 700, 250]

    before = (app.compact_profile, app._compact_notch_mode, app.notch_expanded,
              app.notch_height, tuple(rect))
    app.set_notch(False, None, False, "unknown")
    app.set_notch(False, float("nan"), False, "inspect")
    app.set_notch(False, True, False, "inspect")
    assert (app.compact_profile, app._compact_notch_mode, app.notch_expanded,
            app.notch_height, tuple(rect)) == before
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    app._notch_thread = SimpleNamespace(join=lambda timeout=None: None)
    app.set_notch(False, None, False, "controls")
    assert app._notch_transition[3] == widget.COMPACT_NOTCH_TRANSITION_SECONDS


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_mode_clamps_rendered_frame_without_moving_saved_anchor(native_notch):
    app, _monitors, rect, _calls = native_notch
    anchor = (1800, 1020)
    app._notch_manual_position = True
    app._notch_position = anchor
    app.set_notch(True, 300, False, "inspect")
    assert rect == [1360, 780, 560, 300]
    assert app._notch_position == anchor

    app.set_widget_lock(True, False)
    assert app._notch_position == anchor
    app.set_notch(True, 420, True, "inspect")
    assert rect == [1360, 660, 560, 420]
    assert app.notch_expanded is True
    assert app._notch_position == anchor
    assert app.select_monitor("display-two")[0]["selected"] is True

    pinned = app.set_keep_open(True, expected_revision=0)
    assert pinned == {"keepOpen": True, "keepOpenRevision": 1}
    assert app.set_keep_open(False, expected_revision=0) == pinned
    assert app.get_widget_lock()["keepOpen"] is True
    saved = json.loads(app.window_state_path.read_text(encoding="utf-8"))
    assert saved["keep_open"] is True
    assert saved["widget_locked"] is True
    assert saved["widget_lock_expanded"] is True


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_lock_keeps_inspect_measurement_when_legacy_presentation_args_change(native_notch):
    app, _monitors, rect, _calls = native_notch
    app.set_notch(True, 114, False, "inspect")
    assert rect[2:] == [560, 114]
    api = widget.WindowApi(None, None, None, on_lock=app.set_widget_lock,
                           on_get_lock=app.get_widget_lock,
                           on_compact_profile=lambda: app.compact_profile)

    locked = api.set_widget_lock(True, False, 420, expected_revision=0)
    assert locked["locked"] is True
    assert rect[2:] == [560, 114]
    assert app.notch_expanded is True
    assert app.notch_height == 114
    unlocked = api.set_widget_lock(False, False, 420, expected_revision=1)
    assert unlocked["locked"] is False
    assert rect[2:] == [560, 114]
    assert app.notch_expanded is True
    assert app.notch_height == 114


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_stable_anchor_returns_when_work_area_widens(native_notch):
    app, monitors, _rect, _calls = native_notch
    app._compact_notch_mode = "idle"
    panel = "\\\\?\\DISPLAY#PANEL"
    monitors[0].update(id=panel, stableId=panel)
    app.selected_monitor_id = panel
    app._notch_manual_position = True
    app._notch_position = (1800, 160)

    monitors[0]["area"] = (0, 0, 1600, 1080)
    assert app._notch_target() == (420, 100, 1180, 160)
    assert app._notch_position == (1800, 160)
    monitors[0]["area"] = (0, 0, 1920, 1080)
    assert app._notch_target() == (420, 100, 1500, 160)
    assert app._notch_position == (1800, 160)


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_typed_anchor_centers_expansion_and_drag_saves_actual_width(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    app._compact_notch_mode = "idle"
    app._notch_manual_position = True
    app._notch_position = (1500, 100)  # Old untyped anchors retain their left-edge meaning.
    app._notch_reference_width = None
    assert app._notch_target() == (420, 100, 1500, 100)
    app._compact_notch_mode = "inspect"
    app.notch_height = 180
    assert app._notch_target() == (560, 180, 1360, 100)

    app._notch_manual_position = False
    app._notch_position = None
    app.set_notch(True, 180, False, "inspect")
    assert rect[2] == 560
    _drag_compact_notch(app, rect, 400, 100, monkeypatch)
    assert app._notch_position == (400, 100)
    assert app._notch_reference_width == 560
    app.save_bounds()
    assert widget._load_notch_placement(app.window_state_path)["reference_width"] == 560
    app._on_notch_drag_begin()
    rect[:2] = [460, 130]
    app.on_moved(*rect[:2])
    app._finish_notch_drag(False)
    assert app._notch_position == (400, 100)
    assert app._notch_reference_width == 560

    app.set_notch(False, None, False, "idle")
    assert rect == [470, 100, 420, 100]
    app.set_notch(True, 180, False, "inspect")
    assert rect == [400, 100, 560, 180]
    assert app._notch_position == (400, 100)


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_dashboard_mode_uses_fixed_size_and_clamps_to_work_area(native_notch):
    app, monitors, rect, calls = native_notch
    app._compact_notch_mode = "dashboard"
    assert widget._compact_notch_dimensions("dashboard") == (720, 540)
    assert app._notch_target() == (720, 540, 600, 0)
    app.set_notch(False, 540, False, "dashboard")
    assert rect == [600, 0, 720, 540]
    assert calls[-1] == (720, 540, 600, 0)
    monitors[0]["area"] = (0, 0, 640, 480)
    assert app._notch_target() == (640, 480, 0, 0)


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_monitor_names_persist_by_stable_identity_after_restart_and_reorder(native_notch):
    app, monitors, _rect, _calls = native_notch
    panel = "\\\\?\\DISPLAY#PANEL"
    external = "\\\\?\\DISPLAY#EXTERNAL"
    monitors[0].update(id=panel, stableId=panel, gdiId="display-one", label="Laptop")
    monitors[1].update(id=external, stableId=external, gdiId="display-two", label="Monitor")
    app.selected_monitor_id = panel

    renamed = app.set_monitor_name(panel, "Studio")
    assert next(row for row in renamed if row["stableId"] == panel)["label"] == "Studio"
    stored = json.loads(app.window_state_path.read_text(encoding="utf-8"))
    assert stored["monitor_names"] == {panel.casefold(): "Studio"}

    restarted = object.__new__(widget.WidgetApp)
    restarted._notch_lock = threading.RLock()
    restarted.window_state_path = app.window_state_path
    restarted.selected_monitor_id = panel.lower()
    restarted._notch_manual_position = False
    restarted._notch_position = None
    restarted._notch_reference_width = None
    monitors.reverse()
    monitors[1]["stableId"] = panel.lower()
    monitors[1]["id"] = panel.lower()
    after_restart = restarted.get_monitors()
    assert next(row for row in after_restart if row["stableId"] == panel.lower())["label"] == "Studio"
    assert next(row for row in after_restart if row["stableId"] == external)["label"] == "Monitor"

@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_drag_reference_width_uses_physical_dpi_geometry(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    app.select_monitor("display-two")
    app.set_notch(True, 180, False, "inspect")
    assert rect[2] == 700  # 560 logical pixels at 125% DPI.
    _drag_compact_notch(app, rect, -2100, 300, monkeypatch)
    assert app._notch_reference_width == 700

    app.set_notch(False, None, False, "rest")
    assert rect[2] == 525
    saved_center = -2100 + 700 / 2
    rendered_center = rect[0] + rect[2] / 2
    assert abs(saved_center - rendered_center) <= 0.5


def test_notch_reference_width_loads_only_positive_integer_values(tmp_path):
    state_path = tmp_path / "window-state.json"
    placement = {"monitor_id": "panel", "manual": True, "x": 100, "y": 120}
    for value in (560, True, 0, -1, 560.0, "560"):
        state_path.write_text(json.dumps({"notch": {**placement, "reference_width": value}}),
                              encoding="utf-8")
        loaded = widget._load_notch_placement(state_path)
        assert loaded["x"] == 100 and loaded["y"] == 120
        if type(value) is int and value > 0:
            assert loaded["reference_width"] == value
        else:
            assert "reference_width" not in loaded


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_initial_position_lock_capture_records_actual_inspect_width(native_notch):
    app, _monitors, rect, _calls = native_notch
    app.set_notch(True, 180, False, "inspect")
    assert rect[2] == 560
    assert app._notch_manual_position is False
    app.set_widget_lock(True, False)
    assert app._notch_manual_position is True
    assert app._notch_reference_width == 560


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_locked_compact_drag_requires_token_and_updates_physical_anchor(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    button_down = False
    monkeypatch.setattr(app, "_primary_button_down", lambda: button_down)
    app._notch_manual_position = True
    app._notch_position = (1800, 1020)
    app.set_widget_lock(True, False)

    assert app._on_notch_drag_begin() is False
    assert app.begin_drag() is False
    assert app.begin_drag("compact-v1") == {"accepted": False, "generation": None}
    button_down = True
    accepted = app.begin_drag("compact-v1")
    assert accepted["accepted"] is True
    generation = accepted["generation"]
    assert isinstance(generation, int)
    rect[:2] = [-2300, 900]
    app.on_moved(*rect[:2])
    assert app._notch_position == (1800, 1020)
    assert app.widget_locked is True
    assert app.selected_monitor_id == "display-one"
    button_down = False
    assert app.end_drag(generation, True) is True
    assert app._notch_position == (-2300, 900)
    assert app._notch_reference_width == 420
    assert app.selected_monitor_id == "display-two"
    assert app.widget_locked is True
    assert app.end_drag(generation, True) is False
    assert app.end_drag(generation, False) is False
    app.select_monitor("display-one")
    assert app.selected_monitor_id == "display-two"

    rect[:2] = [100, 100]
    app.on_moved(*rect[:2])
    target = app._notch_target()
    assert rect[:2] == [target[2], target[3]]
    assert app._notch_position == (-2300, 900)


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_locked_compact_drag_expiry_rolls_back_and_stale_end_cannot_finish_next(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(app, "_primary_button_down", lambda: True)
    app._notch_manual_position = True
    app._notch_position = (1800, 1020)
    app.set_widget_lock(True, False)
    original = (app._notch_position, app._notch_reference_width, app.selected_monitor_id)

    first = app.begin_drag("compact-v1")["generation"]
    app._on_notch_drag_begin()
    rect[:2] = [1300, 850]
    app.on_moved(*rect[:2])
    app._expire_compact_drag(first)
    assert app._active_compact_drag_generation is None
    assert (app._notch_position, app._notch_reference_width, app.selected_monitor_id) == original

    second = app.begin_drag("compact-v1")["generation"]
    assert second != first
    app._on_notch_drag_begin()
    monkeypatch.setattr(app, "_primary_button_down", lambda: False)
    app._on_notch_drag_end()
    assert app._active_compact_drag_generation == second
    assert app.end_drag(first, True) is False
    assert app.end_drag(first, False) is False
    assert app._active_compact_drag_generation == second
    assert app.end_drag(second, 1) is False
    assert app._active_compact_drag_generation == second
    assert app.end_drag(second, False) is True
    assert app._active_compact_drag_generation is None


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_cancel_token_rolls_back_typed_anchor_while_button_is_down(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(app, "_primary_button_down", lambda: True)
    app._notch_manual_position = True
    app._notch_position = (1700, 400)
    app._notch_reference_width = 560
    app.set_widget_lock(True, False)
    original = (app._notch_position, app._notch_reference_width, app.selected_monitor_id)
    generation = app.begin_drag("compact-v1")["generation"]

    rect[:2] = [-2300, 300]
    app.on_moved(*rect[:2])
    assert app.end_drag(generation, 1) is False
    assert app._active_compact_drag_generation == generation
    assert app.end_drag(generation, False) is True
    assert app._active_compact_drag_generation is None
    assert (app._notch_position, app._notch_reference_width, app.selected_monitor_id) == original
    assert app.widget_locked is True


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_release_watchdog_commits_without_bridge_mouseup(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    button_state = True
    monkeypatch.setattr(app, "_primary_button_down", lambda: button_state)
    app.set_widget_lock(True, False)
    original = app._notch_position
    generation = app.begin_drag("compact-v1")["generation"]
    rect[:2] = [1400, 800]
    app.on_moved(*rect[:2])
    assert app._notch_position == original

    button_state = None
    app._watch_compact_drag_release(generation)
    assert app._active_compact_drag_generation == generation
    assert app._notch_position == original

    button_state = False
    app._watch_compact_drag_release(generation)
    assert app._active_compact_drag_generation is None
    assert app._notch_position == (1400, 800)
    assert app._notch_reference_width == 420
    assert app.widget_locked is True


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_drag_hide_cancels_and_rolls_back(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(app, "_primary_button_down", lambda: True)
    app.set_widget_lock(True, False)
    original = (app._notch_position, app._notch_reference_width, app.selected_monitor_id)
    generation = app.begin_drag("compact-v1")["generation"]
    rect[:2] = [1400, 800]
    app.on_moved(*rect[:2])
    app.hide()
    assert app._active_compact_drag_generation is None
    assert (app._notch_position, app._notch_reference_width, app.selected_monitor_id) == original


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_native_hidden_state_before_end_rolls_back_even_without_hide_event(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(app, "_primary_button_down", lambda: True)
    app._notch_manual_position = True
    app._notch_position = (1700, 400)
    app._notch_reference_width = 560
    app.set_widget_lock(True, False)
    original = (app._notch_position, app._notch_reference_width, app.selected_monitor_id)
    generation = app.begin_drag("compact-v1")["generation"]
    rect[:2] = [1300, 300]
    app.on_moved(*rect[:2])
    monkeypatch.setattr(app, "_primary_button_down", lambda: False)
    monkeypatch.setattr(app, "_native_state", lambda: {
        "visible": False, "minimized": False, "maximized": False})

    assert app.end_drag(generation, True) is True
    assert app._active_compact_drag_generation is None
    assert (app._notch_position, app._notch_reference_width, app.selected_monitor_id) == original
    assert app.widget_locked is True


def test_compact_drag_bridge_preserves_legacy_bool_and_rejects_unknown_intents():
    calls = []
    api = widget.WindowApi(None, None, None,
                           on_drag_begin=lambda intent=None: {"accepted": True, "generation": 9},
                           on_drag_end=lambda generation=True, commit=None: calls.append((generation, commit)) or True)
    assert api.begin_drag() == {"accepted": True, "generation": 9}
    assert api.begin_drag("compact-v1") == {"accepted": True, "generation": 9}
    assert api.begin_drag("unknown") == {"accepted": False, "generation": None}
    assert api.end_drag(True) is True
    assert api.end_drag(9) is False
    assert api.end_drag(9, True) is True
    assert api.end_drag(generation=9, commit=True) is True
    assert api.end_drag(9, 1) is False
    assert calls == [(True, None), (9, True), (9, True)]


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_locked_compact_edge_transition_ignores_intermediate_callbacks(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    anchor = (1800, 1020)
    app._notch_manual_position = True
    app._notch_position = anchor
    app.set_widget_lock(True, False)
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    app._notch_thread = SimpleNamespace(join=lambda timeout=None: None)

    app.set_notch(True, 300, False, "inspect")
    expected = (560, 300, 1360, 780)
    assert app._widget_lock_render_position == expected[2:]
    rect[:] = [1380, 790, 560, 300]
    app.on_moved(1380, 790)
    assert app._notch_transition is not None
    assert rect == [1380, 790, 560, 300]
    assert app._notch_position == anchor

    app._notch_transition = None
    app._notch_command_guard_until = 0
    app._notch_native_frames.clear()
    rect[:2] = [100, 50]
    app.on_moved(100, 50)
    assert rect == [1360, 780, 560, 300]
    assert app._notch_position == anchor


def test_compact_notch_api_forwards_allowlisted_mode_and_keep_open_revision():
    notch_calls, pin_calls = [], []
    keep_state = {"keepOpen": False, "keepOpenRevision": 3}
    api = widget.WindowApi(None, None, None,
                           on_notch=lambda *args: notch_calls.append(args),
                           on_keep_open=lambda *args: pin_calls.append(args) or keep_state,
                           on_get_keep_open=lambda: keep_state)
    api.set_notch(True, 220, True, "inspect")
    api.set_notch(True, 220, True, "not-allowed")
    assert notch_calls == [(True, 220, True, "inspect")]
    assert api.get_keep_open() == keep_state
    assert api.set_keep_open(1, 3) == keep_state
    assert api.set_keep_open(True, True) == keep_state
    assert api.set_keep_open(True, 3) == keep_state
    assert pin_calls == [(True, 3)]


def test_historical_locked_expanded_state_does_not_enable_keep_open(tmp_path):
    state_path = tmp_path / "window-state.json"
    state_path.write_text(json.dumps({"widget_locked": True,
                                      "widget_lock_expanded": True}), encoding="utf-8")
    assert widget._load_widget_lock(state_path) == {
        "locked": True, "expanded": True, "keep_open": False}


@pytest.mark.parametrize("native_notch", [True], indirect=True)
def test_compact_reduced_motion_applies_mode_bounds_immediately(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    api = widget.WindowApi(None, None, None, on_notch=app.set_notch)
    api.set_notch(True, 200, True, "inspect")
    assert rect == [680, 0, 560, 200]
    assert app._notch_transition is None
    assert app._notch_thread is None


def test_recenter_cli_is_opt_in_and_consumed_once(monkeypatch):
    launches = []

    class StubWidgetApp:
        def __init__(self, *, start_hidden=False, recenter_on_start=False):
            self.recenter_on_start = recenter_on_start
            self.start_hidden = start_hidden

        @staticmethod
        def notify_existing():
            return False

        def run(self):
            launches.append((self.start_hidden, self.recenter_on_start))

    monkeypatch.setattr(widget, "WidgetApp", StubWidgetApp)
    monkeypatch.setattr(widget, "claim_instance_mutex", lambda name=None: (None, "acquired"))
    monkeypatch.setattr(widget.product, "set_app_user_model_id", lambda *a: True)
    widget.main([])
    widget.main(["--background", "--recenter"])
    assert launches == [(False, False), (True, True)]


@pytest.mark.parametrize("recenter_on_start", [False, True])
def test_startup_recenter_is_opt_in_and_preserves_lock_and_keep_open(tmp_path, monkeypatch,
                                                                    recenter_on_start):
    path = tmp_path / "window-state.json"
    path.write_text(json.dumps({
        "widget_locked": True,
        "widget_lock_expanded": True,
        "keep_open": True,
        "notch": {"monitor_id": "display-one", "manual": True, "x": 900, "y": 300,
                  "reference_width": 560},
    }), encoding="utf-8")
    monitors = [{"id": "display-one", "label": "Panel", "index": 1, "primary": True,
                 "scale": 1.0, "area": (0, 0, 1920, 1080)}]
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    monkeypatch.setattr(widget, "_monitor_descriptors", lambda: monitors)
    monkeypatch.setattr(widget, "_system_scale", lambda: 1.0)
    app = widget.WidgetApp(compact_profile=True, recenter_on_start=recenter_on_start)
    try:
        assert app.get_widget_lock()["locked"] is True
        assert app.get_widget_lock()["keepOpen"] is True
        if recenter_on_start:
            assert app._notch_manual_position is False
            assert app._notch_reference_width is None
            assert app.initial_bounds() == (420, 110, 750, 0)

            rect = list(app.initial_bounds())
            app.window = SimpleNamespace(evaluate_js=lambda _script: None,
                                         hide=lambda: None)
            monkeypatch.setattr(app, "_native_window_handle", lambda: 1)
            monkeypatch.setattr(app, "_native_rect", lambda: tuple(rect))

            def place(width, height, x, y, **_kwargs):
                rect[:] = [x, y, width, height]
                return True

            monkeypatch.setattr(app, "_place", place)
            app.restore_saved_placement()
            app._recenter_after_start()
            app._recenter_after_start()
            assert app.widget_locked is True
            assert app.keep_open is True
            assert app._widget_lock_revision == 0
            assert app.initial_bounds() == (420, 110, 750, 0)
            saved = json.loads(path.read_text(encoding="utf-8"))
            assert saved["widget_locked"] is True and saved["keep_open"] is True
            assert saved["notch"] == {"monitor_id": "display-one", "manual": False}
        else:
            assert app._notch_manual_position is True
            assert app._notch_reference_width == 560
            assert app.initial_bounds() == (420, 110, 970, 300)
    finally:
        if app._bounds_timer:
            app._bounds_timer.cancel()
        app.control.close()


def test_animation_reversal_starts_at_current_native_rectangle(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    # Keep the animation worker dormant so the assertion covers the next
    # transition's actual native starting rectangle deterministically.
    app._notch_thread = SimpleNamespace(join=lambda timeout=None: None)
    app.set_notch(True, 900)
    assert app._notch_transition[0] == (680, 148, 620, 0)
    assert app._notch_transition[1] == (680, 900, 620, 0)
    assert app._notch_transition[3] == 0.14

    rect[:] = [760, 0, 400, 540]
    app.set_notch(False)
    assert app._notch_transition[0] == (400, 540, 760, 0)
    assert app._notch_transition[1] == (680, 148, 620, 0)
    assert app._notch_transition[3] == 0.11

    app.set_notch(True, 900)
    assert app._notch_transition[3] == 0.14
    app.set_notch(True, 850)
    assert app._notch_transition[0] == (400, 540, 760, 0)
    assert app._notch_transition[1] == (680, 850, 620, 0)
    assert app._notch_transition[3] == 0.11


def test_notch_smootherstep_stays_monotonic_bounded_and_horizontally_fixed():
    start = (400, 96, 760, 0)
    target = (400, 900, 760, 0)
    frames = [widget._notch_transition_frame(start, target, step / 100)
              for step in range(101)]

    assert widget._notch_transition_frame(start, target, 0) == start
    assert widget._notch_transition_frame(start, target, 1) == target
    assert all(frame[0] == 400 and frame[2:] == (760, 0) for frame in frames)
    assert [frame[1] for frame in frames] == sorted(frame[1] for frame in frames)
    assert all(96 <= frame[1] <= 900 for frame in frames)

    reverse = [widget._notch_transition_frame(target, start, step / 100)
               for step in range(101)]
    assert [frame[1] for frame in reverse] == sorted(
        (frame[1] for frame in reverse), reverse=True)
    assert all(96 <= frame[1] <= 900 for frame in reverse)


def test_expanded_notch_honors_measured_heights_down_to_collapsed_height(native_notch):
    app, _monitors, _rect, _calls = native_notch
    assert widget._notch_height(None) == 560
    assert widget._notch_height(148) == 148
    assert widget._notch_height(174) == 174
    assert widget._notch_height(24) == widget.NOTCH_COLLAPSED_HEIGHT
    assert widget._notch_height(-40) == widget.NOTCH_COLLAPSED_HEIGHT
    with pytest.raises(ValueError):
        widget._notch_height("134")
    app.notch_expanded = True
    for measured_height in (148, 174):
        app.notch_height = widget._notch_height(measured_height)
        assert app._notch_target()[1] == measured_height


def test_notch_deadline_pacer_accounts_for_placement_time_and_skips_late_slots():
    interval = widget.NOTCH_FRAME_INTERVAL
    due = 10.0 + interval

    # Placement finishes before the next absolute slot, so only the remaining
    # time is waited instead of adding a fresh interval after placement.
    assert widget._advance_notch_deadline(due, due - 0.002) == due
    # A slow placement crosses two additional slots; schedule the first future
    # slot directly instead of emitting a burst of catch-up frames.
    assert widget._advance_notch_deadline(due, due + 2.5 * interval) == pytest.approx(
        due + 3 * interval)


def test_inspection_hotkey_choice_persists_in_existing_window_state(tmp_path, monkeypatch):
    path = tmp_path / "window-state.json"
    original = {"x": 120, "y": 140, "width": 420, "height": 880,
                "maximized": False, "notch": {"monitor_id": "old-display"}}
    path.write_text(json.dumps(original), encoding="utf-8")
    monkeypatch.setattr(widget, "_window_state_path", lambda: path)
    app = widget.WidgetApp()
    try:
        assert app.inspection_hotkey == "ctrl_shift_t"
        assert app.set_inspection_hotkey("off") == {"ok": True, "choice": "off", "status": "off"}
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved == {**original, "inspection_hotkey": "off"}
        assert widget._load_inspection_hotkey(path) == "off"
    finally:
        app.control.close()


def test_hotkey_inspection_reveals_without_repositioning(native_notch):
    app, _monitors, rect, calls = native_notch
    app._window_loaded = True
    app.visible = False
    app.minimized = False

    app._request_current_chat_inspection()

    assert rect == [620, 0, 680, 148]
    assert calls[-2:] == ["show", "window.tokenTrackerInspect && window.tokenTrackerInspect()"]


def test_inspection_hotkey_change_off_and_quit_unregister_on_owner_thread(tmp_path, monkeypatch):
    class FakeFunction:
        def __init__(self, function):
            self.function = function
            self.argtypes = None
            self.restype = None

        def __call__(self, *args):
            return self.function(*args)

    class FakeUser32:
        def __init__(self):
            self.quit = threading.Event()
            self.register_threads = []
            self.unregister_threads = []
            self.posted = []
            self.PeekMessageW = FakeFunction(self._peek_message)
            self.RegisterHotKey = FakeFunction(self._register_hotkey)
            self.GetMessageW = FakeFunction(self._get_message)
            self.PostThreadMessageW = FakeFunction(self._post_thread_message)
            self.UnregisterHotKey = FakeFunction(self._unregister_hotkey)

        def _peek_message(self, *_args):
            return 0

        def _register_hotkey(self, *_args):
            self.quit.clear()
            self.register_threads.append(threading.get_ident())
            return 1

        def _get_message(self, *_args):
            self.quit.wait(timeout=2)
            return 0

        def _post_thread_message(self, thread_id, message, *_args):
            self.posted.append((thread_id, message))
            self.quit.set()
            return 1

        def _unregister_hotkey(self, *_args):
            self.unregister_threads.append(threading.get_ident())
            return 1

    fake_user32 = FakeUser32()
    fake_kernel32 = SimpleNamespace(GetCurrentThreadId=FakeFunction(threading.get_ident))
    monkeypatch.setattr(widget.ctypes, "WinDLL",
                        lambda name, **_kwargs: fake_user32 if name == "user32" else fake_kernel32,
                        raising=False)
    monkeypatch.setattr(widget, "_window_state_path", lambda: tmp_path / "window-state.json")
    app = widget.WidgetApp()
    app._window_loaded = True
    try:
        assert app._start_inspection_hotkey() == "registered"
        assert app.set_inspection_hotkey("ctrl_alt_t")["status"] == "registered"
        assert app.set_inspection_hotkey("off") == {"ok": True, "choice": "off", "status": "off"}
        assert app.set_inspection_hotkey("ctrl_shift_t")["status"] == "registered"
        app.quit()
    finally:
        if not app.quitting:
            app.quit()

    assert len(fake_user32.register_threads) == 3
    assert fake_user32.unregister_threads == fake_user32.register_threads
    assert len(fake_user32.posted) == 3
    assert all(message == widget.WM_QUIT for _thread_id, message in fake_user32.posted)
    assert [thread_id for thread_id, _message in fake_user32.posted] == fake_user32.register_threads


@pytest.fixture
def native_notch(tmp_path, monkeypatch, request):
    """No real HWND, monitor movement, saved user placement or telemetry reads."""
    monitors = [
        {"id": "display-one", "label": "Monitor 1", "index": 1, "primary": True,
         "scale": 1.0, "area": (0, 0, 1920, 1080)},
        {"id": "display-two", "label": "Monitor 2", "index": 2, "primary": False,
         "scale": 1.25, "area": (-2560, 40, 0, 1440)},
    ]
    monkeypatch.setattr(widget, "_window_state_path", lambda: tmp_path / "window-state.json")
    monkeypatch.setattr(widget, "_monitor_descriptors", lambda: monitors)
    monkeypatch.setattr(widget, "_system_scale", lambda: 1.0)
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: False)
    app = widget.WidgetApp(compact_profile=getattr(request, "param", False))
    rect = [760, 0, 400, 96]
    calls = []
    app.window = SimpleNamespace(restore=lambda: calls.append("restore"), show=lambda: calls.append("show"),
                                 hide=lambda: calls.append("hide"), minimize=lambda: calls.append("minimize"),
                                 evaluate_js=lambda script: calls.append(script))
    monkeypatch.setattr(app, "_native_window_handle", lambda: 1)
    monkeypatch.setattr(app, "_native_rect", lambda: tuple(rect))
    monkeypatch.setattr(app, "_native_state", lambda: None)

    def place(width, height, x, y, **kwargs):
        rect[:] = [x, y, width, height]
        calls.append((width, height, x, y))
        app.on_moved(x, y)
        app.on_bounds_resized(width, height)
        return True

    monkeypatch.setattr(app, "_place", place)
    app.restore_saved_placement()
    yield app, monitors, rect, calls
    app._cancel_notch_transition()
    if app._notch_thread:
        app._notch_thread.join(timeout=2)
    if app._bounds_timer:
        app._bounds_timer.cancel()
    app.control.close()


def test_monitor_selection_validates_id_and_uses_target_display_scale(native_notch):
    app, monitors, rect, calls = native_notch
    assert app.get_monitors()[0]["selected"] is True
    before = list(rect)
    for invalid in (None, True, 2, "disconnected"):
        descriptors = app.select_monitor(invalid)
        assert descriptors[0]["selected"] is True
        assert rect == before
    descriptors = app.select_monitor("display-two")
    assert descriptors[1]["selected"] is True
    assert descriptors[1]["workArea"] == {"x": -2560, "y": 40, "width": 2560, "height": 1400}
    assert rect == [-1705, 40, 850, 185]
    app.set_notch(True, 900)
    assert rect == [-1705, 40, 850, 1125]


def _drag_notch(app, rect, x, y):
    app._on_notch_drag_begin()
    rect[:2] = [x, y]
    app.on_moved(x, y)
    app._on_notch_drag_end()


def _drag_compact_notch(app, rect, x, y, monkeypatch):
    monkeypatch.setattr(app, "_primary_button_down", lambda: True)
    started = app.begin_drag("compact-v1")
    assert started["accepted"] is True
    rect[:2] = [x, y]
    app.on_moved(x, y)
    monkeypatch.setattr(app, "_primary_button_down", lambda: False)
    assert app.end_drag(started["generation"], True) is True


def test_content_height_above_560_only_caps_at_monitor_boundary(native_notch):
    app, monitors, rect, calls = native_notch
    app.set_notch(True, 900)
    assert rect == [620, 0, 680, 900]
    app.set_notch(True, 2000)
    assert rect == [620, 0, 680, 1080]
    assert widget._notch_height(999) == 999


def test_manual_drag_survives_hover_refresh_show_and_restart(native_notch):
    app, monitors, rect, calls = native_notch
    _drag_notch(app, rect, 120, 170)
    assert app._notch_manual_position is True
    for expanded, height in ((True, 780), (False, None), (True, 900), (True, 850)):
        app.set_notch(expanded, height)
        assert rect[:2] == [120, 170]
    # Height respects room beneath the anchor instead of moving it upward.
    app.set_notch(True, 2000)
    assert rect == [120, 170, 680, 910]
    app.show()
    assert rect[:2] == [120, 170]
    app.save_bounds()
    restarted = widget.WidgetApp()
    try:
        assert restarted.initial_bounds() == (420, 110, 250, 170)
        assert restarted._notch_manual_position is True
        assert widget._load_notch_placement(app.window_state_path) == {
            "monitor_id": "display-one", "manual": True, "x": 120, "y": 170,
            "reference_width": 680}
    finally:
        if restarted._bounds_timer:
            restarted._bounds_timer.cancel()
        restarted.control.close()


def test_recenter_clears_manual_anchor_and_persists_selected_display(native_notch):
    app, monitors, rect, calls = native_notch
    app.select_monitor("display-two")
    _drag_notch(app, rect, -2200, 220)
    assert app._notch_manual_position is True
    app.recenter()
    assert rect == [-1705, 40, 850, 185]
    app.save_bounds()
    assert widget._load_notch_placement(app.window_state_path) == {"monitor_id": "display-two"}


def test_locked_expanded_intent_ignores_notch_state_and_keeps_anchor(native_notch):
    app, _monitors, rect, _calls = native_notch
    state = app.set_widget_lock(True, True, 900)
    anchor = tuple(rect[:2])
    assert state == {"locked": True, "expanded": True, "revision": 1,
                     "keepOpen": False, "keepOpenRevision": 0}
    assert app._notch_position == anchor
    assert rect[3] == 900

    # A stale collapsed intent is ignored, while the new measurement applies
    # to height alone and keeps the captured native screen anchor.
    app.set_notch(False, 134)
    assert app.notch_expanded is True
    assert rect == [anchor[0], anchor[1], 680, 148]
    app.set_notch(False, 96)
    assert app.notch_expanded is True
    assert rect == [anchor[0], anchor[1], 680, 148]


def test_locked_collapsed_intent_cannot_expand_from_stale_notch_calls(native_notch):
    app, _monitors, rect, _calls = native_notch
    anchor = tuple(rect[:2])
    app.set_widget_lock(True, False)

    app.set_notch(True, 900)

    assert app.notch_expanded is False
    assert rect == [anchor[0], anchor[1], 680, 148]


def test_locked_widget_rejects_native_drag_and_monitor_changes(native_notch):
    app, _monitors, rect, _calls = native_notch
    app.set_widget_lock(True, True, 800)
    anchor = tuple(rect[:2])
    selected = app.selected_monitor_id

    assert app.begin_drag() is False
    assert app._on_notch_drag_begin() is False
    assert app._notch_user_dragging is False
    rect[:2] = [-2200, 220]
    app.on_moved(*rect[:2])
    assert tuple(rect[:2]) == anchor
    assert app._notch_position == anchor

    monitors = app.select_monitor("display-two")
    assert next(monitor for monitor in monitors if monitor["selected"])["id"] == selected
    assert app.selected_monitor_id == selected


def test_recenter_invalidates_a_queued_lock_request(native_notch):
    app, _monitors, _rect, _calls = native_notch
    old_revision = app.get_widget_lock()["revision"]

    app.recenter()
    result = app.set_widget_lock(True, True, 780, expected_revision=old_revision)

    assert result == {"locked": False, "expanded": False, "revision": old_revision + 1,
                      "keepOpen": False, "keepOpenRevision": 0}
    assert app.widget_locked is False
    assert app._notch_manual_position is False


def test_widget_lock_persists_without_overwriting_state_and_recenter_unlocks(native_notch, monkeypatch):
    app, _monitors, rect, calls = native_notch
    app.window_state_path.write_text(json.dumps({"inspection_hotkey": "off", "custom": "keep"}),
                                     encoding="utf-8")
    app.set_widget_lock(True, True, 780)
    captured = json.loads(app.window_state_path.read_text(encoding="utf-8"))
    assert captured["notch"] == {"monitor_id": "display-one", "manual": True, "x": 620, "y": 0,
                                  "reference_width": 680}
    if app._bounds_timer:
        app._bounds_timer.cancel()
        app._bounds_timer = None
    app.save_bounds()
    saved = json.loads(app.window_state_path.read_text(encoding="utf-8"))
    assert saved["inspection_hotkey"] == "off"
    assert saved["custom"] == "keep"
    assert saved["widget_locked"] is True
    assert saved["widget_lock_expanded"] is True
    assert saved["notch"] == captured["notch"]

    restarted = widget.WidgetApp()
    try:
        assert restarted.get_widget_lock() == {"locked": True, "expanded": True, "revision": 0,
                                               "keepOpen": False, "keepOpenRevision": 0}
        assert restarted.initial_bounds()[1] == 110
        startup_rect = [760, 0, 400, 560]
        restarted.window = SimpleNamespace(evaluate_js=lambda _script: None)
        monkeypatch.setattr(restarted, "_native_window_handle", lambda: 1)
        monkeypatch.setattr(restarted, "_native_rect", lambda: tuple(startup_rect))

        def place_startup(width, height, x, y, **_kwargs):
            startup_rect[:] = [x, y, width, height]
            return True

        monkeypatch.setattr(restarted, "_place", place_startup)
        restarted.restore_saved_placement()
        assert startup_rect == [750, 0, 420, 110]
        assert restarted.notch_expanded is True
    finally:
        if restarted._bounds_timer:
            restarted._bounds_timer.cancel()
        restarted.control.close()

    app.recenter()
    assert app.widget_locked is False
    assert app.notch_expanded is True
    assert app.get_widget_lock()["revision"] == 2
    assert app._notch_manual_position is False
    assert rect[:2] == [620, 0]
    app.save_bounds()
    saved = json.loads(app.window_state_path.read_text(encoding="utf-8"))
    assert saved["inspection_hotkey"] == "off"
    assert saved["custom"] == "keep"
    assert saved["widget_locked"] is False
    assert saved["widget_lock_expanded"] is True
    assert saved["notch"]["manual"] is False
    assert any("tokenTrackerSetWidgetLock" in call and '"locked":false' in call
               for call in calls if isinstance(call, str))


def test_widget_lock_api_strictly_validates_booleans_and_preserves_callback_order():
    state = {"locked": False, "expanded": True, "revision": 4}
    calls = []

    def set_lock(*args):
        calls.append(args)
        return {"locked": args[0], "expanded": args[1], "revision": 5}

    api = widget.WindowApi(None, None, None, None, None, None, None, None, None, None,
                           lambda: "drag began", lambda moved: moved,
                           on_lock=set_lock, on_get_lock=lambda: state)
    assert api.get_widget_lock() == state
    assert api.set_widget_lock(1, False) == state
    assert api.set_widget_lock(True, 1) == state
    assert api.set_widget_lock(True, False, "134") == state
    assert api.set_widget_lock(True, False, True) == state
    assert api.set_widget_lock(True, False, 134, expected_revision=True) == state
    assert api.set_widget_lock(True, False, 134, expected_revision=4.0) == state
    assert api.set_widget_lock(True, False, 134, expected_revision=-1) == state
    assert calls == []
    assert api.begin_drag() == "drag began"
    assert api.end_drag(False) is False

    assert api.set_widget_lock(True, False, 134) == {
        "locked": True, "expanded": False, "revision": 5}
    assert calls == [(True, False, 148)]
    assert api.set_widget_lock(True, False, 134, expected_revision=4) == {
        "locked": True, "expanded": False, "revision": 5}
    assert calls[-1] == (True, False, 148, 4)


def test_disconnected_monitor_and_offscreen_anchor_are_rescued(native_notch):
    app, monitors, rect, calls = native_notch
    app.select_monitor("display-two")
    _drag_notch(app, rect, -2200, 220)
    monitors.pop()
    app.set_notch(True, 800)
    assert rect == [620, 0, 680, 800]
    assert app.selected_monitor_id == "display-one"
    assert app._notch_manual_position is False
    app._notch_manual_position, app._notch_position = True, (5000, 100)
    app.set_notch(False)
    assert rect == [620, 0, 680, 148]
    assert app._notch_manual_position is False


def test_programmatic_animation_frames_never_replace_manual_anchor(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    _drag_notch(app, rect, 180, 140)
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    frame_waiting = threading.Event()
    release_worker = threading.Event()
    original_wait = app._notch_wake.wait
    first_wait = True

    def wait_for_retarget(timeout=None):
        nonlocal first_wait
        if first_wait:
            first_wait = False
            frame_waiting.set()
            release_worker.wait(timeout=2)
        return original_wait(timeout)

    monkeypatch.setattr(app._notch_wake, "wait", wait_for_retarget)
    worker = None
    try:
        app.set_notch(True, 900)
        # Retarget only after the worker has reached its between-frames wait.
        worker = app._notch_thread
        assert frame_waiting.wait(timeout=2), "animation worker did not reach its frame wait"
        app.set_notch(False)
        app.set_notch(True, 720)
        assert app._notch_thread is worker
    finally:
        release_worker.set()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert rect == [180, 140, 680, 720]
    assert app._notch_position == (180, 140)
    app.save_bounds()
    assert widget._load_notch_placement(app.window_state_path)["x"] == 180


def test_hidden_or_minimized_hover_cannot_surface_widget(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    for action in (app.hide, app.minimize):
        app.visible, app.minimized = True, False
        app.set_notch(True, 850)
        worker = app._notch_thread
        action()
        if worker:
            worker.join(timeout=2)
        calls.clear()
        app.set_notch(False)
        app.set_notch(True, 900)
        app.recenter()
        assert all(isinstance(call, str) and "tokenTrackerSetWidgetLock" in call
                   for call in calls)
        assert app.visible is False


def test_monitor_api_exposes_descriptors_and_explicit_recenter(native_notch):
    app, monitors, rect, calls = native_notch
    api = widget.WindowApi(None, None, None, on_monitors=app.get_monitors,
                           on_monitor=app.select_monitor, on_recenter=app.recenter)
    assert api.get_monitors()[0]["selected"]
    assert api.select_monitor("display-two")[1]["selected"]
    assert api.recenter()[1]["selected"]


def test_notch_api_preserves_legacy_callback_arity_and_forwards_reduced_motion():
    calls = []
    api = widget.WindowApi(None, None, None, on_notch=lambda *args: calls.append(args))

    api.set_notch(True)
    api.set_notch(False, 300, False)
    api.set_notch(True, 420, True)
    api.set_notch(False, None, True)

    assert calls == [(True,), (False, 300), (True, 420, True), (False, None, True)]


def test_reduced_motion_applies_notch_bounds_immediately(native_notch, monkeypatch):
    app, _monitors, rect, _calls = native_notch
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    api = widget.WindowApi(None, None, None, on_notch=app.set_notch)

    api.set_notch(True, 900, True)

    assert rect == [620, 0, 680, 900]
    assert app._notch_transition is None
    assert app._notch_thread is None


def test_drag_to_old_animation_coordinate_is_saved_after_guard_expires(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    initial_position = tuple(rect[:2])
    _drag_notch(app, rect, 180, 140)
    after_guard = app._notch_command_guard_until + 1
    monkeypatch.setattr(widget.time, "monotonic", lambda: after_guard)
    rect[:2] = initial_position
    app.on_moved(*initial_position)
    assert app._notch_position == initial_position
    app.save_bounds()
    assert widget._load_notch_placement(app.window_state_path)["manual"] is True


def test_native_drag_takes_over_animation_even_at_a_recent_frame(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    monkeypatch.setattr(widget, "_notch_animation_enabled", lambda: True)
    app.set_notch(True, 900)
    worker = app._notch_thread
    app._on_notch_drag_begin()
    if worker:
        worker.join(timeout=2)
    rect[:2] = [760, 0]
    app.on_moved(760, 0)
    calls.clear()
    app.set_notch(True, 700)
    assert calls == []
    assert app._notch_position == (760, 0)
    app._on_notch_drag_end()
    app.set_notch(True, 700)
    assert rect[:2] == [760, 0]


def test_adjusted_recenter_frame_and_delayed_callback_do_not_become_manual(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    app._notch_drag_events_registered = True
    app.select_monitor("display-two")
    _drag_notch(app, rect, -1505, 80)
    app.recenter()
    # Actual mixed-DPI QA: native frame differed from the requested frame.
    # Hover following the header click may also initiate an expanded resize.
    app.set_notch(True, 700)
    rect[:2] = [-1587, 2]
    app.on_moved(-1587, 2)
    assert app._notch_manual_position is False
    app.save_bounds()
    assert widget._load_notch_placement(app.window_state_path) == {"monitor_id": "display-two"}
    after_guard = app._notch_command_guard_until + 10
    monkeypatch.setattr(widget.time, "monotonic", lambda: after_guard)
    app.on_moved(-1587, 2)
    assert app._notch_manual_position is False
    # A real native gesture remains authoritative, even at an old frame.
    _drag_notch(app, rect, -1587, 2)
    assert app._notch_manual_position is True
    assert app._notch_position == (-1587, 2)


def test_drag_end_saves_final_physical_position_before_delayed_moved_callback(native_notch):
    app, monitors, rect, calls = native_notch
    app._notch_drag_events_registered = True
    app._on_notch_drag_begin()
    rect[:2] = [120, 170]
    # No moved callback has been dispatched yet when the modal drag ends.
    app._on_notch_drag_end()
    assert app._notch_position == (120, 170)
    app.recenter()
    rect[:2] = [762, 2]
    app.on_moved(120, 170)  # delayed callback carries old logical event values
    assert app._notch_manual_position is False


def test_fallback_guard_handles_adjusted_frames_and_still_expires(native_notch, monkeypatch):
    app, monitors, rect, calls = native_notch
    app._notch_drag_events_registered = False
    app.recenter()
    rect[:2] = [778, 2]
    app.on_moved(778, 2)
    assert app._notch_manual_position is False
    after_guard = app._notch_command_guard_until + 1
    monkeypatch.setattr(widget.time, "monotonic", lambda: after_guard)
    app.on_moved(778, 2)
    assert app._notch_position == (778, 2)


def test_javascript_drag_bridge_saves_without_native_modal_move(native_notch):
    app, monitors, rect, calls = native_notch
    app._notch_drag_events_registered = True
    api = widget.WindowApi(None, None, None, on_drag_begin=app.begin_drag, on_drag_end=app.end_drag)
    assert api.begin_drag() is True
    # Installed pywebviewMoveWindow directly calls window.move/SetWindowPos.
    # It does not deliver a WinForms ResizeBegin/End gesture.
    rect[:2] = [160, 120]
    app.on_moved(160, 120)
    rect[:2] = [220, 180]  # the last moved event is still waiting for dispatch
    assert api.end_drag(True) is True
    assert app._notch_position == (220, 180)
    assert app._notch_user_dragging is False
    app.on_moved(160, 120)  # old logical callback must not save a new position
    assert app._notch_position == (220, 180)
    app.set_notch(True, 700)
    assert rect[:2] == [220, 180]
    assert api.end_drag(True) is False


def test_drag_region_click_without_movement_preserves_automatic_center(native_notch):
    app, monitors, rect, calls = native_notch
    app._notch_drag_events_registered = True
    assert app.begin_drag() is True
    rect[:2] = [778, 2]  # an old adjusted resize callback finishes during press
    app.on_moved(778, 2)
    assert app.end_drag(False) is True
    assert app._notch_manual_position is False
    assert rect == [620, 0, 680, 148]
    assert app.end_drag("false") is False


def test_hidden_window_rejects_javascript_drag_start(native_notch):
    app, monitors, rect, calls = native_notch
    for minimized in (False, True):
        app.visible, app.minimized = False, minimized
        calls.clear()
        assert app.begin_drag() is False
        assert app.end_drag(True) is False
        assert calls == []


def test_native_widget_opens_runway_page():
    source = Path(widget.__file__).read_text(encoding="utf-8")
    assert 'f"http://{HTTP_HOST}:{HTTP_PORT}/runway.html"' in source


def test_stats_range_maps_runway_selections_to_supported_day_windows(monkeypatch):
    received = []
    snapshot = {"providers": [{"name": "Codex", "chats": []}]}
    monkeypatch.setattr(server.CACHE, "get", lambda force=False: snapshot)

    def record_trim(value, limit, pins, **kwargs):
        received.append(kwargs["days"])
        return value

    monkeypatch.setattr(server, "trim_chats", record_trim)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address, timeout=5)
    try:
        for requested in ("1d", "7d", "30d"):
            connection.request("GET", "/api/stats?limit=50&range=" + requested)
            response = connection.getresponse()
            assert response.status == 200
            json.loads(response.read())
        assert received == [1, 7, 30]
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_snapshot_warmup_logs_failures_without_raising(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(server.CACHE, "get", lambda: (_ for _ in ()).throw(RuntimeError("private detail")))
    with caplog.at_level(logging.ERROR, logger=widget.__name__):
        widget._warm_snapshot()
    assert "Branchglance snapshot warmup failed" in caplog.text


def test_live_endpoint_returns_bounded_failure_and_rate_limits_logs(monkeypatch, caplog):
    import logging

    monkeypatch.setattr(server, "live_state",
                        lambda: (_ for _ in ()).throw(NameError("private transcript path")))
    server._LIVE_FAILURE_LOGGED_AT.clear()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address, timeout=5)
    try:
        with caplog.at_level(logging.WARNING, logger=server.__name__):
            for _ in range(2):
                connection.request("GET", "/api/live")
                response = connection.getresponse()
                body = response.read()
                assert response.status == 503
                assert json.loads(body) == {"ok": False, "error": "Live telemetry unavailable"}
                assert b"private transcript path" not in body
            assert sum("Live telemetry failed (NameError)" in record.message for record in caplog.records) == 1
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_forced_snapshot_requests_waiting_on_cold_build_share_result(monkeypatch):
    cache = server.SnapshotCache()
    waiter_count = 4
    build_started = threading.Event()
    release_build = threading.Event()
    build_guard = threading.Lock()
    builds = []
    results = []
    errors = []
    wait_guard = threading.Lock()
    waiting_count = 0
    all_waiting = threading.Event()

    def build_snapshot():
        with build_guard:
            snapshot = {"generation": len(builds) + 1}
            builds.append(snapshot)
        build_started.set()
        if not release_build.wait(2):
            raise TimeoutError("test did not release the snapshot build")
        return snapshot

    monkeypatch.setattr(server, "build_snapshot", build_snapshot)

    original_wait = cache.condition.wait

    def observed_wait(timeout=None):
        nonlocal waiting_count
        with wait_guard:
            waiting_count += 1
            if waiting_count == waiter_count:
                all_waiting.set()
        return original_wait(timeout)

    cache.condition.wait = observed_wait

    def get_snapshot():
        try:
            results.append(cache.get(force=True))
        except Exception as exc:  # keep worker-thread failures visible to pytest
            errors.append(exc)

    first = threading.Thread(target=get_snapshot)
    waiters = [threading.Thread(target=get_snapshot) for _ in range(waiter_count)]
    first.start()
    try:
        assert build_started.wait(2)
        for waiter in waiters:
            waiter.start()
        assert all_waiting.wait(2)
    finally:
        release_build.set()
        first.join(timeout=2)
        for waiter in waiters:
            if waiter.ident is not None:
                waiter.join(timeout=2)

    assert not first.is_alive()
    assert all(not waiter.is_alive() for waiter in waiters)
    assert errors == []
    assert len(builds) == 1
    assert len(results) == waiter_count + 1
    assert len({id(result) for result in results}) == waiter_count + 1
    assert all(result["generation"] == builds[0]["generation"] for result in results)
    assert all(result["snapshotCache"]["generation"] == 1 for result in results)


def test_background_snapshot_returns_cached_value_while_rebuild_is_blocked(monkeypatch):
    cache = server.SnapshotCache()
    cached = {"generation": 1}
    cache.value, cache.at = cached, time.monotonic()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_build():
        started.set()
        assert release.wait(2)
        finished.set()
        return {"generation": 2}

    monkeypatch.setattr(server, "build_snapshot", slow_build)
    started_at = time.monotonic()
    try:
        first = cache.get(force=True, background=True)
        assert first is not cached and first["generation"] == cached["generation"]
        assert first["snapshotCache"]["building"] is True
        assert time.monotonic() - started_at < 0.5
        assert started.wait(2)
    finally:
        release.set()
    assert finished.wait(2)
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)
    assert cache.value == {"generation": 2}


def test_repeated_forced_background_snapshots_share_one_builder(monkeypatch):
    cache = server.SnapshotCache()
    cached = {"generation": 1}
    cache.value, cache.at = cached, time.monotonic()
    started, release = threading.Event(), threading.Event()
    builds = []

    def slow_build():
        builds.append(True)
        started.set()
        assert release.wait(2)
        return {"generation": 2}

    monkeypatch.setattr(server, "build_snapshot", slow_build)
    try:
        first = cache.get(force=True, background=True)
        assert first is not cached and first["generation"] == cached["generation"]
        assert first["snapshotCache"]["building"] is True
        assert started.wait(2)
        for _ in range(8):
            result = cache.get(force=True, background=True)
            assert result is not cached and result["generation"] == cached["generation"]
            assert result["snapshotCache"]["building"] is True
        assert len(builds) == 1
    finally:
        release.set()
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)
    assert cache.value == {"generation": 2}


def test_background_snapshot_failure_keeps_cache_and_allows_retry(monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(server.time, "monotonic", lambda: clock[0])
    cache = server.SnapshotCache()
    cached = {"generation": 1}
    cache.value, cache.at = cached, clock[0]
    attempts = []

    def fail_once():
        attempts.append(True)
        if len(attempts) == 1:
            raise RuntimeError("private provider detail")
        return {"generation": 2}

    monkeypatch.setattr(server, "build_snapshot", fail_once)
    first = cache.get(force=True, background=True)
    assert first is not cached and first["generation"] == cached["generation"]
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)
    assert cache.value == cached
    second = cache.get(force=True, background=True)
    assert second is not cached and second["generation"] == cached["generation"]
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)
    assert cache.value == cached
    assert attempts == [True]
    clock[0] += cache.RETRY_COOLDOWN_SECONDS + 0.01
    third = cache.get(force=True, background=True)
    assert third["generation"] == cached["generation"]
    with cache.condition:
        assert cache.condition.wait_for(lambda: not cache.building, timeout=2)
    assert cache.value == {"generation": 2}
    assert len(attempts) == 2


def test_background_request_without_snapshot_returns_promptly_and_starts_one_real_build(monkeypatch):
    # A cold first build can outlast the widget's 25 s fetch timeout, so a background
    # request with no snapshot answers at once (503 upstream) and the widget retries.
    cache = server.SnapshotCache()
    started, release = threading.Event(), threading.Event()
    built = {"generation": 1}
    calls = []

    def slow_build():
        calls.append(1)
        started.set()
        assert release.wait(2)
        return built

    monkeypatch.setattr(server, "build_snapshot", slow_build)
    try:
        assert cache.get(background=True) is None
        assert started.wait(2)
        assert cache.get(background=True) is None, "a second cold request starts no second build"
    finally:
        release.set()
    deadline = time.monotonic() + 2
    result = None
    while result is None and time.monotonic() < deadline:
        result = cache.get(background=True)
        if result is None:
            time.sleep(0.01)
    assert len(calls) == 1
    assert result is not None and result is not built
    assert result["generation"] == built["generation"]
    assert result["snapshotCache"]["generation"] == 1


def test_stats_handler_enables_background_only_for_opt_in_query(monkeypatch):
    calls = []
    snapshot = {"providers": [{"name": "Codex", "chats": []}]}

    def get_snapshot(force=False, background=False):
        calls.append((force, background))
        return snapshot

    monkeypatch.setattr(server.CACHE, "get", get_snapshot)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    connection = HTTPConnection(*httpd.server_address, timeout=5)
    try:
        for path in ("/api/stats?background=1", "/api/stats?background=1&refresh=1", "/api/stats"):
            connection.request("GET", path)
            response = connection.getresponse()
            assert response.status == 200
            json.loads(response.read())
        assert calls == [(False, True), (True, True), (False, False)]
    finally:
        connection.close()
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_forced_snapshot_after_completed_build_still_rebuilds(monkeypatch):
    cache = server.SnapshotCache()
    snapshots = [{"generation": 1}, {"generation": 2}]
    monkeypatch.setattr(server, "build_snapshot", lambda: snapshots.pop(0))

    first = cache.get(force=True)
    second = cache.get(force=True)

    assert first["generation"] == 1 and first["snapshotCache"]["generation"] == 1
    assert second["generation"] == 2 and second["snapshotCache"]["generation"] == 2
    assert second is not first


def test_failed_snapshot_build_preserves_cache_and_allows_recovery(monkeypatch):
    cache = server.SnapshotCache()
    original = {"generation": 1}
    monkeypatch.setattr(server, "build_snapshot", lambda: original)
    first = cache.get(force=True)
    assert first is not original and first["generation"] == original["generation"]
    original_at = cache.at

    def fail_build():
        raise RuntimeError("snapshot unavailable")

    monkeypatch.setattr(server, "build_snapshot", fail_build)
    with pytest.raises(RuntimeError, match="snapshot unavailable"):
        cache.get(force=True)
    assert cache.value == original
    assert cache.at == original_at

    recovered = {"generation": 2}
    monkeypatch.setattr(server, "build_snapshot", lambda: recovered)
    result = cache.get(force=True)
    assert result is not recovered and result["generation"] == recovered["generation"]


def test_missing_stable_monitor_preserves_anchor_and_centers_fallback(native_notch):
    app, monitors, _rect, _calls = native_notch
    target = "\\\\?\\DISPLAY#PANEL"
    monitors[1].update(id="\\\\?\\DISPLAY#OTHER", stableId="\\\\?\\DISPLAY#OTHER")
    app.selected_monitor_id = target
    app._notch_manual_position = True
    app._notch_position = (-2200, 220)

    assert app._notch_target() == (680, 148, 620, 0)
    assert app.selected_monitor_id == target
    assert app._notch_manual_position is True
    assert app._notch_position == (-2200, 220)


def test_returned_stable_monitor_restores_saved_anchor(native_notch):
    app, monitors, _rect, _calls = native_notch
    target = "\\\\?\\DISPLAY#PANEL"
    monitors[1].update(id="\\\\?\\DISPLAY#OTHER", stableId="\\\\?\\DISPLAY#OTHER")
    app.selected_monitor_id = target
    app._notch_manual_position = True
    app._notch_position = (-2200, 220)
    assert app._notch_target() == (680, 148, 620, 0)

    monitors[1].update(id=target, stableId=target)

    assert app._notch_target() == (850, 185, -2200, 220)
    assert app.selected_monitor_id == target
    assert app._notch_manual_position is True
    assert app._notch_position == (-2200, 220)


def test_recenter_while_stable_monitor_is_missing_adopts_fallback(native_notch):
    app, monitors, _rect, _calls = native_notch
    target = "\\\\?\\DISPLAY#PANEL"
    monitors[1].update(id=target, stableId=target)
    app.selected_monitor_id = target
    app._notch_manual_position = True
    app._notch_position = (-2200, 220)
    monitors.pop()

    app.recenter()

    assert app.selected_monitor_id == "display-one"
    assert app._notch_manual_position is False
    assert app._notch_position is None
