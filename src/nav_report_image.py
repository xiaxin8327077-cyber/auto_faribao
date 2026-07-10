import math
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFilter, ImageFont


IMAGE_WIDTH = 1320
MIN_IMAGE_HEIGHT = 420
TABLE_TOP = 178
TABLE_ROW_HEIGHT = 66
TABLE_HEADER_HEIGHT = 42


@dataclass(frozen=True)
class NavReportImageRow:
    index: int
    name: str
    code: str
    shares_text: str
    nav_text: str
    nav_date_text: str
    change_text: str
    change_pct_text: str
    income_text: str
    change_positive: Optional[bool] = None
    income_positive: Optional[bool] = None


@dataclass(frozen=True)
class NavReportImageModel:
    title: str
    query_line: str
    total_amount: Optional[Decimal]
    total_text: str
    weather: str
    rows: tuple[NavReportImageRow, ...]


@dataclass(frozen=True)
class NavPeriodReportImageRow:
    index: int
    name: str
    code: str
    shares_text: str
    start_nav_text: str
    start_date_text: str
    end_nav_text: str
    end_date_text: str
    change_text: str
    change_pct_text: str
    income_text: str
    change_positive: Optional[bool] = None
    income_positive: Optional[bool] = None


@dataclass(frozen=True)
class NavPeriodReportImageModel:
    title: str
    query_line: str
    total_amount: Optional[Decimal]
    total_text: str
    weather: str
    rows: tuple[NavPeriodReportImageRow, ...]


def build_nav_report_image_model(
    results,
    title: str = "理财净值日报",
    generated_at: Optional[datetime] = None,
    target_date: Optional[date] = None,
) -> NavReportImageModel:
    generated_at = generated_at or datetime.now()
    results = tuple(results)
    query_suffix = (
        f"查询日期：{target_date:%Y-%m-%d}"
        if target_date
        else "最新披露净值 vs 上一期披露净值"
    )
    rows = tuple(_build_row(index, result) for index, result in enumerate(results, 1))
    amounts = [
        amount
        for result in results
        for amount in [_result_estimated_amount(result)]
        if amount is not None
    ]
    total_amount = _quantize_money(sum(amounts, Decimal("0"))) if amounts else None
    total_text = f"{_format_signed_money(total_amount)} 元" if total_amount is not None else "--"
    weather = "rain" if total_amount is not None and total_amount < 0 else "sun"
    return NavReportImageModel(
        title=title,
        query_line=f"查询时间：{generated_at:%Y-%m-%d %H:%M}    {query_suffix}",
        total_amount=total_amount,
        total_text=total_text,
        weather=weather,
        rows=rows,
    )


def render_nav_report_image(
    results,
    title: str = "理财净值日报",
    generated_at: Optional[datetime] = None,
    target_date: Optional[date] = None,
    output_dir=None,
) -> str:
    model = build_nav_report_image_model(
        results,
        title=title,
        generated_at=generated_at,
        target_date=target_date,
    )
    out_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / f"nav_report_{uuid.uuid4().hex}.png"
    _draw_report(model).save(image_path, quality=96)
    return str(image_path)


def build_nav_period_report_image_model(
    results,
    title: str,
    period_label: str,
    start_date: date,
    generated_at: Optional[datetime] = None,
) -> NavPeriodReportImageModel:
    generated_at = generated_at or datetime.now()
    results = tuple(results)
    rows = tuple(_build_period_row(index, result) for index, result in enumerate(results, 1))
    amounts = [
        amount
        for result in results
        for amount in [_period_result_estimated_amount(result)]
        if amount is not None
    ]
    total_amount = _quantize_money(sum(amounts, Decimal("0"))) if amounts else None
    total_text = f"{_format_signed_money(total_amount)} 元" if total_amount is not None else "--"
    weather = "rain" if total_amount is not None and total_amount < 0 else "sun"
    return NavPeriodReportImageModel(
        title=title,
        query_line=(
            f"查询时间：{generated_at:%Y-%m-%d %H:%M}    "
            f"统计周期：{period_label}    周期起点：{start_date:%Y-%m-%d}"
        ),
        total_amount=total_amount,
        total_text=total_text,
        weather=weather,
        rows=rows,
    )


def render_nav_period_report_image(
    results,
    title: str,
    period_label: str,
    start_date: date,
    generated_at: Optional[datetime] = None,
    output_dir=None,
) -> str:
    model = build_nav_period_report_image_model(
        results,
        title=title,
        period_label=period_label,
        start_date=start_date,
        generated_at=generated_at,
    )
    out_dir = Path(output_dir) if output_dir else Path(tempfile.gettempdir())
    out_dir.mkdir(parents=True, exist_ok=True)
    image_path = out_dir / f"nav_period_report_{uuid.uuid4().hex}.png"
    _draw_period_report(model).save(image_path, quality=96)
    return str(image_path)


def _build_row(index: int, result) -> NavReportImageRow:
    product = getattr(result, "product", None)
    name = getattr(product, "name", "") or getattr(product, "code", "") or "-"
    code = getattr(product, "code", "") or "-"
    shares = _optional_decimal(getattr(result, "shares", None))

    if getattr(result, "error", ""):
        return NavReportImageRow(
            index=index,
            name=name,
            code=code,
            shares_text=_format_shares(shares),
            nav_text="查询失败",
            nav_date_text="",
            change_text="--",
            change_pct_text=str(getattr(result, "error", ""))[:20],
            income_text="--",
        )

    record, previous = _select_records(result)
    if not record:
        return NavReportImageRow(
            index=index,
            name=name,
            code=code,
            shares_text=_format_shares(shares),
            nav_text="暂无净值",
            nav_date_text="",
            change_text="--",
            change_pct_text="",
            income_text="--",
        )

    change_text = "--"
    change_pct_text = ""
    income_text = "--"
    change_positive = None
    income_positive = None
    if previous:
        delta = _decimal(record.unit_nav) - _decimal(previous.unit_nav)
        change_positive = delta > 0
        change_text = _format_signed_decimal(delta)
        if _decimal(previous.unit_nav) != 0:
            pct = delta / _decimal(previous.unit_nav) * Decimal("100")
            change_pct_text = _format_signed_pct(pct)
        else:
            change_pct_text = "无法计算"

        if shares is not None:
            income = _quantize_money(delta * shares)
            income_positive = income > 0
            income_text = f"{_format_money(income)} 元"

    return NavReportImageRow(
        index=index,
        name=name,
        code=code,
        shares_text=_format_shares(shares),
        nav_text=_format_nav(record.unit_nav),
        nav_date_text=f"{record.nav_date:%Y-%m-%d}",
        change_text=change_text,
        change_pct_text=change_pct_text,
        income_text=income_text,
        change_positive=change_positive,
        income_positive=income_positive,
    )


def _build_period_row(index: int, result) -> NavPeriodReportImageRow:
    product = getattr(result, "product", None)
    name = getattr(product, "name", "") or getattr(product, "code", "") or "-"
    code = getattr(product, "code", "") or "-"
    shares = _optional_decimal(getattr(result, "shares", None))

    latest = getattr(result, "latest", None)
    baseline = getattr(result, "baseline", None)
    if getattr(result, "error", ""):
        return NavPeriodReportImageRow(
            index=index,
            name=name,
            code=code,
            shares_text=_format_shares(shares),
            start_nav_text="统计失败",
            start_date_text=str(getattr(result, "error", ""))[:16],
            end_nav_text="--",
            end_date_text="",
            change_text="--",
            change_pct_text="",
            income_text="--",
        )

    if not latest:
        return NavPeriodReportImageRow(
            index=index,
            name=name,
            code=code,
            shares_text=_format_shares(shares),
            start_nav_text="暂无净值",
            start_date_text="",
            end_nav_text="--",
            end_date_text="",
            change_text="--",
            change_pct_text="",
            income_text="--",
        )

    if not baseline:
        return NavPeriodReportImageRow(
            index=index,
            name=name,
            code=code,
            shares_text=_format_shares(shares),
            start_nav_text="数据不足",
            start_date_text="",
            end_nav_text=_format_nav(latest.unit_nav),
            end_date_text=f"{latest.nav_date:%Y-%m-%d}",
            change_text="--",
            change_pct_text="",
            income_text="--",
        )

    delta = _decimal(latest.unit_nav) - _decimal(baseline.unit_nav)
    change_positive = delta > 0
    pct_text = "无法计算"
    if _decimal(baseline.unit_nav) != 0:
        pct_text = _format_signed_pct(delta / _decimal(baseline.unit_nav) * Decimal("100"))

    income_text = "--"
    income_positive = None
    if shares is not None:
        income = _quantize_money(delta * shares)
        income_positive = income > 0
        income_text = f"{_format_money(income)} 元"

    return NavPeriodReportImageRow(
        index=index,
        name=name,
        code=code,
        shares_text=_format_shares(shares),
        start_nav_text=_format_nav(baseline.unit_nav),
        start_date_text=f"{baseline.nav_date:%Y-%m-%d}",
        end_nav_text=_format_nav(latest.unit_nav),
        end_date_text=f"{latest.nav_date:%Y-%m-%d}",
        change_text=_format_signed_decimal(delta),
        change_pct_text=pct_text,
        income_text=income_text,
        change_positive=change_positive,
        income_positive=income_positive,
    )


def _select_records(result):
    query = getattr(result, "query_result", None)
    if query:
        return getattr(query, "record", None), getattr(query, "previous", None)
    return getattr(result, "latest", None), getattr(result, "previous", None)


def _result_estimated_amount(result) -> Optional[Decimal]:
    shares = _optional_decimal(getattr(result, "shares", None))
    if shares is None:
        return None
    record, previous = _select_records(result)
    if not record or not previous:
        return None
    return (_decimal(record.unit_nav) - _decimal(previous.unit_nav)) * shares


def _period_result_estimated_amount(result) -> Optional[Decimal]:
    shares = _optional_decimal(getattr(result, "shares", None))
    latest = getattr(result, "latest", None)
    baseline = getattr(result, "baseline", None)
    if shares is None or not latest or not baseline:
        return None
    return (_decimal(latest.unit_nav) - _decimal(baseline.unit_nav)) * shares


def _draw_report(model: NavReportImageModel) -> Image.Image:
    height = max(
        MIN_IMAGE_HEIGHT,
        TABLE_TOP + TABLE_HEADER_HEIGHT + len(model.rows) * TABLE_ROW_HEIGHT + 70,
    )
    palette = _palette(model.weather)
    image = Image.new("RGBA", (IMAGE_WIDTH, height))
    _draw_gradient(image, palette["bg_top"], palette["bg_bottom"])
    _draw_weather(image, model.weather)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 22, IMAGE_WIDTH - 24, height - 22),
        radius=26,
        fill=palette["card"],
        outline=palette["border"],
        width=1,
    )
    _draw_weather(image, model.weather)
    draw = ImageDraw.Draw(image)

    title_font = _font(38, bold=True)
    draw.text((54, 48), model.title, font=title_font, fill=palette["title"])
    chip_x = 54 + _text_width(draw, model.title, title_font) + 28
    draw.rounded_rectangle(
        (chip_x, 54, chip_x + 326, 98),
        radius=22,
        fill=palette["chip"],
        outline=palette["chip_border"],
    )
    draw.text((chip_x + 22, 67), "预估总收益", font=_font(15), fill=palette["muted"])
    total_color = palette["up"] if model.total_amount is not None and model.total_amount >= 0 else palette["down"]
    draw.text((chip_x + 136, 59), model.total_text, font=_font(25, bold=True), fill=total_color)
    draw.text((56, 120), model.query_line, font=_font(17), fill=palette["muted"])

    _draw_table(draw, model.rows, 52, TABLE_TOP, IMAGE_WIDTH - 52, TABLE_ROW_HEIGHT, palette)
    return image.convert("RGB")


def _draw_period_report(model: NavPeriodReportImageModel) -> Image.Image:
    height = max(
        MIN_IMAGE_HEIGHT,
        TABLE_TOP + TABLE_HEADER_HEIGHT + len(model.rows) * TABLE_ROW_HEIGHT + 70,
    )
    palette = _palette(model.weather)
    image = Image.new("RGBA", (IMAGE_WIDTH, height))
    _draw_gradient(image, palette["bg_top"], palette["bg_bottom"])
    _draw_weather(image, model.weather)

    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle(
        (24, 22, IMAGE_WIDTH - 24, height - 22),
        radius=26,
        fill=palette["card"],
        outline=palette["border"],
        width=1,
    )
    _draw_weather(image, model.weather)
    draw = ImageDraw.Draw(image)

    title_font = _font(38, bold=True)
    draw.text((54, 48), model.title, font=title_font, fill=palette["title"])
    chip_x = 54 + _text_width(draw, model.title, title_font) + 28
    draw.rounded_rectangle(
        (chip_x, 54, chip_x + 326, 98),
        radius=22,
        fill=palette["chip"],
        outline=palette["chip_border"],
    )
    draw.text((chip_x + 22, 67), "预估总收益", font=_font(15), fill=palette["muted"])
    total_color = palette["up"] if model.total_amount is not None and model.total_amount >= 0 else palette["down"]
    draw.text((chip_x + 136, 59), model.total_text, font=_font(25, bold=True), fill=total_color)
    draw.text((56, 120), model.query_line, font=_font(17), fill=palette["muted"])

    _draw_period_table(draw, model.rows, 52, TABLE_TOP, IMAGE_WIDTH - 52, TABLE_ROW_HEIGHT, palette)
    return image.convert("RGB")


def _draw_table(draw, rows, left, top, right, row_height, palette):
    cols = {
        "seq": (left + 28, left + 78),
        "product": (left + 98, left + 488),
        "code": (left + 488, left + 618),
        "share": (left + 628, left + 760),
        "nav": (left + 772, left + 920),
        "change": (left + 948, left + 1108),
        "income": (left + 1118, right - 22),
    }
    draw.rounded_rectangle((left, top, right, top + TABLE_HEADER_HEIGHT), radius=10, fill=palette["header"])
    for label, key in [
        ("序", "seq"),
        ("产品", "product"),
        ("代码", "code"),
        ("份额", "share"),
        ("净值", "nav"),
        ("涨跌", "change"),
        ("收益", "income"),
    ]:
        x1, x2 = cols[key]
        _center_text(draw, label, (x1 + x2) / 2, top + 10, _font(18, bold=True), palette["muted_dark"])

    y = top + TABLE_HEADER_HEIGHT
    for row in rows:
        draw.rectangle((left, y, right, y + row_height), fill=palette["row_even"] if row.index % 2 else palette["row_odd"])
        draw.rounded_rectangle((cols["seq"][0] + 4, y + 20, cols["seq"][0] + 34, y + 50), radius=11, fill=palette["badge"])
        _center_text(draw, str(row.index), cols["seq"][0] + 19, y + 23, _font(14, bold=True), palette["accent"])
        draw.text(
            (cols["product"][0], y + 18),
            _fit_text(draw, row.name, cols["product"][1] - cols["product"][0] - 8, _font(18, bold=True)),
            font=_font(18, bold=True),
            fill=palette["text"],
        )
        _center_text(draw, row.code, sum(cols["code"]) / 2, y + 19, _font(17, bold=True), palette["text"])
        _center_text(draw, row.shares_text, sum(cols["share"]) / 2, y + 19, _font(17), palette["text"])
        _center_text(draw, row.nav_text, sum(cols["nav"]) / 2, y + 12, _font(20, bold=True), palette["text"])
        _center_text(draw, row.nav_date_text, sum(cols["nav"]) / 2, y + 39, _font(13), palette["muted"])
        change_color = _state_color(row.change_positive, palette)
        _center_text(draw, row.change_text, sum(cols["change"]) / 2, y + 10, _font(19, bold=True), change_color)
        _center_text(draw, row.change_pct_text, sum(cols["change"]) / 2, y + 39, _font(13), change_color)
        income_color = _state_color(row.income_positive, palette)
        _center_text(draw, row.income_text, sum(cols["income"]) / 2, y + 21, _font(19, bold=True), income_color)
        y += row_height
    draw.line((left, y, right, y), fill=palette["line"], width=1)


def _draw_period_table(draw, rows, left, top, right, row_height, palette):
    cols = {
        "seq": (left + 26, left + 72),
        "product": (left + 84, left + 444),
        "code": (left + 452, left + 556),
        "share": (left + 564, left + 668),
        "start": (left + 676, left + 800),
        "end": (left + 808, left + 932),
        "change": (left + 946, left + 1096),
        "income": (left + 1108, right - 18),
    }
    draw.rounded_rectangle((left, top, right, top + TABLE_HEADER_HEIGHT), radius=10, fill=palette["header"])
    for label, key in [
        ("序", "seq"),
        ("产品", "product"),
        ("代码", "code"),
        ("份额", "share"),
        ("期初", "start"),
        ("期末", "end"),
        ("涨跌", "change"),
        ("收益", "income"),
    ]:
        x1, x2 = cols[key]
        _center_text(draw, label, (x1 + x2) / 2, top + 10, _font(18, bold=True), palette["muted_dark"])

    y = top + TABLE_HEADER_HEIGHT
    for row in rows:
        draw.rectangle((left, y, right, y + row_height), fill=palette["row_even"] if row.index % 2 else palette["row_odd"])
        draw.rounded_rectangle((cols["seq"][0] + 4, y + 20, cols["seq"][0] + 34, y + 50), radius=11, fill=palette["badge"])
        _center_text(draw, str(row.index), cols["seq"][0] + 19, y + 23, _font(14, bold=True), palette["accent"])
        draw.text(
            (cols["product"][0], y + 18),
            _fit_text(draw, row.name, cols["product"][1] - cols["product"][0] - 8, _font(18, bold=True)),
            font=_font(18, bold=True),
            fill=palette["text"],
        )
        _center_text(draw, row.code, sum(cols["code"]) / 2, y + 19, _font(17, bold=True), palette["text"])
        _center_text(draw, row.shares_text, sum(cols["share"]) / 2, y + 19, _font(17), palette["text"])
        _center_text(draw, row.start_nav_text, sum(cols["start"]) / 2, y + 12, _font(18, bold=True), palette["text"])
        _center_text(draw, row.start_date_text, sum(cols["start"]) / 2, y + 39, _font(12), palette["muted"])
        _center_text(draw, row.end_nav_text, sum(cols["end"]) / 2, y + 12, _font(18, bold=True), palette["text"])
        _center_text(draw, row.end_date_text, sum(cols["end"]) / 2, y + 39, _font(12), palette["muted"])
        change_color = _state_color(row.change_positive, palette)
        _center_text(draw, row.change_text, sum(cols["change"]) / 2, y + 10, _font(19, bold=True), change_color)
        _center_text(draw, row.change_pct_text, sum(cols["change"]) / 2, y + 39, _font(13), change_color)
        income_color = _state_color(row.income_positive, palette)
        _center_text(draw, row.income_text, sum(cols["income"]) / 2, y + 21, _font(19, bold=True), income_color)
        y += row_height
    draw.line((left, y, right, y), fill=palette["line"], width=1)


def _draw_weather(image: Image.Image, weather: str):
    if weather == "rain":
        _draw_rain(image)
    else:
        _draw_sun(image)


def _draw_sun(image: Image.Image):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cx, cy = 1106, 82
    for radius, color in ((176, (255, 221, 124, 82)), (124, (255, 195, 72, 112)), (66, (255, 149, 22, 165))):
        draw.ellipse((cx - radius, cy - radius, cx + radius, cy + radius), fill=color)
    for angle in range(0, 360, 30):
        rad = math.radians(angle)
        draw.line(
            (
                cx + math.cos(rad) * 92,
                cy + math.sin(rad) * 92,
                cx + math.cos(rad) * 190,
                cy + math.sin(rad) * 190,
            ),
            fill=(255, 168, 30, 108),
            width=7,
        )
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.2)))


def _draw_rain(image: Image.Image):
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    cloud = (170, 194, 220, 98)
    light = (221, 235, 248, 118)
    draw.ellipse((870, 2, 1010, 136), fill=cloud)
    draw.ellipse((970, -20, 1150, 146), fill=cloud)
    draw.ellipse((1092, 10, 1272, 150), fill=cloud)
    draw.rounded_rectangle((908, 80, 1284, 182), radius=45, fill=cloud)
    draw.ellipse((954, 42, 1044, 114), fill=light)
    draw.ellipse((1070, 50, 1180, 124), fill=light)
    for index, x in enumerate(range(905, 1260, 34)):
        y = 184 + (index % 3) * 16
        draw.line((x, y, x - 13, y + 34), fill=(86, 137, 194, 86), width=5)
        draw.line((x + 3, y + 2, x - 10, y + 36), fill=(154, 189, 226, 100), width=2)
    image.alpha_composite(layer.filter(ImageFilter.GaussianBlur(0.1)))


def _draw_gradient(image: Image.Image, top, bottom):
    pixels = image.load()
    width, height = image.size
    for y in range(height):
        t = y / max(1, height - 1)
        color = tuple(int(top[i] * (1 - t) + bottom[i] * t) for i in range(3)) + (255,)
        for x in range(width):
            pixels[x, y] = color


def _palette(weather: str):
    positive = weather != "rain"
    return {
        "bg_top": (255, 249, 239) if positive else (239, 248, 255),
        "bg_bottom": (255, 252, 246) if positive else (250, 253, 255),
        "card": (255, 255, 255, 232),
        "border": "#f1c789" if positive else "#c8dff6",
        "text": "#23180e" if positive else "#121a2c",
        "title": "#25170c" if positive else "#17223a",
        "muted": "#8a735e" if positive else "#63708a",
        "muted_dark": "#6d451d" if positive else "#334f75",
        "header": "#fff1d8" if positive else "#eaf3ff",
        "row_even": "#fffaf2" if positive else "#f8fbff",
        "row_odd": "#fff6ea" if positive else "#f3f8fe",
        "badge": "#ffe2b0" if positive else "#dcecff",
        "accent": "#df6617" if positive else "#2b6fe8",
        "up": "#ff5a1f",
        "down": "#009b45",
        "neutral": "#63708a",
        "line": "#ead7bd" if positive else "#cbdcf0",
        "chip": "#fff2df" if positive else "#eafbf2",
        "chip_border": "#ffb36d" if positive else "#a9e4be",
    }


def _state_color(positive: Optional[bool], palette):
    if positive is True:
        return palette["up"]
    if positive is False:
        return palette["down"]
    return palette["neutral"]


@lru_cache(maxsize=64)
def _font(size: int, bold: bool = False):
    for path in _font_candidates(bold):
        if path and Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _font_candidates(bold: bool):
    env_name = "NAV_REPORT_FONT_BOLD" if bold else "NAV_REPORT_FONT"
    env_path = Path.home().joinpath(".fonts", "NotoSansCJKsc-Bold.otf" if bold else "NotoSansCJKsc-Regular.otf")
    candidates = [
        _getenv_path(env_name),
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Bold.otf" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        str(env_path),
        _fontconfig_match("Noto Sans CJK SC:style=Bold" if bold else "Noto Sans CJK SC"),
        _fontconfig_match("WenQuanYi Micro Hei"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    return [candidate for candidate in candidates if candidate]


def _getenv_path(name: str) -> str:
    import os

    return os.environ.get(name, "")


@lru_cache(maxsize=8)
def _fontconfig_match(pattern: str) -> str:
    try:
        proc = subprocess.run(
            ["fc-match", "-f", "%{file}", pattern],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""
    except Exception:
        return ""


def _center_text(draw, text: str, center_x: float, y: int, font_obj, fill):
    draw.text((center_x - _text_width(draw, text, font_obj) / 2, y), text, font=font_obj, fill=fill)


def _fit_text(draw, text: str, max_width: float, font_obj) -> str:
    if _text_width(draw, text, font_obj) <= max_width:
        return text
    while text and _text_width(draw, text + "...", font_obj) > max_width:
        text = text[:-1]
    return text + "..."


def _text_width(draw, text: str, font_obj) -> float:
    try:
        return draw.textlength(text, font=font_obj)
    except Exception:
        return font_obj.getlength(text)


def _optional_decimal(value) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _format_nav(value) -> str:
    return f"{_decimal(value).quantize(Decimal('0.000001'))}"


def _format_signed_decimal(value: Decimal) -> str:
    text = f"{value.quantize(Decimal('0.000001'))}"
    return f"+{text}" if value > 0 else text


def _format_signed_pct(value: Decimal) -> str:
    text = f"{value.quantize(Decimal('0.0001'))}%"
    return f"+{text}" if value > 0 else text


def _format_signed_money(value: Decimal) -> str:
    text = f"{_quantize_money(value)}"
    return f"+{text}" if value > 0 else text


def _format_money(value: Decimal) -> str:
    return f"{_quantize_money(value)}"


def _quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _format_shares(value: Optional[Decimal]) -> str:
    if value is None:
        return "--"
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")
