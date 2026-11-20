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


def exercise_provider_chips(page):
    chips = page.locator(ui.PROVIDERS)
    expect(chips).to_have_count(2)
    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "false")
    expect(chips.last).to_have_attribute("aria-pressed", "true")
    expect(chips.last).to_have_attribute("aria-disabled", "true")
    chips.first.click()
    expect(chips.first).to_have_attribute("aria-pressed", "true")
    chips.last.click()
    expect(chips.last).to_have_attribute("aria-pressed", "false")
    expect(chips.first).to_have_attribute("aria-pressed", "true")
    expect(chips.first).to_have_attribute("aria-disabled", "true")


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
    exercise_provider_chips(page)


def test_provider_legend_values_share_one_line_box_at_compact_and_default_sizes(page):
    open_insights(page)

    def assert_provider_rows_share_line():
        rows = page.locator(f"{ui.HERO} .dash-split-legend")
        expect(rows).to_be_visible()
        boxes = rows.evaluate("el => { const grid=getComputedStyle(el), names=[...el.querySelectorAll('.dash-legend-name')], values=[...el.querySelectorAll('.dash-legend-item b')]; return names.map((name,i) => { const item=name.closest('.dash-legend-item'), value=values[i], n=name.getBoundingClientRect(), v=value?.getBoundingClientRect(), itemStyle=getComputedStyle(item); return {name:name.textContent.trim(), nameY:n.y, valueY:v?.y, nameH:n.height, valueH:v?.height, legendWidth:el.getBoundingClientRect().width, gridColumns:grid.gridTemplateColumns, itemDisplay:itemStyle.display, itemColumns:itemStyle.gridTemplateColumns, nameRow:getComputedStyle(name).gridRow, valueRow:value?getComputedStyle(value).gridRow:null}; }); }")
        assert len(boxes) >= 2, f"expected provider name/value rows, got {boxes!r}"
        for row in boxes:
            assert row["valueY"] is not None and abs(row["nameY"] - row["valueY"]) < 1, f"provider name and value must share one line box: {row!r}"
            assert row["nameH"] <= row["valueH"] + 1, f"provider name wrapped to multiple line boxes: {row!r}"

    for height in (608, 478):
        page.set_viewport_size({"width": 720, "height": height})
        for value in ("24h", "7d", "30d"):
            button = page.locator(f'{ui.RANGE}[data-dashboard-range="{value}"]')
            with page.expect_response(lambda response: urlsplit(response.url).path == "/api/insights" and parse_qs(urlsplit(response.url).query).get("range") == [value]):
                button.click()
            expect(button).to_have_attribute("aria-pressed", "true")
            page.locator(f'{ui.METRIC}[data-dashboard-metric="cost"]').click()
            expect(page.locator(f'{ui.METRIC}[data-dashboard-metric="cost"]')).to_have_attribute("aria-pressed", "true")
            assert_provider_rows_share_line()
        page.locator(f'{ui.METRIC}[data-dashboard-metric="tokens"]').click()
        with page.expect_response(lambda response: urlsplit(response.url).path == "/api/insights" and parse_qs(urlsplit(response.url).query).get("range") == ["24h"]):
            page.locator(f'{ui.RANGE}[data-dashboard-range="24h"]').click()
        assert_provider_rows_share_line()


def test_insights_buttons_click(page):
    open_insights(page)
    # Iterate current control identities, because clicking redraws the panel.
    buttons = page.locator(f"{ui.INSIGHTS} button:visible")
    identities = buttons.evaluate_all("els => els.map(e => [e.getAttribute('data-dashboard-provider'), e.getAttribute('data-dashboard-chart-style'), e.getAttribute('data-dashboard-models'), e.getAttribute('data-dashboard-range'), e.getAttribute('data-dashboard-metric'), e.getAttribute('data-dashboard-action') || e.getAttribute('aria-label') || e.textContent.trim()])")
    assert identities
    provider_chips = []
    for provider, style, models, range_value, metric, label in identities:
        if provider is not None:
            provider_chips.append(provider)
            continue
        if label == "set-plan":
            # Navigates to Settings; its routing is covered by the Node dashboard tests.
            continue
        attr, value = next(((name, val) for name, val in (("chart-style", style), ("models", models), ("range", range_value), ("metric", metric)) if val is not None), (None, None))
        assert attr is not None, f"Unexpected visible Insights button without a known control identity: {label!r}"
        button = page.locator(f'[data-dashboard-{attr}="{value}"]')
        if button.get_attribute("aria-disabled") == "true":
            expect(button).to_have_attribute("aria-disabled", "true")
            assert button.get_attribute("title"), f"disabled {label!r} control explains why it is unavailable"
            assert button.get_attribute("disabled") is None, "aria-disabled preserves keyboard focus so its reason can be discovered"
            expect(page.get_by_role("button", name=label)).to_have_attribute("aria-disabled", "true")
            button.focus()
            assert button.evaluate("el => document.activeElement === el"), "aria-disabled button remains keyboard focusable"
            for key in ("Enter", "Space"):
                button.press(key)
                expect(button).to_have_attribute("aria-pressed", "false")
            continue
        button.click()
    assert len(provider_chips) == 2
    exercise_provider_chips(page)


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


def test_tall_by_work_chart_scrolls_at_full_and_compact_heights(page):
    open_insights(page)
    page.evaluate("""() => {
      const panel = document.querySelector('#dashboard-insights');
      const rows = ['Opus', 'Sonnet', 'Haiku', 'GPT 6 Codex', 'Other (2)'].map(name =>
        `<div role="row" class="dash-work-row" style="height:26px"><span role="rowheader">${name}</span><span>1.2M</span><span>900K</span><span>2.1M</span></div>`).join('');
      panel.innerHTML = `<section class="dash-hero"><div class="dash-hero-meta"><span class="dash-eyebrow">Tokens over 30 days</span></div><div class="dash-hero-number-row"><strong class="dash-hero-number">12.4M</strong></div></section>
        <section class="dash-insights-body"><section class="dash-lower"><div class="dash-lower-main"><div class="dash-work-block"><div class="dash-work-grid" style="--work-cols:2">
          <div role="row" class="dash-work-row is-head"><span>By work</span><span>Build</span><span>Review</span><span>Total</span></div>${rows}
          <div role="row" class="dash-work-row is-foot" style="height:26px"><span>Total</span><span>4.8M</span><span>3.6M</span><span>8.4M</span></div>
        </div></div></div><div class="dash-side"><div class="dash-limits"><div class="dash-section-label">Quota used</div><table class="dash-peaks-table dash-quota-table"><tbody>
          <tr class="dash-quota-row"><th>Codex</th><td>62%<span class="dash-quota-sub">resets in 2h 10m</span></td><td>41%<span class="dash-quota-sub">resets in 4d 12h</span></td></tr>
          <tr class="dash-quota-row"><th>Claude</th><td>55%<span class="dash-quota-sub">resets in 1h 20m</span></td><td>73%<span class="dash-quota-sub">resets in 3d 8h</span></td></tr>
        </tbody></table></div></div><dl class="dash-summary"><div><dt>Sessions</dt><dd>12</dd></div><div><dt>Active hours</dt><dd>34h</dd></div></dl></section>
        <section class="dash-chart-section"><div class="dash-section-head"><span class="dash-section-title">Daily tokens</span></div><div class="dash-plot"><div class="dash-axis"><span class="dash-axis-value">2M</span></div><svg class="dash-chart" viewBox="0 0 668 100" aria-label="Daily token chart"></svg></div><div class="dash-dates"><span class="dash-date is-first">Oct 17</span><span class="dash-date is-last">Nov 15</span></div></section></section>
        <p class="dash-status-line">Updated 1m ago</p>`;
    }""")
    for height in (608, 478):
        layout = page.evaluate("""height => {
          const view = document.querySelector('#dashboard-view');
          const panel = document.querySelector('#dashboard-insights');
          view.style.setProperty('--dashboard-height', `${height}px`);
          view.dataset.openCompact = String(height === 478);
          view.dataset.size = height === 478 ? 'compact' : 'full';
          const chart = panel.querySelector('.dash-chart-section'), dates = panel.querySelector('.dash-dates');
          const c = chart.getBoundingClientRect(), d = dates.getBoundingClientRect();
          const last=panel.lastElementChild, p=panel.getBoundingClientRect(), lastBox=last.getBoundingClientRect();
          return {chartHeight:c.height, datesInside:d.top >= c.top && d.bottom <= c.bottom,
            scrollHeight:panel.scrollHeight, clientHeight:panel.clientHeight,
            lastChildBottom:lastBox.bottom-p.top+panel.scrollTop,
            overflow:getComputedStyle(panel).overflowY,
            headerOutside:!panel.contains(document.querySelector('.dashboard-header'))};
        }""", height)
        assert layout["chartHeight"] >= 194, (height, layout)
        assert layout["datesInside"], (height, layout)
        assert layout["overflow"] == "auto", (height, layout)
        assert layout["lastChildBottom"] <= layout["scrollHeight"] + 0.5, (height, layout)
        assert layout["headerOutside"], (height, layout)
        if height == 478:
            assert layout["scrollHeight"] > layout["clientHeight"], (height, layout)
            page.locator(ui.INSIGHTS).evaluate("el => el.scrollTop = el.scrollHeight")
            assert page.locator(ui.INSIGHTS).evaluate("el => el.scrollTop > 0"), height
        else:
            assert layout["scrollHeight"] == layout["clientHeight"], (height, layout)


def test_compact_quota_cells_do_not_overlap_at_328px(page):
    page.set_viewport_size({"width": 328, "height": 110})
    page.wait_for_function("window.__tokenTrackerTest && document.querySelector('#resets')")
    page.evaluate("""() => {
      const now = Date.now(), age = new Date(now - 12 * 60000).toISOString();
      window.__tokenTrackerTest.renderResets([
        {name:'Codex',connected:true,limitsUpdatedAt:age,fiveHour:{percent:42,resetAt:now/1000+3600},weekly:{percent:35,resetAt:now/1000+4*86400}},
        {name:'Claude',connected:true,limitsUpdatedAt:age,fiveHour:{percent:100,resetAt:now/1000+6*86400+23*3600},weekly:{percent:100,resetAt:now/1000+6*86400+23*3600}}
      ]);
    }""")
    assert page.locator(".widget").evaluate("el => el.offsetWidth") == 328
    left = page.locator("#r1-weekly-left")
    expect(left).to_contain_text("100%")
    expect(left).to_contain_text("12m ago")
    expect(page.locator("#r1-weekly-cd")).to_contain_text("6d 23h")
    layout = page.evaluate("""() => {
      const row=document.querySelector('#r1-weekly-row'), left=document.querySelector('#r1-weekly-left'), age=left.querySelector('i');
      const reset=document.querySelector('#r1-weekly-row .rr-moment'), l=left.getBoundingClientRect(), r=reset.getBoundingClientRect();
      const a=getComputedStyle(age), c=getComputedStyle(document.querySelector('#r1-weekly-cd'));
      return {gap:r.left-l.right, resetRight:r.right<=row.getBoundingClientRect().right+0.5,
        ageOverflow:age.scrollWidth>age.clientWidth, ageOverflowStyle:a.overflow, ageEllipsis:a.textOverflow,
        resetAlign:c.textAlign};
    }""")
    assert abs(layout["gap"] - 8) < 0.5, layout
    assert layout["resetRight"], layout
    assert layout["ageOverflow"], layout
    assert layout["ageOverflowStyle"] == "hidden", layout
    assert layout["ageEllipsis"] == "ellipsis", layout
    assert layout["resetAlign"] == "right", layout


def test_horizontal_layout(page):
    open_settings(page)
    bad = page.locator(ui.SETTINGS).locator(ui.INTERACTIVE).evaluate_all("els => els.filter(e => {const r=e.getBoundingClientRect(), p=document.querySelector('#dashboard-view').getBoundingClientRect(); return r.width && (r.left < p.left - 1 || r.right > p.right + 1)}).map(e => e.outerHTML)")
    assert not bad, bad


def test_settings_keyboard_reaches_controls(page):
    # The shared fixture denies automatic actions; grant greet only to the embedded test frame.
    def reset_frame_policy(route):
        if urlsplit(route.request.frame.url).path == "/reset_actions.html":
            route.fulfill(json={"schemaVersion": 1, "generation": "ui-rig-reset-frame",
                                "providers": [{"name": "Codex", "collect": True, "show": True,
                                               "send": True, "sendOrigin": "provider_env",
                                               "capabilities": {"greet": True, "continue": False}}]})
        else:
            route.continue_()

    page.route("**/api/provider-policy", reset_frame_policy)
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


def test_folded_model_tooltip_keeps_untrusted_names_as_text(page):
    hostile_names = [
        '<img src="/__tooltip_probe__" onerror="window.__tooltipInjected=true">',
        '<svg onload="window.__tooltipInjected=true">& "quoted"</svg>',
    ]

    def model_payload(route):
        data = route.fetch().json()
        data["providers"][0]["models"] = [
            {"name": f"safe-model-{i}", "tokens": 1000 - i, "apiEquivalentCost": 1}
            for i in range(5)
        ] + [{"name": name, "tokens": 1, "apiEquivalentCost": None}
             for name in hostile_names]
        route.fulfill(json=data)

    page.route("**/api/insights?*", model_payload)
    page.evaluate("window.__tooltipInjected=false")
    open_insights(page)
    other = page.locator(".dash-model-name.other")
    expect(other).to_be_visible()
    tip = page.locator(".dash-tooltip")
    for activate in (other.focus, other.hover):
        activate()
        expect(tip).to_be_visible()
        for name in hostile_names:
            expect(tip).to_contain_text(name)
        assert tip.locator("img, svg").count() == 0
        assert page.evaluate("window.__tooltipInjected") is False
        page.keyboard.press("Escape")
        expect(tip).to_have_attribute("aria-hidden", "true")
        expect(tip).to_have_css("opacity", "0")
        expect(page.locator(ui.DASHBOARD)).to_be_visible()
        other.blur()


@pytest.mark.parametrize("native_settle", [True, False])
def test_close_crossfade_hides_controls_even_with_explicit_visibility(page, native_settle):
    page.emulate_media(reduced_motion="no-preference")
    open_insights(page)
    tab = page.locator(".dashboard-tab").first
    tab.evaluate("el => el.style.setProperty('visibility', 'visible', 'important')")
    page.evaluate("() => window.__tokenTrackerTest.closeDashboard()")
    page.wait_for_function("window.__tokenTrackerTest.dashboardState.finish !== null")
    page.wait_for_function("document.body.classList.contains('dashboard-closing') && getComputedStyle(document.querySelector('.dashboard-header')).opacity === '0'")
    expect(tab).to_have_css("visibility", "visible")
    if native_settle:
        page.evaluate("() => window.tokenTrackerNotchSettled('controls', false, window.__tokenTrackerTest.dashboardState.generation)")
    page.wait_for_function("document.body.classList.contains('dashboard-crossfade')")
    expect(page.locator(ui.DASHBOARD)).not_to_be_visible()
    expect(tab).not_to_be_visible()
    expect(page.locator(".widget")).to_have_css("animation-name", "dashboard-fade-in")
    page.wait_for_function("!window.__tokenTrackerTest.dashboardState.swapping")
    expect(page.locator(ui.DASHBOARD)).not_to_be_visible()
