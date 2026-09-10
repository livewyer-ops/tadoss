import argparse
import os

import uvicorn


def main():
    parser = argparse.ArgumentParser(description="TADOSS service and schema management")
    parser.add_argument(
        "command", nargs="?", choices=["serve", "migrate"], default="serve"
    )
    args = parser.parse_args()
    if args.command == "migrate":
        from .store import DEFAULT_DATABASE, initialize

        initialize(os.getenv("TADOSS_DATABASE", DEFAULT_DATABASE))
        return
    uvicorn.run(
        "tadoss.api:create_app",
        factory=True,
        host=os.getenv("TADOSS_HOST", "127.0.0.1"),
        port=int(os.getenv("TADOSS_PORT", "8000")),
        access_log=False,  # TADS permits query-string credentials.
        proxy_headers=False,
    )


if __name__ == "__main__":
    main()
