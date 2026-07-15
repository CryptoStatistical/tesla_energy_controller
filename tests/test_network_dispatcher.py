from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DISPATCHER = ROOT / "scripts" / "networkmanager_dispatcher.sh"


def test_dispatcher_restores_rules_when_wifi_returns(tmp_path: Path) -> None:
    calls = tmp_path / "calls"
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >>'{calls}'\n")
    bootstrap.chmod(0o755)
    logger = tmp_path / "logger"
    logger.write_text("#!/bin/sh\n:\n")
    logger.chmod(0o755)
    env = os.environ | {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "NETWORK_CONFIG_FILE": str(tmp_path / "missing-config"),
        "NETWORK_DEVICE": "wlan0",
        "NETWORK_BOOTSTRAP": str(bootstrap),
    }

    subprocess.run([str(DISPATCHER), "wlan0", "up"], env=env, check=True)

    assert calls.read_text() == "restore\n"


def test_dispatcher_ignores_other_interfaces_and_actions(tmp_path: Path) -> None:
    bootstrap = tmp_path / "bootstrap"
    bootstrap.write_text("#!/bin/sh\nexit 99\n")
    bootstrap.chmod(0o755)
    env = os.environ | {
        "NETWORK_CONFIG_FILE": str(tmp_path / "missing-config"),
        "NETWORK_DEVICE": "wlan0",
        "NETWORK_BOOTSTRAP": str(bootstrap),
    }

    subprocess.run([str(DISPATCHER), "eth0", "up"], env=env, check=True)
    subprocess.run([str(DISPATCHER), "wlan0", "down"], env=env, check=True)
