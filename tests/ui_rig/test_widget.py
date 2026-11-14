import os
from urllib.parse import parse_qs, urlsplit

import pytest
if os.environ.get("TOKEN_TRACKER_UI_RIG") == "1":
    from playwright.sync_api import expect

import ui_selectors as ui

pytestmark = pytest.mark.ui_rig


def open_insights(page):
    page.locator(ui.OPEN).click()
    expect(page.locator(ui.DASHBOARD)).to_be_visible()
    expect(page.locator(ui.HERO)).to_be_visible()


def open_settings(page):
    open_insights(page)
    page.locator(ui.SETTINGS_TAB).click()
    expect(page.locator(ui.SETTINGS)).to_be_visible()
    expect(page.locator(ui.SETTINGS).get_by_role("switch").first).to_be_visible()


def test_tree_smoke(page):
    expect(page.locator(ui.OPEN)).to_be_visible()
    open_insights(page)


def test_ranges_metrics_and_providers(page):
    open_insights(page)
    for value in ("24h", "7d", "30d"):
        button = page.locator(f'{ui.RANGE}[data-dashboard-range="{value}"]')
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/insights" and parse_qs(urlsplit(response.url).query).get("range") == [value]):
            button.click()
        expect(button).to_have_attribute("aria-pressed", "true")
        expect(page.locator(ui.HERO)).to_contain_text({"24h": "24", "7d": "7", "30d": "30"}[value])
    for value in ("tokens", "cost"):
        page.locator(f'{ui.METRIC}[data-dashboard-metric="{value}"]').click()
        expect(page.locator(f'{ui.METRIC}[data-dashboard-metric="{value}"]')).to_have_attribute("aria-pressed", "true")
        if value == "tokens":
            expect(page.locator(ui.HERO)).to_contain_text("Tokens over")
        else:
            expect(page.locator(f"{ui.HERO} .dash-hero-currency")).to_have_text("$")
            expect(page.locator(ui.HERO)).to_contain_text("estimated")
    chips = page.locator(ui.PROVIDERS)
    expect(chips).to_have_count(2)
    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    chips.last.click()
    expect(chips.last).to_have_attribute("aria-pressed", "true")
    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "true")


def test_insights_buttons_click(page):
    open_insights(page)
    # Iterate current control identities, because clicking redraws the panel.
    buttons = page.locator(f"{ui.INSIGHTS} button:visible")
    identities = buttons.evaluate_all("els => els.map(e => [e.getAttribute('data-dashboard-provider'), e.getAttribute('data-dashboard-chart-style'), e.getAttribute('data-dashboard-models')])")
    assert identities
    for provider, style, models in identities:
        attr, value = next((name, val) for name, val in (("provider", provider), ("chart-style", style), ("models", models)) if val is not None)
        page.locator(f'[data-dashboard-{attr}="{value}"]').click()


def test_settings_switches_and_clock(page):
    open_settings(page)
    for label, action in (("Lock widget position", "lock"), ("Keep widget open", "keep-open")):
        switch = page.get_by_role("switch", name=label)
        before = switch.get_attribute("aria-checked")
        switch.click()
        expect(switch).to_have_attribute("aria-checked", "false" if before == "true" else "true")
        assert page.evaluate("action => window.__rigCalls.some(call => call[0] === action)", action)
    clocks = page.locator(ui.CLOCK)
    expect(clocks).to_have_count(2)
    expect(page.locator(f'{ui.CLOCK}[aria-pressed="true"]')).to_have_count(1)
    idle = page.locator(f'{ui.CLOCK}[aria-pressed="false"]')
    value = idle.get_attribute("data-dashboard-clock")
    idle.click()
    expect(page.locator(f'{ui.CLOCK}[data-dashboard-clock="{value}"]')).to_have_attribute("aria-pressed", "true")
    expect(page.locator(f'{ui.CLOCK}[aria-pressed="true"]')).to_have_count(1)


def test_settings_rerender_keeps_reset_form_loaded(page):
    open_settings(page)
    frame = page.locator(ui.RESET_FRAME)
    expect(page.frame_locator(ui.RESET_FRAME).locator(ui.INTERACTIVE).first).to_be_attached()
    frame.evaluate("el => { el.contentWindow.__rigMarker = 1; }")
    page.get_by_role("switch", name="Lock widget position").click()
    page.locator(f'{ui.CLOCK}[aria-pressed="false"]').click()
    expect(page.locator(f'{ui.CLOCK}[aria-pressed="true"]')).to_have_count(1)
    assert frame.evaluate("el => el.contentWindow.__rigMarker") == 1, "the reset form reloaded on a Settings re-render"


def test_settings_reachable(page):
    open_settings(page)
    content = page.locator(ui.SETTINGS_SCROLLER)
    size = content.evaluate("el => ({scroll: el.scrollHeight, client: el.clientHeight})")
    assert size["scroll"] > size["client"]
    assert content.evaluate("el => ['auto', 'scroll'].includes(getComputedStyle(el).overflowY)")
    content.evaluate("el => el.scrollTop = el.scrollHeight")
    last = page.locator(ui.ACTION.format("debug-preview"))
    assert last.evaluate("el => { const r=el.getBoundingClientRect(), p=document.querySelector('#dashboard-content').getBoundingClientRect(); return r.bottom <= p.bottom && r.top >= p.top; }")


def test_horizontal_layout(page):
    open_settings(page)
    bad = page.locator(ui.SETTINGS).locator(ui.INTERACTIVE).evaluate_all("els => els.filter(e => {const r=e.getBoundingClientRect(), p=document.querySelector('#dashboard-view').getBoundingClientRect(); return r.width && (r.left < p.left - 1 || r.right > p.right + 1)}).map(e => e.outerHTML)")
    assert not bad, bad


def test_settings_keyboard_reaches_controls(page):
    open_settings(page)
    controls = page.locator(ui.SETTINGS).locator(ui.INTERACTIVE).filter(visible=True)
    frame = page.frame_locator(ui.RESET_FRAME)
    # Count the embedded form only after it has rendered, or the expected total races its load.
    expect(frame.locator(ui.INTERACTIVE).filter(visible=True).first).to_be_visible()
    inner = frame.locator(ui.INTERACTIVE).filter(visible=True)
    expected_outer, expected_inner = controls.count(), inner.count()
    expected = expected_outer + expected_inner
    assert expected_outer > 0
    page.locator(ui.SETTINGS_TAB).focus()
    seen = set()
    for _ in range(100):
        page.keyboard.press("Tab")
        index = controls.evaluate_all("els => els.indexOf(document.activeElement)")
        if index >= 0:
            visible = page.evaluate("""() => {const r=document.activeElement.getBoundingClientRect(), p=document.querySelector('#dashboard-content').getBoundingClientRect(); return r.top >= p.top && r.bottom <= p.bottom;}""")
            if visible:
                seen.add(("outer", index))
        inner_index = inner.evaluate_all("els => els.indexOf(document.activeElement)")
        if inner_index >= 0 and page.locator(ui.RESET_FRAME).evaluate("el => document.activeElement === el"):
            seen.add(("frame", inner_index))
    assert len(seen) == expected, (len(seen), expected)


def test_debug_controls(page):
    open_settings(page)
    # Settings scrolls (test_settings_reachable); bring the About controls into view.
    page.locator(ui.SETTINGS_SCROLLER).evaluate("el => el.scrollTop = el.scrollHeight")
    with page.expect_response(lambda response: urlsplit(response.url).path == "/api/debug-report"):
        page.locator(ui.ACTION.format("debug-preview")).click()
    expect(page.get_by_role("textbox", name="Debug report text")).to_have_value("# Branchglance debug report\nFictional report")
    page.locator(ui.ACTION.format("debug-copy")).click()
    page.locator(ui.ACTION.format("debug-issue")).click()
    assert page.evaluate("window.__rigCalls.some(call => call[0] === 'open')")
