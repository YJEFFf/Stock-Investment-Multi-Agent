"""Workflow-triggered bounded off-hours repair. Models propose data.

Trusted Python builds a two-file commit (routes + changelog), tests it, obtains
an independent review bound to that exact commit, then uses deploy/pull.sh.
No order functions, prompts, or investment policy can enter that diff.
"""
import asyncio
import json
import os
import signal
import subprocess
import tempfile
import time as clock
from uuid import uuid4
from datetime import datetime, time, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool

from src import codex_plan, collector_health as health, collector_routes as routes, notify, run_history
from src.market_calendar import is_krx_trading_day
from src.portfolio_store import portfolio_lock

ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = ROOT / "logs/collector_repair"
MAX_ATTEMPTS = 2
WORK_DEADLINE = None
LABELS = {"repair": "자동 수리", "deployment": "배포 복구", "watchdog": "감시 점검",
          "worker_process": "수집 점검 프로그램", "watchdog_process": "완료 감시 프로그램",
          "index": "코스피200 지수", "stock_news": "종목 뉴스", "sector_news": "업종 뉴스",
          "sector_groups": "업종 목록", "sector_members": "업종 구성종목", "quotes": "보조 시세"}
IMPACTS = {"index": "장전 필수 입력 불가 시 신규 매수 판단 차단",
           "stock_news": "종목 뉴스 결손 시 해당 분석 의견 제외·기존 degraded 기준 적용",
           "sector_news": "업종 배경 뉴스 누락 가능", "sector_groups": "업종 분류 갱신 불가 가능",
           "sector_members": "업종 분류 갱신 불가 가능", "quotes": "한투 시세 실패 시 보조 시세 사용 불가 가능"}


def budget(cap):
    if WORK_DEADLINE is None:
        return cap
    remaining = WORK_DEADLINE - clock.monotonic()
    if remaining <= 0:
        raise RuntimeError("repair work time budget exhausted")
    return min(cap, remaining)


class Proposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    supported: StrictBool
    manifest_json: str
    reason: str


class Review(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: StrictBool
    commit: str
    manifest_sha256: str
    reason: str


def now():
    return datetime.now(health.KST)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def read_state():
    path = STATE_DIR / "state.json"
    if not path.exists():
        return {"incidents": {}, "baseline": {}, "attempts": {}, "shadow_days": []}
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(k in value for k in ("incidents", "baseline", "attempts", "shadow_days")):
        raise ValueError("repair state corrupt")
    return value


def audit(event, **fields):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with (STATE_DIR / "events.jsonl").open("a") as f:
        f.write(json.dumps({"at": now().isoformat(), "event": event, **fields}, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())


def report(state, key, problem, message, *, at=None, phase=None):
    """Durable incident transitions; failed enqueue never consumes the alert."""
    at = at or now()
    incident = state["incidents"].get(key)
    if problem:
        if not incident or not incident["open"]:
            incident = {"open": True, "since": at.isoformat(), "last_sent": None, "sent_problem": False}
            state["incidents"][key] = incident
        # Only a new incident or a changed repair stage warrants another alert.
        due = not incident["sent_problem"] or incident.get("phase") != phase
        if due and notify.send_telegram_alert(f"⚠️ [SIMA] 수집 자동화 오류 — {LABELS.get(key, key)}\n{message}\n발견: {incident['since']}"):
            incident["escalated"] = incident["sent_problem"]
            incident["last_sent"] = at.isoformat()
            incident["sent_problem"] = True
            incident["phase"] = phase
    elif incident and incident["open"]:
        if not incident["sent_problem"] or notify.send_telegram_alert(f"✅ [SIMA] 수집 자동화 복구 — {LABELS.get(key, key)}\n{message}"):
            incident["open"] = False
            incident["recovered_at"] = at.isoformat()


def queue_notice(state, key, message):
    state.setdefault("notices", {})[key] = message
    atomic_json(STATE_DIR / "state.json", state)


def flush_notices(state):
    for key, message in list(state.get("notices", {}).items()):
        if notify.send_telegram_alert(message):
            del state["notices"][key]


def repair_progress(state, key, message):
    queue_notice(state, key, f"🔧 [SIMA] 오류 수정 진행\n{message}")
    flush_notices(state)
    atomic_json(STATE_DIR / "state.json", state)


def read_workflow():
    path = STATE_DIR / "workflow.json"
    return json.loads(path.read_text()) if path.exists() else {"requests": {}, "days": {}, "incidents": {}}


def request_identity(slots=None, params=None):
    return json.dumps({"slots": slots or {}, "params": params or {}}, sort_keys=True)


def request_repair(source, error, *, slots=None, params=None):
    """Called only after a real collector failed. Never fetch, analyze or deploy."""
    if source not in routes.DEFAULT_URLS:
        raise ValueError("unknown collector source")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "workflow.lock"):
        at = now()
        state = read_workflow()
        entry = state["requests"].setdefault(source, {"since": at.isoformat()})
        entry.update(generation=uuid4().hex, last_at=at.isoformat(), error=error[:500])
        entry.setdefault("samples", {})[request_identity(slots, params)] = {"slots": slots or {}, "params": params or {}}
        state["days"].setdefault(at.date().isoformat(), {})["failed"] = True
        phase = "waiting_close" if not repair_window(at) else "queued"
        message = ("장중 변경을 보류하고 16:10부터 원인 확인·수리를 진행합니다." if phase == "waiting_close"
                   else "발견된 오류를 다음 수리 처리에서 확인합니다.")
        # Save the request before attempting notification delivery.
        atomic_json(STATE_DIR / "workflow.json", state)
        report(state, source, True, f"{error[:400]}\n{message}", phase=phase)
        atomic_json(STATE_DIR / "workflow.json", state)


def source_succeeded(source, payload=None, *, slots=None, params=None, data=None, complete=False):
    """Reuse actual successful responses; never make a canary request here."""
    at = now()
    if data is None and payload is not None:
        try:
            normalized = routes.normalize(source, payload)
            if source == "index":
                data = health.index_data(normalized, at)
            elif source == "stock_news" and (slots or {}).get("ticker") in health.CANARIES:
                data = {(slots or {})["ticker"]: health.news_data(normalized, at)}
            elif source == "sector_news":
                data = health.news_data(normalized, at, hub=True)
            elif source == "quotes":
                rows = [r for r in json.loads(normalized)["datas"] if r.get("itemCode") in health.CANARIES]
                data = health.quotes_data(json.dumps({"datas": rows}), at)
        except (ValueError, KeyError, TypeError):
            # Normal empty news is valid. It simply provides no comparison evidence.
            data = None
    if data is None and source not in read_workflow()["requests"]:
        return
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "workflow.lock"):
        state = read_workflow()
        if data is not None:
            baseline = state.setdefault("baseline", {})
            if source == "stock_news":
                data = {**baseline.get(source, {}).get("data", {}), **data}
            if source != "stock_news" or set(data) == set(health.CANARIES):
                baseline[source] = {"status": "ok", "data": data, "checked_at": at.isoformat()}
            else:
                baseline[source] = {"status": "partial", "data": data, "checked_at": at.isoformat()}
        entry = state["requests"].get(source)
        if entry:
            if complete:
                entry["samples"] = {}
            else:
                entry.get("samples", {}).pop(request_identity(slots, params), None)
            if not entry.get("samples"):
                report(state, source, False, "실제 워크플로우에서 실패했던 요청의 정상 응답을 확인했습니다.")
                if not state["incidents"].get(source, {}).get("open"):
                    del state["requests"][source]
        atomic_json(STATE_DIR / "workflow.json", state)


def workflow_finished(stage, *, healthy):
    """Passive shadow: outcomes of existing jobs, with no extra source probes."""
    if os.environ.get("SIMA_COLLECTOR_AUTOREPAIR") != "1":
        return
    if stage not in {"premarket", "afterclose"}:
        raise ValueError("unknown workflow stage")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "workflow.lock"):
        at = now()
        state = read_workflow()
        day = state["days"].setdefault(at.date().isoformat(), {})
        in_stage = ((stage == "premarket" and time(8) <= at.time() < time(9))
                    or (stage == "afterclose" and at.time() >= time(16)))
        if not healthy:
            day["failed"] = True
        elif in_stage and is_krx_trading_day(at.date()):
            day[stage] = routes.digest(routes.load())
        atomic_json(STATE_DIR / "workflow.json", state)


def shadow_ready(state, at):
    if any(day < at.date().isoformat() for day in state["shadow_days"]):
        return True
    for day, obs in read_workflow()["days"].items():
        if (day < at.date().isoformat() and not obs.get("failed") and obs.get("premarket")
                and obs.get("premarket") == obs.get("afterclose")):
            return True
    return False


def repair_window(at=None):
    at = at or now()
    return not is_krx_trading_day(at.date()) or at.time() < time(8, 15) or at.time() >= time(16, 10)


def next_repair_time(at=None):
    at = at or now()
    return at if repair_window(at) else at.replace(hour=16, minute=10, second=0, microsecond=0)


def attempt_window(at=None):
    at = at or now()
    slot = "closed_day" if not is_krx_trading_day(at.date()) else ("after_close" if at.time() >= time(16, 10) else "before_open")
    return f"{at.date().isoformat()}:{slot}"


def current_analysis_blocks(at):
    # A completed morning run is not a reason to postpone after-close repairs.
    # Never clear its history or invoke it again; active runs are excluded by lock.
    return (is_krx_trading_day(at.date()) and at.time() < time(16, 10)
            and run_history.already_analyzed(at.date().isoformat(), ROOT / "logs/pipeline.jsonl"))


def attempts_used(state, at):
    slot = attempt_window(at)
    return state["attempts"].get(slot, 0) + (state["attempts"].get(at.date().isoformat(), 0) if slot.endswith(":before_open") else 0)


def require_window():
    if not repair_window() or current_analysis_blocks(now()):
        raise RuntimeError("repair window closed or analysis already started")


def require_publish_window(*, rollback=False):
    at = now()
    # Reserve ten minutes for deployment, read-only checks and a rollback.
    cutoff = time(8, 23) if rollback else time(8, 15)
    if is_krx_trading_day(at.date()) and cutoff <= at.time() < time(16, 10):
        raise RuntimeError("deployment time reserve exhausted")
    if current_analysis_blocks(at):
        raise RuntimeError("analysis already started")


def git(*args, cwd=ROOT, timeout=30):
    proc = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, timeout=timeout)
    if proc.returncode:
        # Never log git remote credentials or inherited environment.
        raise RuntimeError(f"git {args[0]} failed ({proc.returncode})")
    return proc.stdout.strip()


def model_call(prompt_name, payload, response_model, *, search=False):
    env = codex_plan._subscription_environment()
    codex_plan._ensure_filesystem_isolation(env, timeout_s=budget(15))
    codex_plan._ensure_chatgpt_login(env, timeout_s=budget(15))
    prompt = (ROOT / "prompts" / prompt_name).read_text() + "\n\nUNTRUSTED INPUT DATA:\n" + json.dumps(payload, ensure_ascii=False)
    with tempfile.TemporaryDirectory(prefix="sima-repair-model-") as tmp:
        path = Path(tmp)
        path.chmod(0o733)
        schema = path / "schema.json"
        schema.write_text(json.dumps(response_model.model_json_schema()))
        schema.chmod(0o644)
        output = path / "result.json"
        args = (["--search"] if search else []) + [
            "exec", "--json", "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
            "--sandbox", "read-only", "--model", codex_plan.DEFAULT_MODEL,
            "--output-schema", str(schema), "--output-last-message", str(output), "-C", tmp, "-",
        ]
        started = clock.monotonic()
        result = codex_plan._run(codex_plan._codex_command(*args), env=env,
                                 input_text=prompt, cwd=tmp, timeout_s=budget(120))
        audit("model_call", purpose=prompt_name, success=result.returncode == 0,
              elapsed_s=round(clock.monotonic() - started, 2), usage=codex_plan._usage_from_events(result.stdout))
        if result.returncode:
            raise RuntimeError("repair model unavailable")
        return response_model.model_validate_json(output.read_text())


def run_tests(worktree):
    result = subprocess.run([str(ROOT / ".venv/bin/python"), "-m", "pytest", "-q"],
                            cwd=worktree, text=True, capture_output=True, timeout=budget(60))
    if result.returncode:
        raise RuntimeError("candidate test suite failed: " + result.stdout[-1000:])
    return result.stdout[-500:]


def build_commit(worktree, manifest, base, *, rollback=False):
    (worktree / "config/collector_routes.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    path = worktree / "docs/CHANGELOG.md"
    text = path.read_text()
    action = "자동 수리 롤백" if rollback else "자동 수리"
    entry = (f"\n## {now():%Y-%m-%d} (수집 설정 {action})\n\n"
             f"- 기준 코드 `{base}`에서 수집 URL/응답 키 설정만 변경.\n"
             f"- 설정 SHA256 `{routes.digest(manifest)}`. 전체 테스트·데이터 대조·독립 검토 후\n"
             "  배포기가 반영한다. 최종 성공/롤백과 리뷰 커밋은 logs/collector_repair/events.jsonl에 기록.\n"
             "- 투자 정책·주문 코드·분석 이력 변경 없음.\n")
    marker = "\n---\n"
    if marker not in text:
        raise RuntimeError("changelog header changed")
    path.write_text(text.replace(marker, marker + entry, 1))
    git("add", "config/collector_routes.json", "docs/CHANGELOG.md", cwd=worktree)
    git("-c", "user.name=SIMA collector repair", "-c", "user.email=sima-repair@localhost",
        "commit", "-m", "Restore prior collector routes" if rollback else "Repair validated Naver collector routes", cwd=worktree)
    commit = git("rev-parse", "HEAD", cwd=worktree)
    changed = set(git("diff", "--name-only", base, commit, cwd=worktree).splitlines())
    if changed != {"config/collector_routes.json", "docs/CHANGELOG.md"}:
        raise RuntimeError("repair changed protected files")
    return commit


def deploy(commit):
    process = subprocess.Popen([str(ROOT / "deploy/pull.sh")], cwd=ROOT,
                               env={**os.environ, "SIMA_REVIEWED": commit},
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                               start_new_session=True)
    try:
        process.communicate(timeout=90)
    except subprocess.TimeoutExpired:
        # Kill the whole deployment process group before touching the repo again.
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        raise RuntimeError("deployment time budget exhausted")
    if process.returncode or git("rev-parse", "HEAD") != commit:
        raise RuntimeError("automatic deployment failed")


def rollback_candidate(tree, current, commit):
    # Keep both the attempted repair and rollback in CHANGELOG (git revert would
    # remove the repair entry). Only restore the previously reviewed config.
    require_publish_window(rollback=True)
    rollback = build_commit(tree, current, commit, rollback=True)
    txn = json.loads((STATE_DIR / "transaction.json").read_text())
    atomic_json(STATE_DIR / "transaction.json", {**txn, "status": "rolling_back", "rollback": rollback})
    git("push", "origin", f"{rollback}:refs/heads/main", cwd=tree)
    deploy(rollback)
    audit("rollback", commit=commit, rollback=rollback, success=True)
    atomic_json(STATE_DIR / "transaction.json", {"status": "rolled_back", "commit": commit, "rollback": rollback})


def transaction_blocked():
    path = STATE_DIR / "transaction.json"
    if not path.exists():
        return False
    try:
        return json.loads(path.read_text()).get("status") not in {"complete", "rolled_back", "not_pushed"}
    except (ValueError, OSError):
        return True


def recover_transaction():
    """Recover a process crash without rerunning any analysis or order."""
    if not transaction_blocked():
        return
    require_window()
    txn = json.loads((STATE_DIR / "transaction.json").read_text())
    base, commit = txn["base"], txn["commit"]
    git("fetch", "--quiet", "origin")
    remote, head = git("rev-parse", "origin/main"), git("rev-parse", "HEAD")
    if txn.get("rollback") and remote == txn["rollback"] and head in {base, commit, txn["rollback"]}:
        with portfolio_lock(run_history.RUN_LOCK_PATH, blocking=False):
            require_publish_window(rollback=True)
            deploy(txn["rollback"])
            atomic_json(STATE_DIR / "transaction.json", {**txn, "status": "rolled_back"})
        routes.load.cache_clear()
        return
    if remote == base and head == base:
        atomic_json(STATE_DIR / "transaction.json", {"status": "not_pushed", "commit": commit})
        return
    if remote != commit or head not in {base, commit}:
        raise RuntimeError("incomplete deployment differs from recorded commit; manual review required")
    manifest = routes.validate_manifest(json.loads(git("show", f"{base}:config/collector_routes.json")))
    with portfolio_lock(run_history.RUN_LOCK_PATH, blocking=False):
        with tempfile.TemporaryDirectory(prefix="sima-route-recovery-", dir=ROOT.parent) as temp:
            tree = Path(temp) / "repo"
            git("worktree", "add", "--quiet", "--detach", str(tree), commit)
            try:
                rollback_candidate(tree, manifest, commit)
            finally:
                git("worktree", "remove", "--force", str(tree))
    routes.load.cache_clear()


def compatibility_evidence(previous, checked, changed):
    evidence = {}
    for name in changed:
        old, new = previous[name]["data"], checked["sources"][name]["data"]
        if name == "stock_news":
            pairs = {code: (old[code], new[code]) for code in health.CANARIES}
        else:
            pairs = {name: (old, new)}
        evidence[name] = {}
        for key, (left, right) in pairs.items():
            common = sorted(set(left) & set(right))
            evidence[name][key] = {"old_count": len(left), "new_count": len(right),
                                  "matching_ids": len(common), "old_checked_at": previous[name].get("checked_at"),
                                  "samples": [{"id": k, "old": left[k], "new": right[k]} for k in common[:4]]}
    return evidence


def repair(state, current, report_data):
    global WORK_DEADLINE
    # Ten minutes for proposal/review, separate bounded activation/rollback.
    at = now()
    seconds = 600
    if is_krx_trading_day(at.date()) and at.time() < time(8, 15):
        seconds = min(seconds, (at.replace(hour=8, minute=15, second=0, microsecond=0) - at).total_seconds())
    WORK_DEADLINE = clock.monotonic() + seconds
    require_window()
    slot = attempt_window(at)
    if attempts_used(state, at) >= MAX_ATTEMPTS:
        raise RuntimeError("repair session attempt budget exhausted")
    capacity = asyncio.run(codex_plan.read_capacity())
    if not capacity.buy_judgment_allowed:
        raise RuntimeError("Codex capacity unavailable for repair")
    state["attempts"][slot] = state["attempts"].get(slot, 0) + 1
    atomic_json(STATE_DIR / "state.json", state)  # claim before invoking a model
    audit("repair_started", slot=slot, attempt=state["attempts"][slot])
    queue_notice(state, f"start-{slot}-{state['attempts'][slot]}", "🔧 [SIMA] 수집 자동 수리 시작\n수집 주소·응답 필드를 수정하고 데이터 대조·전체 테스트·독립 검토 후 배포합니다. 성공한 분석은 다시 실행하지 않습니다.")
    flush_notices(state)
    if not git("status", "--porcelain", "--untracked-files=no") == "":
        raise RuntimeError("production checkout has changes")
    git("fetch", "--quiet", "origin")
    base = git("rev-parse", "HEAD")
    if git("rev-parse", "origin/main") != base:
        raise RuntimeError("upstream moved; automatic merge forbidden")
    failed = {k: v for k, v in report_data["sources"].items() if v["status"] == "structure"}
    proposal = model_call("collector_repair.md", {"manifest": current, "failures": failed,
                          "allowed_hosts": sorted(routes.HOSTS), "allowed_keys": {k: sorted(v) for k, v in routes.KEYS.items()}}, Proposal, search=True)
    require_window()
    if not proposal.supported:
        raise RuntimeError("repair requires manual change: " + proposal.reason[:300])
    candidate = routes.validate_manifest(json.loads(proposal.manifest_json))
    changed = {k for k in current["routes"] if current["routes"][k] != candidate["routes"][k]}
    if not changed or not changed <= set(failed):
        raise RuntimeError("proposal changed healthy or unrelated routes")
    repair_progress(state, f"validate-{slot}-{state['attempts'][slot]}", "수정안을 만들었습니다. 실제 데이터와 과거 정상 자료를 대조하고 전체 테스트를 실행합니다.")
    checked = health.probe(candidate, full=True, timeout_s=budget(90))
    if any(v["status"] != "ok" for v in checked["sources"].values()):
        raise RuntimeError("candidate live validation failed")
    health.validate_compatibility(state["baseline"], checked, changed)
    require_window()
    with tempfile.TemporaryDirectory(prefix="sima-route-worktree-", dir=ROOT.parent) as temp:
        tree = Path(temp) / "repo"
        git("worktree", "add", "--quiet", "--detach", str(tree), base)
        try:
            commit = build_commit(tree, candidate, base)
            tests = run_tests(tree)
            repair_progress(state, f"review-{commit}", "데이터 대조와 전체 테스트를 통과했습니다. 독립 검토를 진행합니다.")
            diff = git("diff", base, commit, "--", "config/collector_routes.json", cwd=tree)
            review = model_call("collector_repair_review.md", {"commit": commit, "manifest_sha256": routes.digest(candidate),
                                "diff": diff, "test_result": tests, "changed": sorted(changed),
                                "live_validation": {k: {"status": v["status"], "records": len(v.get("data", {}))} for k, v in checked["sources"].items()},
                                "proposal_reason": proposal.reason,
                                "compatibility_evidence": compatibility_evidence(state["baseline"], checked, changed)}, Review)
            if not review.approved or review.commit != commit or review.manifest_sha256 != routes.digest(candidate):
                raise RuntimeError("independent review rejected candidate: " + review.reason[:300])
            audit("review_approved", commit=commit, manifest=review.manifest_sha256, reason=review.reason[:1000])
            require_window()
            # Lock is shared with decide_buys; it also excludes collection-only runs.
            with portfolio_lock(run_history.RUN_LOCK_PATH, blocking=False):
                require_window()
                if git("rev-parse", "HEAD") != base or git("status", "--porcelain", "--untracked-files=no"):
                    raise RuntimeError("production changed during repair")
                require_publish_window()
                repair_progress(state, f"publish-{commit}", "독립 검토를 통과했습니다. 서버에 배포한 뒤 실제 수집 결과를 확인합니다.")
                # Normal fast-forward push rejects concurrent upstream changes.
                atomic_json(STATE_DIR / "transaction.json", {"status": "publishing", "base": base, "commit": commit})
                try:
                    git("push", "origin", f"{commit}:refs/heads/main", cwd=tree)
                    require_window()
                    deploy(commit)
                    # Probe explicit artifact; controller still has old modules pinned.
                    after = health.probe(candidate, full=True, timeout_s=90)
                    if any(v["status"] != "ok" for v in after["sources"].values()):
                        raise RuntimeError("post-deployment check failed")
                except Exception:
                    # push may have succeeded despite a lost response. Fetch the
                    # actual remote before deciding what can safely be restored.
                    git("fetch", "--quiet", "origin")
                    remote = git("rev-parse", "origin/main")
                    if remote == commit:
                        rollback_candidate(tree, current, commit)
                        queue_notice(state, f"rollback-{commit}", "↩️ [SIMA] 자동 배포 실패·이전 수집 설정 복원 완료\n이전 설정도 오류가 있을 수 있어 다음 점검까지 정상으로 처리하지 않습니다.")
                    elif remote == base and git("rev-parse", "HEAD") == base:
                        atomic_json(STATE_DIR / "transaction.json", {"status": "not_pushed", "commit": commit})
                    # Unknown state remains blocked and is visible to watchdog.
                    raise
                atomic_json(STATE_DIR / "transaction.json", {"status": "complete", "commit": commit})
                audit("deployed", commit=commit, manifest=routes.digest(candidate))
                queue_notice(state, f"success-{commit}", f"✅ [SIMA] 수집 자동 수정·배포 완료\n변경: {', '.join(sorted(changed))}\n전체 테스트·데이터 대조·독립 검토 통과\n버전: {commit[:12]} · 장전 분석은 예정 시각에 한 번 실행")
                return after
        finally:
            git("worktree", "remove", "--force", str(tree))


def repair_disposition(state, at, *, enabled):
    """Explain why a queued structural failure can or cannot be repaired now."""
    if not repair_window(at):
        return "waiting_close", f"장전 분석·장중·장후 정산 보호 시간입니다. {next_repair_time(at):%m/%d %H:%M}부터 재확인 후 자동 수리합니다."
    if transaction_blocked():
        return "transaction", "미완결 배포 복구 중입니다. 신규 매수 판단 차단 유지."
    if current_analysis_blocks(at):
        return "analysis", "오늘 분석이 이미 시작되어 변경을 보류합니다. 16:10 이후 다시 확인합니다."
    if not enabled or os.environ.get("SIMA_COLLECTOR_AUTOREPAIR") != "1":
        return "disabled", "자동 수리 비활성 상태입니다. 실제 업무의 오류 알림은 유지합니다."
    if not shadow_ready(state, at):
        return "shadow", "첫 정상 거래일의 매수 판단·장후 측정 완료 기록을 기다립니다. 완료 다음 날부터 자동 수리합니다."
    slot = attempt_window(at)
    # Count any attempts made by the previous premarket-only controller too.
    if attempts_used(state, at) >= MAX_ATTEMPTS:
        return "budget", "이번 시간대 자동 수리 2회 한도 도달. 미해결 요청을 보존하고 다음 거래일 장후 수리에서 다시 확인합니다."
    retry = state.get("retry", {})
    if retry.get("slot") == slot and at < datetime.fromisoformat(retry["after"]):
        return "cooldown", f"직전 수리 실패: {retry.get('error', '')}. {retry['after']} 이후 예약된 수리 회차에서 재시도합니다."
    return "ready", "장외 오류를 확인했습니다. 자동 수리·검증·배포를 시작합니다."


def scheduled_full(at):
    return is_krx_trading_day(at.date()) and (at.hour, at.minute) in {(7, 30), (16, 10), (16, 20)}


def check(*, allow_repair=False, full=False, monitoring=False):
    global WORK_DEADLINE
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "worker.lock", blocking=False):
        state = read_state()
        flush_notices(state)
        state["started_at"] = now().isoformat()
        atomic_json(STATE_DIR / "state.json", state)
        if transaction_blocked():
            message = "미완결 배포 복구 대기. 16:10 이후 복구하며 신규 매수 판단은 차단합니다."
            if repair_window():
                try:
                    recover_transaction()
                    txn = json.loads((STATE_DIR / "transaction.json").read_text())
                    outcome = "이전 수집 설정 복원 완료" if txn.get("status") == "rolled_back" else "미게시 변경 정리 완료"
                    queue_notice(state, f"transaction-{state['started_at']}", f"↩️ [SIMA] 중단된 수집 배포 정리 완료\n{outcome}. 현재 수집 상태를 다시 확인합니다. 성공한 분석은 다시 실행하지 않습니다.")
                except Exception as exc:
                    message = f"미완결 배포 복구 실패: {str(exc)[:300]}. 신규 매수 판단 차단 유지."
            report(state, "deployment", transaction_blocked(), message, phase="recovering" if repair_window() else "waiting_close")
        else:
            report(state, "deployment", False, "미완결 배포 없음")
        current = routes.load()
        for name, item in read_workflow().get("baseline", {}).items():
            if item.get("status") == "ok" and item.get("checked_at", "") > state["baseline"].get(name, {}).get("checked_at", ""):
                state["baseline"][name] = item
        previous = state["baseline"].copy()
        full = full or (monitoring and scheduled_full(now()))
        result = health.probe(current, full=full, monitoring=monitoring)
        atomic_json(STATE_DIR / "latest.json", result)
        at = now()
        phase, explanation = repair_disposition(state, at, enabled=allow_repair)
        pending = state.setdefault("pending_repairs", {})
        for name, item in result["sources"].items():
            status = item["status"]
            detail = "수집 경로 정상 확인. 대기 중이던 수리는 취소합니다."
            item_phase = status
            if status == "structure":
                pending.setdefault(name, {"since": at.isoformat()})
                pending[name].update(status=phase, error=item.get("error"), checked_at=at.isoformat())
                detail, item_phase = explanation, phase
            else:
                pending.pop(name, None)
                if status != "ok":
                    detail = ("일시 오류로 짧은 재시도 후 다음 예약된 수리 회차를 기다립니다." if status in {"transient", "dependency"}
                              else "데이터 의미·최신성 검증 실패로 주소 자동 변경 대상이 아닙니다. 원인 확인이 필요합니다.")
            report(state, name, status != "ok", f"{item.get('error', '')}\n{detail}\n영향: {IMPACTS.get(name, '')}", phase=item_phase)
            if item["status"] == "ok" and (name != "sector_members" or full):
                state["baseline"][name] = {**item, "checked_at": result["checked_at"]}
        all_ok = all(v["status"] == "ok" for v in result["sources"].values())
        at = now()
        observation = state.setdefault("shadow_observations", {}).setdefault(at.date().isoformat(), {})
        if not all_ok:
            observation["failed"] = True
        if all_ok and full and time(7, 30) <= at.time() < time(8, 25):
            observation["premarket"] = result["manifest"]
        if (all_ok and full and observation.get("premarket") == result["manifest"] and not observation.get("failed")
                and is_krx_trading_day(at.date()) and at.time() >= time(16, 10)):
            if at.date().isoformat() not in state["shadow_days"]:
                state["shadow_days"].append(at.date().isoformat())
        state["last_check"] = at.isoformat()
        atomic_json(STATE_DIR / "state.json", state)
        if pending and phase == "ready":
            try:
                # Keep evidence from before the repair; healthy canaries may be newer.
                for key, value in previous.items():
                    if result["sources"][key]["status"] != "ok":
                        state["baseline"][key] = value
                result = repair(state, current, result)
                pending.clear()
                state.pop("retry", None)
                atomic_json(STATE_DIR / "latest.json", result)
                for name, item in result["sources"].items():
                    state["baseline"][name] = {**item, "checked_at": result["checked_at"]}
                    report(state, name, False, "수집 설정 자동 수정·검증·배포 완료")
                report(state, "repair", False, "수집 설정 자동 배포 완료")
            except Exception as exc:
                audit("repair_failed", error=str(exc)[:1000])
                state["retry"] = {"slot": attempt_window(), "after": (now() + timedelta(minutes=30)).isoformat(), "error": str(exc)[:500]}
                next_phase, next_step = repair_disposition(state, now(), enabled=allow_repair)
                for entry in pending.values():
                    entry["status"] = next_phase
                report(state, "repair", True, f"수리 실패: {str(exc)[:500]}\n검증을 통과한 변경만 반영합니다.\n다음 처리: {next_step}", phase="failed")
            finally:
                WORK_DEADLINE = None
        elif all_ok:
            state.pop("retry", None)
            report(state, "repair", False, "수집 경로 정상 확인. 추가 수리 없음.")
        state["completed_at"] = now().isoformat()
        state["last_check"] = state["completed_at"]
        flush_notices(state)
        atomic_json(STATE_DIR / "state.json", state)
        audit("check_completed", manifest=result["manifest"], statuses={k: v["status"] for k, v in result["sources"].items()})
        return result


def preanalysis_problem(at=None):
    """No periodic preflight dependency. Actual input validation stays mandatory."""
    if os.environ.get("SIMA_COLLECTOR_AUTOREPAIR") != "1":
        return None
    if transaction_blocked():
        return "collector_deployment_incomplete"
    return None


def process_pending():
    """Drain real incidents only. An empty queue performs no probes or alerts."""
    requests = {k: v for k, v in read_workflow()["requests"].items()
                if v.get("diagnosed_generation") != v["generation"]}
    state = read_state()
    blocked = transaction_blocked()
    if not requests and not blocked:
        # Deliver only already-pending incident progress messages, never health notices.
        if state.get("notices"):
            with portfolio_lock(STATE_DIR / "worker.lock", blocking=False):
                state = read_state()
                flush_notices(state)
                atomic_json(STATE_DIR / "state.json", state)
        return None
    watchdog()
    phase, message = repair_disposition(state, now(), enabled=True)
    if phase != "ready" and not (blocked and repair_window()):
        with portfolio_lock(STATE_DIR / "workflow.lock"):
            workflow = read_workflow()
            for source in workflow["requests"]:
                report(workflow, source, True, message, phase=phase)
            atomic_json(STATE_DIR / "workflow.json", workflow)
        return None
    result = check(allow_repair=True, full=True)
    with portfolio_lock(STATE_DIR / "workflow.lock"):
        workflow = read_workflow()
        for source, request in requests.items():
            current = workflow["requests"].get(source)
            if (current and current["generation"] == request["generation"]
                    and all(v["status"] == "ok" for v in result["sources"].values()) and not transaction_blocked()):
                # Canaries do not prove the original ticker/group request recovered.
                # Do not probe again without a new workflow failure. Actual success
                # of every recorded request is the only way to close this incident.
                current["diagnosed_generation"] = request["generation"]
                report(workflow, source, True, "수리 점검 경로는 정상입니다. 원래 실패한 업무 요청의 복구 여부는 다음 정규 실행에서 확인합니다.", phase="awaiting_workflow")
        atomic_json(STATE_DIR / "workflow.json", workflow)
    return result


def watchdog():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Different lock from worker so a hung worker cannot silence its watchdog.
    with portfolio_lock(STATE_DIR / "watchdog.lock", blocking=False):
        at = now()
        try:
            state = read_state()
            last = datetime.fromisoformat(state["completed_at"]) if state.get("completed_at") else None
            started = datetime.fromisoformat(state["started_at"]) if state.get("started_at") else None
            running = started is not None and (last is None or started > last)
            problem = running and not timedelta(0) <= at - started <= timedelta(minutes=20)
            message = f"오류 수리 작업이20분 넘게 완료되지 않았습니다. 마지막 완료: {last}."
        except Exception as exc:
            problem, message = True, f"수집 자동화 상태를 읽지 못함: {type(exc).__name__}"
        # Separate state file: never overwrite a concurrent worker update.
        path = STATE_DIR / "watchdog.json"
        alerts = json.loads(path.read_text()) if path.exists() else {"incidents": {}}
        report(alerts, "watchdog", problem, message, at=at)
        report(alerts, "deployment", transaction_blocked(), "수집 설정 배포가 완결되지 않았습니다. 신규 매수 판단 차단 유지.", at=at)
        alerts["checked_at"] = at.isoformat()
        atomic_json(path, alerts)


def controller_status(kind, error=None):
    """Track failures of the worker/watchdog without racing worker state writes."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "controller.lock", blocking=False):
        path = STATE_DIR / "controller.json"
        alerts = json.loads(path.read_text()) if path.exists() else {"incidents": {}}
        report(alerts, kind, error is not None, error or "감시 프로그램 정상 실행 재개", phase="failed" if error else None)
        atomic_json(path, alerts)
