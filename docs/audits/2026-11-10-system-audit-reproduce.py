"""Offline adversarial audit of the deployed code. Expected observations document defects.
No production state writes, external model calls or broker requests. Run from repository root.
"""
import asyncio, json, os, sys, tempfile
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from src import analysts, collectors, evaluation, exit_only, judgment, kis, llm, mobile_data, notify, operating_mode, order_guard, order_recovery, pipeline, portfolio_store, run_history, sell, translate, notion_sync
from src.schemas import AnalystOpinion, Decision, FillRecord, NewsItem, OHLCVBar, PortfolioState, Position, RiskGateConfig, SellAction, GateResult
ROOT=Path(__file__).resolve().parents[2]/'logs/audits/2026-11-10/system-audit'
ROOT.mkdir(parents=True,exist_ok=True)
AT=datetime(2026,11,10,14,tzinfo=exit_only.KST)
START=AT.replace(day=9,hour=16)
def position(q=100):
    return Position(ticker='005930',sector='반도체',quantity=q,entry_price=100,peak_price=100,entry_day=date(2026,11,2),weight=.1,exit_plan=sell.DEFAULT_EXIT_PLAN)
def fixture(path):
    p=PortfolioState(positions=[position(9)],cash_weight=.9)
    account=dict(total=2000,securities=900,observed_at=START.isoformat(),holdings=[dict(pdno='005930',hldg_qty=9,prpr=100,prdt_name='fixture')])
    return exit_only.initialize(p.model_dump_json().encode(),account,at=START,path=path)
def quote_response(stamp='2026-11-10T09:01:00',status='OPEN',price=80):
    return {'datas':[dict(itemCode='005930',closePriceRaw=price,highPriceRaw=max(100,price),lowPriceRaw=80,openPriceRaw=100,localTradedAt=stamp,marketStatus=status)]}
def naver(payload):
    with patch.object(collectors.requests,'get',return_value=SimpleNamespace(json=lambda:payload)):
        return collectors.fetch_naver_quotes(['005930'],today=AT.date())
def op(agent):
    return AnalystOpinion(agent=agent,ticker='005930',score=.9,confidence=.9,evidence=['prompt:fixture@1'],as_of=AT)
async def fake_llm(**kw):
    m=kw['response_model']; fields=m.model_fields
    if 'score' in fields: return m(score=.9,confidence=.9,reasoning='offline fixture')
    if 'strength' in fields: return m(argument='offline fixture',strength=.9)
    return m(action='BUY',reasoning='offline fixture',stop_loss_pct=10,take_profit_fraction=1/3,trail_pct=7)

def stale_naver(tmp):
    state=fixture(tmp/'state.json'); q=naver(quote_response())['005930']
    exit_only.apply_quotes(state,{'005930':(q,'naver')},at=AT)
    assert len(state.sales)==1
    return {'provider_trade_time':'09:01','consumer_time':'14:00','sales':len(state.sales),'recorded_quote_at':state.quotes['005930'].observed_at.isoformat()}

def closed_naver(tmp):
    state=fixture(tmp/'state.json'); q=naver(quote_response('2026-11-10T15:30:00','CLOSE',110))['005930']
    exit_only.apply_quotes(state,{'005930':(q,'naver')},at=AT.replace(hour=15,minute=35))
    assert state.quotes['005930'].price==100 and state.missing_quotes==['005930']
    return {'valid_closing_price':110,'stored_price':state.quotes['005930'].price,'missing':state.missing_quotes}

def stale_evening(tmp):
    fixture(tmp/'exit_only_state.json')
    result=mobile_data.snapshot(tmp,now=AT.replace(hour=20))
    assert not result.warnings and result.operation['status']=='exit_only'
    return {'last_quote':result.account['observed_at'],'status':result.operation['status'],'warnings':result.warnings}

def next_year(tmp):
    fixture(tmp/'exit_only_state.json')
    try: mobile_data.snapshot(tmp,now=AT.replace(year=2027,month=1,day=4))
    except ValueError as exc: return {'raised':str(exc)}
    raise AssertionError('expected uncaught calendar failure')

def partial_news(tmp):
    rec=run_history.new_run(AT,'fixture','fixture'); token=run_history.CURRENT.set(rec)
    try:
        with patch.object(collectors,'fetch_company_news',return_value=None), patch.object(collectors,'fetch_sector_news',return_value=[NewsItem(title='반도체 업종 뉴스',press=None,url='https://n.news.naver.com/mnews/article/001/0000000001',published_at=AT)]), patch.object(llm,'call_structured',side_effect=fake_llm):
            news=asyncio.run(pipeline.make_news_analyst_fn()('005930','반도체',AT))
            decision=asyncio.run(judgment.judge([op('chart'),*news,op('disclosure')],3))
        gate=pipeline.check_gate(decision,PortfolioState(),RiskGateConfig(),.08)
        assert run_history.input_failed(rec) and not decision.degraded and gate.approved
        return {'required_source_failed':True,'decision_degraded':decision.degraded,'gate_approved':gate.approved}
    finally: run_history.CURRENT.reset(token)

def empty_sector(tmp):
    payload=json.dumps({'articles':[dict(title='완전히 다른 업종 기사',officeId='001',articleId='0000000001',datetime='2026-11-10 07:00:00')]})
    with patch.object(collectors,'_fetch_with_retries',side_effect=lambda u,p,**kw:p(payload)):
        news=collectors.fetch_sector_news('')
    assert len(news)==1
    return {'sector':'','included_titles':[x.title for x in news]}

def stale_chart(tmp):
    bars=[OHLCVBar(date=date(2026,8,3)+timedelta(days=i),open=100,high=101,low=99,close=100,volume=1000) for i in range(60)]
    with patch.object(kis,'fetch_daily_ohlcv',return_value=bars): context=collectors.fetch_market_context('005930')
    with patch.object(llm,'call_structured',side_effect=fake_llm): opinion=asyncio.run(analysts.chart_analyst(context))
    assert opinion and (context.as_of.date()-bars[-1].date).days>30
    return {'latest_bar':bars[-1].date.isoformat(),'opinion_as_of':opinion.as_of.isoformat(),'rsi14':context.indicators['rsi14'],'prefilter_pass':pipeline.passes_quant_filter(context.indicators,0)}

def malformed_chart(tmp):
    row=dict(stck_bsop_date='20261109',stck_oprc='100',stck_hgpr='90',stck_lwpr='110',stck_clpr='100',acml_vol='-1')
    bars=kis._parse_daily_chart({'output2':[row,row]})
    assert len(bars)==2 and bars[0].high<bars[0].low
    return {'accepted_bars':len(bars),'duplicate_dates':True,'high_below_low':True,'negative_volume':True}

def missing_portfolio(tmp):
    portfolio=portfolio_store.load_portfolio(tmp/'deleted-state.json')
    decision=Decision(ticker='005930',action='BUY',reason='fixture',inputs=[op('chart')],degraded=False)
    gate=pipeline.check_gate(decision,portfolio,RiskGateConfig(),.08)
    assert gate.approved
    return {'missing_file_treated_as_cash':portfolio.cash_weight,'gate_approved':gate.approved}

def nan_gate(tmp):
    portfolio=PortfolioState.model_validate({'positions':[{**position().model_dump(),'weight':float('nan')}],'cash_weight':float('nan')})
    decision=Decision(ticker='005930',action='BUY',reason='fixture',inputs=[op('chart')],degraded=False)
    gate=pipeline.check_gate(decision,portfolio,RiskGateConfig(),.08)
    assert gate.approved
    return {'nonfinite_weights_approved':gate.approved}

def partial_sell(tmp):
    portfolio=PortfolioState(positions=[position()],cash_weight=.9)
    with patch.object(kis,'fetch_daily_fill_totals',return_value=(0,0.,0.)), patch.object(kis,'place_market_sell_order',return_value='123'), patch.object(kis,'fill_after_order',return_value=FillRecord(quantity=20,amount=1600,fee=1,complete=False)):
        updated,fill=asyncio.run(sell.execute_sell_order(portfolio,SellAction(ticker='005930',reason='stop_loss',sell_fraction=1),80))
    assert not updated.positions and fill.quantity==20
    return {'order_quantity':100,'observed_filled':20,'local_remaining':0,'broker_remaining_if_unfilled_cancelled':80,'order_guard_still_present':order_guard.is_blocked('005930')}

def ready_commit_crash(tmp):
    original=PortfolioState(); portfolio_store.save_portfolio(original)
    bars=[OHLCVBar(date=AT.date()-timedelta(days=31-i),open=100,high=101,low=99,close=100,volume=100) for i in range(30)]
    decision=Decision(ticker='005930',action='BUY',reason='감사 재현',inputs=[op('chart')],degraded=False)
    q=kis.Quote(price=100,day_high=101,day_low=99,prev_close=100)
    with patch.object(kis,'fetch_daily_ohlcv',return_value=bars), patch.object(kis,'fetch_quote',return_value=q), patch.object(kis,'fetch_account_balance',return_value=100000), patch.object(kis,'fetch_daily_fill_totals',return_value=(0,0.,0.)), patch.object(kis,'place_market_buy_order',return_value='123'), patch.object(kis,'fill_after_order',return_value=FillRecord(quantity=100,amount=10000,fee=1,complete=True)), patch.object(pipeline,'display_name',return_value='fixture'), patch.object(translate,'to_korean',return_value='감사 재현'):
        updated=asyncio.run(pipeline.execute_buy_order(decision,GateResult(approved=True,rejected_by=None),original,'반도체',.1,log_path=tmp/'journal.jsonl'))
    # Simulate process death before the caller persists the returned portfolio.
    assert updated.positions[0].quantity==100 and (tmp/'journal.jsonl').exists()
    assert order_guard.load_orders()['005930'].ready_to_commit
    with patch.object(kis,'fetch_daily_fill_totals',side_effect=AssertionError('should be excluded')):
        recovered=order_recovery.recover_pending_order(datetime.now(exit_only.KST))
    assert not recovered and order_guard.is_blocked('005930')
    return {'ready_to_commit':True,'filled_and_journaled_quantity':100,'persisted_positions':len(portfolio_store.load_portfolio().positions),'automatic_recovery':recovered,'still_blocked':True}

def flat_rsi(tmp):
    bars=[OHLCVBar(date=AT.date()-timedelta(days=60-i),open=100,high=100,low=100,close=100,volume=100) for i in range(60)]
    indicators=collectors.compute_indicators(bars)
    reasons=pipeline.quant_filter_reasons(indicators,0)
    assert indicators['rsi14']==100 and reasons==['rsi_overbought']
    return {'all_closes':100,'rsi14':indicators['rsi14'],'prefilter_reasons':reasons}

def hidden_corrupt_history(tmp):
    fixture(tmp/'exit_only_state.json')
    (tmp/'trade_journal.jsonl').write_text('{broken record\n')
    records=mobile_data.Records(tmp); records.rows('trade_journal.jsonl')
    result=mobile_data.snapshot(tmp,now=AT.replace(hour=20))
    assert records.warnings and not result.warnings
    return {'reader_warnings':records.warnings,'displayed_warnings':result.warnings}

def blocked_monitor(tmp):
    import threading
    started,release=threading.Event(),threading.Event()
    fixture(tmp/'exit_only_state.json')
    def blocked(*args,**kw):
        started.set()
        if not release.wait(3): raise AssertionError('audit did not release thread')
        return kis.Quote(price=100,day_high=101,day_low=99)
    async def scenario():
        first=asyncio.create_task(exit_only.monitor(at=AT,path=tmp/'exit_only_state.json'))
        try:
            assert await asyncio.to_thread(started.wait,1)
            await asyncio.wait_for(exit_only.monitor(at=AT+timedelta(minutes=1),path=tmp/'exit_only_state.json'),.5)
            assert not first.done()
            return {'first_monitor_waiting':True,'next_minute_returned_without_check':True}
        finally:
            release.set(); await first
    with patch.object(kis,'fetch_quote',side_effect=blocked), patch.object(exit_only,'flush_outbox',return_value=None):
        return asyncio.run(scenario())

def split_price_series(tmp):
    bars=[OHLCVBar(date=AT.date()-timedelta(days=60-i),open=100 if i<59 else 50,high=100 if i<59 else 50,low=100 if i<59 else 50,close=100 if i<59 else 50,volume=100) for i in range(60)]
    indicators=collectors.compute_indicators(bars)
    plan=sell.volatility_exit_plan(pipeline._sigma_20d_pct(bars))
    assert indicators['return_5d_pct']==-50
    return {'economic_event':'synthetic 2-for-1 split, unchanged value','reported_return_5d_pct':indicators['return_5d_pct'],'stop_loss_pct':plan.stop_loss_pct}

def notion_duplicate_after_response_loss(tmp):
    calls=[]
    def accepted_then_lost(*args,**kw):
        calls.append(kw['json'])
        if len(calls)==1: raise notion_sync.requests.Timeout('accepted but response lost')
        return SimpleNamespace(status_code=200,json=lambda:{'id':'second-page'})
    with patch.object(notion_sync.requests,'request',side_effect=accepted_then_lost), patch.object(notion_sync.time,'sleep',return_value=None):
        result=notion_sync._notion_request('POST','/pages',{'fixture':'same page'})
    assert len(calls)==2 and calls[0]==calls[1] and result['id']=='second-page'
    return {'same_creation_request_sent':len(calls),'reported_success':True,'remote_pages_if_first_request_committed':2}

def notion_delete_failure_ignored(tmp):
    calls=[]
    def response(method,path,body=None):
        calls.append(method)
        if method=='GET': return {'results':[{'id':'old-block'}]}
        return None if method=='DELETE' else {'results':[]}
    with patch.object(notion_sync,'_notion_request',side_effect=response):
        result=notion_sync._replace_children('page',[{'type':'paragraph','fixture':'new'}])
    assert result
    return {'delete_failed':True,'replacement_reported_success':result,'calls':calls}

def journal_repair_loses_concurrent_append(tmp):
    from scripts import reconcile_trade_journal as repair
    import contextlib,io
    path=tmp/'journal.jsonl'
    first={'event':'buy','ticker':'005930','day':'2026-11-02','quantity':10,'entry_price':100}
    concurrent={'event':'sell','ticker':'005930','day':'2026-11-10','shares_sold':2}
    path.write_text(json.dumps(first)+'\n')
    def broker(*args,**kwargs):
        with path.open('a') as f:f.write(json.dumps(concurrent)+'\n')
        return (10,1100.,1.)
    with patch.object(repair,'DEFAULT_TRADE_JOURNAL_LOG_PATH',path), patch.object(repair,'APPLY',True), patch.object(kis,'fetch_daily_fill_totals',side_effect=broker), contextlib.redirect_stdout(io.StringIO()):
        status=repair.main()
    rows=[json.loads(x) for x in path.read_text().splitlines()]
    assert status==0 and len(rows)==1 and rows[0]['event']=='buy'
    return {'concurrent_sell_appended':True,'remaining_journal_rows':len(rows),'reported_exit_status':status}

def missing_fees(tmp):
    with patch.object(kis,'_kis_get',return_value={'output2':{'tot_ccld_qty':'20','tot_ccld_amt':'2000'}}): totals=kis.fetch_daily_fill_totals('005930',AT.date(),'sell')
    fill=kis.fill_between((0,0.,0.),totals); costs=pipeline._trade_costs(fill,20,2000,100)
    assert costs['fee_amount']==0 and costs['fee_source']=='fill'
    return {'fee_field_absent':True,'recorded_fee':costs['fee_amount'],'fee_source':costs['fee_source']}

def incomplete_balance(tmp):
    payload={'output1':[{'pdno':'005930','hldg_qty':'10','pchs_avg_pric':'100','prpr':'110','evlu_amt':'1100'}], 'output2':[{'tot_evlu_amt':'10000','dnca_tot_amt':'5000','scts_evlu_amt':'5000'}], 'ctx_area_nk100':'NEXT_PAGE'}
    with patch.object(kis,'_kis_get',return_value=payload): result=kis.fetch_account_snapshot()
    assert result and len(result.holdings)==1
    return {'continuation_present':True,'declared_securities':result.securities,'listed_securities':sum(float(h['evlu_amt']) for h in result.holdings),'reported_success':True}

def shifted_horizon(tmp):
    days=[date(2026,11,d) for d in (2,3,4,5,6,9,10)]
    index={d:OHLCVBar(date=d,open=100,high=100,low=100,close=100,volume=100) for d in days}
    stock={d:b.model_copy(update={'close':110 if d.day==10 else 100,'high':110}) for d,b in index.items() if d.day!=4}
    result=evaluation.forward_excess_return(stock,index,days[0],5)
    assert abs(result-.1)<1e-8
    return {'declared_k':5,'expected_exit':'2026-11-09','used_exit':'2026-11-10','return':result,'missing_session':'2026-11-04'}

CASES=[stale_naver,closed_naver,stale_evening,next_year,partial_news,empty_sector,stale_chart,malformed_chart,missing_portfolio,nan_gate,partial_sell,ready_commit_crash,missing_fees,incomplete_balance,shifted_horizon,flat_rsi,hidden_corrupt_history,blocked_monitor,split_price_series,notion_duplicate_after_response_loss,notion_delete_failure_ignored,journal_repair_loses_concurrent_append]
results=[]
for fn in CASES:
    with tempfile.TemporaryDirectory(prefix='sima-system-audit-') as tmp,ExitStack() as stack:
        tmp=Path(tmp)
        stack.enter_context(patch.dict(os.environ,{'SIMA_COLLECTOR_AUTOREPAIR':'0','SIMA_APP_ALERTS_ENABLED':'0','SIMA_EXTENDED_ORDER_RECOVERY':'1'}))
        stack.enter_context(patch('requests.sessions.Session.request',side_effect=AssertionError('network forbidden in audit')))
        stack.enter_context(patch.object(notify,'send_telegram_alert',return_value=True))
        stack.enter_context(patch.object(order_guard,'STATE_PATH',tmp/'orders.json'))
        stack.enter_context(patch.object(portfolio_store,'PORTFOLIO_STATE_PATH',tmp/'portfolio.json'))
        stack.enter_context(patch.object(portfolio_store,'PORTFOLIO_LOCK_PATH',tmp/'portfolio.lock'))
        stack.enter_context(patch.object(order_recovery,'JOURNAL_PATH',tmp/'journal.jsonl'))
        stack.enter_context(patch.object(order_recovery,'AUDIT_DIR',tmp/'recoveries'))
        try: results.append({'case':fn.__name__,'reproduced':True,'observed':fn(tmp)})
        except Exception as exc: results.append({'case':fn.__name__,'reproduced':False,'error':repr(exc)})
(ROOT/'reproduction-results.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))
print(json.dumps(results,ensure_ascii=False,indent=2))
assert all(r['reproduced'] for r in results), 'Inspect cases that did not reproduce'
