from __future__ import annotations

import math

from .models import ChargeState, Decision, GridMeasurement
from .solar import GridSource
from .tesla import VehicleClient


_UNSET = object()
MANUAL_OVERRIDE_TOLERANCE_A = 0.5


def _manual_override_current_a(car: ChargeState, threshold_a: int | float) -> float | None:
    current_a = max(float(car.current_request_a), float(car.actual_current_a))
    if current_a < float(threshold_a) - MANUAL_OVERRIDE_TOLERANCE_A:
        return None
    return current_a


class EnergyController:
    def __init__(
        self,
        grid: GridSource,
        vehicle: VehicleClient,
        *,
        dry_run: bool,
        control_mode: str,
        expected_phases: int,
        nominal_phase_voltage_v: float,
        min_voltage_v: float,
        max_voltage_v: float,
        solar_utilization_percent: float,
        target_grid_import_w: float,
        max_grid_current_a: float,
        min_charge_amps: int,
        max_charge_amps: int,
        command_hysteresis_a: int,
        max_ramp_up_a: int,
        grid_import_limit_w: float = 3000.0,
        grid_import_emergency_w: float = 3300.0,
        grid_hold_band_w: float = 200.0,
        grid_surplus_stable_reads: int = 3,
    ) -> None:
        self.grid = grid
        self.vehicle = vehicle
        self.dry_run = dry_run
        self.control_mode = control_mode
        self.expected_phases = expected_phases
        self.nominal_phase_voltage_v = nominal_phase_voltage_v
        self.min_voltage_v = min_voltage_v
        self.max_voltage_v = max_voltage_v
        self.solar_utilization_percent = solar_utilization_percent
        self.target_grid_import_w = target_grid_import_w
        self.max_grid_current_a = max_grid_current_a
        self.min_charge_amps = min_charge_amps
        self.max_charge_amps = max_charge_amps
        self.command_hysteresis_a = command_hysteresis_a
        self.max_ramp_up_a = max_ramp_up_a
        self.grid_import_limit_w = grid_import_limit_w
        self.grid_import_emergency_w = grid_import_emergency_w
        self.grid_hold_band_w = grid_hold_band_w
        self.grid_surplus_stable_reads = grid_surplus_stable_reads
        self._surplus_stable_reads = 0
        self._paused_for_power_quota = False

    def restore_power_quota_pause(self) -> None:
        self._paused_for_power_quota = True

    def _decision(
        self,
        action: str,
        reason: str,
        *,
        measurement: GridMeasurement | None = None,
        car: ChargeState | None = None,
        current_a=_UNSET,
        target_a: int | None = None,
    ) -> Decision:
        if current_a is _UNSET:
            current = car.current_request_a if car is not None else None
        else:
            current = current_a
        return Decision(
            action,
            reason,
            current_a=current,
            target_a=target_a,
            manual_override_active=action == "manual-override",
            manual_override_a=target_a if action == "manual-override" else None,
            grid_power_w=measurement.total_power_w if measurement is not None else None,
            solar_power_w=measurement.solar_power_w if measurement is not None else None,
            voltage_v=car.voltage_v if car is not None else None,
            actual_current_a=car.actual_current_a if car is not None else None,
            charger_power_kw=car.charger_power_kw if car is not None else None,
        )

    def _not_charging_decision(self, car: ChargeState) -> Decision:
        return self._decision(
            "skip",
            f"Tesla non in carica ({car.charging_state})",
            car=car,
            current_a=None,
            target_a=0,
        )

    def _safety_decision(
        self,
        car: ChargeState,
        measurement: GridMeasurement | None = None,
    ) -> Decision | None:
        if not self.min_voltage_v <= car.voltage_v <= self.max_voltage_v:
            return self._decision(
                "skip",
                f"sicurezza: tensione {car.voltage_v:g} V fuori intervallo "
                f"{self.min_voltage_v:g}-{self.max_voltage_v:g} V",
                measurement=measurement,
                car=car,
                target_a=car.current_request_a,
            )
        if car.phases != self.expected_phases:
            return self._decision(
                "skip",
                f"sicurezza: Tesla riporta {car.phases} fasi, attese {self.expected_phases}",
                measurement=measurement,
                car=car,
                target_a=car.current_request_a,
            )
        return None

    def _stale_measurement_decision(
        self,
        measurement: GridMeasurement,
        car: ChargeState,
    ) -> Decision:
        return self._decision(
            "skip",
            "misura SolarEdge cloud già elaborata",
            measurement=measurement,
            car=car,
            target_a=car.current_request_a,
        )

    def _target_amps(self, grid: GridMeasurement, car: ChargeState) -> int:
        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        if self.control_mode == "solar-production":
            if grid.solar_power_w is None:
                raise ValueError("SolarEdge non ha restituito la produzione FV")
            usable_solar_w = grid.solar_power_w * self.solar_utilization_percent / 100
            raw_target = usable_solar_w / watts_per_amp
        elif self.control_mode == "meter-closed-loop":
            return self._target_amps_for_meter(
                grid,
                car,
                allowed_import_w=self.target_grid_import_w,
                manual_override_amps=self.max_charge_amps + 1,
            )
        else:
            correction_w = self.target_grid_import_w - grid.total_power_w
            raw_target = car.current_request_a + correction_w / watts_per_amp
        target = math.floor(raw_target)

        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = min(self.max_charge_amps, car_limit)

        # Il limite per fase è applicabile solo quando misuriamo la rete.
        if self.control_mode == "grid-surplus" and len(grid.phase_power_w) == self.expected_phases:
            phase_limit_w = self.max_grid_current_a * voltage
            phase_headroom_a = min(
                (phase_limit_w - phase_power) / voltage for phase_power in grid.phase_power_w
            )
            upper = min(upper, math.floor(car.current_request_a + phase_headroom_a))

        target = max(self.min_charge_amps, min(target, upper))
        if target > car.current_request_a:
            target = min(target, car.current_request_a + self.max_ramp_up_a)
        return target

    def _target_amps_for_budget(
        self,
        grid: GridMeasurement,
        car: ChargeState,
        *,
        non_tesla_power_w: float,
        extra_grid_power_w: float,
        manual_override_amps: int,
    ) -> int:
        if grid.solar_power_w is None:
            raise ValueError("SolarEdge non ha restituito la produzione FV")
        if grid.solar_power_w <= 0:
            return 0
        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        usable_solar_w = grid.solar_power_w * self.solar_utilization_percent / 100
        raw_target = (usable_solar_w + extra_grid_power_w - non_tesla_power_w) / watts_per_amp
        target = math.floor(raw_target)

        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = max(0, min(self.max_charge_amps, car_limit, manual_override_amps - 1))
        if upper <= 0:
            return 0
        if target < self.min_charge_amps:
            target = self.min_charge_amps
        target = min(target, upper)
        if target > car.current_request_a:
            target = min(target, car.current_request_a + self.max_ramp_up_a)
        return target

    def _target_amps_for_meter(
        self,
        grid: GridMeasurement,
        car: ChargeState,
        *,
        allowed_import_w: float,
        manual_override_amps: int,
    ) -> int:
        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        import_power_w = grid.import_power_w
        if import_power_w is None:
            import_power_w = max(grid.total_power_w, 0.0)

        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = max(0, min(self.max_charge_amps, car_limit, manual_override_amps - 1))
        if upper <= 0:
            return 0

        if import_power_w > self.grid_import_emergency_w:
            self._surplus_stable_reads = 0
            excess_w = max(import_power_w - allowed_import_w, watts_per_amp)
            return max(0, min(upper, car.current_request_a - math.ceil(excess_w / watts_per_amp) - 1))

        if import_power_w > self.grid_import_limit_w:
            self._surplus_stable_reads = 0
            excess_w = max(import_power_w - allowed_import_w, watts_per_amp)
            return max(0, min(upper, car.current_request_a - math.ceil(excess_w / watts_per_amp)))

        correction_w = allowed_import_w - grid.total_power_w
        if abs(correction_w) <= self.grid_hold_band_w:
            self._surplus_stable_reads = 0
            return car.current_request_a

        raw_target = car.current_request_a + math.floor(correction_w / watts_per_amp)
        target = max(0, min(math.floor(raw_target), upper))

        if target > car.current_request_a:
            if correction_w <= 0:
                self._surplus_stable_reads = 0
            else:
                self._surplus_stable_reads += 1
            if self._surplus_stable_reads < self.grid_surplus_stable_reads:
                return car.current_request_a
            target = min(target, car.current_request_a + self.max_ramp_up_a)
        else:
            self._surplus_stable_reads = 0

        if 0 < target < self.min_charge_amps:
            target = self.min_charge_amps
        return target

    def _target_amps_for_power_quota(
        self,
        grid: GridMeasurement,
        car: ChargeState,
        *,
        projected_import_w: float,
        power_quota_limit_w: float,
        power_quota_hysteresis_w: float,
        extra_grid_power_w: float,
        manual_override_amps: int,
    ) -> tuple[int, str]:
        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = max(0, min(self.max_charge_amps, car_limit, manual_override_amps - 1))
        if upper <= 0:
            return 0, "limite di corrente non disponibile"

        import_power_w = grid.import_power_w
        if import_power_w is None:
            import_power_w = max(float(grid.total_power_w), 0.0)
        import_power_w = max(float(import_power_w), 0.0)
        export_power_w = max(float(grid.export_power_w or 0.0), 0.0)
        solar_power_w = max(float(grid.solar_power_w or 0.0), 0.0)
        grid_balance_w = import_power_w - export_power_w

        if projected_import_w > power_quota_limit_w + power_quota_hysteresis_w:
            excess_w = projected_import_w - power_quota_limit_w
            target = max(0, car.current_request_a - math.ceil(excess_w / watts_per_amp))
            target = min(target, upper)
            if target < self.min_charge_amps:
                if target > 0:
                    return target, "proiezione 15 min oltre quota: corrente sotto minimo"
                return 0, "proiezione 15 min oltre quota: sospensione Tesla"
            return target, "proiezione 15 min oltre quota: corrente ridotta"

        target_import_w = (
            min(max(float(extra_grid_power_w), 0.0), power_quota_limit_w)
            if solar_power_w > 0
            else power_quota_limit_w
        )
        correction_w = target_import_w - grid_balance_w
        if correction_w < -self.grid_hold_band_w:
            target = max(0, car.current_request_a - math.ceil(abs(correction_w) / watts_per_amp))
            target = min(target, upper)
            if target < self.min_charge_amps:
                if grid_balance_w > power_quota_limit_w + self.grid_hold_band_w:
                    if target > 0:
                        return target, "import ALFA oltre quota: corrente sotto minimo"
                    return 0, "import ALFA oltre quota: sospensione Tesla"
                return min(self.min_charge_amps, upper), (
                    "Import oltre extra rete; corrente minima Tesla entro quota"
                )
            return target, "import ALFA oltre target rete: corrente ridotta"
        if abs(correction_w) <= self.grid_hold_band_w:
            return car.current_request_a, "import ALFA entro target rete"

        quota_room_w = max(
            power_quota_limit_w + power_quota_hysteresis_w - projected_import_w,
            0.0,
        )
        safe_increase_w = min(correction_w, export_power_w + quota_room_w)
        if safe_increase_w > 0:
            increase_a = math.floor(safe_increase_w / watts_per_amp)
            if increase_a > 0:
                target = min(
                    upper,
                    car.current_request_a + self.max_ramp_up_a,
                    car.current_request_a + increase_a,
                )
                return target, "import ALFA sotto target rete: corrente aumentata"

        return car.current_request_a, "import ALFA entro target rete"

    def _resume_from_power_quota(
        self,
        measurement: GridMeasurement,
        car: ChargeState,
        *,
        projected_import_w: float,
        power_quota_limit_w: float,
        power_quota_hysteresis_w: float,
        extra_grid_power_w: float,
        manual_override_amps: int,
    ) -> Decision | None:
        if not self._paused_for_power_quota:
            return None
        if car.charging_state.casefold() not in {"stopped", "no_power"}:
            self._paused_for_power_quota = False
            return None
        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        import_power_w = measurement.import_power_w
        if import_power_w is None:
            import_power_w = max(float(measurement.total_power_w), 0.0)
        import_power_w = max(float(import_power_w), 0.0)
        export_power_w = max(float(measurement.export_power_w or 0.0), 0.0)
        resume_limit_w = power_quota_limit_w - power_quota_hysteresis_w
        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = max(0, min(self.max_charge_amps, car_limit, manual_override_amps - 1))
        available_restart_w = max(resume_limit_w - projected_import_w + export_power_w, 0.0)
        quota_target_a = min(upper, math.floor(available_restart_w / watts_per_amp))
        if quota_target_a <= 0:
            return Decision(
                "hold",
                "Tesla sospesa: attendo margine sulla quota 15 min",
                current_a=car.current_request_a,
                target_a=0,
                grid_power_w=measurement.total_power_w,
                solar_power_w=measurement.solar_power_w,
                voltage_v=car.voltage_v,
                actual_current_a=car.actual_current_a,
                charger_power_kw=car.charger_power_kw,
            )
        target_a = min(self.min_charge_amps, quota_target_a)
        if target_a >= manual_override_amps:
            return None
        if self.dry_run:
            return Decision(
                "dry-run",
                "margine quota ripristinato: riavvio Tesla calcolato",
                current_a=car.current_request_a,
                target_a=target_a,
                grid_power_w=measurement.total_power_w,
                solar_power_w=measurement.solar_power_w,
                voltage_v=car.voltage_v,
                actual_current_a=car.actual_current_a,
                charger_power_kw=car.charger_power_kw,
            )
        self.vehicle.set_charging_amps(target_a)
        self.vehicle.start_charging()
        self._paused_for_power_quota = False
        return Decision(
            "start",
            "margine quota ripristinato: ricarica Tesla riavviata",
            current_a=car.current_request_a,
            target_a=target_a,
            grid_power_w=measurement.total_power_w,
            solar_power_w=measurement.solar_power_w,
            voltage_v=car.voltage_v,
            actual_current_a=car.actual_current_a,
            charger_power_kw=car.charger_power_kw,
        )

    def _is_urgent_meter_drop(
        self,
        measurement: GridMeasurement,
        car: ChargeState,
        target: int,
        *,
        meter_mode: bool,
    ) -> bool:
        import_power_w = measurement.import_power_w
        if import_power_w is None:
            import_power_w = max(measurement.total_power_w, 0.0)
        return (
            meter_mode
            and target < car.current_request_a
            and import_power_w > self.grid_import_limit_w
        )

    def decide_minimum_from_snapshot(
        self,
        measurement: GridMeasurement,
        car: ChargeState,
        *,
        projected_quarter_hour_import_w: float | None,
        power_quota_limit_w: float | None,
        power_quota_hysteresis_w: float = 0.0,
        manual_override_amps: int | None = None,
    ) -> Decision:
        if not car.is_charging:
            return self._not_charging_decision(car)
        override_a = (
            _manual_override_current_a(car, manual_override_amps)
            if manual_override_amps is not None
            else None
        )
        if override_a is not None:
            display_a = int(round(override_a))
            return self._decision(
                "manual-override",
                f"override manuale: Tesla impostata a {display_a} A",
                measurement=measurement,
                car=car,
                target_a=display_a,
            )
        safety_decision = self._safety_decision(car, measurement)
        if safety_decision is not None:
            return safety_decision
        if not measurement.fresh:
            return self._stale_measurement_decision(measurement, car)

        car_limit = car.current_request_max_a or self.max_charge_amps
        upper = min(self.max_charge_amps, car_limit)
        if manual_override_amps is not None:
            upper = min(upper, manual_override_amps - 1)
        upper = max(0, upper)
        if upper <= 0:
            target = 0
            quota_reason = "limite di corrente non disponibile"
        else:
            target = min(self.min_charge_amps, upper)
            quota_reason = "fuori finestra solare: corrente minima Tesla entro quota"

        voltage = car.voltage_v or self.nominal_phase_voltage_v
        watts_per_amp = voltage * self.expected_phases
        import_power_w = measurement.import_power_w
        if import_power_w is None:
            import_power_w = max(float(measurement.total_power_w), 0.0)
        import_power_w = max(float(import_power_w), 0.0)

        if power_quota_limit_w is not None and projected_quarter_hour_import_w is not None:
            projected_import_w = max(float(projected_quarter_hour_import_w), 0.0)
            if projected_import_w > power_quota_limit_w + power_quota_hysteresis_w:
                excess_w = projected_import_w - power_quota_limit_w
                target = max(0, car.current_request_a - math.ceil(excess_w / watts_per_amp))
                target = min(target, upper)
                if target < self.min_charge_amps:
                    if target > 0:
                        quota_reason = "proiezione 15 min oltre quota: corrente sotto minimo"
                    else:
                        quota_reason = "proiezione 15 min oltre quota: sospensione Tesla"
                else:
                    quota_reason = "proiezione 15 min oltre quota: corrente ridotta"
            elif import_power_w > power_quota_limit_w + self.grid_hold_band_w:
                excess_w = import_power_w - power_quota_limit_w
                target = max(0, car.current_request_a - math.ceil(excess_w / watts_per_amp))
                target = min(target, upper)
                if target < self.min_charge_amps:
                    if target > 0:
                        quota_reason = "import ALFA oltre quota: corrente sotto minimo"
                    else:
                        quota_reason = "import ALFA oltre quota: sospensione Tesla"
                else:
                    quota_reason = "import ALFA oltre quota: corrente ridotta"

        if target == 0 and not self.dry_run:
            self.vehicle.stop_charging()
            self._paused_for_power_quota = True
            return self._decision(
                "stop",
                quota_reason,
                measurement=measurement,
                car=car,
                target_a=0,
            )
        if abs(target - car.current_request_a) < self.command_hysteresis_a:
            return self._decision(
                "hold",
                quota_reason,
                measurement=measurement,
                car=car,
                target_a=target,
            )
        if self.dry_run:
            return self._decision(
                "dry-run",
                quota_reason,
                measurement=measurement,
                car=car,
                target_a=target,
            )
        self.vehicle.set_charging_amps(target)
        return self._decision(
            "set",
            quota_reason,
            measurement=measurement,
            car=car,
            target_a=target,
        )

    def decide_from_snapshot(
        self,
        measurement: GridMeasurement,
        car: ChargeState,
        *,
        non_tesla_power_w: float | None = None,
        extra_grid_power_w: float | None = None,
        manual_override_amps: int | None = None,
        use_meter_reading: bool | None = None,
        projected_quarter_hour_import_w: float | None = None,
        power_quota_limit_w: float | None = None,
        power_quota_hysteresis_w: float = 0.0,
    ) -> Decision:
        legacy_mode = (
            non_tesla_power_w is None
            or extra_grid_power_w is None
            or manual_override_amps is None
        )
        meter_mode = (
            self.control_mode == "meter-closed-loop"
            if use_meter_reading is None
            else use_meter_reading
        )
        quota_mode = (
            meter_mode
            and not legacy_mode
            and projected_quarter_hour_import_w is not None
            and power_quota_limit_w is not None
        )
        if quota_mode and car.is_charging:
            self._paused_for_power_quota = False
        if not car.is_charging and quota_mode:
            resumed = self._resume_from_power_quota(
                measurement,
                car,
                projected_import_w=projected_quarter_hour_import_w,
                power_quota_limit_w=power_quota_limit_w,
                power_quota_hysteresis_w=power_quota_hysteresis_w,
                extra_grid_power_w=extra_grid_power_w,
                manual_override_amps=manual_override_amps,
            )
            if resumed is not None:
                return resumed
        if not car.is_charging:
            return self._not_charging_decision(car)
        override_a = (
            _manual_override_current_a(car, manual_override_amps)
            if manual_override_amps is not None
            else None
        )
        if override_a is not None:
            display_a = int(round(override_a))
            return self._decision(
                "manual-override",
                f"override manuale: Tesla impostata a {display_a} A",
                measurement=measurement,
                car=car,
                target_a=display_a,
            )
        safety_decision = self._safety_decision(car, measurement)
        if safety_decision is not None:
            return safety_decision
        if not measurement.fresh:
            return self._stale_measurement_decision(measurement, car)

        quota_reason = ""
        if quota_mode:
            assert extra_grid_power_w is not None
            assert manual_override_amps is not None
            assert projected_quarter_hour_import_w is not None
            assert power_quota_limit_w is not None
            target, quota_reason = self._target_amps_for_power_quota(
                measurement,
                car,
                projected_import_w=projected_quarter_hour_import_w,
                power_quota_limit_w=power_quota_limit_w,
                power_quota_hysteresis_w=power_quota_hysteresis_w,
                extra_grid_power_w=extra_grid_power_w,
                manual_override_amps=manual_override_amps,
            )
        elif legacy_mode:
            target = self._target_amps(measurement, car)
        elif meter_mode:
            assert extra_grid_power_w is not None
            assert manual_override_amps is not None
            target = self._target_amps_for_meter(
                measurement,
                car,
                allowed_import_w=extra_grid_power_w,
                manual_override_amps=manual_override_amps,
            )
        else:
            assert non_tesla_power_w is not None
            assert extra_grid_power_w is not None
            assert manual_override_amps is not None
            target = self._target_amps_for_budget(
                measurement,
                car,
                non_tesla_power_w=non_tesla_power_w,
                extra_grid_power_w=extra_grid_power_w,
                manual_override_amps=manual_override_amps,
            )
        urgent_meter_drop = (
            quota_mode
            and target < car.current_request_a
            and projected_quarter_hour_import_w
            > power_quota_limit_w + power_quota_hysteresis_w
        ) or self._is_urgent_meter_drop(
            measurement,
            car,
            target,
            meter_mode=meter_mode and not quota_mode,
        )
        if not urgent_meter_drop and abs(target - car.current_request_a) < self.command_hysteresis_a:
            return self._decision(
                "hold",
                quota_reason or "variazione entro isteresi",
                measurement=measurement,
                car=car,
                target_a=target,
            )
        if self.dry_run:
            return self._decision(
                "dry-run",
                quota_reason or "comando calcolato ma non inviato",
                measurement=measurement,
                car=car,
                target_a=target,
            )
        if quota_mode and target == 0:
            self.vehicle.stop_charging()
            self._paused_for_power_quota = True
            return self._decision(
                "stop",
                quota_reason,
                measurement=measurement,
                car=car,
                target_a=0,
            )
        self.vehicle.set_charging_amps(target)
        return self._decision(
            "set",
            quota_reason or "corrente Tesla aggiornata",
            measurement=measurement,
            car=car,
            target_a=target,
        )

    def run_once(self) -> Decision:
        car = self.vehicle.get_charge_state()
        if not car.is_charging:
            return self._not_charging_decision(car)
        safety_decision = self._safety_decision(car)
        if safety_decision is not None:
            return safety_decision

        # SolarEdge viene interrogato soltanto dopo aver verificato che la Tesla
        # sia già in carica, con tensione e numero di fasi ammessi.
        measurement = self.grid.read()
        return self.decide_from_snapshot(measurement, car)
