# NAV Daily Profit Detail Design

## Goal

Make the cumulative-profit summary on the mobile NAV dashboard open a compact daily-profit history without changing the existing profit ledger or triggering provider queries.

## Interaction

- The cumulative-profit hero is a keyboard-accessible button-like control.
- Activating it opens a bottom sheet over the current dashboard.
- The sheet shows the cumulative total, the accounting start date, and one row per NAV date in reverse chronological order.
- Each row contains only the date and that day's combined profit. Positive values use orange-red, negative values use green, and zero uses the normal text color.
- The sheet closes through the close icon, backdrop click, or Escape key. Body scrolling is restored after close.
- Empty ledgers show a clear empty state instead of an empty list.

## Data

`NavDashboardStore.payload()` adds `daily_profits`, built exclusively from immutable `profit_entries`. Entries are grouped by `nav_date`, summed with `Decimal`, sorted newest first, and serialized as `{date, amount}`. This is presentation data only; no state migration or new network request is required.

## Safety

- Existing cumulative and daily accounting semantics remain unchanged.
- The private-link header continues to protect the API.
- Enterprise WeChat queries and scheduled pushes are untouched.
- The sheet renders only API-provided dates and numeric values.

## Verification

- Unit test grouping, summing, date ordering, and cumulative-total consistency.
- Route/page test the required dialog structure and controls.
- Run the full pytest suite and compile checks.
- Verify the deployed page at 390px and confirm open, scroll, close, and invalid-link states.
