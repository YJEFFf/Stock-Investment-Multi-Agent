import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from src import kis, order_guard, order_recovery as recovery, portfolio_store as store
from src.schemas import ExitPlan, PortfolioState, Position, UnconfirmedOrder

NOW = datetime(2026, 10, 12, 9, 4, tzinfo=ZoneInfo('Asia/Seoul'))


@pytest.fixture
def waiting(monkeypatch):
    monkeypatch.setenv("SIMA_EXTENDED_ORDER_RECOVERY", "1")
    plan = ExitPlan(stop_loss_pct=-.067715, take_profit_pct=.13543, take_profit_fraction=1/3, trail_pct=-.054172)
    position = Position(ticker='009240', sector='', quantity=183, entry_day=NOW.date(), entry_price=40600,
                        peak_price=41200, weight=.08, exit_plan=plan)
    store.save_portfolio(PortfolioState(positions=[position], cash_weight=.92))
    order_guard._save({'009240': UnconfirmedOrder(ticker='009240', side='buy', quantity=183,
                                                created_at=NOW-timedelta(minutes=2), fills_before=(0,0.,0.))})
    row = dict(event='buy', ticker='009240', day=NOW.date().isoformat(), quantity=183, entry_price=40600,
               fee_amount=40, entry_price_source='fill_partial', order_no='3324', exit_plan_source='volatility',
               exit_plan=plan.model_dump(mode='json'))
    recovery.JOURNAL_PATH.parent.mkdir(parents=True,exist_ok=True)
    recovery.JOURNAL_PATH.write_text(json.dumps(row)+'\n')
    calls=[]
    def fills(ticker,day,side,**kw):
        assert kw['policy'].max_attempts==1
        calls.append(side)
        return (183,7464450.,1050.) if side=='buy' else (0,0.,0.)
    monkeypatch.setattr(kis,'fetch_daily_fill_totals',fills)
    monkeypatch.setattr(kis,'fetch_holdings',lambda **kw:{'009240':(183,40789.344)})
    def forbidden(*a,**k): raise AssertionError('recovery must never submit an order')
    monkeypatch.setattr(kis,'place_market_buy_order',forbidden)
    monkeypatch.setattr(kis,'place_market_sell_order',forbidden)
    return calls


def test_complete_late_fill_persists_before_unblock_and_is_idempotent(waiting, monkeypatch):
    old=store.load_portfolio();clear=order_guard.clear
    def checked_clear(ticker):
        assert store.load_portfolio().positions[0].entry_price==7464450/183
        assert json.loads(recovery.JOURNAL_PATH.read_text())['fee_amount']==1050
        clear(ticker)
    monkeypatch.setattr(order_guard,'clear',checked_clear)
    assert recovery.recover_pending_buy(NOW)
    new=store.load_portfolio();old.positions[0].entry_price=7464450/183
    assert new==old
    row=json.loads(recovery.JOURNAL_PATH.read_text())
    assert row['fill_recovery']['fee_before']==40
    assert row['entry_price_source']=='fill'
    assert not order_guard.load_orders()
    assert not recovery.recover_pending_buy(NOW)
    assert waiting==['buy','sell']


def test_after_close_uses_longer_reads_but_never_retries_orders(waiting, monkeypatch):
    fills = kis.fetch_daily_fill_totals
    policies = []
    def slow_broker(ticker, day, side, **kwargs):
        policies.append(kwargs["policy"])
        if kwargs["policy"].timeout_seconds <= 2:
            return None
        return fills(ticker, day, side, **kwargs)
    monkeypatch.setattr(kis, "fetch_daily_fill_totals", slow_broker)
    assert not recovery.recover_pending_order(NOW)
    assert recovery.recover_after_close(NOW.replace(hour=15, minute=50)) == ["009240"]
    assert policies[0].timeout_seconds == 2
    assert all(p.timeout_seconds == 10 and p.max_attempts == 1 for p in policies[1:])
    assert not order_guard.load_orders()
    assert recovery.recover_after_close(NOW.replace(hour=15, minute=51)) == []


def test_after_close_recovery_rejects_intraday_and_disabled_mode(waiting, monkeypatch):
    with pytest.raises(RuntimeError, match="after 15:40"):
        recovery.recover_after_close(NOW)
    monkeypatch.delenv("SIMA_EXTENDED_ORDER_RECOVERY")
    assert recovery.recover_after_close(NOW.replace(hour=15, minute=50)) == []
    assert waiting == []
    assert order_guard.load_orders()


@pytest.mark.parametrize('problem',['partial','overfill','timeout','sold','quantity','average','nan','old_day','addon','lost_response','missing_journal','changed_marker','changed_journal','changed_position'])
def test_ambiguous_evidence_never_unblocks(waiting,monkeypatch,problem):
    before=store.load_portfolio()
    if problem in ['partial','overfill','timeout','sold','nan']:
        def fills(t,d,side,**kw):
            if problem=='timeout':return None
            if side=='sell':return (1,40000.,10.) if problem=='sold' else (0,0.,0.)
            return {'partial':(7,284200.,40.),'overfill':(184,7500000.,1050.),'nan':(183,float('nan'),1050.)}.get(problem,(183,7464450.,1050.))
        monkeypatch.setattr(kis,'fetch_daily_fill_totals',fills)
    elif problem in ['quantity','average']:
        monkeypatch.setattr(kis,'fetch_holdings',lambda **kw:{'009240':(182 if problem=='quantity' else 183,40600.)})
    elif problem=='old_day':
        orders=order_guard.load_orders();orders['009240'].created_at-=timedelta(days=1);order_guard._save(orders)
    elif problem in ['addon','lost_response']:
        row=json.loads(recovery.JOURNAL_PATH.read_text())
        if problem=='addon':row['exit_plan_source']='existing_position'
        else:row['order_no']=None
        recovery.JOURNAL_PATH.write_text(json.dumps(row)+'\n')
    elif problem=='missing_journal':recovery.JOURNAL_PATH.unlink()
    else:
        def race(**kw):
            if problem=='changed_marker':
                orders=order_guard.load_orders();orders['009240'].quantity=184;order_guard._save(orders)
            elif problem=='changed_journal':recovery.JOURNAL_PATH.write_text(recovery.JOURNAL_PATH.read_text()+'{}\n')
            else:
                state=store.load_portfolio();state.positions[0].weight=.09;store.save_portfolio(state)
            return {'009240':(183,40789.344)}
        monkeypatch.setattr(kis,'fetch_holdings',race)
    assert not recovery.recover_pending_buy(NOW)
    assert order_guard.is_blocked('009240')
    if problem!='changed_position':assert store.load_portfolio()==before


@pytest.mark.parametrize('failure_at',['journal','portfolio','audit_commit','clear'])
def test_crash_retry_keeps_block_until_both_files_are_committed(waiting,monkeypatch,failure_at):
    atomic=recovery._atomic_write;save=store.save_portfolio;clear=order_guard.clear
    def fail(*a,**k):raise OSError('injected crash')
    if failure_at in ['journal','audit_commit']:
        def write(path,data):
            if path==recovery.JOURNAL_PATH and failure_at=='journal' or path.name=='committed.json' and failure_at=='audit_commit':fail()
            atomic(path,data)
        monkeypatch.setattr(recovery,'_atomic_write',write)
    elif failure_at=='portfolio':monkeypatch.setattr(store,'save_portfolio',fail)
    else:monkeypatch.setattr(order_guard,'clear',fail)
    with pytest.raises(OSError):recovery.recover_pending_buy(NOW)
    assert order_guard.is_blocked('009240')
    monkeypatch.setattr(recovery,'_atomic_write',atomic);monkeypatch.setattr(store,'save_portfolio',save);monkeypatch.setattr(order_guard,'clear',clear)
    assert recovery.recover_pending_buy(NOW)
    assert not order_guard.load_orders()
    assert json.loads(recovery.JOURNAL_PATH.read_text())['fill_recovery']['entry_price_before']==40600


def test_quote_updates_during_query_preserved_and_network_outside_lock(waiting,monkeypatch):
    def holding(**kw):
        with store.portfolio_lock(blocking=False):
            state=store.load_portfolio();state.positions[0].peak_price=42000;store.save_portfolio(state)
        return {'009240':(183,40789.344)}
    monkeypatch.setattr(kis,'fetch_holdings',holding)
    assert recovery.recover_pending_buy(NOW)
    assert store.load_portfolio().positions[0].peak_price==42000


def test_sell_marker_not_recovered(waiting):
    orders=order_guard.load_orders();orders['009240'].side='sell';order_guard._save(orders)
    assert not recovery.recover_pending_buy(NOW)
    assert waiting==[]


def test_minute_job_recovers_after_saving_and_next_evaluation_uses_correct_price(waiting, monkeypatch):
    from scripts import check_stop_loss as csl
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(csl,'datetime',Clock)
    monkeypatch.setattr(csl,'is_krx_trading_day',lambda d:True)
    seen=[]
    async def evaluate(portfolio,*args):
        seen.append((portfolio.positions[0].entry_price,order_guard.is_blocked('009240')))
        return portfolio
    monkeypatch.setattr(csl.pipeline,'evaluate_holdings',evaluate)
    import asyncio
    asyncio.run(csl.main());asyncio.run(csl.main())
    assert seen==[(40600,True),(7464450/183,False)]


def test_busy_commit_leaves_order_blocked(waiting,monkeypatch):
    from contextlib import contextmanager
    original=store.portfolio_lock
    @contextmanager
    def lock(*a,**kw):
        raise store.PortfolioLockBusy('injected writer')
        yield
    def holding(**kw):
        monkeypatch.setattr(store,'portfolio_lock',lock)
        return {'009240':(183,40789.344)}
    monkeypatch.setattr(kis,'fetch_holdings',holding)
    assert not recovery.recover_pending_buy(NOW)
    assert order_guard.is_blocked('009240')
    monkeypatch.setattr(store,'portfolio_lock',original)


@pytest.mark.parametrize('missing_fee',[False,True])
def test_broker_parser_requires_fee_and_propagates_bounded_policy(waiting, monkeypatch, missing_fee):
    # 원본 파서까지 통과시켜 단위 테스트의 fetch_* 모킹 경계 아래도 검증한다.
    import importlib.util
    spec=importlib.util.spec_from_file_location('recovery_kis_parser',kis.__file__)
    module=importlib.util.module_from_spec(spec)
    import sys
    monkeypatch.setitem(sys.modules,spec.name,module);spec.loader.exec_module(module)
    calls=[]
    def get(path,tr,params,*,policy):
        assert policy is recovery.QUERY_POLICY
        calls.append(path)
        if path==kis.BALANCE_PATH:
            return {'output1':[{'pdno':'009240','hldg_qty':'183','pchs_avg_pric':'40789.344'}]}
        buy=params['SLL_BUY_DVSN_CD']=='02'
        output={'tot_ccld_qty':'183' if buy else '0','tot_ccld_amt':'7464450' if buy else '0','prsm_tlex_smtl':'1050' if buy else '0'}
        if missing_fee:output.pop('prsm_tlex_smtl')
        return {'output2':output}
    monkeypatch.setattr(module,'_kis_get',get)
    monkeypatch.setattr(kis,'fetch_daily_fill_totals',module.fetch_daily_fill_totals)
    monkeypatch.setattr(kis,'fetch_holdings',module.fetch_holdings)
    assert recovery.recover_pending_buy(NOW) is not missing_fee
    assert order_guard.is_blocked('009240') is missing_fee
    assert len(calls)==(1 if missing_fee else 3)


@pytest.mark.parametrize('problem',['prior_holding','duplicate_row','ready'])
def test_unattributable_or_committed_orders_are_left_alone(waiting,problem):
    if problem=='ready':
        orders=order_guard.load_orders();orders['009240'].ready_to_commit=True;order_guard._save(orders)
    elif problem=='duplicate_row':recovery.JOURNAL_PATH.write_text(recovery.JOURNAL_PATH.read_text()*2)
    else:
        prior={'event':'buy','ticker':'009240','day':'2026-10-08','quantity':1}
        recovery.JOURNAL_PATH.write_text(json.dumps(prior)+'\n'+recovery.JOURNAL_PATH.read_text())
    assert not recovery.recover_pending_buy(NOW)
    assert waiting==[]
    assert order_guard.is_blocked('009240')


@pytest.mark.parametrize('source', ['fill_partial', 'quote_fallback', 'daily_avg', 'position_avg'])
def test_buy_recovery_survives_next_day_and_all_unverified_sources(waiting, source, monkeypatch):
    row = json.loads(recovery.JOURNAL_PATH.read_text())
    row['entry_price_source'] = source
    recovery.JOURNAL_PATH.write_text(json.dumps(row)+'\n')
    original = kis.fetch_daily_fill_totals
    def query(ticker, day, side, **kw):
        assert day == NOW.date()  # 현재 날짜가 아닌 주문 날짜의 원장
        return original(ticker, day, side, **kw)
    monkeypatch.setattr(kis, 'fetch_daily_fill_totals', query)
    assert recovery.recover_pending_order(NOW + timedelta(days=1))
    assert not order_guard.load_orders()


def test_later_transaction_prevents_old_buy_recovery(waiting):
    with recovery.JOURNAL_PATH.open('a') as f:
        f.write(json.dumps(dict(event='sell',ticker='009240',day='2026-10-13',shares_sold=1))+'\n')
    assert not recovery.recover_pending_order(NOW + timedelta(days=1))
    assert order_guard.is_blocked('009240')


@pytest.fixture
def waiting_sell(waiting, monkeypatch):
    buy = json.loads(recovery.JOURNAL_PATH.read_text())
    buy.update(day='2026-10-09', entry_price_source='fill', quantity=183)
    row = dict(event='sell',ticker='009240',day=NOW.date().isoformat(),shares_before=183,
               shares_sold=183,shares_after=0,portfolio_weight_after=0,entry_price=40600,
               exit_price=38000,exit_price_source='fill_partial',sell_amount=6954000,fee_amount=900)
    recovery.JOURNAL_PATH.write_text(json.dumps(buy)+'\n'+json.dumps(row)+'\n')
    orders=order_guard.load_orders();orders['009240'].side='sell';order_guard._save(orders)
    store.save_portfolio(PortfolioState())
    def fills(t,d,side,**kw):
        assert d==NOW.date()
        assert kw['require_fee']
        return (183,6960000.,980.) if side=='sell' else (0,0.,0.)
    monkeypatch.setattr(kis,'fetch_daily_fill_totals',fills)
    monkeypatch.setattr(kis,'fetch_holdings',lambda **kw:{})


def test_full_sell_recovery_persists_actual_amount_costs_before_release(waiting_sell, monkeypatch):
    state=store.load_portfolio()
    clear=order_guard.clear
    def checked(ticker):
        row=json.loads(recovery.JOURNAL_PATH.read_text().splitlines()[-1])
        assert row['sell_amount']==6960000
        assert row['fee_amount']==980
        assert row['fee_source']=='fill'
        assert row['sell_amount_source']=='fill'
        assert row['exit_price']==6960000/183
        assert row['net_pnl_amount'] < (6960000 - 40600*183)
        clear(ticker)
    monkeypatch.setattr(order_guard,'clear',checked)
    assert recovery.recover_pending_order(NOW+timedelta(days=1))
    assert store.load_portfolio()==state
    assert not recovery.recover_pending_order(NOW+timedelta(days=1))


@pytest.mark.parametrize('problem', ['partial','overfill','missing_fee','holding','local_holding','buy_same_day',
                                    'missing_prior','later_trade','duplicate','unaccepted_quote','changed_marker','changed_journal'])
def test_sell_ambiguity_never_releases(waiting_sell, monkeypatch, problem):
    rows=[json.loads(l) for l in recovery.JOURNAL_PATH.read_text().splitlines()]
    if problem in ('partial','overfill','missing_fee','buy_same_day'):
        def fills(t,d,side,**kw):
            if side=='buy':return (1,40000.,10.) if problem=='buy_same_day' else (0,0.,0.)
            if problem=='missing_fee':return None
            return (182 if problem=='partial' else 184 if problem=='overfill' else 183,6960000.,980.)
        monkeypatch.setattr(kis,'fetch_daily_fill_totals',fills)
    elif problem=='holding':monkeypatch.setattr(kis,'fetch_holdings',lambda **kw:{'009240':(1,40600.)})
    elif problem=='local_holding':
        store.save_portfolio(PortfolioState(positions=[Position(ticker='009240',sector='',weight=.01,quantity=1)]))
    elif problem=='missing_prior':rows=rows[1:]
    elif problem=='later_trade':rows.append(dict(event='buy',ticker='009240',day='2026-10-13',quantity=1))
    elif problem=='duplicate':rows.append(rows[-1])
    elif problem=='unaccepted_quote':rows[-1]['exit_price_source']='quote_fallback'
    elif problem in ('changed_marker','changed_journal'):
        def race(**kw):
            if problem=='changed_marker':
                orders=order_guard.load_orders();orders['009240'].quantity=1;order_guard._save(orders)
            else:
                with recovery.JOURNAL_PATH.open('a') as f:f.write('{}\n')
            return {}
        monkeypatch.setattr(kis,'fetch_holdings',race)
    recovery.JOURNAL_PATH.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    assert not recovery.recover_pending_order(NOW+timedelta(days=1))
    assert order_guard.is_blocked('009240')


def test_accepted_quote_sell_can_recover(waiting_sell):
    rows=[json.loads(l) for l in recovery.JOURNAL_PATH.read_text().splitlines()]
    rows[-1]['exit_price_source']='quote_fallback'
    recovery.JOURNAL_PATH.write_text(''.join(json.dumps(r)+'\n' for r in rows))
    order_guard.mark_accepted('009240','123')
    assert recovery.recover_pending_order(NOW)


@pytest.mark.parametrize('failure_at',['journal','audit_commit','clear'])
def test_sell_recovery_crash_resumes_without_unblocking_early(waiting_sell,monkeypatch,failure_at):
    atomic=recovery._atomic_write;clear=order_guard.clear
    def fail(*a,**k):raise OSError('injected crash')
    def write(path,data):
        if (path==recovery.JOURNAL_PATH and failure_at=='journal') or (path.name=='committed.json' and failure_at=='audit_commit'):fail()
        atomic(path,data)
    monkeypatch.setattr(recovery,'_atomic_write',write)
    if failure_at=='clear':monkeypatch.setattr(order_guard,'clear',fail)
    with pytest.raises(OSError):recovery.recover_pending_order(NOW)
    assert order_guard.is_blocked('009240')
    monkeypatch.setattr(recovery,'_atomic_write',atomic);monkeypatch.setattr(order_guard,'clear',clear)
    assert recovery.recover_pending_order(NOW+timedelta(days=1))


def test_minute_job_recovers_sell_even_with_no_positions(waiting_sell,monkeypatch):
    from scripts import check_stop_loss as csl
    import asyncio
    class Clock(datetime):
        @classmethod
        def now(cls,tz=None):return NOW
    monkeypatch.setattr(csl,'datetime',Clock)
    monkeypatch.setattr(csl,'is_krx_trading_day',lambda d:True)
    asyncio.run(csl.main())
    assert not order_guard.load_orders()


def test_unknown_historical_quantity_cannot_be_assumed_zero(waiting):
    original=recovery.JOURNAL_PATH.read_text()
    recovery.JOURNAL_PATH.write_text(json.dumps(dict(event='buy',ticker='009240',day='2026-10-01'))+'\n'+original)
    assert not recovery.recover_pending_order(NOW)
    assert order_guard.is_blocked('009240')


def test_extended_recovery_requires_explicit_activation(waiting_sell, monkeypatch):
    monkeypatch.delenv("SIMA_EXTENDED_ORDER_RECOVERY")
    assert not recovery.recover_pending_order(NOW)
    assert order_guard.is_blocked('009240')


def test_legacy_mode_does_not_release_quote_fallback(waiting, monkeypatch):
    row=json.loads(recovery.JOURNAL_PATH.read_text());row['entry_price_source']='quote_fallback'
    recovery.JOURNAL_PATH.write_text(json.dumps(row)+'\n')
    monkeypatch.delenv("SIMA_EXTENDED_ORDER_RECOVERY")
    assert not recovery.recover_pending_order(NOW)
    assert order_guard.is_blocked('009240')


@pytest.mark.parametrize('side', ['buy', 'sell'])
def test_recovery_alert_only_after_commit_and_release(side, request, monkeypatch):
    request.getfixturevalue('waiting_sell' if side == 'sell' else 'waiting')
    messages = []
    def enqueue(message):
        assert not order_guard.load_orders()
        row = json.loads(recovery.JOURNAL_PATH.read_text().splitlines()[-1])
        assert row['entry_price_source' if side == 'buy' else 'exit_price_source'] == 'fill'
        messages.append(message)
        return True
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', enqueue)
    assert recovery.recover_pending_order(NOW)
    assert not recovery.recover_pending_order(NOW + timedelta(minutes=1))
    assert len(messages) == 1
    assert '체결 확인 복구 완료' in messages[0]
    assert '183주' in messages[0]


def test_failed_notification_retries_without_orders_or_broker_reads(waiting_sell, monkeypatch):
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', lambda message: False)
    assert recovery.recover_pending_order(NOW)
    assert not order_guard.load_orders()
    def forbidden(*args, **kwargs):
        raise AssertionError('notification retry must not query broker or resubmit orders')
    monkeypatch.setattr(kis, 'fetch_daily_fill_totals', forbidden)
    monkeypatch.setattr(kis, 'fetch_holdings', forbidden)
    messages = []
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', lambda message: messages.append(message) or True)
    assert not recovery.recover_pending_order(NOW + timedelta(minutes=1))
    assert not recovery.recover_pending_order(NOW + timedelta(minutes=2))
    assert len(messages) == 1


def test_recovery_alert_waits_for_release_after_crash(waiting_sell, monkeypatch):
    clear = order_guard.clear
    def crash(ticker):
        raise OSError('crash before release')
    messages = []
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', lambda message: messages.append(message) or True)
    monkeypatch.setattr(order_guard, 'clear', crash)
    with pytest.raises(OSError):
        recovery.recover_pending_order(NOW)
    recovery._flush_recovery_alerts()
    assert messages == []
    monkeypatch.setattr(order_guard, 'clear', clear)
    assert recovery.recover_pending_order(NOW + timedelta(minutes=1))
    assert len(messages) == 1


def test_incomplete_sell_never_announces_recovery(waiting_sell, monkeypatch):
    messages = []
    monkeypatch.setattr(recovery.notify, 'send_telegram_alert', lambda message: messages.append(message) or True)
    monkeypatch.setattr(kis, 'fetch_daily_fill_totals', lambda *a, **kw: (13, 494000., 65.))
    assert not recovery.recover_pending_order(NOW)
    assert order_guard.load_orders()
    assert not messages
