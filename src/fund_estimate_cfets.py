"""同一实时曲线方法的历史17:00与盘中输入，不拼接17:15收盘回归曲线。"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json

import requests

from src.beijing_time import TZ_CN
from src.fund_estimate_store import iso_now
from src.fund_estimate_types import FACTOR_NAMES, FACTOR_SET_ID, FactorObservation, decimal_text, ensure_aware
from src.portfolio_confirmation import is_trading_day


CFETS_REALTIME = 'cfets_realtime_mean'
CURVE_URL = 'https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/RtimeYldCurv'
CURVE_TYPES = {'gov': '100101', 'cdb': '230101', 'aaa': '270101'}


class CurveUnavailable(ValueError):
    """transient=True 表示网络/服务端临时故障，可重试；False 表示来源本就没有该时点数据。"""

    def __init__(self, message, *, transient=False):
        super().__init__(message)
        self.transient = transient


def parse_curve(payload: dict, day: date, quote_time: str) -> dict[float, Decimal]:
    data = payload.get('data') or {}
    if data.get('date') != day.isoformat() or data.get('time') != quote_time:
        # 请求指定时点却回退到别的时点：接口以此表达该时点无数据，不是临时故障。
        raise CurveUnavailable('曲线返回的日期/时间与请求不一致')
    points = {}
    for row in data.get('data') or []:
        if not isinstance(row, list) or len(row) < 8:
            continue
        try:
            tenor = float(row[3])
            raw = row[1] if row[1] not in ('', None) else row[6]
            value = Decimal(str(raw))
        except (ValueError, TypeError, InvalidOperation):
            continue
        if value.is_finite():
            if tenor in points and points[tenor] != value:
                # 同期限冲突是服务端坏数据，可恢复，保留重试而不是永久丢这一天。
                raise CurveUnavailable('同一期限出现冲突均值', transient=True)
            points[tenor] = value
    if not points:
        raise CurveUnavailable('曲线暂无有效标准期限点')
    return points


class CfetsCurveSource:
    cache_family = CFETS_REALTIME
    factor_set_id = FACTOR_SET_ID
    factor_names = FACTOR_NAMES
    required_terms = {'gov': (3., 5., 10.), 'cdb': (5.,), 'aaa': (3.,)}

    def __init__(self, store, *, fetcher=None, clock=None, calendar=None, intraday_stale_minutes=15):
        self.store = store
        self.clock = clock or (lambda: datetime.now(TZ_CN))
        self.calendar = calendar or is_trading_day
        self.session = requests.Session() if fetcher is None else None
        self.fetcher = fetcher or self._fetch
        self.intraday_stale_minutes = max(1, int(intraday_stale_minutes))
        self._failed = {}
        self._transient = set()
        self._live_cache = None

    def _fetch(self, kind, day, quote_time):
        response = self.session.get(CURVE_URL, params={
            'lang': 'CN', 'qttndate': day.isoformat() if day else '',
            'qttntime': quote_time or '', 'bondType': CURVE_TYPES[kind],
        }, headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://www.chinamoney.com.cn/chinese/bkcurvrty/'}, timeout=(4, 10))
        response.raise_for_status()
        return response.json()

    def _previous_day(self, day):
        cursor = day - timedelta(days=1)
        for _ in range(32):
            if self.calendar(cursor):
                return cursor
            cursor -= timedelta(days=1)
        raise CurveUnavailable('未找到前一交易日')

    def _levels(self, day, quote_time):
        stamp = iso_now(self.clock())
        cached = self.store.read_curve_levels(self.cache_family, day.isoformat(), quote_time, stamp)
        if cached:
            return cached['levels']
        cache_key = (day, quote_time)
        failed = self._failed.get(cache_key)
        if failed and self.clock() - failed[0] < timedelta(minutes=30):
            # 退避期内沿用上次失败分类：临时故障可继续重试，永久缺失不制造假水位。
            raise CurveUnavailable('该时点曲线暂不可用', transient=failed[1])
        levels = {}
        try:
            for kind in CURVE_TYPES:
                points = parse_curve(self.fetcher(kind, day, quote_time), day, quote_time)
                required = self.required_terms[kind]
                if any(tenor not in points for tenor in required):
                    raise CurveUnavailable('曲线缺少必要标准期限')
                levels[kind] = {str(tenor): decimal_text(points[tenor]) for tenor in required}
        except requests.RequestException as exc:
            self._failed[cache_key] = (self.clock(), True)
            raise CurveUnavailable('未取得完整同口径曲线', transient=True) from exc
        except CurveUnavailable as exc:
            self._failed[cache_key] = (self.clock(), exc.transient)
            raise
        except (ValueError, KeyError, TypeError) as exc:
            self._failed[cache_key] = (self.clock(), True)
            raise CurveUnavailable('未取得完整同口径曲线', transient=True) from exc
        self.store.save_curve_levels(self.cache_family, day.isoformat(), quote_time, levels, iso_now(self.clock()))
        return levels

    def fetch_daily_factors(self, start, end, source_family):
        if source_family != CFETS_REALTIME:
            raise CurveUnavailable('不能将实时曲线改名为其他因素来源')
        now = ensure_aware(self.clock(), TZ_CN)
        last_closed = now.date() if now.time() >= time(17) else now.date() - timedelta(days=1)
        result = []
        self._transient = set()
        day = start
        while day <= min(end, last_closed):
            if self.calendar(day):
                previous = self._previous_day(day)
                try:
                    current = self._levels(day, '17:00')
                    base = self._levels(previous, '17:00')
                except CurveUnavailable as exc:
                    if exc.transient:
                        self._transient.add(day)
                    day += timedelta(days=1)
                    continue
                result.append(self._observation(day, '17:00', previous, current, base))
            day += timedelta(days=1)
        return result

    def take_transient_failures(self):
        """返回并清空最近一次 fetch_daily_factors 中因临时故障缺失的日期。"""
        failed, self._transient = self._transient, set()
        return set(failed)

    def read_intraday_factors(self, now):
        now = ensure_aware(now, TZ_CN)
        slot = now.strftime('%Y-%m-%dT%H:%M')
        if self._live_cache and self._live_cache[0] == slot:
            return self._live_cache[1]
        day = now.date()
        quote_time = min(now.strftime('%H:%M'), '17:00')
        try:
            if not self.calendar(day):
                raise CurveUnavailable('当前非交易日')
            previous = self._previous_day(day)
            base = self._levels(previous, '17:00')
            # 真实接口先读当前可发布时点，然后三个券种都锁定该同一时点。
            if self.session is not None:
                latest = self._fetch('gov', None, None).get('data') or {}
                quote_time = str(latest.get('time') or '')
                if latest.get('date') != day.isoformat() or not quote_time:
                    raise CurveUnavailable('实时曲线日期落后')
            market_time = datetime.combine(day, time.fromisoformat(quote_time), TZ_CN)
            if market_time > now:
                raise CurveUnavailable('实时曲线尚未更新')
            closed = now.time() >= time(17, 0)
            current = self._levels(day, quote_time)
            observation = self._observation(day, quote_time, previous, current, base)
            if not closed and (now - market_time) > timedelta(minutes=self.intraday_stale_minutes):
                # 当日完整曲线即使延迟仍可估算；保留来源时点，读路径明确标注行情陈旧。
                # 不用请求时点替换它：接口可能原样回显尚未发生的时间。
                observation = replace(observation, basis_status='intraday_reference')
            elif closed and quote_time != '17:00':
                # 17:00 最终曲线未发布时保留最后可用时点，并标注为收盘参考而非收盘值。
                observation = replace(observation, basis_status='close_reference')
        except (CurveUnavailable, requests.RequestException, ValueError, TypeError):
            observation = FactorObservation(self.factor_set_id, CFETS_REALTIME, day.isoformat(), None,
                iso_now(self.clock()), None, {name: None for name in self.factor_names}, None, 'missing',
                list(self.factor_names), None, 'missing')
        self._live_cache = (slot, observation)
        return observation

    def _observation(self, day, quote_time, previous, current, base):
        def delta(kind, tenor):
            key = str(float(tenor))
            return (Decimal(current[kind][key]) - Decimal(base[kind][key])) * 100
        gov3, gov5, gov10 = delta('gov', 3), delta('gov', 5), delta('gov', 10)
        values = {'gov_3y_bp': decimal_text(gov3), 'gov_slope_10y_3y_bp': decimal_text(gov10-gov3),
                  'cdb_spread_5y_bp': decimal_text(delta('cdb', 5)-gov5),
                  'credit_aaa_spread_3y_bp': decimal_text(delta('aaa', 3)-gov3),
                  'calendar_days': str((day-previous).days)}
        version = hashlib.sha256(json.dumps([CFETS_REALTIME, day.isoformat(), quote_time, previous.isoformat(), values], sort_keys=True).encode()).hexdigest()[:24]
        stamp = iso_now(self.clock())
        return FactorObservation(FACTOR_SET_ID, CFETS_REALTIME, day.isoformat(), stamp, stamp,
            f'{day.isoformat()}T{quote_time}:00+08:00', values, (day-previous).days,
            'native', [], previous.isoformat(), version)
