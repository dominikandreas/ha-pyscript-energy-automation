# /config/pyscript/jkbms_capacity_ranking.py

# =============================================================================
# JK-BMS LFP cell capacity ranking / weak-cell detector
#
# Method:
# - Uses the cell_voltages attribute from the JK-BMS delta-cell-voltage entity.
# - During discharge, records one snapshot per event when average cell voltage
#   first crosses configured lower-knee thresholds.
# - Ranks cells by deviation from pack mean at those threshold crossings.
# - Integrates JK-BMS balancer activity as a top-end diagnostic signal.
#
# Interpretation:
# - Higher avg_deviation_mv near the lower knee = stronger candidate.
# - Lower / more negative avg_deviation_mv = weaker / lower-capacity candidate.
# - More top-balance activity = cell often high at top, useful as confirmation
#   when combined with the lower-knee ranking.
#
# Important:
# - This is mostly valid within each pack.
# - Across packs, treat comparison as approximate because current sharing,
#   temperature, wiring resistance, BMS calibration, and age can differ.
# - Balancer integration is approximate unless your integration exposes exact
#   per-cell balancing current and direction.
# =============================================================================


# -----------------------------------------------------------------------------
# CONFIGURATION
# -----------------------------------------------------------------------------

PACKS = {
    "pack_1": {
        "cell_voltages": "sensor.basement_jkbms_40726490042_delta_cell_voltage",
        "balancer": "binary_sensor.40726490042_balancer",
        "soc": "sensor.basement_jkbms_40726490042_battery",
        "current": "sensor.basement_jkbms_40726490042_current",
        "output_sensor": "sensor.jkbms_40726490042_capacity_ranking",
    },
    "pack_2": {
        "cell_voltages": "sensor.basement_jkbms_40807491748_delta_cell_voltage",
        "balancer": "binary_sensor.40807491748_balancer",
        "soc": "sensor.basement_jkbms_40807491748_battery",
        "current": "sensor.basement_jkbms_40807491748_current",
        "output_sensor": "sensor.jkbms_40807491748_capacity_ranking",
    },
}

EXPECTED_CELL_COUNT = 16
CRON_INTERVAL_SECONDS = 60.0

# Your configured system limits.
DISCHARGE_LIMIT_CELL_VOLTAGE = 2.90
CHARGE_LIMIT_CELL_VOLTAGE = 3.50
BALANCING_START_VOLTAGE = 3.45

# Event trigger thresholds.
# One snapshot is taken once per discharge event when avg cell voltage crosses
# each threshold.
#
# 3.15 V = early lower knee, gentle signal
# 3.10 V = good practical ranking point
# 3.05 V = strong weak-cell signal, still above your 2.90 V cutoff
VOLTAGE_THRESHOLDS = [3.15, 3.10, 3.05]

# Rearm after a meaningful recharge.
# 3.33 V is realistic for a high-SOC rested LFP pack; SOC is an additional
# rearm signal when voltage is slightly below the flat-curve threshold.
RESET_AVG_CELL_VOLTAGE = 3.33
RESET_SOC = 90.0

# Negative current = discharge.
# Tune these to your actual current sensors.
#
# For 280 Ah / 314 Ah packs, -2 A is too close to idle.
# Start with roughly 20 A to 120 A discharge per pack if your sensors are
# pack-specific.
MIN_DISCHARGE_CURRENT_A = -20.0
MAX_DISCHARGE_CURRENT_A = -120.0

# Ignore tiny deltas because JK-BMS cell voltage data is often quantized to ~1 mV.
MIN_PACK_DELTA_MV = 3.0

# Do not capture the deepest lower-knee snapshots if the lowest cell is already
# too close to your 2.90 V cutoff.
MIN_CELL_VOLTAGE_CAPTURE_LIMIT = 2.98

# Balancer integration.
# Set this to your JK-BMS active balancer current setting. Common values are
# 0.6 A, 1.0 A, or 2.0 A depending on model/config.
BALANCE_CURRENT_A = 2.0

# Use 1.0 if you only want equivalent moved Ah. Use ~0.85 if you want a rough
# transferred-energy-equivalent estimate for an active balancer.
BALANCE_EFFICIENCY = 0.9

# The JK integration exposes a 16-character mask like "0000000000000000".
# This script assumes the leftmost bit is cell 1. Set True if your integration
# uses the opposite bit order.
BALANCER_MASK_REVERSED = False


# -----------------------------------------------------------------------------
# MAIN TRIGGER
# -----------------------------------------------------------------------------

@time_trigger("cron(* * * * *)")
def evaluate_cell_capacities():
    """
    Runs every minute.

    For each pack:
    - reads SOC/current/cell voltages
    - integrates balancer activity
    - detects whether a discharge event is active
    - captures threshold snapshots once per event
    - updates a custom HA sensor with rankings and diagnostic attributes
    """
    for pack_name, config in PACKS.items():
        _evaluate_pack(pack_name, config)


# -----------------------------------------------------------------------------
# PACK EVALUATION
# -----------------------------------------------------------------------------

def _evaluate_pack(pack_name, config):
    try:
        soc = float(state.get(config["soc"]))
        current = float(state.get(config["current"]))
    except (ValueError, TypeError):
        return

    voltages = _read_cell_voltages(config["cell_voltages"])

    if not voltages or len(voltages) != EXPECTED_CELL_COUNT:
        return

    avg_cell_voltage = sum(voltages) / len(voltages)
    min_cell_voltage = min(voltages)
    max_cell_voltage = max(voltages)
    pack_delta_mv = (max_cell_voltage - min_cell_voltage) * 1000.0

    out_entity = config["output_sensor"]
    attrs = state.getattr(out_entity) or {}

    event_active = bool(attrs.get("event_active", False))
    crossed_thresholds = attrs.get("crossed_thresholds", [])
    threshold_stats = attrs.get("threshold_stats", {})
    previous_avg_cell_voltage = _coerce_optional_float(
        attrs.get("previous_avg_cell_voltage")
    )

    if not isinstance(crossed_thresholds, list):
        crossed_thresholds = []

    if not isinstance(threshold_stats, dict):
        threshold_stats = {}

    balancer_attrs = _update_balancer_stats(
        config=config,
        existing_attrs=attrs,
        voltages=voltages,
        avg_cell_voltage=avg_cell_voltage,
    )

    # -------------------------------------------------------------------------
    # Upper charge / balancing region
    #
    # We explicitly do not record capacity-ranking data here. Balancing changes
    # relative cell voltages deliberately, so it should not affect lower-knee
    # capacity ranking. Balancer stats are still integrated above.
    # -------------------------------------------------------------------------
    if avg_cell_voltage >= BALANCING_START_VOLTAGE:
        event_active = False
        crossed_thresholds = []

        _write_status(
            pack_name=pack_name,
            config=config,
            value="Balancing / upper charge region - armed for next discharge",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # -------------------------------------------------------------------------
    # Rearm event after meaningful recharge
    # -------------------------------------------------------------------------
    if avg_cell_voltage >= RESET_AVG_CELL_VOLTAGE or soc >= RESET_SOC:
        event_active = False
        crossed_thresholds = []

        _write_status(
            pack_name=pack_name,
            config=config,
            value="Armed for next discharge",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # -------------------------------------------------------------------------
    # Discharge current gate
    # -------------------------------------------------------------------------
    current_ok = MAX_DISCHARGE_CURRENT_A <= current <= MIN_DISCHARGE_CURRENT_A

    if not current_ok:
        _write_status(
            pack_name=pack_name,
            config=config,
            value="Waiting for stable discharge current",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # -------------------------------------------------------------------------
    # Ignore near-perfectly balanced samples
    # -------------------------------------------------------------------------
    if pack_delta_mv < MIN_PACK_DELTA_MV:
        _write_status(
            pack_name=pack_name,
            config=config,
            value="Waiting for meaningful cell spread",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # -------------------------------------------------------------------------
    # Minimum-cell safety guard
    # -------------------------------------------------------------------------
    if min_cell_voltage <= MIN_CELL_VOLTAGE_CAPTURE_LIMIT:
        _write_status(
            pack_name=pack_name,
            config=config,
            value="Minimum cell too low - not capturing deeper threshold",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # -------------------------------------------------------------------------
    # Discharge event active
    # -------------------------------------------------------------------------
    event_active = True
    did_capture = False

    if previous_avg_cell_voltage is None:
        _write_status(
            pack_name=pack_name,
            config=config,
            value="Initialized threshold crossing baseline",
            event_active=event_active,
            crossed_thresholds=crossed_thresholds,
            threshold_stats=threshold_stats,
            soc=soc,
            current=current,
            avg_cell_voltage=avg_cell_voltage,
            min_cell_voltage=min_cell_voltage,
            max_cell_voltage=max_cell_voltage,
            pack_delta_mv=pack_delta_mv,
            balancer_attrs=balancer_attrs,
        )
        return

    # Highest threshold first: 3.15, then 3.10, then 3.05.
    for threshold in sorted(VOLTAGE_THRESHOLDS, reverse=True):
        threshold_key = _threshold_key(threshold)
        crossed_now = previous_avg_cell_voltage > threshold >= avg_cell_voltage

        if crossed_now and threshold_key not in crossed_thresholds:
            threshold_stats = _record_threshold_snapshot(
                threshold_stats=threshold_stats,
                threshold=threshold,
                voltages=voltages,
                avg_cell_voltage=avg_cell_voltage,
                soc=soc,
                current=current,
                pack_delta_mv=pack_delta_mv,
            )

            crossed_thresholds.append(threshold_key)
            did_capture = True

    if did_capture:
        value = "Captured threshold snapshot"
    else:
        value = "Discharge event active"

    _write_status(
        pack_name=pack_name,
        config=config,
        value=value,
        event_active=event_active,
        crossed_thresholds=crossed_thresholds,
        threshold_stats=threshold_stats,
        soc=soc,
        current=current,
        avg_cell_voltage=avg_cell_voltage,
        min_cell_voltage=min_cell_voltage,
        max_cell_voltage=max_cell_voltage,
        pack_delta_mv=pack_delta_mv,
        balancer_attrs=balancer_attrs,
    )


# -----------------------------------------------------------------------------
# SENSOR READERS
# -----------------------------------------------------------------------------

def _read_cell_voltages(entity_id):
    """
    Reads cell_voltages from the JK-BMS delta-cell-voltage entity.

    Expected HA attribute format:

    attributes:
      cell_voltages:
        - 3.312
        - 3.312
        ...
    """
    attrs = state.getattr(entity_id)

    if not attrs or "cell_voltages" not in attrs:
        return None

    try:
        return [float(v) for v in attrs["cell_voltages"]]
    except (ValueError, TypeError):
        return None


def _coerce_optional_float(value):
    if value is None:
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def _read_balancer_mask(config):
    """
    Reads the JK-BMS balancer cells bitmask.

    Expected HA attribute format:

    attributes:
      cells: "0000000000000000"
    """
    balancer_entity = config.get("balancer")

    if not balancer_entity:
        return None, "unconfigured", False

    balancer_state = state.get(balancer_entity)
    balancer_attrs = state.getattr(balancer_entity) or {}
    cells_mask = balancer_attrs.get("cells")

    if not isinstance(cells_mask, str):
        return None, balancer_state, False

    cells_mask = cells_mask.strip()

    if len(cells_mask) != EXPECTED_CELL_COUNT:
        return None, balancer_state, False

    if BALANCER_MASK_REVERSED:
        cells_mask = cells_mask[::-1]

    balancer_on = str(balancer_state).lower() in ["on", "true", "1"]
    return cells_mask, balancer_state, balancer_on


# -----------------------------------------------------------------------------
# BALANCER INTEGRATION
# -----------------------------------------------------------------------------

def _update_balancer_stats(config, existing_attrs, voltages, avg_cell_voltage):
    """
    Integrates JK-BMS balancer activity.

    The JK integration usually exposes only a bitmask of active cells, not exact
    per-cell current or source/sink direction. Therefore this is an approximate
    top-end diagnostic:

    - balance_active_seconds_by_cell: how long each cell was marked active
    - estimated_balance_ah_by_cell: active_seconds * assumed current * efficiency
    - balance_donor/receiver estimates: heuristic based on voltage relative to
      pack average while active

    Use this to confirm lower-knee ranking, not as a standalone capacity meter.
    """
    cells_mask, balancer_state, balancer_on = _read_balancer_mask(config)

    balance_seconds = existing_attrs.get("balance_active_seconds_by_cell")
    donor_seconds = existing_attrs.get("balance_donor_seconds_by_cell")
    receiver_seconds = existing_attrs.get("balance_receiver_seconds_by_cell")

    if not _is_cell_list(balance_seconds):
        balance_seconds = [0.0] * EXPECTED_CELL_COUNT

    if not _is_cell_list(donor_seconds):
        donor_seconds = [0.0] * EXPECTED_CELL_COUNT

    if not _is_cell_list(receiver_seconds):
        receiver_seconds = [0.0] * EXPECTED_CELL_COUNT

    active_cells = []
    donor_cells = []
    receiver_cells = []

    if cells_mask is not None and balancer_on:
        for i, bit in enumerate(cells_mask):
            if bit != "1":
                continue

            cell_num = i + 1
            active_cells.append(cell_num)
            balance_seconds[i] += CRON_INTERVAL_SECONDS

            # Heuristic for active balancers: active cell above mean is probably
            # donating; active cell below mean is probably receiving. Some JK
            # integrations may only flag high/source cells, so treat these as
            # diagnostic hints, not truth.
            if voltages[i] >= avg_cell_voltage:
                donor_cells.append(cell_num)
                donor_seconds[i] += CRON_INTERVAL_SECONDS
            else:
                receiver_cells.append(cell_num)
                receiver_seconds[i] += CRON_INTERVAL_SECONDS

    estimated_balance_ah = [
        round(seconds / 3600.0 * BALANCE_CURRENT_A * BALANCE_EFFICIENCY, 4)
        for seconds in balance_seconds
    ]
    estimated_donor_ah = [
        round(seconds / 3600.0 * BALANCE_CURRENT_A * BALANCE_EFFICIENCY, 4)
        for seconds in donor_seconds
    ]
    estimated_receiver_ah = [
        round(seconds / 3600.0 * BALANCE_CURRENT_A * BALANCE_EFFICIENCY, 4)
        for seconds in receiver_seconds
    ]

    top_balance_ranking = []

    for i in range(EXPECTED_CELL_COUNT):
        top_balance_ranking.append({
            "cell": i + 1,
            "balance_seconds": round(balance_seconds[i], 1),
            "estimated_balance_ah": estimated_balance_ah[i],
            "estimated_donor_ah": estimated_donor_ah[i],
            "estimated_receiver_ah": estimated_receiver_ah[i],
            "last_voltage": round(voltages[i], 4),
        })

    top_balance_ranking = sorted(
        top_balance_ranking,
        key=lambda x: x["estimated_balance_ah"],
        reverse=True,
    )

    return {
        "balancer_entity": config.get("balancer"),
        "balancer_state": balancer_state,
        "balancer_on": balancer_on,
        "balancer_cells_mask": cells_mask,
        "balancer_active_cells": active_cells,
        "balancer_donor_cells_heuristic": donor_cells,
        "balancer_receiver_cells_heuristic": receiver_cells,
        "balance_active_seconds_by_cell": balance_seconds,
        "balance_donor_seconds_by_cell": donor_seconds,
        "balance_receiver_seconds_by_cell": receiver_seconds,
        "estimated_balance_ah_by_cell": estimated_balance_ah,
        "estimated_balance_donor_ah_by_cell": estimated_donor_ah,
        "estimated_balance_receiver_ah_by_cell": estimated_receiver_ah,
        "top_balance_ranking": top_balance_ranking,
        "balance_current_assumed_a": BALANCE_CURRENT_A,
        "balance_efficiency_assumed": BALANCE_EFFICIENCY,
        "balancer_mask_reversed": BALANCER_MASK_REVERSED,
    }


def _is_cell_list(value):
    return isinstance(value, list) and len(value) == EXPECTED_CELL_COUNT


# -----------------------------------------------------------------------------
# SNAPSHOT / RANKING LOGIC
# -----------------------------------------------------------------------------

def _record_threshold_snapshot(
    threshold_stats,
    threshold,
    voltages,
    avg_cell_voltage,
    soc,
    current,
    pack_delta_mv,
):
    """
    Records one threshold snapshot.

    For each cell, we store deviation from the pack mean:

        deviation_mv = (cell_voltage - avg_cell_voltage) * 1000

    This is better than raw voltage because it removes common-mode pack voltage.
    """
    threshold_key = _threshold_key(threshold)
    num_cells = len(voltages)

    stats = threshold_stats.get(threshold_key)

    if not isinstance(stats, dict):
        stats = {}

    deviation_sums_mv = stats.get("deviation_sums_mv")
    sample_count = stats.get("sample_count", 0)

    if (
        not isinstance(deviation_sums_mv, list)
        or len(deviation_sums_mv) != num_cells
        or not isinstance(sample_count, int)
    ):
        deviation_sums_mv = [0.0] * num_cells
        sample_count = 0

    deviations_mv = [
        (v - avg_cell_voltage) * 1000.0
        for v in voltages
    ]

    for i in range(num_cells):
        deviation_sums_mv[i] += deviations_mv[i]

    sample_count += 1

    avg_deviations_mv = [
        round(v / sample_count, 2)
        for v in deviation_sums_mv
    ]

    cells = []

    for i in range(num_cells):
        cells.append({
            "cell": i + 1,
            "avg_deviation_mv": avg_deviations_mv[i],
            "last_deviation_mv": round(deviations_mv[i], 2),
            "last_voltage": round(voltages[i], 4),
        })

    # Higher relative voltage near lower knee = stronger candidate.
    # More negative relative voltage = weaker candidate.
    ranking = sorted(
        cells,
        key=lambda x: x["avg_deviation_mv"],
        reverse=True,
    )

    stats = {
        "threshold_v": threshold,
        "sample_count": sample_count,
        "ranking": ranking,
        "strongest_candidate_cell": ranking[0]["cell"],
        "weakest_candidate_cell": ranking[-1]["cell"],
        "strongest_avg_deviation_mv": ranking[0]["avg_deviation_mv"],
        "weakest_avg_deviation_mv": ranking[-1]["avg_deviation_mv"],
        "last_avg_cell_voltage": round(avg_cell_voltage, 4),
        "last_soc": round(soc, 1),
        "last_current_a": round(current, 1),
        "last_pack_delta_mv": round(pack_delta_mv, 1),
        "deviation_sums_mv": deviation_sums_mv,
    }

    threshold_stats[threshold_key] = stats
    return threshold_stats


def _build_combined_ranking(threshold_stats):
    """
    Combines all threshold rankings into one score.

    The combined score is the average of avg_deviation_mv across all thresholds.

    More positive = stronger candidate.
    More negative = weaker candidate.
    """
    sums = {}
    counts = {}

    for threshold_key, stats in threshold_stats.items():
        if not isinstance(stats, dict):
            continue

        ranking = stats.get("ranking", [])

        if not isinstance(ranking, list):
            continue

        for item in ranking:
            if not isinstance(item, dict):
                continue

            cell = item.get("cell")
            avg_deviation_mv = item.get("avg_deviation_mv")

            if cell is None or avg_deviation_mv is None:
                continue

            try:
                cell = int(cell)
                avg_deviation_mv = float(avg_deviation_mv)
            except (ValueError, TypeError):
                continue

            sums[cell] = sums.get(cell, 0.0) + avg_deviation_mv
            counts[cell] = counts.get(cell, 0) + 1

    combined = []

    for cell, total in sums.items():
        count = counts[cell]
        combined.append({
            "cell": cell,
            "combined_avg_deviation_mv": round(total / count, 2),
            "threshold_count": count,
        })

    return sorted(
        combined,
        key=lambda x: x["combined_avg_deviation_mv"],
        reverse=True,
    )


def _build_diagnostic_ranking(combined_ranking, balancer_attrs):
    """
    Combines bottom-knee ranking with top-balancer activity.

    This does not produce a precise Ah capacity estimate. It produces a useful
    diagnosis score:

    - negative bottom-knee deviation = weak-at-bottom signal
    - high top-balance Ah = high-at-top signal
    - both together = stronger low-capacity candidate
    """
    if not isinstance(combined_ranking, list):
        combined_ranking = []

    balance_ah = balancer_attrs.get("estimated_balance_ah_by_cell", [])

    if not _is_cell_list(balance_ah):
        balance_ah = [0.0] * EXPECTED_CELL_COUNT

    bottom_scores = {}
    for item in combined_ranking:
        if not isinstance(item, dict):
            continue
        try:
            cell = int(item.get("cell"))
            score = float(item.get("combined_avg_deviation_mv"))
        except (ValueError, TypeError):
            continue
        bottom_scores[cell] = score

    diagnostic = []

    for cell in range(1, EXPECTED_CELL_COUNT + 1):
        bottom_mv = bottom_scores.get(cell)
        bal_ah = float(balance_ah[cell - 1])

        if bottom_mv is None:
            weak_bottom_score = 0.0
        else:
            # Only negative lower-knee deviations count as weak-at-bottom.
            weak_bottom_score = max(0.0, -bottom_mv)

        # Not a physical unit. Higher means more suspicious.
        # 10 mV weak-at-bottom is weighted similarly to roughly 0.1 Ah of
        # top-balancer activity with the default factor below.
        suspicion_score = round(weak_bottom_score + bal_ah * 100.0, 2)

        if bottom_mv is not None and bottom_mv < -5.0 and bal_ah > 0.0:
            diagnosis = "weak_bottom_and_top_balanced"
        elif bottom_mv is not None and bottom_mv < -5.0:
            diagnosis = "weak_at_bottom"
        elif bal_ah > 0.0:
            diagnosis = "top_balanced"
        else:
            diagnosis = "normal_or_insufficient_data"

        diagnostic.append({
            "cell": cell,
            "diagnosis_score": suspicion_score,
            "bottom_combined_deviation_mv": bottom_mv,
            "estimated_top_balance_ah": round(bal_ah, 4),
            "diagnosis": diagnosis,
        })

    return sorted(
        diagnostic,
        key=lambda x: x["diagnosis_score"],
        reverse=True,
    )


def _threshold_key(threshold):
    """
    Converts 3.15 -> '3_15' and 3.10 -> '3_10' for safe HA attribute keys.
    """
    return f"{threshold:.2f}".replace(".", "_")


# -----------------------------------------------------------------------------
# STATE WRITER
# -----------------------------------------------------------------------------

def _write_status(
    pack_name,
    config,
    value,
    event_active,
    crossed_thresholds,
    threshold_stats,
    soc,
    current,
    avg_cell_voltage,
    min_cell_voltage,
    max_cell_voltage,
    pack_delta_mv,
    balancer_attrs,
):
    combined_ranking = _build_combined_ranking(threshold_stats)
    diagnostic_ranking = _build_diagnostic_ranking(combined_ranking, balancer_attrs)

    if combined_ranking:
        combined_strongest_cell = combined_ranking[0]["cell"]
        combined_weakest_cell = combined_ranking[-1]["cell"]
        combined_strongest_score = combined_ranking[0]["combined_avg_deviation_mv"]
        combined_weakest_score = combined_ranking[-1]["combined_avg_deviation_mv"]
    else:
        combined_strongest_cell = None
        combined_weakest_cell = None
        combined_strongest_score = None
        combined_weakest_score = None

    if diagnostic_ranking:
        most_suspicious_cell = diagnostic_ranking[0]["cell"]
        most_suspicious_score = diagnostic_ranking[0]["diagnosis_score"]
    else:
        most_suspicious_cell = None
        most_suspicious_score = None

    state.set(
        config["output_sensor"],
        value=value,
        new_attributes={
            "event_active": event_active,
            "crossed_thresholds": crossed_thresholds,
            "threshold_stats": threshold_stats,

            "combined_ranking": combined_ranking,
            "combined_strongest_candidate_cell": combined_strongest_cell,
            "combined_weakest_candidate_cell": combined_weakest_cell,
            "combined_strongest_score_mv": combined_strongest_score,
            "combined_weakest_score_mv": combined_weakest_score,

            "diagnostic_ranking": diagnostic_ranking,
            "most_suspicious_cell": most_suspicious_cell,
            "most_suspicious_score": most_suspicious_score,

            "last_soc": round(soc, 1),
            "last_current_a": round(current, 1),
            "last_avg_cell_voltage": round(avg_cell_voltage, 4),
            "previous_avg_cell_voltage": round(avg_cell_voltage, 4),
            "last_min_cell_voltage": round(min_cell_voltage, 4),
            "last_max_cell_voltage": round(max_cell_voltage, 4),
            "last_pack_delta_mv": round(pack_delta_mv, 1),

            **balancer_attrs,

            "voltage_thresholds": VOLTAGE_THRESHOLDS,
            "reset_avg_cell_voltage": RESET_AVG_CELL_VOLTAGE,
            "reset_soc": RESET_SOC,
            "balancing_start_voltage": BALANCING_START_VOLTAGE,
            "discharge_limit_cell_voltage": DISCHARGE_LIMIT_CELL_VOLTAGE,
            "charge_limit_cell_voltage": CHARGE_LIMIT_CELL_VOLTAGE,
            "min_cell_voltage_capture_limit": MIN_CELL_VOLTAGE_CAPTURE_LIMIT,

            "min_discharge_current_a": MIN_DISCHARGE_CURRENT_A,
            "max_discharge_current_a": MAX_DISCHARGE_CURRENT_A,
            "min_pack_delta_mv": MIN_PACK_DELTA_MV,

            "friendly_name": f"{pack_name.replace('_', ' ').title()} Cell Capacity Ranking",
            "icon": "mdi:battery-arrow-down",
        },
    )


# -----------------------------------------------------------------------------
# SERVICES
# -----------------------------------------------------------------------------

@service
def reset_jkbms_capacity_rankings():
    """
    Call from Developer Tools -> Services after physically rearranging cells
    or when you want to discard the learned ranking and balancer history.
    """
    for pack_name, config in PACKS.items():
        state.set(
            config["output_sensor"],
            value="Awaiting data",
            new_attributes={
                "event_active": False,
                "crossed_thresholds": [],
                "threshold_stats": {},
                "previous_avg_cell_voltage": None,
                "combined_ranking": [],
                "combined_strongest_candidate_cell": None,
                "combined_weakest_candidate_cell": None,
                "combined_strongest_score_mv": None,
                "combined_weakest_score_mv": None,
                "diagnostic_ranking": [],
                "most_suspicious_cell": None,
                "most_suspicious_score": None,

                "balancer_entity": config.get("balancer"),
                "balancer_state": None,
                "balancer_on": False,
                "balancer_cells_mask": None,
                "balancer_active_cells": [],
                "balancer_donor_cells_heuristic": [],
                "balancer_receiver_cells_heuristic": [],
                "balance_active_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "balance_donor_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "balance_receiver_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_donor_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_receiver_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "top_balance_ranking": [],
                "balance_current_assumed_a": BALANCE_CURRENT_A,
                "balance_efficiency_assumed": BALANCE_EFFICIENCY,
                "balancer_mask_reversed": BALANCER_MASK_REVERSED,

                "voltage_thresholds": VOLTAGE_THRESHOLDS,
                "reset_avg_cell_voltage": RESET_AVG_CELL_VOLTAGE,
                "reset_soc": RESET_SOC,
                "balancing_start_voltage": BALANCING_START_VOLTAGE,
                "discharge_limit_cell_voltage": DISCHARGE_LIMIT_CELL_VOLTAGE,
                "charge_limit_cell_voltage": CHARGE_LIMIT_CELL_VOLTAGE,
                "min_cell_voltage_capture_limit": MIN_CELL_VOLTAGE_CAPTURE_LIMIT,

                "min_discharge_current_a": MIN_DISCHARGE_CURRENT_A,
                "max_discharge_current_a": MAX_DISCHARGE_CURRENT_A,
                "min_pack_delta_mv": MIN_PACK_DELTA_MV,

                "friendly_name": f"{pack_name.replace('_', ' ').title()} Cell Capacity Ranking",
                "icon": "mdi:battery-arrow-down",
            },
        )


@service
def reset_jkbms_balancer_stats():
    """
    Clears only the integrated balancer statistics, while keeping lower-knee
    threshold history.
    """
    for pack_name, config in PACKS.items():
        attrs = state.getattr(config["output_sensor"]) or {}

        state.set(
            config["output_sensor"],
            value="Balancer stats reset",
            new_attributes={
                **attrs,
                "balancer_entity": config.get("balancer"),
                "balancer_state": None,
                "balancer_on": False,
                "balancer_cells_mask": None,
                "balancer_active_cells": [],
                "balancer_donor_cells_heuristic": [],
                "balancer_receiver_cells_heuristic": [],
                "balance_active_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "balance_donor_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "balance_receiver_seconds_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_donor_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "estimated_balance_receiver_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT,
                "top_balance_ranking": [],
                "diagnostic_ranking": _build_diagnostic_ranking(
                    attrs.get("combined_ranking", []),
                    {"estimated_balance_ah_by_cell": [0.0] * EXPECTED_CELL_COUNT},
                ),
                "balance_current_assumed_a": BALANCE_CURRENT_A,
                "balance_efficiency_assumed": BALANCE_EFFICIENCY,
                "balancer_mask_reversed": BALANCER_MASK_REVERSED,
                "friendly_name": f"{pack_name.replace('_', ' ').title()} Cell Capacity Ranking",
                "icon": "mdi:battery-arrow-down",
            },
        )


@service
def arm_jkbms_capacity_rankings():
    """
    Clears only the current discharge-event latch, but keeps historical rankings
    and balancer integration.

    Use this if the script got stuck in an event state after a Home Assistant
    restart or sensor outage.
    """
    for pack_name, config in PACKS.items():
        attrs = state.getattr(config["output_sensor"]) or {}
        threshold_stats = attrs.get("threshold_stats", {})

        if not isinstance(threshold_stats, dict):
            threshold_stats = {}

        state.set(
            config["output_sensor"],
            value="Manually armed for next discharge",
            new_attributes={
                **attrs,
                "event_active": False,
                "crossed_thresholds": [],
                "threshold_stats": threshold_stats,
                "previous_avg_cell_voltage": None,
                "friendly_name": f"{pack_name.replace('_', ' ').title()} Cell Capacity Ranking",
                "icon": "mdi:battery-arrow-down",
            },
        )
