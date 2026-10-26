from datetime import datetime

import pytest


def _payload(secid='1.000001', rows=None):
    market, code = secid.split('.', 1)
    return {'rc': 0, 'data': {'market': int(market), 'code': code, 'preClose': 100,
                             'trends': rows or ['2026-10-23 09:30,99,99,99,1,1,99',
                                                '2026-10-23 09:31,101,101,101,1,1,101']}}


def _quote(day='2026-10-23', stamp='2026-10-23T09:31:40+08:00', price='101'):
    return {'secid': '1.000001', 'market_id': 'CN', 'quote_date': day,
            'source_updated_at': stamp, 'price': price, 'change_points': '1', 'available': True}


def test_historical_intraday_points_use_close_and_market_local_dates():
    from src.stock_market_trends import parse_intraday_trend

    payload = _payload('100.DJIA', ['2026-10-24 00:01,99,120,80,1,1,100',
                                  '2026-10-24 00:02,101,130,70,1,1,100'])
    trend = parse_intraday_trend(payload, secid='100.DJIA', market='US', quote_date='2026-10-23')
    assert trend['quote_date'] == '2026-10-23'
    assert trend['points'][0] == {'time': '2026-10-23T12:01:00-04:00', 'price': '99'}
    assert trend['points'][1]['price'] == '101'
    assert trend['pre_close'] == '100'


def test_history_rejects_another_security_or_another_trading_date():
    from src.stock_market_trends import parse_intraday_trend

    with pytest.raises(ValueError):
        parse_intraday_trend(_payload(), secid='0.399001', market='CN', quote_date='2026-10-23')
    with pytest.raises(ValueError):
        parse_intraday_trend(_payload(), secid='1.000001', market='CN', quote_date='2026-10-26')


def test_active_quote_extends_or_updates_curve_and_failure_keeps_earlier_points():
    from src.stock_market_trends import attach_intraday_trend

    first = attach_intraday_trend(_quote(), _payload())
    assert len(first['trend']['points']) == 2
    next_quote = _quote(stamp='2026-10-23T09:32:10+08:00', price='102')
    updated = attach_intraday_trend(next_quote, None, first)
    assert len(updated['trend']['points']) == 3
    assert updated['trend']['points'][-1]['price'] == '102'
    same_minute = attach_intraday_trend(_quote(stamp='2026-10-23T09:32:45+08:00', price='103'), None, updated)
    assert len(same_minute['trend']['points']) == 3
    assert same_minute['trend']['points'][-1]['price'] == '103'


def test_new_trading_day_drops_previous_day_curve():
    from src.stock_market_trends import attach_intraday_trend

    old = attach_intraday_trend(_quote(), _payload())
    new = attach_intraday_trend(_quote(day='2026-10-26', stamp='2026-10-26T09:30:15+08:00'), None, old)
    assert len(new['trend']['points']) == 1
    assert all(point['time'].startswith('2026-10-26') for point in new['trend']['points'])


def test_points_outside_session_and_missing_prices_are_not_drawn_as_zero():
    from src.stock_market_trends import parse_intraday_trend

    payload = _payload(rows=['2026-10-23 09:00,50', '2026-10-23 09:30,99',
                             '2026-10-23 09:31,-', '2026-10-23 12:00,150',
                             '2026-10-23 13:00,101', '2026-10-23 17:00,200'])
    trend = parse_intraday_trend(payload, secid='1.000001', market='CN', quote_date='2026-10-23')
    assert [point['price'] for point in trend['points']] == ['99', '101']


def test_history_from_a_different_previous_close_is_not_spliced_into_quotes():
    from src.stock_market_trends import attach_intraday_trend

    payload = _payload()
    payload['data']['preClose'] = 200
    result = attach_intraday_trend(_quote(), payload)
    assert len(result['trend']['points']) == 1
    assert result['trend']['coverage'] == 'observed_since_collection'
