"""Kill switch state — file-based so it survives restarts.

The kill switch lets the bot owner silence the bot inside the allowed group
without redeploying or removing it from the chat. When engaged, all
``@authorized_only`` handlers drop their updates silently. Admin-only
handlers (``@admin_only``) deliberately bypass the kill switch so the owner
can still issue ``/revive`` (and ``/clear``) from a killed state — otherwise
engaging the switch would lock the owner out with no way back in.

Flag lives at ``data/killed.flag`` — on the EC2 host this path sits on the
instance's EBS-backed disk, so the kill state survives service restarts and
redeploys without any volume configuration.
"""
from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
_KILL_FLAG_PATH: Path = _PROJECT_ROOT / "data" / "killed.flag"


def is_killed() -> bool:
    """Return True if the kill switch is currently engaged."""
    return _KILL_FLAG_PATH.exists()


def kill() -> None:
    """Engage the kill switch by creating the flag file. Idempotent."""
    _KILL_FLAG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _KILL_FLAG_PATH.touch()
    logger.warning("☠️  Kill switch ENGAGED — playing dead for the peasants")


def revive() -> bool:
    """Disengage the kill switch. Return True if it was engaged, False if no-op."""
    if not _KILL_FLAG_PATH.exists():
        return False
    _KILL_FLAG_PATH.unlink()
    logger.info("🟢 Kill switch DISENGAGED — back from the dead, regrettably")
    return True
