import asyncio
import sys

from evals.cli import main

sys.exit(asyncio.run(main()))
