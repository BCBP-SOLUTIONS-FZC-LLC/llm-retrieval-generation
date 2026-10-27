"""``python -m rag.infrastructure.sweeps <name>|all``: one invocation, one sweep (CronJob style).

Exit code: 0 clean or skipped, 1 a tenant failed, 2 an alert (an audit-chain break pages: a break
is a tamper signal and is never self-healed), 64 usage.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from rag.infrastructure.config import Settings
from rag.infrastructure.sweeps.runner import NAMES, open_pools, run_sweep


async def main(argv: list[str]) -> int:
    """Run the named sweep (or all of them, one after another) and return the exit code."""
    if len(argv) != 1 or argv[0] not in (*NAMES, "all"):
        print(f"usage: python -m rag.infrastructure.sweeps <{'|'.join((*NAMES, 'all'))}>")
        return 64
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    pools = await open_pools(settings)
    try:
        outcomes = [
            await run_sweep(settings, name, pools) for name in (NAMES if argv[0] == "all" else argv)
        ]
    finally:
        await pools.close()
    if any(o.alerts for o in outcomes):
        return 2
    return 0 if all(o.ok for o in outcomes) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
