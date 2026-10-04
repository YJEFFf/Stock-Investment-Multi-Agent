'use strict';
const $ = selector => document.querySelector(selector);
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const amount = n => n == null ? '—' : new Intl.NumberFormat('ko-KR', {maximumFractionDigits:0}).format(n);
const money = n => n == null ? '—' : `${amount(n)}원`;
const pct = (n, sign = false) => n == null ? '—' : `${sign && n > 0 ? '+' : ''}${Number(n).toFixed(2)}%`;
const signedMoney = n => n == null ? '—' : `${n > 0 ? '+' : ''}${money(n)}`;
const tone = n => n > 0 ? 'gain' : n < 0 ? 'loss' : 'neutral';
const stamp = value => { if (!value) return '기록 없음'; const d = new Date(value); return Number.isNaN(d.valueOf()) ? String(value) : new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit',hour12:false}).format(d); };
const shortDay = value => value ? `${Number(value.slice(5,7))}.${Number(value.slice(8,10))}` : '—';
const standalone = () => window.matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isiOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
const state = {data:null, alerts:[], push:null, key:null, authenticated:false, offline:false, filter:'all', loading:false, checkedAt:null};
const checkedTime = value => value ? new Intl.DateTimeFormat('ko-KR', {timeZone:'Asia/Seoul',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}).format(new Date(value)) : '아직 확인 전';
const icons = {
  home:'<path d="m3 10 9-7 9 7v10a1 1 0 0 1-1 1h-5v-7H9v7H4a1 1 0 0 1-1-1Z"/>',
  holdings:'<path d="M12 3a9 9 0 1 0 9 9h-9Z"/><path d="M16 3v5h5a7 7 0 0 0-5-5Z"/>',
  trades:'<path d="M4 7h16m-4-4 4 4-4 4M20 17H4m4-4-4 4 4 4"/>',
  alerts:'<path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9M10 21h4"/>',
  insights:'<path d="M4 20h17M7 16V9m6 7V4m6 12v-5"/>',
};
const icon = name => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${icons[name] || icons.home}</svg>`;
const route = () => location.hash.slice(1).split('/')[0] || 'home';
function seenAt() { try { return localStorage.getItem('sima-seen-at') || ''; } catch (_) { return ''; } }
function hasUnread() { return state.alerts.some(a => a.created_at > seenAt()); }
function toast(text) { $('#toast').textContent = text; $('#toast').hidden = false; clearTimeout(toast.timer); toast.timer = setTimeout(() => $('#toast').hidden = true, 5000); }
async function api(path, options = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {credentials:'same-origin', cache:'no-store', ...options, signal:controller.signal,
      headers:{'Content-Type':'application/json','X-SIMA-Request':'1', ...options.headers}});
    if (!response.ok) { const error = new Error(`HTTP ${response.status}`); error.status = response.status; throw error; }
    return await response.json();
  } finally { clearTimeout(timeout); }
}
function empty(title, detail, name = 'insights') { return `<div class="empty">${icon(name)}<h3>${esc(title)}</h3><p>${esc(detail)}</p></div>`; }
function heading(title, detail) { return `<div class="page-title"><p class="eyebrow">MY PAPER PORTFOLIO</p><h1>${esc(title)}</h1><p class="subtitle">${esc(detail)}</p></div>`; }
function progress(value, total) { return `<svg class="progress-svg" viewBox="0 0 100 8" preserveAspectRatio="none" role="img" aria-label="${value}/${total}"><rect class="progress-bg" width="100" height="8" rx="4"/><rect class="progress-fill" width="${Math.max(0, Math.min(100, value / total * 100))}" height="8" rx="4"/></svg>`; }
function installSteps() { return '<ol class="install-steps"><li>Safari의 공유 메뉴를 열어주세요.</li><li>‘홈 화면에 추가’를 선택하고, ‘웹 앱으로 열기’가 보이면 켜주세요.</li><li>홈 화면의 SIMA를 열고 연결 코드를 입력하세요.</li></ol>'; }
function showInstall() {
  const dialog = $('#install-dialog');
  dialog.innerHTML = `<div class="row"><h2 id="install-title">아이폰에 SIMA 설치하기</h2><form method="dialog"><button class="text-button" aria-label="설치 안내 닫기">닫기</button></form></div><p class="subtitle">아이폰 Safari에서 아래 주소를 열고 홈 화면에 추가하면 앱 아이콘으로 실행할 수 있어요.</p><p class="install-address">${esc(location.origin)}</p>${installSteps()}<p class="fine">이미 홈 화면에 SIMA가 있다면 그 아이콘으로 열어주세요. 처음 연결할 때는 새 연결 코드가 필요할 수 있어요.</p><a class="text-button" href="https://support.apple.com/ko-kr/guide/iphone/iphea86e5236/ios" target="_blank" rel="noreferrer">Apple 설치 안내 ↗</a>`;
  dialog.showModal();
}
function refreshBusy(busy) {
  const button = $('#refresh');
  button.disabled = busy;
  button.setAttribute('aria-busy', String(busy));
  button.setAttribute('aria-label', busy ? '최신 기록 확인 중' : '최신 기록 불러오기');
}
function showLogin(message = '') {
  state.data = null; state.alerts = []; state.authenticated = false; state.checkedAt = null;
  $('#navigation').hidden = true; $('#refresh').hidden = true;
  // 설치 감지는 안내에만 사용한다. 감지 결과 때문에 개인 인증을 막지 않는다.
  const installHelp = isiOS && !standalone() ? `<details class="card disclosure"><summary>아이폰에서 푸시 알림도 받으려면</summary><p>홈 화면에 설치한 SIMA에서 연결해주세요. Safari와 홈 화면 앱은 별도 연결 코드가 필요할 수 있어요.</p>${installSteps()}</details>` : '';
  $('#main').innerHTML = `<section class="connect"><img class="connect-logo" src="/assets/icon.svg" alt="SIMA"><p class="eyebrow">나만의 투자 기록</p><h1>매일의 투자,<br>한눈에 담다.</h1><p class="subtitle">내 모의투자 현황부터 중요한 알림까지.<br>SIMA에서 차분하게 확인하세요.</p><div class="card"><h3>내 기기 연결하기</h3><p class="fine">전달받은 일회용 연결 코드를 입력해주세요.<br>연결한 기기에서만 투자 기록을 볼 수 있어요.</p><div class="divider"></div><form id="login-form"><label for="code">연결 코드</label><input id="code" name="code" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="XXXX-XXXX-XXXX-XXXX" maxlength="40" required><button class="button" type="submit">SIMA 시작하기</button></form><p id="login-error" class="error-message" role="alert">${esc(message)}</p></div>${installHelp}<p class="footer-note">개인용 모의투자 대시보드<br>내 기록과 알림을 한 곳에서</p></section>`;
  $('#login-form')?.addEventListener('submit', async event => {
    event.preventDefault(); const button = event.target.querySelector('button'); button.disabled = true;
    try { const result = await api('/api/login', {method:'POST',body:JSON.stringify({code:$('#code').value})}); state.key = result.vapid_public_key; state.authenticated = true; await refresh(); }
    catch (error) { $('#login-error').textContent = error.status === 429 ? '잠시 후 다시 시도해주세요.' : error.status === 401 ? '코드를 확인해주세요. 사용했거나 만료된 코드는 새로 발급받아야 해요.' : '연결하지 못했습니다. 인터넷 연결을 확인해주세요.'; button.disabled = false; }
  });
}
function nav() {
  const tabs = [['home','홈'],['holdings','보유'],['trades','거래'],['alerts','알림'],['insights','검증']];
  $('#navigation').innerHTML = tabs.map(([key,label]) => `<a class="nav-item ${route() === key ? 'active' : ''}" href="#${key}" ${route() === key ? 'aria-current="page"' : ''}>${icon(key)}<span>${label}</span>${key === 'alerts' && hasUnread() ? '<i class="nav-dot" aria-label="읽지 않은 알림"></i>' : ''}</a>`).join('');
  $('#navigation').hidden = false; $('#refresh').hidden = false;
}
function holdingsRows(holdings) {
  return holdings.map(h => `<div class="holding row"><div class="stock-identity"><div class="stock-mark">${esc(h.name.slice(0,2))}</div><div><div class="stock-name">${esc(h.name)}</div><div class="stock-caption">${amount(h.quantity)}주 · ${esc(h.ticker)}</div></div></div><div class="align-right"><div class="amount">${money(h.value)}</div><div class="change ${tone(h.pnl)}">${pct(h.pnl_pct,true)}</div></div></div>`).join('');
}
function operationCard() {
  const o = state.data.operation;
  const bad = ['failed','degraded','missing','skipped','unconfirmed'].includes(o.status);
  const idle = ['holiday','waiting'].includes(o.status);
  const reasons = {universe_fetch_failed:'구성 종목 자료를 불러오지 못했어요.',all_market_contexts_unavailable:'시세 자료를 불러오지 못했어요.',deadline_exceeded:'정해진 시각 안에 분석을 끝내지 못했어요.',codex_plan_capacity_insufficient:'분석에 사용할 잔여 한도가 부족해요.'};
  const detail = o.status === 'holiday' ? `다음 거래일 ${shortDay(o.next_trading_day)} · 오전 8:30 분석 예정` : o.run?.ended_at ? `${stamp(o.run.ended_at)} 종료 · ${o.run.decisions ?? 0}종목 판단` : bad ? (reasons[o.reason] || '알림에서 실행 결과와 사유를 확인해주세요.') : '오전 8:30에 그날의 투자 판단을 시작해요.';
  return `<div class="card status-card"><span class="status-dot ${bad ? 'error' : idle ? 'waiting' : ''}"></span><div><h3>${esc(o.label)}</h3><p>${esc(detail)}</p>${o.unconfirmed_orders ? `<p class="danger-text">확인이 필요한 주문 ${o.unconfirmed_orders}건</p>` : ''}</div></div>`;
}
function navChart() {
  const rows = state.data.nav_history;
  if (rows.length < 2) return empty('첫 기록을 담았어요', `${rows.length ? shortDay(rows[0].day) + '부터 ' : ''}실제 계좌 기록이 쌓이면 변화 그래프가 나타나요.`);
  const values = rows.map(r => Number(r.total)), min = Math.min(...values), max = Math.max(...values), span = max - min || max * .01 || 1;
  const points = values.map((v,i) => `${(i / (values.length - 1) * 300 + 8).toFixed(2)},${(110 - (v - min) / span * 90).toFixed(2)}`).join(' ');
  const last = points.split(' ').pop().split(',');
  return `<svg class="spark-chart" viewBox="0 0 316 128" role="img" aria-label="관측 기간 계좌 가치 변화"><path class="chart-grid" d="M8 20H308M8 65H308M8 110H308"/><polyline class="chart-line" points="${points}"/><circle class="chart-dot" cx="${last[0]}" cy="${last[1]}" r="4"/></svg><div class="chart-labels"><span>${shortDay(rows[0].day)}</span><span>${shortDay(rows.at(-1).day)}</span></div><p class="fine">저장된 관측일의 계좌 가치 · 최초 투자일 이후 전체 곡선과는 기간이 달라요.</p>`;
}
function home() {
  const d = state.data, a = d.account, m = d.measurements;
  const hero = a ? `<div class="card hero"><div class="hero-label">내 총자산</div><div class="hero-value">${amount(a.total)}<small>원</small></div><div class="hero-pnl ${tone(a.pnl)}">${signedMoney(a.pnl)} (${pct(a.return_since_start * 100,true)})<span>초기 자금 대비</span></div><div class="hero-bottom"><div><small>보유 현금</small><strong>${money(a.cash)}</strong></div><div><small>주식 평가액</small><strong>${money(a.securities)}</strong><div class="stock-pnl ${tone(a.securities_pnl)}"><span>${signedMoney(a.securities_pnl)}</span><span>(${pct(a.securities_return_pct,true)})</span></div><small class="stock-pnl-label">보유 주식 매입금 대비</small></div></div><p class="fine">자료 기준일 ${esc(a.day)}<br>증권사 잔고 확인 ${stamp(a.observed_at)} (한국시간)</p></div>` : `<div class="card">${empty('계좌 기록을 기다리고 있어요','다음 정상 장 마감 조회 후 계좌 현황이 표시돼요.')}</div>`;
  return heading('나의 투자 현황', '오늘도, 기록을 바탕으로 차분하게.') + hero + operationCard() + `<div class="section-heading"><h2>자산의 흐름</h2><span class="eyebrow">관측 기간</span></div><div class="card">${navChart()}</div><div class="section-heading"><h2>보유 종목 <span class="subtitle">${d.holdings.length}</span></h2><a class="text-button" href="#holdings">자세히 보기 →</a></div><div class="card">${d.holdings.length ? holdingsRows(d.holdings) : empty('보유 종목이 없어요','현금으로 기다리는 것도 정상적인 투자 판단이에요.','holdings')}</div><div class="section-heading"><h2>차곡차곡 쌓는 검증</h2><a class="text-button" href="#insights">전체 보기 →</a></div><div class="card"><div class="row"><div><p class="eyebrow">복구 이후 정상 분석일</p><div class="progress-count">${m.normal_days}<small> / ${m.target_days}일</small></div></div><span class="tag">표본 축적 중</span></div>${progress(m.normal_days,m.target_days)}<p class="fine">정상 관망도 포함해요. 분석 장애가 난 날은 제외해요.</p></div>`;
}
function holdingsPage() {
  const d = state.data, a = d.account;
  return heading('보유 종목', a ? `${a.day} 계좌 조회 기준 · ${d.holdings.length}종목` : '마지막으로 확인한 계좌 기록') + (a ? `<div class="metric-grid"><div class="metric"><div class="label">주식 평가액</div><strong>${amount(a.securities)}<small>원</small></strong></div><div class="metric"><div class="label">현금 비중</div><strong>${a.cash_ratio == null ? '—' : (a.cash_ratio * 100).toFixed(1)}<small>%</small></strong></div></div><p class="fine">장 마감 관측값이에요. 현재 실시간 시세는 아니에요.</p>` : '') + `<div class="section-heading"><h2>종목별 현황</h2></div>` + (d.holdings.length ? d.holdings.map(h => `<article class="card">${holdingsRows([h])}<dl class="detail-grid"><div><dt>평균 매입가</dt><dd>${money(h.entry_price)}</dd></div><div><dt>관측 시점 주가</dt><dd>${money(h.price)}</dd></div><div><dt>평가 손익</dt><dd class="${tone(h.pnl)}">${signedMoney(h.pnl)}</dd></div><div><dt>보유 수량</dt><dd>${amount(h.quantity)}주</dd></div></dl></article>`).join('') : `<div class="card">${empty('보유 종목이 없어요','다음 기록이 갱신되면 여기에 반영돼요.','holdings')}</div>`);
}
const reasonLabels = {stop_loss:'손절',take_profit_first:'첫 부분 익절',take_profit_trail:'트레일링 익절',llm_discretionary:'보유 재평가',gap_too_large:'개장 가격 차이 초과',balance_unavailable:'잔고 조회 실패',quantity_zero:'주문 가능 수량 없음',order_rejected:'주문 거부',price_data_unavailable:'시세 조회 실패',order_response_lost:'주문 응답 확인 필요'};
function tradesPage() {
  const trades = state.data.trades.filter(t => state.filter === 'all' || t.event === state.filter);
  const filters = [['all','전체'],['buy','매수'],['sell','매도'],['buy_skipped','미체결']];
  return heading('거래 기록','체결과 그 이유를 시간순으로 확인해요.') + `<div class="filter-row">${filters.map(([key,label]) => `<button class="filter ${state.filter === key ? 'active' : ''}" data-filter="${key}">${label}</button>`).join('')}</div>` + (trades.length ? trades.map(t => {
    const sell = t.event === 'sell', buy = t.event === 'buy', quantity = sell ? t.shares_sold : t.shares ?? t.shares_bought ?? t.quantity;
    const price = sell ? t.exit_price : t.entry_price ?? t.fill_price;
    const label = sell ? '매도' : buy ? '매수' : '미체결';
    return `<article class="card"><div class="row"><div class="trade-title"><span class="tag ${sell ? 'sell' : buy ? 'buy' : 'warning'}">${label}</span>${esc(t.name)}</div><span class="trade-date">${esc(t.day)}</span></div>${sell || buy ? `<div class="trade-main">${amount(quantity)}주<small>· ${money(price)}</small></div>` : ''}<p class="reason">${esc(reasonLabels[t.reason] || t.reasoning || t.reason || '')}</p>${sell ? `<dl class="detail-grid"><div><dt>매도 금액</dt><dd>${money(t.sell_amount)}</dd></div><div><dt>비용 반영 손익${t.net_pnl_amount == null ? ' 기록 없음' : ''}</dt><dd class="${tone(t.net_pnl_amount)}">${signedMoney(t.net_pnl_amount)}</dd></div><div><dt>매도 후 잔여</dt><dd>${amount(t.shares_after)}주</dd></div><div><dt>보유분 중 매도</dt><dd>${t.position_fraction_sold == null ? '—' : pct(t.position_fraction_sold * 100)}</dd></div></dl><p class="fine">수수료·세금 추정이 포함될 수 있어요.${t.sell_amount_source === 'broker_daily' ? ' 일별 합산 체결 기록이에요.' : ''}</p>` : ''}</article>`;
  }).join('') : `<div class="card">${empty('해당 거래가 없어요','거래가 발생하면 기록이 여기에 쌓여요.','trades')}</div>`) + '<p class="footer-note">최근 250건 · 가격과 금액은 저장된 체결 기록 기준</p>';
}
function alertsPage() {
  const p = state.push || {}, supported = 'Notification' in window && 'PushManager' in window && 'serviceWorker' in navigator;
  const selected = location.hash.split('/')[1];
  const workerStale = p.enabled && (!p.worker_seen || Date.now() - new Date(p.worker_seen).valueOf() > 120000);
  return heading('알림함','중요한 순간을 놓치지 않도록.') + `<div class="card"><div class="row"><h3>이 기기의 푸시 알림</h3><span class="tag ${p.enabled ? '' : 'warning'}">${p.enabled ? '등록됨' : '꺼짐'}</span></div><p class="fine">${!supported ? (isiOS ? '홈 화면에 설치한 SIMA에서 알림을 켜주세요. 설치할 때 ‘웹 앱으로 열기’가 보이면 켜주세요.' : '이 브라우저에서는 푸시 알림을 지원하지 않아요.') : p.enabled ? '테스트 알림을 보내 실제 아이폰 수신을 확인하세요.' : '매매 결과와 분석 오류를 잠금 화면에서도 확인하세요.'}</p>${workerStale ? '<p class="error-message">알림 발송 상태가 갱신되지 않았어요. 수신이 지연될 수 있어요.</p>' : ''}${supported ? `<div class="button-row">${p.enabled ? '<button class="button" id="test-push">테스트 알림 보내기</button><button class="button secondary" id="disable-push">알림 끄기</button>' : '<button class="button" id="enable-push">알림 켜기</button>'}</div>` : ''}${p.pending ? `<p class="fine">발송 대기 ${p.pending}건</p>` : ''}</div><div class="section-heading"><h2>최근 알림</h2><button class="text-button" id="mark-read">모두 읽음</button></div>${state.alerts.length ? state.alerts.map(a => `<details class="card alert-card ${a.created_at > seenAt() ? 'unread' : ''} ${selected === a.id ? 'selected' : ''}" id="alert-${esc(a.id)}" ${selected === a.id ? 'open' : ''}><summary><div class="alert-date">${stamp(a.created_at)}</div><span class="tag ${esc(a.kind)}">${({buy:'매수',sell:'매도',analysis:'분석',error:'확인 필요',warning:'안내',summary:'요약',measurement:'검증',info:'소식'})[a.kind] || '소식'}</span><p class="alert-title">${esc(a.title)}</p></summary><p class="reason">${esc(a.body)}</p></details>`).join('') : `<div class="card">${empty('아직 새 알림이 없어요','앱 연결 이후 발생하는 알림이 여기에 저장돼요.','alerts')}</div>`}<button id="logout" class="button tertiary">이 기기 연결 해제</button>`;
}
function insightsPage() {
  const m = state.data.measurements, monitor = state.data.monitoring;
  const restored = Object.entries(m.segments || {}).find(([key,seg]) => key === 'kis_master_naver_json_20261005' || seg.cohort === 'kis_master_naver_json_20261005');
  const current = restored ? restored[1] : null;
  function icTable(seg) { return `<table><thead><tr><th>수익률 기간</th><th>IC</th><th>측정일</th><th>종목 표본</th></tr></thead><tbody>${[5,10,20].map(k => { const h = seg?.horizons?.[String(k)]; return `<tr><td>${k}거래일</td><td>${h?.mean_ic == null ? '—' : Number(h.mean_ic).toFixed(3)}</td><td>${amount(h?.days_measured)}</td><td>${amount(h?.n_pairs)}</td></tr>`; }).join('')}</tbody></table>`; }
  return heading('검증의 기록','수익과 함께, 판단의 예측력을 살펴봐요.') + `<div class="card"><p class="eyebrow">복구 이후 정상 분석일</p><div class="progress-count">${m.normal_days}<small> / ${m.target_days}일</small></div>${progress(m.normal_days,m.target_days)}<p class="fine">매수가 없는 정상 관망일도 포함해요.<br>장애·부분 실행은 정상 분석일에서 제외해요.</p></div><div class="section-heading"><h2>최근 20거래일</h2><span class="tag">매수 신호 ${monitor.signal_days}/${monitor.total_days}일</span></div><div class="card"><div class="row"><h3>매수 신호 발생률</h3><strong>${monitor.signal_ratio == null ? '—' : pct(monitor.signal_ratio * 100)}</strong></div><div class="days-grid">${monitor.days.map(d => `<div class="day-cell ${d.signal ? 'signal' : esc(d.status)}" title="${esc(d.day)} · ${d.signal ? '매수 신호' : d.status === 'completed' ? '정상 관망' : d.status === 'unknown' ? '실행 상태 미상' : '장애 또는 미실행'}">${shortDay(d.day)}</div>`).join('')}</div><div class="legend"><span class="signal">매수 신호</span><span class="completed">정상 관망</span><span class="failed">장애·미실행</span><span>상태 미상</span></div><p class="fine">이전 실행 상태가 없는 날짜를 정상 관망으로 세지 않아요.</p></div><div class="section-heading"><h2>신호의 예측력</h2><span class="eyebrow">복구 이후</span></div><div class="card">${icTable(current)}<p class="fine">IC는 판단 점수와 이후 실제 수익률의 순위 상관이에요. 필요한 기간이 지난 표본부터 계산해요.</p>${m.segments?.all ? `<details class="disclosure"><summary>이전 전체 기록도 보기</summary>${icTable(m.segments.all)}<p class="fine">수집 방식과 분석 방식이 다른 이전 기록을 포함해요.</p></details>` : ''}</div><div class="section-heading"><h2>운영 확인</h2></div><div class="card"><h3>매수 승인 거부 사유</h3>${Object.keys(monitor.gate_rejections).length ? `<table><tbody>${Object.entries(monitor.gate_rejections).map(([key,value]) => `<tr><td>${esc(({position_limit:'종목 수 한도',sector_concentration:'업종 집중도',degraded_analysis:'분석 일부 누락',daily_loss_limit:'일일 손실 한도',total_exposure:'총 투자 비중'})[key] || key)}</td><td>${value}건</td></tr>`).join('')}</tbody></table>` : '<p class="fine">최근 20거래일 기록에 승인 거부가 없어요.</p>'}<details class="disclosure"><summary>분석가 호출·실패·사용량</summary><div class="table-wrap"><table><thead><tr><th>역할</th><th>호출</th><th>실패</th><th>입출력 토큰</th></tr></thead><tbody>${Object.entries(monitor.analysts).map(([key,s]) => `<tr><td>${esc(({chart:'차트',news:'뉴스',disclosure:'공시',bull:'강세',bear:'약세',manager:'종합 판단'})[key] || key)}</td><td>${s.calls}</td><td>${s.failures} (${Math.round(s.failures / s.calls * 100)}%)</td><td>${amount(s.input_tokens + s.output_tokens)}</td></tr>`).join('') || '<tr><td colspan="4">기록 없음</td></tr>'}</tbody></table></div></details></div><p class="footer-note">관측 기록을 바탕으로 평가해요.<br>60일 도달이 수익성이나 실거래 전환을 보장하지는 않아요.</p>`;
}
function render() {
  if (!state.authenticated || !state.data) return;
  const pages = {home,holdings:holdingsPage,trades:tradesPage,alerts:alertsPage,insights:insightsPage};
  const warnings = [...(state.offline ? ['최신 기록 확인에 실패했어요. 화면은 마지막으로 불러온 기록이에요.'] : []), ...state.data.warnings];
  $('#main').innerHTML = warnings.map(w => `<div class="warning-banner">${esc(w)}</div>`).join('') + (pages[route()] || home)() + `<p class="footer-note">서버 기록 확인 ${checkedTime(state.checkedAt)}<br>계좌 금액은 장 마감 기록 기준 · 새로고침으로 저장된 기록을 확인해요.</p>`;
  nav(); bind();
}
async function refresh({manual = false} = {}) {
  if (state.loading) { if (manual) toast('기록을 확인하고 있어요. 잠시 기다려주세요.'); return; }
  state.loading = true; refreshBusy(true);
  if (manual) toast('서버의 최신 기록을 확인하고 있어요…');
  try {
    const [data, alerts, push] = await Promise.all([api(manual ? '/api/snapshot?refresh=1' : '/api/snapshot'),api('/api/alerts'),api('/api/push')]);
    if (!state.authenticated) return;
    state.data = data; state.alerts = alerts.alerts; state.push = push; state.offline = false; state.checkedAt = new Date().toISOString(); render();
    if (manual) toast(`새로고침 완료 · ${checkedTime(state.checkedAt)} 저장 기록을 확인했어요.`);
  } catch (error) {
    if (error.status === 401) { showLogin('기기 연결이 만료됐어요. 새 연결 코드로 다시 연결해주세요.'); if (manual) toast('기기 연결이 만료됐어요. 다시 연결해주세요.'); }
    else if (state.data) { state.offline = true; render(); toast(error.name === 'AbortError' ? '응답이 늦어 확인을 마치지 못했어요. 다시 눌러주세요.' : error.status === 429 ? '요청이 많아요. 잠시 후 다시 눌러주세요.' : '새로고침에 실패했어요. 인터넷 연결을 확인해주세요.'); }
    else { $('#main').innerHTML = `<div class="card">${empty('기록을 불러오지 못했어요','인터넷 연결을 확인한 뒤 다시 시도해주세요.')}<button class="button" id="retry">다시 불러오기</button></div>`; $('#retry').onclick = () => refresh({manual:true}); if (manual) toast('새로고침에 실패했어요. 다시 시도해주세요.'); }
  } finally { state.loading = false; refreshBusy(false); }
}
function decodeKey(value) { const raw = atob(value.replace(/-/g,'+').replace(/_/g,'/') + '='.repeat((4 - value.length % 4) % 4)); return Uint8Array.from(raw,c => c.charCodeAt(0)); }
async function enablePush() {
  if (!state.key) { toast('서버의 알림 설정을 확인해야 해요.'); return; }
  try {
    // iOS: 알림 권한 요청은 버튼 클릭의 직접 결과로 실행한다.
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') { toast('아이폰 설정에서 SIMA 알림을 허용해주세요.'); return; }
    const registration = await navigator.serviceWorker.getRegistration();
    if (!registration?.active) throw new Error('service worker unavailable');
    const subscription = await registration.pushManager.getSubscription() || await registration.pushManager.subscribe({userVisibleOnly:true,applicationServerKey:decodeKey(state.key)});
    await api('/api/push',{method:'POST',body:JSON.stringify(subscription.toJSON())});
    await refresh(); toast('알림을 등록했어요. 테스트 알림으로 수신을 확인하세요.');
  } catch (_) { toast('알림을 연결하지 못했어요. 다시 시도해주세요.'); }
}
function bind() {
  document.querySelectorAll('[data-filter]').forEach(button => button.onclick = () => { state.filter = button.dataset.filter; render(); });
  $('#enable-push')?.addEventListener('click', enablePush);
  $('#test-push')?.addEventListener('click', async () => { try { await api('/api/push/test',{method:'POST',body:'{}'}); toast('테스트 알림을 요청했어요. 잠금 화면에서도 확인해주세요.'); await refresh(); } catch (_) { toast('테스트 알림 요청에 실패했어요. 잠시 후 다시 시도해주세요.'); } });
  $('#disable-push')?.addEventListener('click', async () => { try { await api('/api/push',{method:'DELETE'}); const reg = await navigator.serviceWorker.ready; const sub = await reg.pushManager.getSubscription(); if (sub) await sub.unsubscribe(); await refresh(); } catch (_) { toast('알림을 해제하지 못했어요. 다시 시도해주세요.'); } });
  $('#mark-read')?.addEventListener('click', () => { try { localStorage.setItem('sima-seen-at',state.alerts[0]?.created_at || new Date().toISOString()); } catch (_) {} render(); });
  $('#logout')?.addEventListener('click', async () => {
    try {
      await api('/api/logout',{method:'POST',body:'{}'});
      state.key = null; showLogin('기기 연결을 해제했어요.');
      if (state.push?.enabled && 'serviceWorker' in navigator) navigator.serviceWorker.getRegistration().then(async reg => {
        const sub = await reg?.pushManager?.getSubscription(); if (sub) await sub.unsubscribe();
      }).catch(() => {});
    }
    catch (_) { toast('연결 해제에 실패했어요. 다시 시도해주세요.'); }
  });
}
async function boot() {
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/service-worker.js').catch(() => toast('앱 설치 연결을 확인해주세요.'));
  try { const session = await api('/api/session'); state.authenticated = session.authenticated; state.key = session.vapid_public_key; if (session.authenticated) await refresh(); else showLogin(); }
  catch (_) { $('#main').innerHTML = `<div class="connect">${empty('연결을 기다리고 있어요','인터넷에 연결하면 SIMA 기록을 볼 수 있어요.')}<button class="button" id="retry">다시 연결하기</button></div>`; $('#retry').onclick = boot; }
}
$('#refresh').onclick = () => refresh({manual:true});
$('#install').hidden = standalone();
$('#install').onclick = showInstall;
window.addEventListener('hashchange', () => { render(); window.scrollTo(0,0); const selected = location.hash.split('/')[1]; if (selected) document.getElementById('alert-' + selected)?.scrollIntoView({block:'center'}); });
document.addEventListener('visibilitychange', () => { if (!document.hidden && state.authenticated) refresh(); });
setInterval(() => { if (!document.hidden && state.authenticated) refresh(); },60000);
boot();
