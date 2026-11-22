"""Selectors for the widget UI; update here when its public markup changes."""
OPEN = "#dashboard-open"
DASHBOARD = "#dashboard-view"
INSIGHTS = "#dashboard-insights"
SETTINGS = "#dashboard-settings"
SETTINGS_TAB = "#dashboard-tab-settings"
CONTENT = "#dashboard-content"
SETTINGS_SCROLLER = "#dashboard-settings"
RANGE = "[data-dashboard-range]"
METRIC = "[data-dashboard-metric]"
PROVIDERS = "[data-dashboard-provider]"
HERO = ".dash-hero"
ACTION = '[data-dashboard-action="{}"]'
CLOCK = "[data-dashboard-clock]"
INTERACTIVE = 'summary, button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex="0"]'

# The widget's read-only reset summary; the editable form lives in the native Settings window.
RESET_GROUP = ".dash-reset-group"

# Standalone Settings page (/reset_actions.html), loaded by the native Settings window.
SETTINGS_SAVED_STATE = "#saved-action-state"
SETTINGS_MODE = 'input[name="mode"]'
SETTINGS_FOOTER = ".settings > footer"
