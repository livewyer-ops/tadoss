import argparse

import uvicorn

from . import RELEASE
from .settings import Settings
from .telemetry import configure_logging


def main() -> None:
    parser = argparse.ArgumentParser(
        description="TADOSS API, worker and database commands"
    )
    parser.add_argument(
        "command",
        nargs="?",
        choices=["serve", "worker", "migrate", "schema-check"],
        default="serve",
    )
    parser.add_argument("--target", default=RELEASE["schemaTarget"])
    args = parser.parse_args()
    if args.target != RELEASE["schemaTarget"]:
        raise SystemExit("Image does not support the selected schema target")
    settings = Settings.load()
    configure_logging(settings.log_level, settings.log_format)
    if args.command in {"migrate", "schema-check"}:
        from tadoss.adapters.postgres import (
            check_schema,
            create_pool,
            initialize,
            transaction,
        )

        with create_pool(
            settings.database_url(),
            min_size=1,
            max_size=1,
            timeout=settings.migrate_timeout,
            statement_timeout=str(int(settings.migrate_timeout * 1000)),
        ) as pool:
            if args.command == "migrate":
                initialize(pool)
            else:
                with transaction(pool) as db:
                    check_schema(db)
        return
    if args.command == "worker":
        from .worker import main as run_worker

        run_worker(settings)
        return
    uvicorn.run(
        "tadoss.api:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
