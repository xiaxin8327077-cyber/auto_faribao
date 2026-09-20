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

---

# 私人理财看板 Logo 接入检查

日期：2026-09-20。范围仅限顶栏 Logo 与浏览器页签图标，不修改财务、交易或数据接口逻辑。

## 视觉基准与证据

- 用户选择：`C:/Users/jsitc/AppData/Local/Temp/codex-clipboard-c62ac4fb-bd5c-4945-9559-1d70c9f5fc42.png`（398 × 392 截图）。
- 原始素材：`C:/Users/jsitc/.codex/generated_images/01a0b20f-380c-7a43-8b38-a419a810fc71/exec-c667b288-c1fe-4ee5-ac33-3aa80536f56b.png`。
- 实际使用：`src/nav_assets/private-wealth-logo.png`（1254 × 1254，RGBA，透明背景）。与原始素材 SHA-256 完全一致：`61e2a0f96b7898ca0a95fa35ab0a63894d42d564a9b02aff348a2c87ec93c499`。
- 本地预览：`http://127.0.0.1:8766/nav#brand-preview`。仅提供模拟数据，禁止交易写入，不连接生产。
- 实现截图：`runtime/brand-qa-mobile-320.png`（320 × 640）、`runtime/brand-qa-mobile-430.png`（430 × 800）、`runtime/brand-qa-desktop.png`（794 × 835）。
- 状态：浅色主题、总览页、模拟数据加载完成；同时检查持仓、交易、定投页面与赎回筛选。

## 尺寸与比较方式

- 图片在顶栏占据 38 × 38 CSS 像素，不拉伸、不重绘。截图中的棋盘格来自透明预览，不作为背景放入网页。
- 浏览器缩放 DPR 为 1.23；视口设置按比例调整后，以 DOM 实测的 320、430、1024 CSS 像素宽度验证。保存的手机截图按 CSS 密度输出。
- 原始素材和用户截图是独立 Logo，不是整页布局稿；仅将标识的轮廓、方向、比例、颜色和透明背景作为视觉目标，其他页面保留现有设计。
- 全图比较：在同一次比较输入中打开用户所选图片和 320 像素实现截图。蓝叶在左、金叶在右、深蓝底座和透明镂空均保留；顶栏与内容区位置正常。
- 重点区域：检查截图顶栏 Logo、标题和刷新区。320 像素下标题右边界约 154，刷新区左边界约 196，无重叠。标识采用原图字节，未以 SVG/CSS 图形替代。

## 必查项

- 字体：保留原有仿宋、17 像素标题及其字重；标题单行，刷新时间仍可见。
- 间距：Logo 与标题间距 4 像素，顶栏仍为约 54 像素高，底栏未移动，滚动条位于两栏之间。
- 颜色：保留源素材蓝色、金色、深蓝色，无 CSS 着色；其他颜色变量未变。
- 图像：透明背景，边缘正常，没有棋盘格、缺图或拉伸；原始文件保留。
- 文案：继续使用“理财组合看板”，无额外品牌名或营销文案。

## 验证结果

- 先新增用例，验证缺少 Logo 标签和资源时确实失败；接入后相关 108 项页面及路由测试通过。
- PNG 本地资源路由返回 200、`image/png`，RGBA 格式；页签与顶栏使用同一素材。
- 320、430、1024 CSS 像素宽度下标题和刷新区不重叠；标识加载成功。
- 总览、持仓、交易、定投切换正常；赎回筛选可选中。未执行申购、赎回或其他真实资金操作。
- 本地浏览器控制台无 warning/error；`git diff --check` 通过。

## Findings / 比较历史

无需要修正的 P0/P1/P2 设计偏差。首次设置视口时发现浏览器缩放使实际宽度仅为 260 CSS 像素；调整测试视口至实测 320 后重新截图比较，未通过修改产品掩盖测试环境差异。

## 后续精细化

- P3：小尺寸页签保留原图完整透明留白，细叶脉会自然简化；如将来需要专门的极小尺寸版本，可另行设计。
- 页签声明和资源已自动验证；操作系统桌面快捷方式图标不在本次范围内。
- 未提交、推送或部署生产。

## 完成清单

- [x] 使用用户选定的原始 Logo
- [x] 顶栏与浏览器页签接入
- [x] 相关测试及本地响应式检查
- [x] 保留原有财务逻辑与滚动布局

final result: passed
