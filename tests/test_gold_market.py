from datetime import datetime
from decimal import Decimal

import pytest

from src.gold_market import (
    gold_slot,
    kline_trend_payload,
    night_trading_date,
    parse_sina_gold_quotes,
    parse_sina_minute_payload,
    quote_row_from_kline,
    session_segments,
    trading_date_of,
)


def trading(day):
    return day.weekday() < 5 and day.isoformat() != "2026-10-01"


def at(text):
    return datetime.fromisoformat(text)


def test_weekday_night_belongs_to_the_next_trading_date_and_friday_night_belongs_to_monday():
    assert night_trading_date(at("2026-10-27").date(), trading).isoformat() == "2026-10-28"
    assert night_trading_date(at("2026-10-23").date(), trading).isoformat() == "2026-10-26"
    assert night_trading_date(at("2026-09-30").date(), trading) is None
    assert night_trading_date(at("2026-10-25").date(), trading) is None


def test_sessions_cover_sge_through_midday_and_shfe_breaks():
    tuesday = at("2026-10-27T20:30:00+08:00")
    assert gold_slot("SGE", tuesday, trading)["active"] is True
    assert gold_slot("SGE", tuesday, trading)["date"] == "2026-10-28"
    assert gold_slot("SHFE", tuesday, trading)["active"] is False
    both = at("2026-10-27T21:30:00+08:00")
    assert gold_slot("SHFE", both, trading)["phase"] == "night"
    assert gold_slot("SHFE", both, trading)["date"] == "2026-10-28"
    assert gold_slot("SGE", at("2026-10-28T12:00:00+08:00"), trading)["active"] is True
    lunch = gold_slot("SHFE", at("2026-10-28T12:00:00+08:00"), trading)
    assert lunch["active"] is False and lunch["phase"] == "lunch"
    assert gold_slot("SHFE", at("2026-10-28T10:20:00+08:00"), trading)["phase"] == "lunch"
    assert gold_slot("SGE", at("2026-10-25T20:00:00+08:00"), trading)["active"] is False
    saturday = gold_slot("SHFE", at("2026-10-24T01:00:00+08:00"), trading)
    assert saturday["active"] is True and saturday["date"] == "2026-10-26"


def test_holiday_eve_has_no_night_and_segments_include_friday_for_monday():
    assert trading_date_of("SHFE", at("2026-09-30T21:30:00+08:00"), trading) is None
    segments = session_segments("SHFE", at("2026-10-26").date(), trading)
    assert segments[0][0] == at("2026-10-23T21:00:00+08:00")
    assert segments[0][1] == at("2026-10-24T02:30:00+08:00")
    assert segments[1][0] == at("2026-10-26T09:00:00+08:00")


def test_sina_quotes_match_the_live_field_positions():
    text = "\n".join([
        'var hq_str_nf_AU0="黄金连续,113000,886.220,888.660,869.620,0.000,869.900,870.100,870.020,0.000,879.260,1,2,149784.000,306260,沪,黄金,2026-11-06,1";',
        'var hq_str_gds_AU9999="867.50,0,866.63,867.50,887.00,866.60,11:46:03,884.09,887.00,254828,1455.00,5.00,2026-11-06,沪金99";',
    ])
    rows = {f"{row['f13']}.{row['f12']}": row for row in parse_sina_gold_quotes(text)["data"]["diff"]}
    assert rows["113.aum"]["f2"] == "870.020"
    assert Decimal(rows["113.aum"]["f4"]) == Decimal("-9.240")
    assert rows["118.AU9999"]["f2"] == "867.50"
    moment = datetime.fromtimestamp(rows["118.AU9999"]["f124"], at("2026-11-06T11:46:03+08:00").tzinfo)
    assert moment.hour == 11 and moment.minute == 46


def test_gold_trend_keeps_the_previous_evening_and_drops_the_lunch_break():
    from src.stock_market_trends import parse_intraday_trend
    from src.gold_market import segment_payload

    segments = segment_payload("SHFE", at("2026-10-28").date(), trading)
    payload = {"rc": 0, "data": {"market": 113, "code": "aum", "preClose": "879", "trends": [
        "2026-10-27 21:01,880", "2026-10-28 09:01,881", "2026-10-28 12:00,882"]}}
    trend = parse_intraday_trend(payload, secid="113.aum", market="SHFE", quote_date="2026-10-28", segments=segments)
    assert [point["price"] for point in trend["points"]] == ["880", "881"]


def test_kline_last_bar_becomes_the_gold_quote():
    row = quote_row_from_kline(
        {"data": {"code": "AU9999", "market": 118, "name": "黄金9999", "prePrice": "884.09",
                  "klines": ["2026-11-05 20:01,887.00", "2026-11-06 13:27,867.80"]}},
        "118.AU9999")
    assert row["f2"] == "867.80"
    assert row["f12"] == "AU9999"
    assert Decimal(row["f4"]) == Decimal("867.80") - Decimal("884.09")
    moment = datetime.fromtimestamp(row["f124"], at("2026-11-06T13:27:00+08:00").tzinfo)
    assert (moment.hour, moment.minute) == (13, 27)
    payload = kline_trend_payload(
        {"data": {"code": "aum", "market": 113, "prePrice": "879.26",
                  "klines": ["2026-11-05 21:01,887.16", "2026-11-06 11:30,870.02"]}},
        "113.aum")
    assert payload["data"]["trends"][0] == "2026-11-05 21:01,887.16"
    assert payload["data"]["preClose"] == "879.26"
    with pytest.raises(ValueError):
        kline_trend_payload({"data": {"code": "aum", "market": 113, "klines": ["2026-11-06 11:30,870.02"]}}, "118.AU9999")
    text = 'var _AU0=([{"d":"2026-11-06 11:30:00","c":"870.020"}]);'
    assert parse_sina_minute_payload(text, "113.aum")["data"]["trends"] == ["2026-11-06 11:30,870.020"]
