from tesla_energy_controller.demand import calculate_power_demand, monthly_peak_power_demand


def test_power_demand_projects_missing_five_minute_slots():
    demand = calculate_power_demand(
        [("2026-06-28T13:00:00+02:00", 12000)],
        observed_at="2026-06-28T13:05:00+02:00",
        import_power_w=18000,
    )

    assert demand.sample_count == 2
    assert demand.sampled_average_w == 15000
    assert demand.projected_average_w == 16000
    assert demand.completed_average_w is None


def test_power_demand_completes_with_three_distinct_slots():
    demand = calculate_power_demand(
        [
            ("2026-06-28T13:00:00+02:00", 12000),
            ("2026-06-28T13:05:00+02:00", 18000),
        ],
        observed_at="2026-06-28T13:10:00+02:00",
        import_power_w=21000,
    )

    assert demand.sample_count == 3
    assert demand.projected_average_w == 17000
    assert demand.completed_average_w == 17000


def test_power_demand_averages_fast_readings_inside_each_five_minute_slot():
    demand = calculate_power_demand(
        [
            ("2026-06-28T13:00:00+02:00", 10000),
            ("2026-06-28T13:02:30+02:00", 14000),
            ("2026-06-28T13:05:00+02:00", 18000),
        ],
        observed_at="2026-06-28T13:10:00+02:00",
        import_power_w=21000,
    )

    assert demand.completed_average_w == 17000


def test_monthly_peak_ignores_incomplete_quarters():
    samples = [
        ("2026-06-28T13:00:00+02:00", 12000),
        ("2026-06-28T13:05:00+02:00", 18000),
        ("2026-06-28T13:10:00+02:00", 21000),
        ("2026-06-28T13:15:00+02:00", 30000),
    ]

    assert monthly_peak_power_demand(samples, "2026-06") == 17000
