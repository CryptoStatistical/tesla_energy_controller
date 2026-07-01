from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import replace

import httpx

from .config import ConfigurationError, Settings, load_env_file
from .controller import EnergyController
from .diagnostics import ErrorReporter
from .factories import build_controller

LOG = logging.getLogger("tesla_energy_controller")


def log_decision(decision) -> None:
    LOG.info(
        "action=%s reason=%s solar_w=%s voltage_v=%s actual_a=%s current_a=%s target_a=%s",
        decision.action,
        decision.reason,
        decision.solar_power_w,
        decision.voltage_v,
        decision.actual_current_a,
        decision.current_a,
        decision.target_a,
    )


def run_forever(
    controller: EnergyController,
    interval: int,
    reporter: ErrorReporter | None = None,
    settings: Settings | None = None,
) -> None:
    stop = False

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    LOG.info("scheduler avviato interval_seconds=%d", interval)
    while not stop:
        started = time.monotonic()
        try:
            log_decision(controller.run_once())
        except Exception as exc:
            LOG.exception("ciclo fallito; nessun comando inviato")
            if reporter is not None:
                result = reporter.notify(
                    exc,
                    context={
                        "component": "scheduler",
                        "energy_source": settings.energy_source if settings else None,
                        "control_mode": settings.control_mode if settings else None,
                    },
                )
                LOG.info("report errore: %s", result.message)
        remaining = interval - (time.monotonic() - started)
        deadline = time.monotonic() + max(0, remaining)
        while not stop and time.monotonic() < deadline:
            time.sleep(min(1, deadline - time.monotonic()))


def main() -> None:
    parser = argparse.ArgumentParser(description="Tesla Energy Controller")
    parser.add_argument(
        "command",
        nargs="?",
        choices=(
            "run",
            "once",
            "doctor",
            "web",
            "report-test",
            "tuya-run",
            "tuya-test",
            "vimar-discover",
            "vimar-pair",
            "vimar-read",
        ),
        default="run",
    )
    parser.add_argument("--vimar-tag", help="Third-Party Tag / Integration ID Vimar")
    parser.add_argument("--vimar-setup-code", help="Setup Code temporaneo Vimar")
    parser.add_argument("--tuya-seconds", type=int, help="Durata del test TuyaLink")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        load_env_file()
        settings = Settings.from_env()
        reporter = ErrorReporter.from_settings(settings)
        if args.command == "report-test":
            result = reporter.send_test()
            LOG.info("%s", result.message)
            if not result.sent:
                sys.exit(2)
            return
        if args.command in {"vimar-discover", "vimar-pair", "vimar-read"}:
            run_vimar_command(args.command, settings, args.vimar_tag, args.vimar_setup_code)
            return
        controller = build_controller(settings)
        if args.command in {"tuya-run", "tuya-test"}:
            duration = None
            if args.command == "tuya-test":
                duration = args.tuya_seconds or int(os.getenv("TUYA_TEST_SECONDS", "120"))
            run_tuya_meter(settings, controller, duration_seconds=duration)
            return
        if args.command == "web":
            from waitress import serve

            from .web import create_app

            serve(
                create_app(settings, controller),
                host=settings.web_host,
                port=settings.web_port,
                threads=4,
            )
            return
        if args.command in {"once", "doctor"}:
            if args.command == "doctor":
                controller.dry_run = True
            try:
                decision = controller.run_once()
            except Exception as exc:
                result = reporter.notify(
                    exc,
                    context={
                        "component": args.command,
                        "energy_source": settings.energy_source,
                        "control_mode": settings.control_mode,
                    },
                )
                LOG.info("report errore: %s", result.message)
                raise
            log_decision(decision)
            if args.command == "doctor":
                LOG.info(
                    "diagnostica completata mode=%s control_mode=%s energy_source=%s tesla_transport=%s",
                    settings.mode,
                    settings.control_mode,
                    settings.energy_source,
                    settings.tesla_transport,
                )
            return
        run_forever(controller, settings.poll_interval_seconds, reporter, settings)
    except (ConfigurationError, ValueError, RuntimeError, ConnectionError, httpx.HTTPError) as exc:
        LOG.error("%s", exc)
        sys.exit(2)


def build_vimar_client(settings: Settings):
    from .vimar import VimarIPConnectorClient, discover_gateways

    host = settings.vimar_host
    port = settings.vimar_port
    device_uid = settings.vimar_device_uid
    if not host or not device_uid:
        gateways = discover_gateways()
        if not gateways and not host:
            raise ConfigurationError("Gateway Vimar non trovato via mDNS; impostare VIMAR_HOST")
        if gateways:
            selected = next((gateway for gateway in gateways if gateway.host == host), gateways[0])
            host = host or selected.host
            port = settings.vimar_port if settings.vimar_host else selected.port
            device_uid = device_uid or selected.device_uid
    return VimarIPConnectorClient(
        host=host,
        port=port,
        device_uid=device_uid,
        private_key_file=settings.vimar_private_key_file,
        ca_cert=settings.vimar_ca_cert,
        tls_verify=settings.vimar_tls_verify,
        tls_check_hostname=settings.vimar_tls_check_hostname,
        timeout_seconds=settings.vimar_timeout_seconds,
        client_name=settings.vimar_client_name,
        protocol_version=settings.vimar_protocol_version,
    )


def run_vimar_command(
    command: str,
    settings: Settings,
    vimar_tag_arg: str | None,
    setup_code_arg: str | None,
) -> None:
    from .vimar import load_credentials, save_credentials

    if command == "vimar-discover":
        from .vimar import discover_gateways

        gateways = discover_gateways()
        if not gateways:
            LOG.warning("nessun gateway Vimar trovato via mDNS")
            return
        for gateway in gateways:
            LOG.info(
                "vimar_gateway host=%s port=%s model=%s protocol=%s software=%s device_uid=%s",
                gateway.host,
                gateway.port,
                gateway.model,
                gateway.protocol_version,
                gateway.software_version,
                gateway.device_uid,
            )
        return

    client = build_vimar_client(settings)
    try:
        if command == "vimar-pair":
            tag = vimar_tag_arg or settings.vimar_third_party_tag
            setup_code = setup_code_arg or settings.vimar_setup_code
            if not tag or not setup_code:
                raise ConfigurationError(
                    "Per vimar-pair servono --vimar-tag/VIMAR_THIRD_PARTY_TAG "
                    "e --vimar-setup-code/VIMAR_SETUP_CODE(_FILE)"
                )
            credentials = client.attach_pair(tag, setup_code)
            save_credentials(settings.vimar_credentials_file, credentials)
            LOG.info(
                "pairing Vimar completato; credenziali salvate in %s",
                settings.vimar_credentials_file,
            )
            return

        credentials = load_credentials(settings.vimar_credentials_file)
        client.attach_credentials(credentials)
        discovery = client.sfdiscovery("Plant", with_values=True)
        points = client.extract_energy_points(discovery)
        if not points:
            LOG.warning("nessun punto Energy trovato nella discovery Vimar")
        total_consumption_w = 0.0
        measured_consumption_count = 0
        for point in points:
            if point.power_w is not None:
                total_consumption_w += point.power_w
                measured_consumption_count += 1
            LOG.info(
                "vimar_energy idsf=%s name=%r sstype=%s consumption_w=%s production_w=%s exchange_w=%s",
                point.idsf,
                point.name,
                point.sstype,
                point.power_w,
                point.production_w,
                point.exchange_w,
            )
        LOG.info(
            "vimar_energy_total monitors=%d total_consumption_w=%s",
            measured_consumption_count,
            total_consumption_w,
        )
    finally:
        client.close()


def run_tuya_meter(
    settings: Settings,
    controller: EnergyController,
    *,
    duration_seconds: int | None = None,
) -> None:
    from .tuya import TuyaEnergyMeterBridge, TuyaLinkConfig, TuyaLinkMqttClient
    from .runtime import RuntimeSettingsStore

    if duration_seconds is not None and duration_seconds < 1:
        raise ConfigurationError("--tuya-seconds deve essere positivo")

    stop = False
    deadline = time.monotonic() + duration_seconds if duration_seconds else None

    def request_stop(_signum, _frame) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    config = TuyaLinkConfig.from_settings(settings)
    runtime_store = RuntimeSettingsStore(settings.runtime_settings_file, settings)

    def load_controller_enabled() -> bool:
        try:
            return runtime_store.load().enabled
        except Exception:
            LOG.exception("tuya_runtime_settings_load_failed")
            return True

    def set_controller_enabled(enabled: bool) -> None:
        try:
            current = runtime_store.load()
            if current.enabled != enabled:
                runtime_store.save(replace(current, enabled=enabled))
            LOG.info("tuya_controller_switch enabled=%s", enabled)
        except Exception:
            LOG.exception("tuya_runtime_settings_save_failed")

    bridge = TuyaEnergyMeterBridge(
        settings,
        controller,
        on_switch=set_controller_enabled,
        meter_enabled=load_controller_enabled(),
    )
    LOG.info(
        "tuya_meter_start host=%s port=%s report_interval_seconds=%d",
        config.host,
        config.port,
        settings.tuya_report_interval_seconds,
    )

    while not stop and (deadline is None or time.monotonic() < deadline):
        client = TuyaLinkMqttClient(config)
        try:
            client.connect()
            client.subscribe_control_topics()
            client.report_properties(bridge.properties())
            next_report = time.monotonic() + settings.tuya_report_interval_seconds
            while not stop and (deadline is None or time.monotonic() < deadline):
                message = client.loop_once(timeout=1)
                if message is not None:
                    bridge.handle_message(client, message)
                client.ping_if_needed()
                now = time.monotonic()
                if now >= next_report:
                    properties = bridge.properties()
                    client.report_properties(properties)
                    LOG.info(
                        "tuya_report solar_w=%s house_w=%s tesla_w=%s total_w=%s enabled=%s",
                        properties["solar_power_w"],
                        properties["house_consumption_w"],
                        properties["tesla_power_w"],
                        properties["total_consumption_w"],
                        properties["meter_switch"],
                    )
                    next_report = now + settings.tuya_report_interval_seconds
        except Exception:
            LOG.exception("tuya_meter_connection_failed")
            if stop or (deadline is not None and time.monotonic() >= deadline):
                break
            time.sleep(5)
        finally:
            client.disconnect()


if __name__ == "__main__":
    main()
