import math
import random

from tesla_energy_controller.controller import EnergyController
from tesla_energy_controller.models import ChargeState, GridMeasurement


class Grid:
    def __init__(self, measurement):
        self.measurement = measurement
        self.reads = 0

    def read(self):
        self.reads += 1
        return self.measurement


class Vehicle:
    def __init__(self, state):
        self.state = state
        self.commands = []
        self.reads = 0

    def get_charge_state(self):
        self.reads += 1
        return self.state

    def set_charging_amps(self, amps):
        self.commands.append(amps)

    def start_charging(self):
        self.commands.append("start")

    def stop_charging(self):
        self.commands.append("stop")


def state(current=6, status="Charging", phases=3, maximum=32):
    return ChargeState(status, current, maximum, current, phases, 230)


def controller(measurement, car, **overrides):
    options = dict(
        dry_run=False,
        control_mode="grid-surplus",
        expected_phases=3,
        nominal_phase_voltage_v=230,
        min_voltage_v=200,
        max_voltage_v=255,
        solar_utilization_percent=100,
        target_grid_import_w=200,
        max_grid_current_a=25,
        min_charge_amps=5,
        max_charge_amps=16,
        command_hysteresis_a=1,
        max_ramp_up_a=2,
    )
    options.update(overrides)
    vehicle = Vehicle(car)
    return EnergyController(Grid(measurement), vehicle, **options), vehicle


def test_surplus_increases_gradually():
    subject, vehicle = controller(GridMeasurement(-3300), state())
    decision = subject.run_once()
    assert decision.target_a == 8
    assert vehicle.commands == [8]


def test_import_reduces_immediately_but_not_below_minimum():
    subject, vehicle = controller(GridMeasurement(2200), state(current=10))
    decision = subject.run_once()
    assert decision.target_a == 7
    assert vehicle.commands == [7]


def test_does_nothing_when_not_charging():
    subject, vehicle = controller(GridMeasurement(-5000), state(status="Stopped"))
    decision = subject.run_once()
    assert decision.action == "skip"
    assert decision.target_a == 0
    assert vehicle.commands == []
    assert subject.grid.reads == 0


def test_phase_mismatch_is_fail_safe():
    subject, vehicle = controller(GridMeasurement(-5000), state(phases=1))
    decision = subject.run_once()
    assert "1 fasi" in decision.reason
    assert vehicle.commands == []


def test_phase_power_caps_increase_on_most_loaded_phase():
    measurement = GridMeasurement(
        total_power_w=-5000,
        phase_power_w=(5000, -5000, -5000),
    )
    subject, vehicle = controller(measurement, state(current=10), max_ramp_up_a=10)
    decision = subject.run_once()
    assert decision.target_a == 13
    assert vehicle.commands == [13]


def test_cached_cloud_measurement_is_not_reused():
    subject, vehicle = controller(GridMeasurement(-5000, fresh=False), state())
    decision = subject.run_once()
    assert decision.action == "skip"
    # La Tesla carica ma la misura cloud e' stantia: il target tenuto resta la
    # corrente attuale (6 A), cosi' la card Target non mostra "—".
    assert decision.target_a == 6
    assert vehicle.commands == []
    assert vehicle.reads == 1


def test_solar_production_becomes_three_phase_current():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=5520)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="solar-production",
        max_ramp_up_a=10,
    )
    decision = subject.run_once()
    assert decision.target_a == 8
    assert decision.solar_power_w == 5520
    assert vehicle.commands == [8]


def test_solar_production_is_clamped_to_configured_minimum():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=440)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="solar-production",
        max_ramp_up_a=10,
    )
    decision = subject.run_once()
    assert decision.target_a == 5
    assert vehicle.commands == [5]


def test_voltage_outside_configured_range_blocks_command():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=6900)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="solar-production",
        min_voltage_v=235,
    )
    decision = subject.run_once()
    assert decision.action == "skip"
    assert "tensione 230 V" in decision.reason
    assert vehicle.commands == []


def test_solar_utilization_percentage_reduces_target():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=6900)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="solar-production",
        solar_utilization_percent=80,
        max_ramp_up_a=10,
    )
    decision = subject.run_once()
    assert decision.target_a == 8


def test_budget_mode_uses_solar_plus_extra_grid_minus_home():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=6000)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        max_charge_amps=13,
        max_ramp_up_a=10,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=3000,
        extra_grid_power_w=2000,
        manual_override_amps=14,
    )
    assert decision.target_a == 7
    assert vehicle.commands == [7]


def test_budget_mode_with_production_never_targets_below_configured_minimum():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=2400)
    subject, vehicle = controller(
        measurement,
        state(current=5),
        min_charge_amps=6,
        max_charge_amps=13,
        max_ramp_up_a=10,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=5),
        non_tesla_power_w=3500,
        extra_grid_power_w=0,
        manual_override_amps=14,
    )
    assert decision.target_a == 6
    assert vehicle.commands == [6]


def test_budget_mode_zero_production_targets_zero():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=0)
    subject, vehicle = controller(measurement, state(current=6), max_charge_amps=13)
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=3000,
        manual_override_amps=14,
    )
    assert decision.target_a == 0
    assert vehicle.commands == [0]


def test_budget_mode_keeps_target_below_manual_override():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=20000)
    subject, vehicle = controller(
        measurement,
        state(current=6),
        max_charge_amps=16,
        max_ramp_up_a=20,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=3000,
        manual_override_amps=14,
    )
    assert decision.target_a == 13
    assert vehicle.commands == [13]


def test_budget_mode_ramps_up_from_manual_override_recovery():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=7137)
    subject, vehicle = controller(measurement, state(current=5), max_ramp_up_a=2)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=5),
        non_tesla_power_w=1413,
        extra_grid_power_w=3000,
        manual_override_amps=14,
    )

    assert decision.target_a == 7
    assert vehicle.commands == [7]


def test_budget_mode_respects_manual_override_threshold():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=12000)
    subject, vehicle = controller(measurement, state(current=14), max_charge_amps=13)
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=14),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
    )
    assert decision.action == "manual-override"
    assert vehicle.commands == []


def test_budget_mode_detects_manual_override_from_actual_current():
    measurement = GridMeasurement(total_power_w=0, solar_power_w=12000)
    car = ChargeState("Charging", 6, 32, 15.8, 3, 230)
    subject, vehicle = controller(measurement, car, max_charge_amps=16)
    decision = subject.decide_from_snapshot(
        measurement,
        car,
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=16,
    )
    assert decision.action == "manual-override"
    assert decision.target_a == 16
    assert vehicle.commands == []


def test_meter_closed_loop_increases_after_stable_surplus():
    measurement = GridMeasurement(
        total_power_w=-1500,
        solar_power_w=5000,
        import_power_w=0,
        export_power_w=1500,
        total_consumption_w=3500,
    )
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="meter-closed-loop",
        max_ramp_up_a=2,
        grid_surplus_stable_reads=3,
    )
    for _ in range(2):
        decision = subject.decide_from_snapshot(
            measurement,
            state(current=6),
            non_tesla_power_w=0,
            extra_grid_power_w=200,
            manual_override_amps=14,
        )
        assert decision.action == "hold"
        assert decision.target_a == 6
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=200,
        manual_override_amps=14,
    )
    assert decision.target_a == 8
    assert vehicle.commands == [8]


def test_meter_closed_loop_reduces_immediately_above_import_limit():
    measurement = GridMeasurement(
        total_power_w=3500,
        solar_power_w=1000,
        import_power_w=3500,
        export_power_w=0,
        total_consumption_w=4500,
    )
    subject, vehicle = controller(
        measurement,
        state(current=10),
        control_mode="meter-closed-loop",
        command_hysteresis_a=3,
        grid_import_limit_w=3000,
        grid_import_emergency_w=5000,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=10),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
    )
    assert decision.target_a == 7
    assert vehicle.commands == [7]


def test_meter_closed_loop_emergency_reduces_extra_amp():
    measurement = GridMeasurement(
        total_power_w=5200,
        solar_power_w=0,
        import_power_w=5200,
        export_power_w=0,
        total_consumption_w=5200,
    )
    subject, vehicle = controller(
        measurement,
        state(current=12),
        control_mode="meter-closed-loop",
        grid_import_limit_w=3000,
        grid_import_emergency_w=5000,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=12),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
    )
    assert decision.target_a == 6
    assert vehicle.commands == [6]


def test_meter_closed_loop_holds_inside_configured_band():
    measurement = GridMeasurement(
        total_power_w=1900,
        solar_power_w=4000,
        import_power_w=1900,
        export_power_w=0,
        total_consumption_w=5900,
    )
    subject, vehicle = controller(
        measurement,
        state(current=8),
        control_mode="meter-closed-loop",
        grid_hold_band_w=200,
    )
    decision = subject.decide_from_snapshot(
        measurement,
        state(current=8),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
    )
    assert decision.action == "hold"
    assert decision.target_a == 8
    assert vehicle.commands == []


def test_runtime_flag_can_disable_meter_control():
    measurement = GridMeasurement(
        total_power_w=3500,
        solar_power_w=6000,
        import_power_w=3500,
        export_power_w=0,
    )
    subject, vehicle = controller(
        measurement,
        state(current=6),
        control_mode="meter-closed-loop",
        max_ramp_up_a=10,
    )

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=3000,
        extra_grid_power_w=2000,
        manual_override_amps=14,
        use_meter_reading=False,
    )

    assert decision.target_a == 7
    assert vehicle.commands == [7]


def test_runtime_flag_can_enable_meter_control():
    measurement = GridMeasurement(
        total_power_w=3500,
        solar_power_w=10000,
        import_power_w=3500,
        export_power_w=0,
    )
    subject, vehicle = controller(
        measurement,
        state(current=10),
        control_mode="solar-production",
    )

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=10),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
        use_meter_reading=True,
    )

    assert decision.target_a == 6
    assert vehicle.commands == [6]


def test_power_quota_reduces_from_projected_quarter_hour_import():
    measurement = GridMeasurement(
        total_power_w=21000,
        solar_power_w=0,
        import_power_w=21000,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=16), command_hysteresis_a=3)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=16),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=20,
        use_meter_reading=True,
        projected_quarter_hour_import_w=19000,
        power_quota_limit_w=17000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 13
    assert vehicle.commands == [13]
    assert "15 min" in decision.reason


def test_power_quota_stops_below_minimum_and_restarts_with_margin():
    measurement = GridMeasurement(
        total_power_w=22000,
        solar_power_w=0,
        import_power_w=22000,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=6))

    stopped = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=22000,
        power_quota_limit_w=17000,
        power_quota_hysteresis_w=500,
    )
    assert stopped.action == "stop"
    assert vehicle.commands == ["stop"]

    clear = GridMeasurement(
        total_power_w=-4000,
        solar_power_w=8000,
        import_power_w=0,
        export_power_w=4000,
    )
    restarted = subject.decide_from_snapshot(
        clear,
        state(current=6, status="Stopped"),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=9000,
        power_quota_limit_w=17000,
        power_quota_hysteresis_w=500,
    )
    assert restarted.action == "start"
    assert restarted.target_a == 5
    assert vehicle.commands == ["stop", 5, "start"]


def test_power_quota_keeps_minimum_when_extra_grid_exceeded_but_quota_available():
    measurement = GridMeasurement(
        total_power_w=6500,
        solar_power_w=1200,
        import_power_w=6500,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=5))

    stopped = subject.decide_from_snapshot(
        measurement,
        state(current=5),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=6500,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )
    assert stopped.action == "hold"
    assert stopped.target_a == 5
    assert "corrente minima" in stopped.reason
    assert vehicle.commands == []


def test_power_quota_resume_uses_quota_headroom_not_extra_grid():
    after_stop = GridMeasurement(
        total_power_w=1200,
        solar_power_w=1200,
        import_power_w=1200,
        export_power_w=0,
    )
    subject, vehicle = controller(after_stop, state(current=5))
    subject.restore_power_quota_pause()

    resumed = subject.decide_from_snapshot(
        after_stop,
        state(current=5, status="Stopped"),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=1200,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert resumed.action == "start"
    assert resumed.target_a == 5
    assert vehicle.commands == [5, "start"]


def test_power_quota_resume_waits_when_quota_has_no_amp_room():
    after_stop = GridMeasurement(
        total_power_w=1200,
        solar_power_w=0,
        import_power_w=1200,
        export_power_w=0,
    )
    subject, vehicle = controller(after_stop, state(current=5))
    subject.restore_power_quota_pause()

    resumed = subject.decide_from_snapshot(
        after_stop,
        state(current=5, status="Stopped"),
        non_tesla_power_w=0,
        extra_grid_power_w=2000,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=6500,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert resumed.action == "hold"
    assert resumed.target_a == 0
    assert "quota 15 min" in resumed.reason
    assert vehicle.commands == []


def test_power_quota_increases_only_to_absorb_export():
    measurement = GridMeasurement(
        total_power_w=-1800,
        solar_power_w=8000,
        import_power_w=0,
        export_power_w=1800,
    )
    subject, vehicle = controller(measurement, state(current=6), max_ramp_up_a=2)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=0,
        power_quota_limit_w=17000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 8
    assert vehicle.commands == [8]


def test_power_quota_ramp_up_uses_watt_distance():
    measurement = GridMeasurement(
        total_power_w=0,
        solar_power_w=7000,
        import_power_w=0,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=6), max_ramp_up_a=2)

    one_amp = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=900,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=0,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )
    assert one_amp.target_a == 7

    vehicle.commands.clear()
    two_amp = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=1500,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=0,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )
    assert two_amp.target_a == 8
    assert vehicle.commands == [8]


def test_power_quota_uses_extra_grid_as_import_target_when_solar_is_available():
    measurement = GridMeasurement(
        total_power_w=0,
        solar_power_w=5000,
        import_power_w=0,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=6), max_ramp_up_a=4)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=1500,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=0,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 8
    assert vehicle.commands == [8]
    assert "target rete" in decision.reason


def test_power_quota_extra_grid_reduces_when_import_is_already_high():
    measurement = GridMeasurement(
        total_power_w=6500,
        solar_power_w=5000,
        import_power_w=6500,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=6), max_ramp_up_a=4)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=6),
        non_tesla_power_w=0,
        extra_grid_power_w=1500,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=6500,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 5
    assert vehicle.commands == [5]
    assert decision.reason == "Import oltre extra rete; corrente minima Tesla entro quota"


def test_power_quota_can_reduce_below_configured_minimum_to_stay_under_quota():
    measurement = GridMeasurement(
        total_power_w=8400,
        solar_power_w=0,
        import_power_w=8400,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=5), max_ramp_up_a=4)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=5),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=8400,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 2
    assert vehicle.commands == [2]
    assert "sotto minimo" in decision.reason


def test_power_quota_resume_can_start_below_configured_minimum():
    measurement = GridMeasurement(
        total_power_w=5000,
        solar_power_w=0,
        import_power_w=5000,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=5))
    subject.restore_power_quota_pause()

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=5, status="Stopped"),
        non_tesla_power_w=0,
        extra_grid_power_w=0,
        manual_override_amps=14,
        use_meter_reading=True,
        projected_quarter_hour_import_w=5000,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert decision.action == "start"
    assert decision.target_a == 2
    assert vehicle.commands == [2, "start"]


def test_power_quota_reduces_when_import_exceeds_extra_grid():
    measurement = GridMeasurement(
        total_power_w=5080,
        solar_power_w=3520,
        import_power_w=5080,
        export_power_w=0,
    )
    subject, vehicle = controller(measurement, state(current=13), max_ramp_up_a=4)

    decision = subject.decide_from_snapshot(
        measurement,
        state(current=13),
        non_tesla_power_w=0,
        extra_grid_power_w=1500,
        manual_override_amps=16,
        use_meter_reading=True,
        projected_quarter_hour_import_w=5253,
        power_quota_limit_w=7000,
        power_quota_hysteresis_w=500,
    )

    assert decision.target_a == 7
    assert vehicle.commands == [7]
    assert "target rete" in decision.reason


def test_power_quota_stress_tracks_noisy_solar_bell_with_extra_grid():
    rng = random.Random(20260701)
    watts_per_amp = 230 * 3
    extra_grid_w = 3000
    quota_w = 7000
    quota_hysteresis_w = 500
    current_a = 1
    cloud_factor = 1.0
    subject, _vehicle = controller(
        GridMeasurement(0),
        state(current=current_a),
        min_charge_amps=1,
        max_charge_amps=13,
        max_ramp_up_a=2,
    )

    ramp_limited = 0
    minimum_cases = 0
    steady_tracking = 0
    target_values = []
    after_import_values = []

    for minute in range(6 * 60, 20 * 60 + 1, 5):
        x = (minute - 6 * 60) / (14 * 60)
        clear_sky_solar_w = 7600 * max(0.0, math.sin(math.pi * x)) ** 1.35
        if rng.random() < 0.10:
            cloud_factor = rng.uniform(0.18, 0.72)
        elif rng.random() < 0.18:
            cloud_factor = min(1.15, cloud_factor + rng.uniform(0.08, 0.25))
        else:
            cloud_factor = min(1.10, max(0.15, cloud_factor + rng.uniform(-0.04, 0.05)))
        solar_w = max(0.0, clear_sky_solar_w * cloud_factor + rng.uniform(-180, 180))

        house_w = 1150 + rng.uniform(-120, 180)
        if rng.random() < 0.11:
            house_w += rng.uniform(900, 2800)
        if 12 * 60 <= minute <= 13 * 60 + 30:
            house_w += 700
        if 18 * 60 + 30 <= minute <= 19 * 60 + 30:
            house_w += 1000

        before_balance_w = house_w + current_a * watts_per_amp - solar_w
        measurement = GridMeasurement(
            total_power_w=before_balance_w,
            solar_power_w=solar_w,
            import_power_w=max(before_balance_w, 0.0),
            export_power_w=max(-before_balance_w, 0.0),
        )

        decision = subject.decide_from_snapshot(
            measurement,
            state(current=current_a),
            non_tesla_power_w=house_w,
            extra_grid_power_w=extra_grid_w,
            manual_override_amps=14,
            use_meter_reading=True,
            projected_quarter_hour_import_w=max(before_balance_w, 0.0),
            power_quota_limit_w=quota_w,
            power_quota_hysteresis_w=quota_hysteresis_w,
        )
        target_a = int(decision.target_a or 0)
        after_balance_w = house_w + target_a * watts_per_amp - solar_w
        after_import_w = max(after_balance_w, 0.0)
        ideal_a = max(
            0,
            min(13, math.floor((solar_w + extra_grid_w - house_w) / watts_per_amp)),
        )

        assert 0 <= target_a <= 13
        assert target_a <= current_a + 2
        assert after_import_w <= quota_w + quota_hysteresis_w + subject.grid_hold_band_w

        if solar_w > 500 and max(before_balance_w, 0.0) <= quota_w + quota_hysteresis_w:
            if ideal_a < subject.min_charge_amps:
                minimum_cases += 1
                assert target_a == subject.min_charge_amps or target_a <= current_a
            elif ideal_a > current_a + subject.max_ramp_up_a:
                ramp_limited += 1
            else:
                # Step Tesla a 1 A trifase: errore atteso entro un ampere + hold band.
                assert abs(after_balance_w - extra_grid_w) <= watts_per_amp + subject.grid_hold_band_w
                steady_tracking += 1

        target_values.append(target_a)
        after_import_values.append(after_import_w)
        current_a = target_a if target_a > 0 else current_a

    assert min(target_values) == 1
    assert max(target_values) == 13
    assert max(after_import_values) < quota_w
    assert ramp_limited >= 10
    assert minimum_cases >= 1
    assert steady_tracking >= 120


def test_power_quota_stress_handles_cloud_drop_and_house_spike():
    watts_per_amp = 230 * 3
    subject, vehicle = controller(
        GridMeasurement(0),
        state(current=12),
        min_charge_amps=1,
        max_charge_amps=13,
        max_ramp_up_a=2,
    )

    def decide(solar_w: float, house_w: float, current_a: int):
        balance_w = house_w + current_a * watts_per_amp - solar_w
        measurement = GridMeasurement(
            total_power_w=balance_w,
            solar_power_w=solar_w,
            import_power_w=max(balance_w, 0.0),
            export_power_w=max(-balance_w, 0.0),
        )
        decision = subject.decide_from_snapshot(
            measurement,
            state(current=current_a),
            non_tesla_power_w=house_w,
            extra_grid_power_w=3000,
            manual_override_amps=14,
            use_meter_reading=True,
            projected_quarter_hour_import_w=max(balance_w, 0.0),
            power_quota_limit_w=7000,
            power_quota_hysteresis_w=500,
        )
        target_a = int(decision.target_a or 0)
        after_import_w = max(house_w + target_a * watts_per_amp - solar_w, 0.0)
        return decision, target_a, after_import_w

    decision, target_a, after_import_w = decide(solar_w=800, house_w=5200, current_a=12)
    assert target_a == 3
    assert after_import_w <= 7000
    assert "quota" in decision.reason

    decision, target_a, after_import_w = decide(solar_w=600, house_w=6500, current_a=target_a)
    assert target_a == 1
    assert after_import_w <= 7000
    assert "quota" in decision.reason

    decision, target_a, after_import_w = decide(solar_w=6000, house_w=1300, current_a=target_a)
    assert target_a == 3
    assert after_import_w == 0
    assert "corrente aumentata" in decision.reason
    assert vehicle.commands == [3, 1, 3]
