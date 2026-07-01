from tesla_energy_controller.energy import reconcile_energy_flows


def test_house_is_real_minus_estimated_grid_balance():
    breakdown = reconcile_energy_flows(
        solar_power_w=5200,
        appliances_power_w=700,
        tesla_power_w=3450,
        import_power_w=300,
        export_power_w=1200,
    )

    assert breakdown.estimated_import_power_w == 0
    assert breakdown.estimated_export_power_w == 1050
    assert breakdown.device_power_w == 150
    assert breakdown.house_power_w == 850
    assert breakdown.appliances_power_w == 700
    assert breakdown.total_consumption_w == 4300
    assert breakdown.meter_balance_available is True


def test_house_is_zero_without_real_import_and_export():
    breakdown = reconcile_energy_flows(
        solar_power_w=5000,
        appliances_power_w=1000,
        tesla_power_w=3000,
        import_power_w=None,
        export_power_w=None,
    )

    assert breakdown.import_power_w == 0
    assert breakdown.export_power_w == 1000
    assert breakdown.device_power_w == 0
    assert breakdown.house_power_w == 1000
    assert breakdown.total_consumption_w == 4000
    assert breakdown.meter_balance_available is False


def test_negative_residual_is_clamped_to_zero_consumption():
    breakdown = reconcile_energy_flows(
        solar_power_w=5000,
        appliances_power_w=1000,
        tesla_power_w=3000,
        import_power_w=0,
        export_power_w=1400,
    )

    assert breakdown.device_power_w == 0
    assert breakdown.house_power_w == 600
    assert breakdown.total_consumption_w == 3600
