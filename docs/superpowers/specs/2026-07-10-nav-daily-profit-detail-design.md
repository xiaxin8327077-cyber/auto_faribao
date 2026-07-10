# NAV Daily Profit Detail Design

## Goal

Make the cumulative-profit summary on the mobile NAV dashboard open a compact daily-profit history without changing the existing profit ledger or triggering provider queries.

## Interaction

- The cumulative-profit hero is a keyboard-accessible button-like control.
- Activating it opens a bottom sheet over the current dashboard.
- The sheet shows the cumulative total, the accounting start date, and a segmented `每日 / 每月 / 每年` control.
- Daily is selected by default. Daily rows use NAV dates, monthly rows use natural calendar months, and yearly rows use natural calendar years; every view is reverse chronological.
- Each row contains only the date and that day's combined profit. Positive values use orange-red, negative values use green, and zero uses the normal text color.
- The sheet closes through the close icon, backdrop click, or Escape key. Body scrolling is restored after close.
- Empty ledgers show a clear empty state instead of an empty list.

## Data

`NavDashboardStore.payload()` adds `daily_profits`, `monthly_profits`, and `yearly_profits`, built exclusively from immutable `profit_entries`. Entries are grouped by NAV date, natural month, and natural year, summed with `Decimal`, and sorted newest first. This is presentation data only; no state migration or new network request is required.

Manually supplied historical monthly totals are stored separately from NAV-date ledger entries. They contribute to cumulative, monthly, and yearly totals, but never appear in the daily view because no exact NAV date exists. Upserts are keyed by `YYYY-MM`, making a repeated import idempotent. Adding January through June 2026 changes the displayed accounting start to `2026-01-01` while the provider-backed NAV ledger continues to begin on `2026-07-01`, preventing historical values from being counted twice.

## Safety

- Existing cumulative and daily accounting semantics remain unchanged.
- The private-link header continues to protect the API.
- Enterprise WeChat queries and scheduled pushes are untouched.
- The sheet renders only API-provided dates and numeric values.

## Verification

- Unit test daily, natural-month, and natural-year grouping, summing, ordering, and cumulative-total consistency.
- Route/page test the required dialog structure and controls.
- Run the full pytest suite and compile checks.
- Verify the deployed page at 390px and confirm open, scroll, close, and invalid-link states.
