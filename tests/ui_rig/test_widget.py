import os
import re
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


@pytest.mark.parametrize("height", [608, 478])
@pytest.mark.parametrize("range_name", ["24h", "7d", "30d"])
@pytest.mark.parametrize("metric", ["tokens", "cost"])
def test_insights_freshness_stamp_stays_below_chart(page, height, range_name, metric):
    def fresh_insights(route):
        data = route.fetch().json()
        from datetime import datetime, timezone
        data["generatedAt"] = datetime.now(timezone.utc).isoformat()
        route.fulfill(json=data)

    page.route("**/api/insights?*", fresh_insights)
    page.set_viewport_size({"width": 720, "height": height})
    open_insights(page)
    providers = page.locator(ui.PROVIDERS)
    expect(providers).to_have_count(2)
    for index in range(2):
        expect(providers.nth(index)).to_have_attribute("aria-pressed", "true")
    page.locator(f'{ui.RANGE}[data-dashboard-range="{range_name}"]').click()
    page.locator(f'{ui.METRIC}[data-dashboard-metric="{metric}"]').click()
    stamp = page.locator(".dash-chart-section .dash-status-line.is-freshness")
    expect(stamp).to_be_visible()
    layout = page.evaluate("""() => {
      const stamp=document.querySelector('.dash-chart-section .dash-status-line.is-freshness');
      const rect=el=>{const r=el.getBoundingClientRect();return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};};
      const overlaps=(a,b)=>a.left<b.right-.5&&a.right>b.left+.5&&a.top<b.bottom-.5&&a.bottom>b.top+.5;
      const box=rect(stamp), plot=rect(document.querySelector('.dash-chart-section .dash-plot'));
      const labels=[...document.querySelectorAll('.dash-chart-section .dash-axis-value,.dash-chart-section .dash-date')].map(rect);
      return {plot:overlaps(box,plot),labels:labels.map(label=>overlaps(box,label)),text:stamp.textContent};
    }""")
    assert not layout["plot"], layout
    assert not any(layout["labels"]), layout
    assert "Updated" in layout["text"], layout


@pytest.mark.parametrize("height", [608, 478])
def test_insights_quota_text_fits_all_filters_ranges_and_metrics(page, height):
    def priced_insights(route):
        data = route.fetch().json()
        for provider in data.get("providers", []):
            total = provider.get("totals", {}).get("tokens", 0)
            provider["totals"]["apiEquivalentCost"] = round(total / 1_000_000 * 0.02, 2)
            for point in provider.get("series", []):
                point["apiEquivalentCost"] = round(point.get("tokens", 0) / 1_000_000 * 0.02, 2)
        route.fulfill(json=data)

    page.route("**/api/insights?*", priced_insights)
    page.set_viewport_size({"width": 720, "height": height})
    page.evaluate("""() => {
      window.tokenTrackerQuotaNow = () => [
        {name: 'Codex', usedPercent: 62, countdown: '1h 5m', weekly: {usedPercent: 41, countdown: '4d 12h'}},
        {name: 'Claude'}
      ];
    }""")
    open_insights(page)

    for filter_name in ("all", "Codex", "Claude"):
        for chip in page.locator(ui.PROVIDERS).all():
            if chip.get_attribute("aria-pressed") == "false":
                chip.click()
        if filter_name != "all":
            other = "Claude" if filter_name == "Codex" else "Codex"
            page.locator(f'{ui.PROVIDERS}[data-dashboard-provider="{other}"]').click()
        for range_name in ("24h", "7d", "30d"):
            range_button = page.locator(f'{ui.RANGE}[data-dashboard-range="{range_name}"]')
            range_button.click()
            expect(range_button).to_have_attribute("aria-pressed", "true")
            for metric in ("tokens", "cost"):
                metric_button = page.locator(f'{ui.METRIC}[data-dashboard-metric="{metric}"]')
                metric_button.click()
                expect(metric_button).to_have_attribute("aria-pressed", "true")
                expect(page.locator(".dash-quota")).to_be_visible()
                missing_note = page.locator(".dash-quota-note")
                if filter_name in ("all", "Claude"):
                    expect(missing_note).to_have_count(1)
                    expect(missing_note).to_have_text("No live reading")
                else:
                    expect(missing_note).to_have_count(0)
                layout = page.evaluate("""() => {
                  const panel = document.querySelector('#dashboard-insights');
                  const quota = panel.querySelector('.dash-quota');
                  const bounds = panel.getBoundingClientRect();
                  const walker = document.createTreeWalker(panel, NodeFilter.SHOW_TEXT);
                  const boxes = [], quotaBoxes = [];
                  for (let node; (node = walker.nextNode());) {
                    if (!node.textContent.trim() || node.parentElement.closest('.sr-only')) continue;
                    const style = getComputedStyle(node.parentElement);
                    if (style.display === 'none' || style.visibility === 'hidden') continue;
                    const range = document.createRange();
                    range.selectNodeContents(node);
                    for (const rect of range.getClientRects()) {
                      if (!rect.width || !rect.height) continue;
                      const item = {text: node.textContent.trim(), left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom};
                      boxes.push(item);
                      if (quota.contains(node.parentElement)) quotaBoxes.push(item);
                    }
                  }
                  const overlap = (a, b) => a.left < b.right - .5 && a.right > b.left + .5 && a.top < b.bottom - .5 && a.bottom > b.top + .5;
                  const intersections = [];
                  for (let i = 0; i < quotaBoxes.length; i++) for (let j = i + 1; j < quotaBoxes.length; j++)
                    if (overlap(quotaBoxes[i], quotaBoxes[j])) intersections.push([quotaBoxes[i].text, quotaBoxes[j].text]);
                  return {right: bounds.right, overflow: boxes.filter(box => box.right > bounds.right + .5), intersections};
                }""")
                assert not layout["overflow"], (height, filter_name, range_name, metric, layout)
                assert not layout["intersections"], (height, filter_name, range_name, metric, layout)


@pytest.mark.parametrize("height", [608, 478])
@pytest.mark.parametrize("quota_state", ["one-provider-missing", "two-provider-peaks"])
def test_insights_quota_columns_align_in_missing_and_peak_states(page, height, quota_state):
    def peaks_insights(route):
        data = route.fetch().json()
        data["peaks"] = [
            {"name": "Codex", "fiveHour": 100, "weekly": 90},
            {"name": "Claude", "fiveHour": 80, "weekly": 70},
        ]
        route.fulfill(json=data)

    page.route("**/api/insights?*", peaks_insights)
    page.set_viewport_size({"width": 720, "height": height})
    if quota_state == "one-provider-missing":
        quota_rows = """[
          {name: 'Codex', usedPercent: 62, countdown: '1h 5m', weekly: {usedPercent: 41, countdown: '4d 12h'}},
          {name: 'Claude'}
        ]"""
    else:
        quota_rows = """[{name: 'Codex'}, {name: 'Claude'}]"""
    page.evaluate(f"window.tokenTrackerQuotaNow = () => {quota_rows};")
    open_insights(page)

    if quota_state == "one-provider-missing":
        expect(page.locator(".dash-quota-note")).to_have_count(1)
        note_alignment = page.locator('.dash-quota-note[data-quota-key="Claude"]').evaluate("""note => {
          const name = note.parentElement.firstChild;
          const range = document.createRange();
          range.selectNodeContents(name);
          return {nameLeft: range.getBoundingClientRect().left, noteLeft: (() => {
            const text = document.createRange(); text.selectNodeContents(note); return text.getBoundingClientRect().left;
          })()};
        }""")
        assert abs(note_alignment["nameLeft"] - note_alignment["noteLeft"]) <= 1, (height, quota_state, note_alignment)
    else:
        expect(page.locator(".dash-quota-peak")).to_be_visible()
        alignment = page.locator(".dash-quota").evaluate("""quota => {
          const live = [...quota.querySelectorAll(':scope > table.dash-quota-table tbody tr:first-child td')];
          const peak = [...quota.querySelectorAll('.dash-quota-peak tbody tr:first-child td')];
          // Compare rendered text edges on both sides; cell boxes include padding that text does not.
          const textRight = cell => { const range = document.createRange(); range.selectNodeContents(cell.firstChild); return range.getBoundingClientRect().right; };
          return {liveRight: live.map(textRight), peakRight: peak.map(textRight)};
        }""")
        assert len(alignment["liveRight"]) == 2 and len(alignment["peakRight"]) == 2, (height, quota_state, alignment)
        assert all(abs(live - peak) <= 1 for live, peak in zip(alignment["liveRight"], alignment["peakRight"])), (height, quota_state, alignment)


@pytest.mark.parametrize("height", [608, 478])
def test_by_work_folded_name_fits_at_compact_width(page, height):
    def six_model_ledger(route):
        route.fulfill(json={"rows": [
            {"provider": "Claude", "modelId": f"M{index}", "displayName": f"M{index}",
             "kind": "build", "totalTokens": 1000000 - index * 1000, "knownCostUsd": 1.0,
             "costComplete": True, "unpricedTokens": 0}
            for index in range(10)
        ], "coverage": {}, "service": {}})

    page.route("**/api/ledger?*", six_model_ledger)
    page.set_viewport_size({"width": 720, "height": height})
    open_insights(page)
    page.locator(f'{ui.METRIC}[data-dashboard-metric="tokens"]').click()
    page.locator('[data-dashboard-models="work"]').click()
    folded = page.locator(".dash-work-row.other .dash-model-name")
    expect(folded).to_be_visible()
    metrics = folded.evaluate("el => ({text:el.textContent.trim(),title:el.title,scrollWidth:el.scrollWidth,clientWidth:el.clientWidth})")
    assert metrics["text"] == "Other (6)", metrics
    assert metrics["title"] == "6 models folded", metrics
    assert metrics["scrollWidth"] <= metrics["clientWidth"], metrics
    names = page.locator(".dash-work-row:not(.is-head):not(.is-foot) .dash-model-name").evaluate_all(
        "els => els.map(el => ({name:el.textContent.trim(),scrollWidth:el.scrollWidth,clientWidth:el.clientWidth}))")
    assert names and all(row["scrollWidth"] <= row["clientWidth"] for row in names), names


def test_provider_legend_values_share_one_line_box_at_compact_and_default_sizes(page):
    open_insights(page)

    def assert_provider_rows_share_line():
        rows = page.locator(f"{ui.HERO} .dash-split-legend")
        expect(rows).to_be_visible()
        boxes = rows.evaluate("el => { const grid=getComputedStyle(el), names=[...el.querySelectorAll('.dash-legend-name')], values=null; return names.map((name,i) => { const item=name.closest('.dash-legend-item'), value=item.querySelector('.dash-legend-value'), n=name.getBoundingClientRect(), v=value?.getBoundingClientRect(), itemStyle=getComputedStyle(item); return {name:name.textContent.trim(), nameY:n.y, valueY:v?.y, nameH:n.height, valueH:v?.height, legendWidth:el.getBoundingClientRect().width, gridColumns:grid.gridTemplateColumns, itemDisplay:itemStyle.display, itemColumns:itemStyle.gridTemplateColumns, nameRow:getComputedStyle(name).gridRow, valueRow:value?getComputedStyle(value).gridRow:null}; }); }")
        assert len(boxes) >= 2, f"expected provider name/value rows, got {boxes!r}"
        for row in boxes:
            assert row["valueY"] is not None and abs(row["nameY"] - row["valueY"]) <= 2, f"provider name and value must share one grid row (baseline alignment may shift cells by 1px; a wrap moves them a full 28px row): {row!r}"
            assert row["nameH"] <= row["valueH"] + 1, f"provider name wrapped to multiple line boxes: {row!r}"

    for height in (608, 478):
        page.set_viewport_size({"width": 720, "height": height})
        for value in ("24h", "7d", "30d"):
            button = page.locator(f'{ui.RANGE}[data-dashboard-range="{value}"]')
            # Ranges load once and are cached, so the second viewport pass makes no request; wait on the pressed state.
            button.click()
            expect(button).to_have_attribute("aria-pressed", "true")
            page.locator(f'{ui.METRIC}[data-dashboard-metric="cost"]').click()
            expect(page.locator(f'{ui.METRIC}[data-dashboard-metric="cost"]')).to_have_attribute("aria-pressed", "true")
            assert_provider_rows_share_line()
        page.locator(f'{ui.METRIC}[data-dashboard-metric="tokens"]').click()
        page.locator(f'{ui.RANGE}[data-dashboard-range="24h"]').click()
        expect(page.locator(f'{ui.RANGE}[data-dashboard-range="24h"]')).to_have_attribute("aria-pressed", "true")
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
    weekly_countdown = page.locator("#r1-weekly-cd")
    expect(weekly_countdown).to_have_text("")
    expect(left).to_have_attribute("title", re.compile(r"resets in 6d 23h", re.I))
    assert re.search(r"resets in 6d 23h", left.locator(".sr-only").inner_text(), re.I)
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


@pytest.mark.parametrize("width", [328, 720])
@pytest.mark.parametrize("mode", ["controls", "inspect"])
def test_widget_quota_rows_and_elapsed_values_do_not_overlap(page, width, mode):
    page.set_viewport_size({"width": width, "height": 478})
    page.wait_for_function("window.__tokenTrackerTest && document.querySelector('#resets')")
    page.evaluate("""({mode}) => {
      document.body.classList.add('runway-compact');
      document.body.dataset.notchMode = mode;
      const now=Date.now();
      window.__tokenTrackerTest.renderResets([
        {name:'Codex',connected:true,limitsUpdatedAt:new Date(now).toISOString(),fiveHour:{percent:42,resetAt:now/1000+3600},weekly:{percent:35,resetAt:now/1000+4*86400}},
        {name:'Claude',connected:true,limitsUpdatedAt:new Date(now).toISOString(),fiveHour:{percent:67,resetAt:now/1000+7200},weekly:{percent:58,resetAt:now/1000+5*86400}}
      ]);
      const list=document.querySelector('#chat-list');
      const job='<div class="runway-refined-row"><span class="runway-refined-title"><span class="r-title"><span class="runway-agent-label">A job</span></span></span><span class="runway-refined-timing"><span class="runway-refined-elapsed">12m 37s</span></span><span class="runway-refined-status">Running</span><span class="runway-refined-activity"></span><span class="runway-refined-path"></span></div>';
      list.innerHTML='<div class="chat"><div class="row">'+job+'</div></div><div class="ag runway-agent"><div class="runway-agent-glance">'+job+'</div></div>';
    }""", {"mode": mode})
    for state in ("readings", "missing"):
        if state == "missing":
            page.evaluate("""() => window.__tokenTrackerTest.renderResets([
              {name:'Claude',connected:true}
            ])""")
        layout = page.evaluate("""() => {
          const intersects=(a,b)=>a.left<b.right-.5&&a.right>b.left+.5&&a.top<b.bottom-.5&&a.bottom>b.top+.5;
          const textRect=node=>{
            const range=document.createRange();range.selectNodeContents(node);
            const viewport={left:0,top:0,right:document.documentElement.clientWidth,bottom:document.documentElement.clientHeight};
            return [...range.getClientRects()].map(r=>{
              let box={left:Math.max(r.left,viewport.left),top:Math.max(r.top,viewport.top),right:Math.min(r.right,viewport.right),bottom:Math.min(r.bottom,viewport.bottom)};
              for(let el=node.parentElement;el;el=el.parentElement){
                const s=getComputedStyle(el),clipsX=s.overflowX!=='visible',clipsY=s.overflowY!=='visible';
                if(clipsX||clipsY){const clip=el.getBoundingClientRect();box={left:clipsX?Math.max(box.left,clip.left):box.left,top:clipsY?Math.max(box.top,clip.top):box.top,right:clipsX?Math.min(box.right,clip.right):box.right,bottom:clipsY?Math.min(box.bottom,clip.bottom):box.bottom};break;}
              }
              return box;
            }).filter(r=>r.right>r.left&&r.bottom>r.top);
          };
          const rendered=node=>{
            for(let el=node.parentElement;el;el=el.parentElement){
              const style=getComputedStyle(el);
              if(el.hidden||el.classList.contains('sr-only')||el.getAttribute('aria-hidden')==='true'||style.display==='none'||style.visibility!=='visible'||Number(style.opacity)<=0)return false;
            }
            return textRect(node).length>0;
          };
          const boxes=[], percentages=[], elapsed=[], hiddenText=[];
          const styleEvidence=node=>{const chain=[];for(let el=node.parentElement;el;el=el.parentElement){const s=getComputedStyle(el),r=el.getBoundingClientRect();chain.push({tag:el.tagName,className:String(el.className||''),display:s.display,visibility:s.visibility,opacity:s.opacity,hidden:el.hidden,ariaHidden:el.getAttribute('aria-hidden'),width:r.width,height:r.height});}return chain;};
          for(const row of document.querySelectorAll('.rr')) {
            const walker=document.createTreeWalker(row,NodeFilter.SHOW_TEXT);
            for(let node;(node=walker.nextNode());) if(node.textContent.trim()) {
              const rects=textRect(node);
              if(rendered(node)) for(const rect of rects) boxes.push({row,node,text:node.textContent.trim(),rect:{left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom},styles:styleEvidence(node)});
              else hiddenText.push({text:node.textContent.trim(),rects:rects.map(r=>({left:r.left,right:r.right,top:r.top,bottom:r.bottom})),styles:styleEvidence(node)});
            }
            const value=row.querySelector('.rr-left b');
            if(value&&value.textContent.includes('%')) {const r=value.getBoundingClientRect(); percentages.push({el:value,x:r.left+r.width/2,y:r.top+r.height/2,text:value.textContent});}
          }
          const overlaps=[];for(let i=0;i<boxes.length;i++)for(let j=i+1;j<boxes.length;j++)if(boxes[i].row===boxes[j].row&&boxes[i].node!==boxes[j].node&&intersects(boxes[i].rect,boxes[j].rect))overlaps.push({texts:[boxes[i].text,boxes[j].text],boxes:[boxes[i],boxes[j]]});
          const covered=percentages.filter(p=>document.elementFromPoint(p.x,p.y)!==p.el&&!p.el.contains(document.elementFromPoint(p.x,p.y))).map(p=>p.text);
          for(const el of document.querySelectorAll('.runway-refined-elapsed')) {
            const text=[...el.childNodes].find(node=>node.nodeType===Node.TEXT_NODE&&node.textContent.trim());
            if(text&&rendered(text)){const r=textRect(text)[0];elapsed.push({text:text.textContent.trim(),scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,rect:{left:r.left,right:r.right,top:r.top,bottom:r.bottom}});}
          }
          return {overlaps,covered,elapsed,hiddenText};
        }""")
        assert not layout["overlaps"], (width, mode, state, layout)
        assert not layout["covered"], (width, mode, state, layout)
        assert all(item["scrollWidth"] <= item["clientWidth"] for item in layout["elapsed"]), (width, mode, state, layout)


def test_parent_and_child_job_titles_ellipsis_on_one_line(page):
    for width in (720, 328):
        page.set_viewport_size({"width": width, "height": 478})
        page.evaluate("""() => {
          document.body.classList.add('runway-compact');
          document.body.dataset.notchMode = 'inspect';
          const title = 'A long parent or child job title that must stay on one line and ellipsize';
          const row = (kind, titleText, branch = '') => `<div class="runway-refined-row">
            <span class="runway-refined-tree-anchor"></span>
            <span class="runway-refined-title"><span class="${kind}" title="${titleText}"><span class="runway-agent-label">${titleText}</span></span></span>
            <span class="runway-refined-timing"><span class="runway-refined-elapsed">1h 05m</span></span>
            <span class="runway-refined-status">Running</span><span class="runway-refined-activity"></span>
            <span class="runway-refined-path">${branch}</span><span class="runway-refined-tokens"></span>
          </div>`;
          document.querySelector('#chat-list').innerHTML = `<div class="chat"><div class="row">${row('r-title', title, 'feature/branch')}</div></div>
            <div class="ag runway-agent"><div class="runway-agent-glance">${row('runway-agent-name', title)}</div></div>`;
        }""")
        metrics = page.locator("#chat-list .runway-refined-title > span").evaluate_all("""els => els.map(el => {
          const label=el.querySelector('.runway-agent-label'), style=getComputedStyle(label), titleStyle=getComputedStyle(el), box=el.getBoundingClientRect();
          const row=el.closest('.runway-refined-row'), titleCell=el.parentElement.getBoundingClientRect(), branch=row.querySelector('.runway-refined-path').getBoundingClientRect();
          let oldTitleWidth=null;
          if(el.classList.contains('runway-agent-name')){
            const baseline=row.cloneNode(true);baseline.style.gridTemplateColumns='minmax(0,1fr) 62px 112px 96px 80px';baseline.style.position='absolute';baseline.style.left='-10000px';baseline.style.top='0';baseline.style.visibility='hidden';row.parentElement.append(baseline);
            oldTitleWidth=baseline.querySelector('.runway-refined-title').getBoundingClientRect().width;baseline.remove();
          }
          return {title:el.title, label:label.textContent, height:box.height, lineHeight:parseFloat(titleStyle.lineHeight), whiteSpace:style.whiteSpace, titleWidth:titleCell.width, branchWidth:branch.width, oldTitleWidth,
            overflow:style.overflow, textOverflow:style.textOverflow, scrollWidth:label.scrollWidth, clientWidth:label.clientWidth};
        })""")
        assert len(metrics) == 2, (width, metrics)
        for item in metrics:
            assert item["title"].startswith("A long parent or child"), (width, item)
            assert item["height"] <= item["lineHeight"] + 1, (width, item)
            assert item["whiteSpace"] == "nowrap", (width, item)
            if item["scrollWidth"] > item["clientWidth"]:
                assert item["overflow"] == "hidden" and item["textOverflow"] == "ellipsis", (width, item)
        assert metrics[1]["oldTitleWidth"] > 0, (width, metrics)
        assert metrics[1]["titleWidth"] >= metrics[1]["oldTitleWidth"] + metrics[0]["branchWidth"] - 1, (width, metrics)


def test_recent_standalone_failure_is_visible_with_muted_age(page):
    page.wait_for_function("window.__tokenTrackerTest && window.__tokenTrackerTest.state.snapshot")
    page.evaluate("""() => {
      const t=window.__tokenTrackerTest, now=new Date().toISOString(), source=t.state.snapshot;
      const provider=source.providers.find(item=>item.name==='Codex');
      const failed={id:'rig-failed',key:'Codex:rig-failed',parentKey:null,title:'Standalone failed job',status:'failed',stateDetail:'errored',updatedAt:now,model:'gpt-6.1-sol'};
      const snapshot={...source,generatedAt:now,graph:{revision:99,edges:{'Codex:rig-failed':null}},providers:[{...provider,chats:[failed]}]};
      t.render(snapshot);
    }""")
    row = page.locator('.chat[data-key="Codex:rig-failed"]')
    expect(row).to_be_visible()
    assert "t-mute" in row.locator('.runway-refined-status').get_attribute("class")
    expect(row.locator('.runway-refined-status')).to_contain_text("Failed")
    expect(row.locator('.runway-refined-elapsed')).not_to_be_empty()


def test_branchless_child_timing_shares_parent_right_edge(page):
    for width in (720, 560, 328):
        page.set_viewport_size({"width": width, "height": 478})
        page.evaluate("""() => {
          document.body.classList.add('runway-compact'); document.body.dataset.notchMode='inspect';
          const row=(path)=>`<div class="runway-refined-row"><span class="runway-refined-title"><span class="r-title"><span class="runway-agent-label">Job title</span></span></span><span class="runway-refined-timing"><span class="runway-refined-elapsed">2m 00s</span></span><span class="runway-refined-status">Running</span><span class="runway-refined-activity"></span><span class="runway-refined-path">${path}</span><span class="runway-refined-tokens"></span></div>`;
          document.querySelector('#chat-list').innerHTML=`<div class="chat"><div class="row">${row('feature/branch')}</div><div class="ag runway-agent"><div class="runway-agent-glance">${row('')}</div></div></div>`;
        }""")
        edges = page.locator("#chat-list .runway-refined-elapsed").evaluate_all("els => els.map(el => el.getBoundingClientRect().right)")
        assert len(edges) == 2, (width, edges)
        assert abs(edges[0] - edges[1]) <= 1, (width, edges)


def test_overflowing_job_list_has_keyboard_reachable_scroll_cue(page):
    page.set_viewport_size({"width": 560, "height": 478})
    page.evaluate("""() => {
      document.body.classList.add('runway-compact'); document.body.dataset.notchMode='inspect';
      const list=document.querySelector('#chat-list'); list.style.height='140px'; list.style.maxHeight='140px';
      list.innerHTML=Array.from({length:12},(_,i)=>`<div class="chat"><div class="row"><div class="runway-refined-row" style="height:50px">Job ${i}</div></div></div>`).join('');
    }""")
    content = page.locator("#chat-list")
    assert content.evaluate("el => el.scrollHeight > el.clientHeight")
    assert content.evaluate("el => getComputedStyle(el).scrollbarWidth === 'thin'")
    assert content.get_attribute("tabindex") == "0"
    content.focus()
    content.press("ArrowDown")
    page.wait_for_function("() => document.querySelector('#chat-list').scrollTop > 0", timeout=2000)
    assert content.evaluate("el => el.scrollTop > 0"), "the visible continuation cue is keyboard scrollable"
    content.evaluate("el => el.scrollTop = 0")
    page.evaluate("() => window.__tokenTrackerTest.syncRunwayOverflowCue()")
    cue = page.locator("#chat-list .runway-list-more")
    expect(cue).to_be_visible()
    expect(cue).to_have_text(re.compile(r"\+\d+ more"))
    cue.focus()
    cue.press("Enter")
    page.wait_for_function("() => document.querySelector('#chat-list').scrollTop > 0", timeout=2000)
    assert content.evaluate("el => el.scrollTop > 0"), "activating the continuation cue advances the list"


def test_locked_widget_dims_recenter_and_explains_why(page):
    page.evaluate("() => window.tokenTrackerInspect()")
    lock = page.locator("#widget-lock")
    expect(lock).to_be_visible()
    if lock.get_attribute("aria-pressed") != "true":
        lock.click()
    recenter = page.locator("#window-recenter")
    expect(recenter).to_have_attribute("aria-disabled", "true")
    expect(recenter).to_have_attribute("title", "Unlock the widget to move it")
    assert recenter.evaluate("el => el.disabled && Number.parseFloat(getComputedStyle(el).opacity) < 1")


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
