"""BETGPTAI v3.0 application entrypoint.

For safety, this delegates to the existing battle-tested runtime while the
project is migrated into the modular package.
"""

from __future__ import annotations

from main import main as legacy_main


async def main() -> None:
    """Run the Telegram bot."""
    await legacy_main()
