# SIMA — Stock Investment Multi-Agent

한국 주식(KOSPI200 유니버스) 대상 멀티에이전트 투자 판단 시스템.

LLM 분석가(차트·뉴스·공시)가 종목별로 독립적으로 의견을 내고, 강세/약세 토론과
포트폴리오 매니저가 이를 종합해 판단하며, 결정론적 리스크 게이트가 최종 승인한다.
매수 없음이 기본 상태이자 정상 출력이다.

## 왜 이렇게 설계했는가

이 프로젝트의 이전 버전은 "pending 매수 신호가 부족하면 분석을 재실행"하는
스케줄러 때문에 사실상 매일 매수를 강제했고, 그 결과 신호에 실제 예측력이 있는지
자체를 측정할 수 없었다. 자세한 실패 원인과 설계 근거는 [`docs/PLAN.md`](docs/PLAN.md),
지켜야 할 절대 규칙은 [`CLAUDE.md`](CLAUDE.md)에 있다.

핵심 원칙만 요약하면:

- 기본 상태는 현금이다. 후보가 0개인 날이 있어야 정상이다.
- 목표 신호 개수·top-N 선택을 두지 않는다. 절대 문턱으로만 거른다.
- 재시도는 데이터 수집 실패에만 허용한다. 결과가 마음에 안 들어서 다시 돌리지 않는다.
- LLM에게 종목 비교를 시키지 않는다. 종목당 독립 호출로 "기준을 충족하는가"만 판정한다.
- 최종 승인권은 코드(리스크 게이트)가 갖는다. LLM은 거부권만 가진다.
- 마일스톤 3까지는 모의투자로만 검증한다. 실거래 연결 없음.

## 아키텍처

```
[수집: 코드]      시세(KIS API)      뉴스(Naver 스크래핑)   공시(DART API)
                        |                      |                  |
[분석: LLM]        차트 분석가            뉴스 분석가         공시 분석가
                        |                      |                  |
                        +----------------------+------------------+
                                               |
[판단: LLM]                          강세/약세 토론  (반대 논거 강제 생성)
                                               |
                                    포트폴리오 매니저 (근거 종합)
                                               |
[게이트: 코드]                   리스크 게이트 (한도 초과 시 무조건 거부)
                                               |
[집행: 코드]                              주문 집행 (모의투자만)
```

수집과 게이트/집행은 결정론적 코드다. 분석가는 데이터를 직접 가져오지 않고
`MarketContext` 등으로 주입받는다.

## 파일 구조

```
src/
  schemas.py           # 계층 간 데이터 계약 (pydantic)
  llm.py                # Claude 래퍼 — 재시도·타임아웃·스키마 검증
  collectors.py          # 수집 — KOSPI200 종목 리스트(Naver), 뉴스(Naver), 공시(DART)
  kis.py                 # 한국투자증권 API 클라이언트 — 시세·잔고·주문 (모의투자 전용)
  market_calendar.py     # KRX 휴장일 판정
  analysts.py            # 차트·뉴스·공시 분석가
  judgment.py            # 강세/약세 토론 + 포트폴리오 매니저 (매수·매도 양쪽)
  pipeline.py            # 판단 → 게이트 → 집행 오케스트레이션, 매도 판단→집행 공유 로직
  sell.py                # 결정론적 손절/트레일링 익절
  portfolio_store.py     # logs/portfolio_state.json 로드/원자적 저장/락
  notify.py              # 텔레그램 알림 (매수/매도/스킵/에러 통일 양식)
  notion_sync.py         # 매매일지·일일 리포트 노션 동기화
  evaluation.py          # IC(information coefficient) 측정
scripts/
  decide_buys.py         # 08:30 — 신규 매수 판단만 (집행 안 함)
  execute_open.py        # 09:00 — 전날 매도 판단 + 오늘 매수 판단 집행 (매도 먼저)
  check_stop_loss.py     # 09:00–15:30 매분 — 결정론적 손절/익절만 (LLM 없음)
  decide_llm_sell.py     # 15:35 — LLM 재량 매도 판단만 (집행 안 함, 다음날 아침 집행)
  run_daily.py           # 위 네 단계를 한 번에 도는 로컬/수동 테스트용 진입점 (cron 미사용)
  setup_notion_workspace.py  # 노션 매매일지·일일 리포트 DB 최초 생성
prompts/                # LLM 프롬프트 (.md, 코드 밖에 분리)
tests/
docs/PLAN.md            # 설계 배경과 근거
```

## 실행 스케줄 (AWS EC2 cron, KST 기준)

하루 판단·집행이 4단계로 나뉘어 있다 — 각 단계가 서로 다른 이유(장 시작 전
판단은 끝나 있어야 하고, 손절 체크는 실시간이어야 하고, 재량 매도 판단은
가격 변동성이 정리된 장 마감 후가 낫다)로 시점이 갈리기 때문이다. 자세한
배경은 [`docs/PLAN.md`](docs/PLAN.md)의 "하루 파이프라인을 4단계로 분리" 참고.

| 시각 | 스크립트 | 하는 일 |
|---|---|---|
| 08:30 | `decide_buys.sh` | 유니버스 → 정량 필터 → 분석가 → 토론+매니저 → 게이트. 승인된 BUY를 `logs/pending_buys.json`에 기록만 하고 집행 안 함 |
| 09:00 | `execute_open.sh` | 전날 15:35에 정해둔 매도(`pending_sells.json`)를 먼저 집행 → 오늘 08:30에 정해둔 매수(`pending_buys.json`) 집행 |
| 09:00–15:30 (매분) | `check_stop_loss.sh` | 보유 종목 전부 결정론적 손절(-10%)/트레일링 익절(+20%)만 체크·집행. LLM 없음 |
| 15:35 | `decide_llm_sell.sh` | 보유 종목 LLM 재량 매도 판단만 (`pending_sells.json`에 기록). 장 마감 후라 집행은 다음 거래일 09:00으로 미룸 |

휴장일(주말·KRX 공휴일)은 `market_calendar.is_krx_trading_day`가 각 스크립트
맨 앞에서 걸러 LLM/KIS 호출 없이 조용히 종료한다. 전체 파이프라인이 예외로
죽거나 개별 단계(보유 종목 평가, 노션 동기화)가 실패하면 `notify.py`가
텔레그램으로 즉시 알린다.

## 설정

```bash
uv sync
```

`.env` 파일(gitignore 대상, 저장소에는 없음)을 만들고 아래 키를 채워넣는다:
- `ANTHROPIC_API_KEY` — Claude 분석가/토론/매니저 호출
- `DART_API_KEY` — 공시 수집
- `KIS_APP_KEY`, `KIS_APP_SECRET`, `KIS_ACCOUNT_NO` — 한국투자증권 **모의투자** API
  (실전투자 키 아님. 이 코드베이스는 모의투자 도메인만 호출한다)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` — 매수/매도/스킵/에러 알림 (선택,
  없으면 알림만 조용히 꺼짐)
- `NOTION_API_KEY`, `NOTION_PARENT_PAGE_ID`, `NOTION_TRADE_JOURNAL_DB_ID`,
  `NOTION_DAILY_REPORT_DB_ID`, `NOTION_INTRO_PAGE_ID` — 매매일지·일일 리포트
  동기화 (선택, `scripts/setup_notion_workspace.py`로 최초 생성)

## 테스트

```bash
.venv/bin/pytest tests/                                  # 전부 목킹, 네트워크·과금 없음
SIMA_LIVE_TEST=1 .venv/bin/pytest tests/test_live_smoke.py -v -s  # 실제 API 확인용, 최소 비용
```

## 현재 상태

- 마일스톤 1 (아무것도 사지 않는 시스템): 완료
- 마일스톤 2 (분석가 투입 — 차트·뉴스·공시): 완료
- 판단 계층 (강세/약세 토론 + 포트폴리오 매니저, 매수·매도 대칭 구조): 완료
- 시세 데이터 소스: KOSPI200 종목 리스트·업종 매핑은 Naver 스크래핑, 개별 종목 시세는 KIS API
- 정량 사전 필터 (`pipeline.quant_prefilter`): 완료 — LLM 호출 전 비용 게이트
- 매도 로직: 결정론적 안전장치(손절 -10%, 트레일링 익절, `src/sell.py`) +
  LLM 재량 매도(`judgment.judge_sell`)까지 전부 구현
- 주문 집행: 매수·매도 둘 다 KIS 모의투자 시장가 주문으로 실제 연결 완료.
  계좌·잔고·현재가 조회, 주문 접수·거부 응답까지 라이브 검증 완료
- **AWS EC2 배포 + cron 자동 실행: 완료, 실서비스 중** (모의투자, 실거래
  연결 없음 — 규칙 7). 하루 판단·집행이 4단계(08:30 매수 판단 / 09:00 집행 /
  09:00–15:30 매분 손절 체크 / 15:35 재량 매도 판단)로 나뉘어 자동 실행된다
  — 자세한 스케줄은 위 "실행 스케줄" 참고
- 휴장일 자동 판정(`market_calendar.is_krx_trading_day`), cron 실패·개별 단계
  실패 시 텔레그램 알림(`notify.py`), 매매일지·일일 리포트 노션 동기화
  (`notion_sync.py`) 전부 연결 완료
- CLAUDE.md "감시 지표"(최근 20영업일 신호 발생률, 게이트 거부 사유별 집계,
  분석가 호출수/실패율/토큰량)를 매일 cron 로그에 자동 기록
- 마일스톤 3 (IC 측정): 인프라만 구축, 실측 데이터 축적 전 — 워크포워드/실시간
  모의투자 결과가 쌓여야 측정 가능(과거 백테스트는 성공 지표로 안 씀)
