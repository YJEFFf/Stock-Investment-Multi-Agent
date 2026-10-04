"""개인 앱의 고정 잔고 조회 요청만 처리하는 별도 프로세스.

웹 프로세스에는 증권사 키를 전달하지 않는다. 입력으로 종목·URL·명령을 받지
않으며 주문/분석/일별 NAV를 호출하지 않는다. 성공 관측만 원자적으로 교체한다.
"""
import os
import signal
import socketserver
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from src.schemas import MobileAccountObservation

KST = ZoneInfo("Asia/Seoul")


def save_observation(fetch, destination: Path) -> bool:
    account = fetch()
    if account is None:
        return False
    now = datetime.now(KST)
    observation = MobileAccountObservation(observed_at=now, day=now.date(), **asdict(account))
    temporary = destination.with_suffix(".tmp")
    try:
        temporary.write_text(observation.model_dump_json())
        temporary.chmod(0o640)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def serve():
    from src import kis

    kis.TOKEN_CACHE_PATH = Path("/var/lib/sima-balance/token.json")
    destination = Path("/var/lib/sima-balance/account.json")
    socket_path = Path("/run/sima-balance/reader.sock")
    policy = kis.RetryPolicy(timeout_seconds=4, max_attempts=2, backoff_seconds=(1,))
    last_request = -float("inf")

    def deadline(*_):
        raise TimeoutError("balance deadline")

    signal.signal(signal.SIGALRM, deadline)

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            nonlocal last_request
            self.connection.settimeout(1)
            try:
                if self.rfile.readline(32) != b"refresh\n":
                    self.wfile.write(b"invalid\n")
                    return
                if time.monotonic() - last_request < 15:
                    self.wfile.write(b"busy\n")
                    return
                last_request = time.monotonic()
                signal.alarm(20)
                try:
                    success = save_observation(lambda: kis.fetch_account_snapshot(policy=policy), destination)
                finally:
                    signal.alarm(0)
                self.wfile.write(b"ok\n" if success else b"failed\n")
            except Exception:
                # 응답/계좌/인증 내용을 웹이나 로그로 유출하지 않는다.
                try:
                    self.wfile.write(b"failed\n")
                except OSError:
                    pass

    socket_path.unlink(missing_ok=True)
    with socketserver.UnixStreamServer(str(socket_path), Handler) as server:
        os.chmod(socket_path, 0o660)
        server.serve_forever()


if __name__ == "__main__":
    serve()
