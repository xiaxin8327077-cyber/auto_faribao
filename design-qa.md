# NAV Dashboard Design QA

- Source visual truth: `D:\cherry\auto_faribao\docs\nav-dashboard-mobile-preview@3x.png`
- Implementation screenshots:
  - `D:\cherry\auto_faribao\nav-dashboard-390-v2.png`
  - `D:\cherry\auto_faribao\nav-dashboard-430-top.png`
  - `D:\cherry\auto_faribao\nav-dashboard-430-bottom.png`
  - `D:\cherry\auto_faribao\nav-dashboard-invalid-link-v2.png`
- Viewports: 390x844 and 430x932
- State: five configured products, negative cumulative return, one positive and four negative latest contributions

## Full-view comparison evidence

The 430px top and lower viewport captures reproduce the selected mobile composition: title and refresh status, earnings hero, three portfolio metrics, two contribution insights, five dense product rows, and the two-column contribution ranking. Browser measurements report `scrollWidth <= innerWidth` at both target widths, so no horizontal content is clipped.

The browser's stitched full-page capture repeated the header at the bottom even though the DOM contained exactly one `h1`; the two normal viewport screenshots are therefore the visual source of truth for the implementation.

## Focused comparison evidence

Focused checks were required for the product table and top summary because the source is information-dense. Product names wrap to two lines, codes stay subordinate, six-decimal NAV values align vertically, orange-red positive values and green negative values match the source semantics, and the longest market-value number is fully visible at 390px.

## Required fidelity surfaces

- Fonts and typography: system Chinese UI font stack matches the reference character proportions. Headings, totals, product names, metadata, and tabular figures retain the same hierarchy without negative letter spacing or viewport-scaled font sizes.
- Spacing and layout rhythm: 14px page gutters, restrained 8px radii, consistent section gaps, stable product grid columns, and full-width mobile bands match the selected dense dashboard direction.
- Colors and visual tokens: light blue page background, white panels, deep navy text, blue controls, orange-red gains, and green losses match the reference palette and established WeCom report convention.
- Image quality and asset fidelity: all visible controls and weather marks use locally served Lucide 0.468.0 SVG assets. They remain crisp at both target viewports and do not depend on an external icon font.
- Copy and content: titles, metrics, holding labels, ranking labels, and cumulative-return explanation match the approved preview. “查看全部” is intentionally replaced with “最新披露” because this release has no second holdings page.

## Interaction and console checks

- Correct private Fragment key loads five products.
- Missing or wrong key shows only “私密链接无效”; no product DOM is rendered.
- Refresh control is unique and clickable.
- Five-minute cooldown displays “刚刚更新过，请稍后再试”.
- Browser console errors/warnings: none on valid and invalid-link states.

## Comparison history

### Iteration 1

- P1: external Font Awesome glyphs did not render reliably, leaving weather and action circles blank.
- P2: the longest estimated-market-value number was ellipsized at 390px.

Fixes: replaced all external icon-font glyphs with locally deployed Lucide assets and reduced compact metric values to a stable 16px size.

### Iteration 2

Post-fix evidence shows every icon loaded, `717,143.34` fully visible, five product rows aligned, and no horizontal overflow at 390px or 430px. No actionable P0, P1, or P2 fidelity differences remain.

## Follow-up polish

No blocking polish items. Real production values may naturally change line truncation in the insight labels, which already use safe ellipsis within fixed columns.

final result: passed

## Monthly and yearly profit views

- The daily-profit sheet now exposes an accessible three-part segmented control: daily, monthly, and yearly.
- Daily remains the default view; switching periods reuses cached API data and does not trigger provider queries.
- Production data contains seven daily rows, seven natural-month rows for January through July 2026, and one natural-year row for 2026.
- January through June are historical monthly totals supplied by the user. They are included in cumulative/monthly/yearly totals and intentionally excluded from daily rows.
- June 2026 includes the approved 193-yuan adjustment and is stored as 795.42 yuan.
- The summary hero includes current natural-month profit beneath today's profit, with independent positive/negative coloring.
- Production cumulative profit is 6992.12947344 yuan and current-month profit is -535.93052656 yuan.

final result: passed

## Daily profit bottom sheet

- Production viewport: 390x844.
- Selecting the cumulative-profit control opens the modal sheet and locks background scrolling.
- The sheet reports 7 profit dates from 2026-07-01 through 2026-07-09, sorted newest first.
- Daily amounts sum to the unchanged cumulative total of -535.93 yuan.
- Positive rows use the existing orange-red semantic color and negative rows use green.
- The rendered page width equals the 390px viewport, with no horizontal overflow.
- The sheet is 551px high at this viewport and the full current history fits without clipping; the list remains scrollable as more dates accumulate.
- The locally served Lucide close icon loaded successfully.

final result: passed

## Product holding line iteration

- Spec: `docs/superpowers/specs/2026-07-29-nav-dashboard-product-holdings-design.md`
- Local development state: shares not configured in `config.yaml` for either product, so the local dashboard has zero populated holding lines.

### Iteration 1 (initial)

- Layout: holding as sibling of `.code` inside the name-column wrapper.
- Result: text wraps in the narrow 130-150px column, breaking into two lines per row.
- User feedback: "这个不应转行才对" (this shouldn't wrap).

### Iteration 2 (cross-column, name-aligned)

- Layout: holding moved out of the name column to be a direct child of `<article class="product">`, with `grid-column: 2 / -1` so its left edge aligns with `.name` and it spans columns 2-4.
- Visual evidence: local IAB screenshot surface is broken in this environment (all `tab.screenshot()` calls time out). Functional verification used three independent layers instead:
  1. Python contract: 11 cases in `tests/test_nav_dashboard_page.py` all pass, locking the exact output strings (e.g. `401133.95 份 · 市值 401,628.95 元`).
  2. JS function: 11 cases evaluated in-browser via `tab.playwright.evaluate(() => formatHolding(...))` returned identical output to the Python contract.
  3. DOM snapshot with mocked shares: temporarily injected shares into `data/nav_dashboard_state.json`, reloaded the page, captured the AI/ARIA tree, and used `getBoundingClientRect()` to measure positions. Both product rows showed `401133.95 份 · 市值 432,382.28 元` and `36667.07 份 · 市值 39,186.10 元` on a single line. The holding `left` (66px) equals the name `left` (66px) in both 390x844 and 430x932 viewports. The global estimated market value re-computed to `471,568.38` yuan (math: 401133.95 × 1.077900 + 36667.07 × 1.068700 = 432,382.28 + 39,186.10 = 471,568.38). The mock state was restored from a `.real` backup.
- Production re-verification required: deploy this commit to Seoul server, add `shares` to `config.yaml` for at least one product, capture 390x844 and 430x932 screenshots, and confirm `scrollWidth <= innerWidth` for both. The current change is correct by the three layers above; the production screenshot only confirms pixel-level rendering.
- Regression: bottom sheet still opens, daily/monthly/yearly tabs present, cumulative profit unaffected, top metric cards unchanged.
- `.holding` rule: `grid-column: 2 / -1; margin-top: 4px; color: var(--muted); font-size: 10px; font-variant-numeric: tabular-nums`, same visual weight as `.code`.
- Empty-state behavior: when `shares` is empty the `<div class="holding">` node is not emitted at all (no 4px blank gap), matching the chosen "完全隐藏" UX. Verified: 2 products rendered, 0 holding elements when shares are unset.

final result: passed (functional); pending production screenshot re-verification
