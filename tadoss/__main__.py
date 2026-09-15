import argparse
import os

import uvicorn

from .telemetry import configure_logging


def main():
    parser = argparse.ArgumentParser(description="TADOSS service and schema management")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "migrate"], default="serve"
    )
    args = parser.parse_args()
    configure_logging()
    if args.command == "migrate":
        from .api import env_flag
        from .store import DEFAULT_DATABASE, create_pool, initialize

        database = os.getenv("TADOSS_DATABASE") or (
            DEFAULT_DATABASE if env_flag("TADOSS_DEV", False) else None
        )
        if database is None:
            raise SystemExit("Set TADOSS_DATABASE")
        # The database may be restarting when a migration Job starts, so wait for it.
        timeout = float(os.getenv("TADOSS_MIGRATE_TIMEOUT", "300"))
        with create_pool(database, min_size=1, max_size=1, timeout=timeout) as pool:
            initialize(pool)
        return
    uvicorn.run(
        "tadoss.api:create_app",
        factory=True,
        host=os.getenv("TADOSS_HOST", "127.0.0.1"),
        port=int(os.getenv("TADOSS_PORT", "8000")),
        log_config=None,
        access_log=False,  # TADS permits query-string credentials.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
