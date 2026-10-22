from datetime import date
from decimal import Decimal

from test_portfolio_transactions import services, seed_product, seed_opening_position, seed_cash, seed_quote
from src.portfolio_models import ProductType, TransactionStatus
from src.portfolio_profit import calculate_latest_profit
from src.portfolio_view import build_portfolio_payload


def test_cross_day_dividend_waits_as_receivable_and_settles_without_new_profit(services):
    repo, service, projector = services
    fund = seed_product(repo, 'fund', ProductType.PUBLIC_FUND)
    seed_opening_position(repo, 'fund', '100')
    seed_cash(repo, 'cash', '1000')
    seed_quote(repo, fund, date(2026, 10, 19), Decimal('1.10'))
    seed_quote(repo, fund, date(2026, 10, 20), Decimal('1.02'))
    tx = service.record_cash_dividend('fund', Decimal('10'), date(2026, 10, 20), 'cross-day',
        destination_cash_product_id='cash', payment_date=date(2026, 10, 22), as_of_date=date(2026, 10, 20))
    assert projector.calculate('cash').total_shares == Decimal('1000')
    assert tx.linked_transaction_id == ''
    assert calculate_latest_profit(repo, fund, date(2026, 10, 20))[1] == Decimal('2')
    assert Decimal(build_portfolio_payload(repo, date(2026, 10, 20))['summary']['total_assets']) == Decimal('1112')
    service.settle_cash_dividends(date(2026, 10, 22))
    assert projector.calculate('cash').total_shares == Decimal('1010')
    assert Decimal(build_portfolio_payload(repo, date(2026, 10, 22))['summary']['total_assets']) == Decimal('1112')
    settled = repo.get_transaction_by_id(tx.id)
    cash = repo.get_transaction_by_id(settled.linked_transaction_id)
    assert cash.trade_date == cash.confirmation_date == date(2026, 10, 22)
    assert service.record_cash_dividend('fund', Decimal('10'), date(2026, 10, 20), 'cross-day',
        destination_cash_product_id='cash', payment_date=date(2026, 10, 22), as_of_date=date(2026, 10, 22)).id == tx.id
    reversed_tx = service.reverse_confirmed(tx.id, 'wrong', 'reverse-cross-day')
    assert projector.calculate('cash').total_shares == Decimal('1000')
    service.reverse_confirmed(reversed_tx.id, 'restore', 'restore-cross-day')
    assert projector.calculate('cash').total_shares == Decimal('1010')

def test_known_event_override_is_atomic_idempotent_and_defaults_to_wallet(services):
    from src.portfolio_dividends import FundDividendService, dividend_records
    from src.portfolio_dividend_provider import FundDividend
    repo, _, projector = services
    seed_product(repo, 'fund', ProductType.PUBLIC_FUND)
    seed_opening_position(repo, 'fund', '100')
    from test_portfolio_transactions import seed_wallet_plus
    seed_wallet_plus(repo, 'wallet-plus', '1000')
    event = FundDividend(date(2026, 10, 20), date(2026, 10, 20), date(2026, 10, 22), Decimal('.08'), 'https://official.test/dividend')
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service = FundDividendService(repo)
    with repo.database.transaction() as conn:
        event_id = service.include_event('fund', event, amount_override=Decimal('7.94'), mode_override='cash', override_source='user-confirmed:7.94', as_of=date(2026,10,22), conn=conn)
        assert service.process_known_event(event_id, date(2026,10,22), conn=conn)
    assert projector.calculate('wallet-plus').total_shares == Decimal('1007.94')
    assert projector.calculate('fund').total_shares == Decimal('100')
    assert not service.process_known_event(event_id, date(2026,10,22))
    data = dividend_records(repo, 'fund')
    assert data['events'][0]['status'] == 'settled'
    assert data['events'][0]['amount'] == '7.94'


def test_discovered_events_use_record_day_rights_and_mark_old_history_display_only(services):
    from dataclasses import replace
    from src.portfolio_dividends import FundDividendService, dividend_records
    from src.portfolio_dividend_provider import FundDividend
    from src.portfolio_models import Transaction, TransactionType
    from test_portfolio_transactions import seed_wallet_plus
    repo, _, projector = services
    fund = seed_product(repo, 'fund', ProductType.PUBLIC_FUND)
    seed_opening_position(repo, 'fund', '100')
    seed_wallet_plus(repo, 'wallet-plus', '1000')
    for key, kind, trade, confirmation, shares in [
        ('same-buy', TransactionType.MANUAL_PURCHASE, date(2026,10,20), date(2026,10,21), '20'),
        ('same-sell', TransactionType.MANUAL_REDEMPTION, date(2026,10,20), date(2026,10,20), '10'),
        ('later-buy', TransactionType.MANUAL_PURCHASE, date(2026,10,21), date(2026,10,22), '40'),
    ]:
        repo.create_transaction(Transaction(key, 'fund', kind, TransactionStatus.CONFIRMED, trade, key,
            amount=Decimal(shares), shares=Decimal(shares), confirmation_nav=Decimal('1'), confirmation_date=confirmation,
            fee_amount=Decimal('0') if kind is TransactionType.MANUAL_PURCHASE else None,
            fee_rate=Decimal('0') if kind is TransactionType.MANUAL_PURCHASE else None))
    projector.rebuild('fund')
    event = FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1')
    old = replace(event, record_date=date(2026,1,20), ex_dividend_date=date(2026,1,20), payment_date=date(2026,1,22))
    class Provider:
        def fetch_dividends(self, product): return [old, event]
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service = FundDividendService(repo, lambda product: Provider())
    service.run(date(2026,10,20))
    records = dividend_records(repo, 'fund')['events']
    assert records[0]['amount'] == '10'
    # 首次持仓之前（2026-01）的历史分红只登记展示。
    old_row = next(e for e in records if e['ex_dividend_date'] == '2026-01-20')
    assert old_row['display_only'] is True
    assert projector.calculate('wallet-plus').total_shares == Decimal('1000')
    repo.database.initialize()
    service.run(date(2026,10,22))
    assert projector.calculate('wallet-plus').total_shares == Decimal('1010')
    assert len([t for t in repo.list_transactions() if t.transaction_type is TransactionType.CASH_DIVIDEND]) == 1


def test_explicit_include_turns_public_history_into_bookable_event(services):
    from src.portfolio_dividends import FundDividend, FundDividendService, dividend_records
    repo, _, _ = services
    seed_product(repo, 'fund', ProductType.PUBLIC_FUND); seed_opening_position(repo, 'fund', '100')
    event = FundDividend(date(2026,1,20), date(2026,1,20), date(2026,1,22), Decimal('.1'), 'https://official.test/old')
    class Provider:
        def fetch_dividends(self, product): return [event]
    service = FundDividendService(repo, lambda product: Provider())
    service.run(date(2026,10,22))
    eid = service.include_event('fund', event)
    row = next(e for e in dividend_records(repo, 'fund')['events'] if e['id'] == eid)
    # 显式核实纳入后恢复为可入账事件，离开“公开历史”展示态。
    assert row['display_only'] is False
    assert row['is_upcoming'] is True


def test_reinvest_waits_for_exact_nav_and_uses_official_confirmation_date(services):
    from src.portfolio_dividends import FundDividendService, dividend_records
    from src.portfolio_dividend_provider import FundDividend
    repo, _, projector = services
    fund = seed_product(repo, 'fund', ProductType.PUBLIC_FUND)
    seed_opening_position(repo, 'fund', '100')
    event = FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1',
        reinvest_nav_date=date(2026,10,20), reinvest_confirmation_date=date(2026,10,21), reinvest_rule_source='https://official.test/announcement')
    service=FundDividendService(repo)
    eid=service.include_event('fund', event, mode_override='reinvest', as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,21))
    assert projector.calculate('fund').total_shares == Decimal('100')
    assert dividend_records(repo,'fund')['events'][0]['status']=='announced'
    seed_quote(repo,fund,date(2026,10,20),Decimal('1.25'))
    service.process_known_event(eid,date(2026,10,21))
    assert projector.calculate('fund').total_shares==Decimal('108')
    assert dividend_records(repo,'fund')['events'][0]['status']=='settled'


def test_adjustment_conflict_blocks_without_posting_money(services):
    from src.portfolio_dividends import FundDividendService, dividend_records
    from src.portfolio_dividend_provider import FundDividend
    from src.portfolio_models import Transaction, TransactionType
    repo, _, projector = services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND)
    seed_opening_position(repo,'fund','100')
    repo.create_transaction(Transaction('adjust','fund',TransactionType.LATEST_PROFIT_ADJUSTMENT,TransactionStatus.CONFIRMED,date(2026,10,20),'adjust',amount=Decimal('10'),shares=Decimal('0'),confirmation_date=date(2026,10,20)))
    event=FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1')
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',event,as_of=date(2026,10,20))
    assert not service.process_known_event(eid,date(2026,10,22))
    assert dividend_records(repo,'fund')['events'][0]['status']=='blocked'
    assert len(repo.list_transactions())==2

def test_dividend_without_ex_date_nav_does_not_claim_gross_cash_as_net_daily_income(services):
    from src.portfolio_profit import product_daily_profit_series
    repo, service, _ = services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND)
    seed_opening_position(repo,'fund','100')
    seed_quote(repo,fund,date(2026,10,16),Decimal('1.1'))
    service.record_cash_dividend('fund',Decimal('10'),date(2026,10,18),'sunday')
    assert calculate_latest_profit(repo,fund,date(2026,10,18))==(date(2026,10,16),None)
    assert product_daily_profit_series(repo,fund,date(2026,10,18))==[]


def test_fund_payload_exposes_mode_and_pending_count_only_on_funds(services):
    from src.portfolio_dividends import FundDividendService
    from src.portfolio_dividend_provider import FundDividend
    repo, _, _=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND)
    seed_cash(repo,'cash','100')
    service=FundDividendService(repo)
    service.include_event('fund',FundDividend(date(2026,10,24),date(2026,10,24),date(2026,10,26),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,22))
    rows={r['id']:r for r in build_portfolio_payload(repo,date(2026,10,22))['products']}
    assert rows['fund']['dividend_mode']=='cash'
    assert rows['fund']['dividend_upcoming_count']==1
    assert 'dividend_mode' not in rows['cash']


def test_cycle_automatically_processes_dividends_before_income():
    from datetime import datetime
    from src.portfolio_jobs import PortfolioJobs
    calls=[]
    jobs=PortfolioJobs(lambda day:0,lambda day:0,lambda day:0,lambda day:calls.append('income'),
        process_dividends=lambda day:calls.append('dividends') or 1)
    result=jobs.run_cycle(datetime(2026,10,22,12))
    assert calls==['dividends','income']
    assert result.dividends_processed==1


def test_real_source_table_structures_do_not_confuse_per_ten_and_per_share():
    from src.portfolio_dividend_provider import parse_csfunds_dividends,parse_eastmoney_dividends
    official='<div id="fenhong"><table><tr><td>2026.10.20</td><td>0.0082元/份</td><td>2026.10.20</td><td>2026.10.22</td></tr></table></div>'
    eastmoney="<table class='w782 comm cfxq'><tr><td>2026年</td><td>2026-10-20</td><td>2026-10-20</td><td>每10份派现金0.0820元</td><td>2026-10-22</td></tr></table>"
    assert parse_csfunds_dividends(official,'https://official.test')[0].per_share==Decimal('.0082')
    assert parse_eastmoney_dividends(eastmoney,'https://eastmoney.test')[0].per_share==Decimal('.0082')

def test_same_day_automatic_dividend_settles_in_one_atomic_operation(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,20),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,20))
    assert projector.calculate('wallet-plus').total_shares==Decimal('1010')
    assert dividend_records(repo,'fund')['events'][0]['status']=='settled'


def test_receivable_reversal_and_restoration_preserve_assets(services):
    repo,service,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_cash(repo,'cash','1000')
    tx=service.record_cash_dividend('fund',Decimal('10'),date(2026,10,20),'pending-reverse',destination_cash_product_id='cash',payment_date=date(2026,10,22),as_of_date=date(2026,10,20))
    rev=service.reverse_confirmed(tx.id,'wrong','reverse-pending')
    assert Decimal(build_portfolio_payload(repo,date(2026,10,20))['summary']['total_assets'])==Decimal('1000')
    service.reverse_confirmed(rev.id,'restore','restore-pending')
    assert Decimal(build_portfolio_payload(repo,date(2026,10,20))['summary']['total_assets'])==Decimal('1010')


def test_reversed_settled_event_becomes_visible_blocked_without_recredit(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,txservice,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,22))
    tx=repo.get_transaction_by_idempotency(eid)
    txservice.reverse_confirmed(tx.id,'wrong','reverse-settled')
    service.process_known_event(eid,date(2026,10,23))
    assert projector.calculate('wallet-plus').total_shares==Decimal('1000')
    assert dividend_records(repo,'fund')['events'][0]['status']=='blocked'


def test_non_divisible_reinvest_earns_next_day_nav_and_reverses_both_legs(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from src.portfolio_profit import product_daily_profit_series
    repo,txservice,projector=services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    seed_quote(repo,fund,date(2026,10,19),Decimal('1.4'))
    seed_quote(repo,fund,date(2026,10,20),Decimal('1.3'))
    event=FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1',date(2026,10,20),date(2026,10,21),'https://official.test/notice')
    service=FundDividendService(repo)
    eid=service.include_event('fund',event,mode_override='reinvest',as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,20))
    assert Decimal(build_portfolio_payload(repo,date(2026,10,20))['summary']['total_assets'])==Decimal('140')
    service.process_known_event(eid,date(2026,10,21))
    expected=Decimal('100')+Decimal('10')/Decimal('1.3')
    assert projector.calculate('fund').total_shares==expected
    assert abs(Decimal(build_portfolio_payload(repo,date(2026,10,21))['summary']['total_assets'])-Decimal('140'))<Decimal('.000000000001')
    seed_quote(repo,fund,date(2026,10,21),Decimal('1.4'))
    assert calculate_latest_profit(repo,fund,date(2026,10,21))[1]==expected*Decimal('.1')
    tx=repo.get_transaction_by_idempotency(eid)
    reversal=txservice.reverse_confirmed(tx.linked_transaction_id,'wrong','reverse-reinvest')
    assert projector.calculate('fund').total_shares==Decimal('100')
    txservice.reverse_confirmed(reversal.id,'restore','restore-reinvest')
    assert projector.calculate('fund').total_shares==expected
    assert calculate_latest_profit(repo,fund,date(2026,10,21))[1]==expected*Decimal('.1')

def test_receivable_retries_report_no_new_processing_and_lock_registered_mode(services):
    import json
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    event=FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1')
    class Provider:
        def fetch_dividends(self, product): return [event]
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo,lambda product:Provider())
    assert service.run(date(2026,10,20))==1
    with repo.database.transaction() as conn:
        conn.execute('UPDATE products SET metadata_json=? WHERE id=?',(json.dumps({'dividend_mode':'reinvest','unrelated':1}),'fund'))
    assert service.run(date(2026,10,21))==0
    assert dividend_records(repo,'fund')['events'][0]['mode']=='cash'
    service.run(date(2026,10,22))
    assert projector.calculate('wallet-plus').total_shares==Decimal('1010')


def test_contradictory_payment_date_does_not_rewrite_or_settle_receivable(services):
    from dataclasses import replace
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    event=FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1')
    class Provider:
        def fetch_dividends(self, product): return [event]
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo,lambda product:Provider())
    service.run(date(2026,10,20))
    event=replace(event,payment_date=date(2026,10,23))
    service.run(date(2026,10,22))
    row=dividend_records(repo,'fund')['events'][0]
    assert row['payment_date']=='2026-10-22'
    assert '冲突' in row['error']
    assert projector.calculate('wallet-plus').total_shares==Decimal('1000')


def test_source_failure_keeps_known_dividend_and_settles_on_due_date(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo, repo.require_product('fund'), date(2026,10,20), Decimal('1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,20))
    class BrokenProvider:
        def fetch_dividends(self,product): raise ValueError('upstream unavailable')
    service=FundDividendService(repo,lambda product:BrokenProvider())
    assert service.run(date(2026,10,22))==1
    assert projector.calculate('wallet-plus').total_shares==Decimal('1010')
    assert 'upstream unavailable' in dividend_records(repo,'fund')['error']


def test_announcements_before_first_holding_are_public_history_only(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from src.portfolio_models import TransactionType
    repo,_,_=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    class Provider:
        def fetch_dividends(self,product): return [FundDividend(date(2026,1,20),date(2026,1,20),date(2026,1,22),Decimal('.1'),'https://official.test/old')]
    FundDividendService(repo,lambda product:Provider()).run(date(2026,10,22))
    data=dividend_records(repo,'fund')
    assert len(data['events'])==1
    event=data['events'][0]
    # 首次持仓之前的历史分红只登记展示：不进即将分红/待核实，不入账。
    assert event['display_only'] is True
    assert event['is_upcoming'] is False
    assert event['historical'] is False
    assert not [t for t in repo.list_transactions() if t.transaction_type is TransactionType.CASH_DIVIDEND]
    row=next(r for r in build_portfolio_payload(repo,date(2026,10,22))['products'] if r['id']=='fund')
    assert row['dividend_upcoming_count']==0

def test_historical_announcements_are_visible_but_do_not_keep_red_dot_lit(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    repo,_,_=services
    seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    class Provider:
        def fetch_dividends(self,product): return [FundDividend(date(2026,8,20),date(2026,8,20),date(2026,8,22),Decimal('.1'),'https://official.test/old')]
    assert FundDividendService(repo,lambda product:Provider()).run(date(2026,10,22))==0
    data=dividend_records(repo,'fund')
    assert data['events'][0]['status']=='blocked'
    assert '历史' in data['events'][0]['error']
    row=next(r for r in build_portfolio_payload(repo,date(2026,10,22))['products'] if r['id']=='fund')
    assert row['dividend_upcoming_count']==0

def test_official_announcement_rules_follow_future_dates_and_share_class():
    from src.portfolio_dividend_provider import parse_csfunds_dividend_notice
    text='''下属分级基金的交易代码 003102 003103 015736
    截止基准日下属分级基金的相关指标
    本次下属分级基金分红方案（单位：元/10 份基金份额） 0.0900 0.0700 0.0800
    权益登记日 2030 年 10 月 20 日
    除息日 2030 年 10 月 20 日
    现金红利发放日 2030 年 10 月 22 日
    红利再投资方式下，所得红利将按除息日的基金份额净值折算为基金份额。
    选择红利再投资方式的投资者，再投资所得的基金份额将于 2030 年 10 月 21 日直接计入其基金账户。
    本基金本次收益分配免收手续费。红利再投资所得的基金份额免收再投资费用。
    权益登记日申请申购的基金份额不参与本次收益分配，权益登记日申请赎回的基金份额参与本次收益分配。'''
    result=parse_csfunds_dividend_notice(text,'015736','https://www.csfunds.com.cn/future.pdf')
    assert result.per_share==Decimal('.008')
    assert result.reinvest_nav_date==date(2030,10,20)
    assert result.reinvest_confirmation_date==date(2030,10,21)
    assert parse_csfunds_dividend_notice(text,'003103','https://www.csfunds.com.cn/future.pdf').per_share==Decimal('.007')

def test_public_notice_supports_single_class_and_explicit_reinvest_day():
    from src.portfolio_dividend_provider import parse_csfunds_dividend_notice
    text='''基金主代码 009999
    本次分红方案（单位：元/10 份基金份额） 0.0900
    权益登记日 2030 年 10 月 20 日 除息日 2030 年 10 月 20 日 现金红利发放日 2030 年 10 月 22 日
    所得红利将按除息日的基金份额净值折算为基金份额。
    再投资所得的基金份额将于 2030 年 10 月 21 日直接计入其基金账户。
    红利再投资所得的基金份额免收再投资费用。
    权益登记日申请申购的基金份额不参与本次收益分配，权益登记日申请赎回的基金份额参与本次收益分配。'''
    assert parse_csfunds_dividend_notice(text,'009999','https://pdf.dfcfw.com/notice.pdf').per_share==Decimal('.009')

def test_automatic_dividend_saves_amount_but_waits_for_exact_ex_date_nav(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo,fund,date(2026,10,19),Decimal('1.1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    assert not service.process_known_event(eid,date(2026,10,22))
    row=dividend_records(repo,'fund')['events'][0]
    assert row['amount']=='10' and '净值' in row['error']
    assert projector.calculate('wallet-plus').total_shares==Decimal('1000')
    assert Decimal(build_portfolio_payload(repo,date(2026,10,22))['summary']['total_assets'])==Decimal('1110')
    seed_quote(repo,fund,date(2026,10,20),Decimal('1'))
    assert service.process_known_event(eid,date(2026,10,22))
    assert projector.calculate('wallet-plus').total_shares==Decimal('1010')
    assert calculate_latest_profit(repo,fund,date(2026,10,22))[1]==Decimal('0')


def test_processing_one_event_cannot_settle_another_conflicted_event(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    seed_wallet_plus(repo,'wallet-plus','1000')
    service=FundDividendService(repo)
    ids=[]
    for product_id in ['fund-a','fund-b']:
        fund=seed_product(repo,product_id,ProductType.PUBLIC_FUND); seed_opening_position(repo,product_id,'100')
        seed_quote(repo,fund,date(2026,10,20),Decimal('1'))
        eid=service.include_event(product_id,FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
        service.process_known_event(eid,date(2026,10,20)); ids.append(eid)
    with repo.database.transaction() as conn:
        conn.execute("UPDATE fund_dividend_events SET status='blocked',error='来源分红日期冲突，需核实' WHERE id=?",(ids[1],))
    service.process_known_event(ids[0],date(2026,10,22))
    assert projector.calculate('wallet-plus').total_shares==Decimal('1010')
    assert dividend_records(repo,'fund-b')['events'][0]['status']=='blocked'

def test_confirmed_source_conflict_stops_previously_known_receivable(services):
    from src.portfolio_dividends import FundDividend,FundDividendService,dividend_records
    from src.portfolio_dividend_provider import DividendSourceConflict
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo,fund,date(2026,10,20),Decimal('1'))
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,20))
    class ConflictedProvider:
        def fetch_dividends(self,product): raise DividendSourceConflict('分红表与公告方案冲突')
    assert FundDividendService(repo,lambda product:ConflictedProvider()).run(date(2026,10,22))==0
    assert projector.calculate('wallet-plus').total_shares==Decimal('1000')
    assert dividend_records(repo,'fund')['events'][0]['status']=='blocked'


def test_first_quote_on_ex_date_cannot_claim_dividend_as_net_profit(services):
    repo,service,_=services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100')
    seed_quote(repo,fund,date(2026,10,20),Decimal('1'))
    service.record_cash_dividend('fund',Decimal('10'),date(2026,10,20),'first-quote-dividend')
    assert calculate_latest_profit(repo,fund,date(2026,10,20))==(date(2026,10,20),None)

def test_same_day_ex_nav_priced_redemption_keeps_nav_loss_and_dividend_rights(services):
    from src.portfolio_dividends import FundDividend,FundDividendService
    from src.portfolio_models import Transaction,TransactionType,RedemptionSettlementStatus
    from test_portfolio_transactions import seed_wallet_plus
    repo,_,projector=services
    fund=seed_product(repo,'fund',ProductType.PUBLIC_FUND); seed_opening_position(repo,'fund','100'); seed_wallet_plus(repo,'wallet-plus','1000')
    seed_quote(repo,fund,date(2026,10,19),Decimal('1'))
    before=Decimal(build_portfolio_payload(repo,date(2026,10,19))['summary']['total_assets'])
    seed_quote(repo,fund,date(2026,10,20),Decimal('.9'))
    repo.create_transaction(Transaction('same-day-sale','fund',TransactionType.MANUAL_REDEMPTION,TransactionStatus.CONFIRMED,date(2026,10,20),'same-day-sale',amount=Decimal('9'),shares=Decimal('10'),confirmation_nav=Decimal('.9'),confirmation_date=date(2026,10,20),settlement_status=RedemptionSettlementStatus.PENDING))
    projector.rebuild('fund')
    service=FundDividendService(repo)
    eid=service.include_event('fund',FundDividend(date(2026,10,20),date(2026,10,20),date(2026,10,22),Decimal('.1'),'https://official.test/1'),as_of=date(2026,10,20))
    service.process_known_event(eid,date(2026,10,20))
    assert repo.get_transaction_by_idempotency(eid).amount==Decimal('10')
    assert Decimal(build_portfolio_payload(repo,date(2026,10,20))['summary']['total_assets'])==before
    assert calculate_latest_profit(repo,fund,date(2026,10,20))[1]==Decimal('0')


def test_public_provider_retries_transient_timeouts(monkeypatch):
    import requests
    from src.portfolio_dividend_provider import PublicFundDividendProvider

    monkeypatch.setattr("src.portfolio_dividend_provider.time.sleep", lambda _seconds: None)

    class Response:
        content = "分红".encode("utf-8")
        def raise_for_status(self):
            pass

    class Session:
        def __init__(self):
            self.calls = 0
        def get(self, url, **kwargs):
            self.calls += 1
            if self.calls < 3:
                raise requests.exceptions.ReadTimeout("read timed out")
            return Response()

    session = Session()
    provider = PublicFundDividendProvider(session)
    assert provider._html("https://example.test/page") == "分红"
    assert session.calls == 3


def test_public_provider_raises_after_retry_budget(monkeypatch):
    import pytest
    import requests
    from src.portfolio_dividend_provider import PublicFundDividendProvider

    monkeypatch.setattr("src.portfolio_dividend_provider.time.sleep", lambda _seconds: None)

    class Session:
        def __init__(self):
            self.calls = 0
        def get(self, url, **kwargs):
            self.calls += 1
            raise requests.exceptions.ReadTimeout("read timed out")

    session = Session()
    provider = PublicFundDividendProvider(session)
    with pytest.raises(requests.exceptions.ReadTimeout):
        provider._html("https://example.test/page")
    assert session.calls == 3
