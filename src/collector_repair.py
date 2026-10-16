"""Bounded premarket repair controller. Models propose data, never code.

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


def report(state, key, problem, message, *, at=None):
    """Durable incident transitions; failed enqueue never consumes the alert."""
    at = at or now()
    incident = state["incidents"].get(key)
    if problem:
        if not incident or not incident["open"]:
            incident = {"open": True, "since": at.isoformat(), "last_sent": None, "sent_problem": False}
            state["incidents"][key] = incident
        sent = datetime.fromisoformat(incident["last_sent"]) if incident["last_sent"] else None
        interval = timedelta(minutes=5 if not incident.get("escalated") else 15)
        due = not incident["sent_problem"] or sent is None or at - sent >= interval
        if due and notify.send_telegram_alert(f"⚠️ [SIMA] 수집 자동화 오류 — {key}\n{message}\n발견: {incident['since']}"):
            incident["escalated"] = incident["sent_problem"]
            incident["last_sent"] = at.isoformat()
            incident["sent_problem"] = True
    elif incident and incident["open"]:
        if not incident["sent_problem"] or notify.send_telegram_alert(f"✅ [SIMA] 수집 자동화 복구 — {key}\n{message}"):
            incident["open"] = False
            incident["recovered_at"] = at.isoformat()


def queue_notice(state, key, message):
    state.setdefault("notices", {})[key] = message
    atomic_json(STATE_DIR / "state.json", state)


def flush_notices(state):
    for key, message in list(state.get("notices", {}).items()):
        if notify.send_telegram_alert(message):
            del state["notices"][key]


def repair_window(at=None):
    at = at or now()
    return is_krx_trading_day(at.date()) and time(7, 30) <= at.time() < time(8, 20)


def require_window():
    if not repair_window() or run_history.already_analyzed(now().date().isoformat(), ROOT / "logs/pipeline.jsonl"):
        raise RuntimeError("repair window closed or analysis already started")


def require_publish_window(*, rollback=False):
    at = now()
    # Reserve ten minutes for deployment, read-only checks and a rollback.
    cutoff = time(8, 25) if rollback else time(8, 15)
    if not is_krx_trading_day(at.date()) or not time(7, 30) <= at.time() < cutoff:
        raise RuntimeError("deployment time reserve exhausted")
    if run_history.already_analyzed(at.date().isoformat(), ROOT / "logs/pipeline.jsonl"):
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
    WORK_DEADLINE = clock.monotonic() + min(600, (now().replace(hour=8, minute=15, second=0, microsecond=0) - now()).total_seconds())
    require_window()
    day = now().date().isoformat()
    if state["attempts"].get(day, 0) >= MAX_ATTEMPTS:
        raise RuntimeError("daily repair attempt budget exhausted")
    capacity = asyncio.run(codex_plan.read_capacity())
    if not capacity.buy_judgment_allowed:
        raise RuntimeError("Codex capacity unavailable for repair")
    state["attempts"][day] = state["attempts"].get(day, 0) + 1
    atomic_json(STATE_DIR / "state.json", state)  # claim before invoking a model
    audit("repair_started", day=day, attempt=state["attempts"][day])
    queue_notice(state, f"start-{day}-{state['attempts'][day]}", "🔧 [SIMA] 수집 자동 수리 시작\n수집 주소·응답 형식만 검사합니다. 매수 판단은 아직 실행하지 않습니다.")
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


def check(*, allow_repair=False, full=False):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with portfolio_lock(STATE_DIR / "worker.lock", blocking=False):
        state = read_state()
        flush_notices(state)
        if transaction_blocked():
            recover_transaction()
        state["started_at"] = now().isoformat()
        atomic_json(STATE_DIR / "state.json", state)
        current = routes.load()
        previous = state["baseline"].copy()
        result = health.probe(current, full=full)
        atomic_json(STATE_DIR / "latest.json", result)
        for name, item in result["sources"].items():
            report(state, name, item["status"] != "ok", item.get("error", "수집 경로 정상 확인"))
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
        has_shadow = any(day < at.date().isoformat() for day in state["shadow_days"])
        structural = any(v["status"] == "structure" for v in result["sources"].values())
        state["last_check"] = at.isoformat()
        atomic_json(STATE_DIR / "state.json", state)
        if allow_repair and structural and repair_window():
            try:
                if not has_shadow:
                    raise RuntimeError("첫 정상 거래일 장후 섀도 점검 전 — 자동 배포 대기")
                if os.environ.get("SIMA_COLLECTOR_AUTOREPAIR") != "1":
                    raise RuntimeError("automatic repair not enabled")
                # Keep evidence from before the repair; healthy canaries may be newer.
                for key, value in previous.items():
                    if result["sources"][key]["status"] != "ok":
                        state["baseline"][key] = value
                result = repair(state, current, result)
                atomic_json(STATE_DIR / "latest.json", result)
                for name, item in result["sources"].items():
                    state["baseline"][name] = {**item, "checked_at": result["checked_at"]}
                    report(state, name, False, "수집 설정 자동 수정·검증·배포 완료")
                report(state, "repair", False, "수집 설정 자동 배포 완료")
            except Exception as exc:
                audit("repair_failed", error=str(exc)[:1000])
                report(state, "repair", True, str(exc)[:500])
        state["completed_at"] = now().isoformat()
        state["last_check"] = state["completed_at"]
        flush_notices(state)
        atomic_json(STATE_DIR / "state.json", state)
        audit("check_completed", manifest=result["manifest"], statuses={k: v["status"] for k, v in result["sources"].items()})
        return result


def preanalysis_problem(at=None):
    """Fail closed for missing preflight/mandatory index; optional inputs degrade."""
    if os.environ.get("SIMA_COLLECTOR_AUTOREPAIR") != "1":
        return None
    at = at or now()
    if transaction_blocked():
        return "collector_deployment_incomplete"
    try:
        value = json.loads((STATE_DIR / "latest.json").read_text())
        checked = datetime.fromisoformat(value["checked_at"])
        if checked > at or at - checked > timedelta(minutes=35) or checked.date() != at.date():
            return "collector_preflight_stale"
        if value["manifest"] != routes.digest(routes.load()):
            return "collector_preflight_version_mismatch"
        if value["sources"]["index"]["status"] != "ok":
            return "collector_index_unavailable"
    except (OSError, ValueError, KeyError, TypeError):
        return "collector_preflight_missing"
    return None


def watchdog():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    # Different lock from worker so a hung worker cannot silence its watchdog.
    with portfolio_lock(STATE_DIR / "watchdog.lock", blocking=False):
        at = now()
        try:
            state = read_state()
            last = datetime.fromisoformat(state["completed_at"]) if state.get("completed_at") else None
            due = None
            if is_krx_trading_day(at.date()):
                for hh, mm, grace in ((7, 30, 5), (8, 0, 5), (8, 15, 7), (16, 20, 10)):
                    scheduled = at.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    deadline = scheduled + timedelta(minutes=grace)
                    if at >= deadline:
                        due = scheduled
            problem = due is not None and (last is None or last < due)
            message = f"예정 수집 점검 완료 기록 없음. 마지막 완료: {last}. 신규 판단은 점검 상태를 확인합니다."
        except Exception as exc:
            problem, message = True, f"수집 자동화 상태를 읽지 못함: {type(exc).__name__}"
        # Separate state file: never overwrite a concurrent worker update.
        path = STATE_DIR / "watchdog.json"
        alerts = json.loads(path.read_text()) if path.exists() else {"incidents": {}}
        report(alerts, "watchdog", problem, message, at=at)
        report(alerts, "deployment", transaction_blocked(), "수집 설정 배포가 완결되지 않았습니다. 신규 매수 판단 차단 유지.", at=at)
        alerts["checked_at"] = at.isoformat()
        atomic_json(path, alerts)
