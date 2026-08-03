"""Minimal long-running service for the StackPilot example."""

from __future__ import annotations

import time


def main() -> None:
    print("minimal ready", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
