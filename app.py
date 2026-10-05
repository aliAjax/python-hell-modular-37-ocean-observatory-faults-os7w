import argparse
import signal
from pathlib import Path

from src.http_api import create_server
from src.recovery import RecoveryService
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


def main(argv=None):
    parser = argparse.ArgumentParser(description="海底观测网设备故障管理")
    parser.add_argument("--db", default="./data.db", help="SQLite database path")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8337)
    args = parser.parse_args(argv)

    repository = SQLiteRepository(args.db)
    rules = RuleEngine()
    service = DomainService(repository, rules)
    recovery = RecoveryService(repository, rules, service)
    static_dir = Path(__file__).resolve().parent / "static"
    server = create_server(args.host, args.port, service, rules, str(static_dir), recovery)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    interrupted = recovery.recover_interrupted()
    for chain in interrupted:
        print("recovery chain %s resumed from breakpoint -> %s" % (chain["id"], chain["status"]), flush=True)
    try:
        print("海底观测网设备故障管理 listening on http://%s:%s" % (args.host, args.port), flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
