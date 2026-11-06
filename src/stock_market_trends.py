"""指数当日分时曲线。来源时间统一转换为各市场当地时间。"""
from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo


_ZONES = {'CN': 'Asia/Shanghai', 'HK': 'Asia/Hong_Kong', 'US': 'America/New_York',
          'SGE': 'Asia/Shanghai', 'SHFE': 'Asia/Shanghai'}
_SOURCE_ZONE = ZoneInfo('Asia/Shanghai')


def _price(value):
    try:
        number = Decimal(str(value))
        return format(number, 'f') if number.is_finite() and number > 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _in_session(stamp, market):
    clock = stamp.time()
    if not time(9, 30) <= clock <= time(15 if market == 'CN' else 16):
        return False
    lunch = time(11, 30) if market == 'CN' else time(12)
    return market == 'US' or not lunch < clock < time(13)


def _history_payload(secid, points, pre_close, source, *, closing_update=False):
    market, code = secid.split('.', 1)
    return {'rc': 0, 'data': {'market': market, 'code': code, 'trends': points, 'preClose': pre_close},
            '_trend_source': source, '_closing_update': closing_update}


def tencent_trend_payload(payload, *, code, secid, market):
    """腾讯分钟数据携带自身交易日；不以抓取日期代替报价日期。"""
    data = payload['data'][code]
    history = data['data']
    day = datetime.strptime(history['date'], '%Y%m%d').date()
    quote = data.get('qt', {}).get(code) or []
    pre_close = quote[4] if len(quote) > 4 else None
    points = []
    for row in history.get('data') or []:
        fields = str(row).split()
        if len(fields) < 2:
            continue
        try:
            clock = datetime.strptime(fields[0], '%H%M').time()
            stamp = datetime.combine(day, clock, ZoneInfo(_ZONES[market])).astimezone(_SOURCE_ZONE)
        except ValueError:
            continue
        points.append(stamp.strftime('%Y-%m-%d %H:%M') + ',' + fields[1])
    return _history_payload(secid, points, pre_close, '腾讯分时行情', closing_update=True)


def yahoo_trend_payload(payload, *, symbol, secid):
    """美股分钟价使用源时间戳，统一转为北京时间后交给同一日期校验器。"""
    data = payload['chart']['result'][0]
    if data['meta'].get('symbol') != symbol:
        raise ValueError('intraday trend security mismatch')
    closes = data['indicators']['quote'][0]['close']
    points = []
    for stamp, value in zip(data.get('timestamp') or [], closes):
        if _price(value) is None:
            continue
        moment = datetime.fromtimestamp(stamp, timezone.utc).astimezone(_SOURCE_ZONE)
        points.append(moment.strftime('%Y-%m-%d %H:%M') + ',' + _price(value))
    return _history_payload(secid, points, data['meta'].get('chartPreviousClose'), 'Yahoo Finance 分时行情')


def _inside_segments(stamp, segments):
    for segment in segments or []:
        try:
            start = datetime.fromisoformat(segment['start'])
            end = datetime.fromisoformat(segment['end'])
        except (TypeError, ValueError):
            continue
        if start <= stamp <= end:
            return True
    return False


def parse_intraday_trend(payload, *, secid, market, quote_date, segments=None):
    """trends2 的 f51/f53 列分别为北京时间和该分钟最新价。"""
    if not isinstance(payload, dict) or payload.get('rc') not in (None, 0):
        raise ValueError('invalid intraday trend response')
    data = payload.get('data')
    if not isinstance(data, dict) or f"{data.get('market')}.{data.get('code')}" != secid:
        raise ValueError('intraday trend security mismatch')
    points = {}
    for raw in data.get('trends') or []:
        fields = str(raw).split(',')
        if len(fields) < 2:
            continue
        try:
            stamp = datetime.strptime(fields[0], '%Y-%m-%d %H:%M').replace(tzinfo=_SOURCE_ZONE)
            stamp = stamp.astimezone(ZoneInfo(_ZONES[market]))
        except (ValueError, KeyError):
            continue
        value = _price(fields[1])
        # 港股收市竞价价格可能在16:00后披露，保留真实更新时间及价格。
        closing_update = payload.get('_closing_update') and market in ('CN', 'HK') and time(15 if market == 'CN' else 16) < stamp.time() < time(20)
        if segments:
            accepted = _inside_segments(stamp, segments)
        else:
            accepted = stamp.date().isoformat() == quote_date and (_in_session(stamp, market) or closing_update)
        if not accepted or value is None:
            continue
        points[stamp.isoformat(timespec='seconds')] = value
    if not points:
        raise ValueError('no valid intraday points for quote date')
    return {'quote_date': quote_date, 'pre_close': _price(data.get('preClose')),
            'points': [{'time': stamp, 'price': points[stamp]} for stamp in sorted(points)],
            'source': payload.get('_trend_source') or '东方财富分时行情', 'coverage': 'intraday_history'}


def attach_intraday_trend(quote, payload, previous=None):
    """补历史曲线、合并实时点；历史接口失败时保留同日已有点，不伪造缺失时段。"""
    result = dict(quote, trend=None)
    if not quote.get('available') or not quote.get('quote_date'):
        return result
    day = quote['quote_date']
    old = (previous or {}).get('trend') or {}
    if old.get('quote_date') != day:
        old = {}
    points = {point['time']: point['price'] for point in old.get('points', [])}
    quote_pre_close = None
    try:
        quote_pre_close = _price(Decimal(str(quote['price'])) - Decimal(str(quote['change_points'])))
    except (InvalidOperation, ValueError, TypeError, KeyError):
        pass
    history = {}
    if payload is not None:
        try:
            history = parse_intraday_trend(payload, secid=quote['secid'], market=quote['market_id'], quote_date=day,
                                           segments=quote.get('session_segments'))
            if quote_pre_close and history.get('pre_close'):
                expected = Decimal(quote_pre_close)
                if abs(Decimal(history['pre_close']) - expected) / expected > Decimal('0.001'):
                    history = {}  # 同代码的跨源映射也须校验昨收，避免拼接不同指数。
        except (ValueError, TypeError, KeyError):
            pass
    incoming = history.get('points') or []
    newer = incoming and (not points or incoming[-1]['time'] >= max(points))
    for point in incoming:
        if newer or point['time'] not in points:
            points[point['time']] = point['price']
    try:
        stamp = datetime.fromisoformat(quote['source_updated_at']).astimezone(ZoneInfo(_ZONES[quote['market_id']]))
        stamp = stamp.replace(second=0, microsecond=0)
        key = stamp.isoformat(timespec='seconds')
        value = _price(quote['price'])
        in_session = (_inside_segments(stamp, quote.get('session_segments'))
                      if quote.get('session_segments') else
                      stamp.date().isoformat() == day and _in_session(stamp, quote['market_id']))
        if in_session and value is not None and (not points or key >= max(points)):
            points[key] = value
    except (ValueError, TypeError, KeyError):
        pass
    if not points:
        return result
    pre_close = history.get('pre_close') or old.get('pre_close')
    if pre_close is None:
        pre_close = quote_pre_close
    ordered = sorted(points)[-800:]
    result['trend'] = {'quote_date': day, 'pre_close': pre_close,
                       'points': [{'time': stamp, 'price': points[stamp]} for stamp in ordered],
                       'updated_at': ordered[-1],
                       'source': history.get('source') or old.get('source') or '公开行情采样',
                       'coverage': history.get('coverage') or old.get('coverage') or 'observed_since_collection'}
    return result
