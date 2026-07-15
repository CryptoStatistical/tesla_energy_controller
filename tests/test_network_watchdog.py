from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WATCHDOG = ROOT / "scripts" / "raspberry_network_watchdog.sh"


def _write_command(bin_dir: Path, name: str, body: str) -> None:
    command = bin_dir / name
    command.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n")
    command.chmod(0o755)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state_file = tmp_path / "failures"
    calls_file = tmp_path / "calls"
    ping_count_file = tmp_path / "ping-count"
    calls_path = shlex.quote(str(calls_file))

    _write_command(bin_dir, "logger", ":")
    _write_command(bin_dir, "sleep", ":")
    _write_command(bin_dir, "flock", ":")
    _write_command(
        bin_dir,
        "nmcli",
        f"printf 'nmcli %s\\n' \"$*\" >>{calls_path}\n"
        "if [[ \"$*\" == *GENERAL.CONNECTION* ]]; then printf 'test-wifi\\n'; fi",
    )
    bootstrap = bin_dir / "network-bootstrap"
    _write_command(
        bin_dir,
        bootstrap.name,
        f"printf 'bootstrap %s\\n' \"$*\" >>{calls_path}",
    )

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{bin_dir}:{env['PATH']}",
            "NETWORK_DEVICE": "wlan0",
            "WIFI_GATEWAY": "192.0.2.1",
            "WIFI_FAILURE_THRESHOLD": "3",
            "WIFI_PING_COUNT": "1",
            "WIFI_PING_TIMEOUT_SECONDS": "1",
            "WIFI_RECOVERY_WAIT_SECONDS": "2",
            "WIFI_WATCHDOG_STATE_FILE": str(state_file),
            "WIFI_WATCHDOG_LOCK_FILE": str(tmp_path / "watchdog.lock"),
            "NETWORK_BOOTSTRAP": str(bootstrap),
            "TEST_PING_COUNT_FILE": str(ping_count_file),
        }
    )
    return env, state_file, calls_file


def test_healthy_gateway_resets_failure_counter(tmp_path: Path) -> None:
    env, state_file, calls_file = _environment(tmp_path)
    state_file.write_text("2\n")
    _write_command(tmp_path / "bin", "ping", "exit 0")

    subprocess.run([str(WATCHDOG)], env=env, check=True)

    assert state_file.read_text() == "0\n"
    assert not calls_file.exists()


def test_third_failure_recovers_wifi_and_network_rules(tmp_path: Path) -> None:
    env, state_file, calls_file = _environment(tmp_path)
    state_file.write_text("2\n")
    _write_command(
        tmp_path / "bin",
        "ping",
        'count="$(cat "$TEST_PING_COUNT_FILE" 2>/dev/null || printf 0)"\n'
        'count=$((count + 1))\n'
        'printf "%s\\n" "$count" >"$TEST_PING_COUNT_FILE"\n'
        'if ((count == 1)); then exit 1; fi',
    )

    subprocess.run([str(WATCHDOG)], env=env, check=True)

    calls = calls_file.read_text()
    assert "nmcli device disconnect wlan0" in calls
    assert "nmcli connection up test-wifi ifname wlan0" in calls
    assert "bootstrap apply" in calls
    assert state_file.read_text() == "0\n"
