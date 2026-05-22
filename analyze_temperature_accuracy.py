import json
import sqlite3
from collections import defaultdict


def safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def connect():
    conn = sqlite3.connect("data/users.db")
    conn.row_factory = sqlite3.Row
    return conn


def fetch_rows():
    conn = connect()
    rows = conn.execute(
        """
        SELECT *
        FROM temperature_settlement_analysis
        ORDER BY trade_source, trade_id
        """
    ).fetchall()
    conn.close()
    return rows


def mean(values):
    values = [v for v in values if v is not None]
    if not values:
        return None
    return sum(values) / len(values)


def print_group_stats(title, groups):
    print(f"\n=== {title} ===")
    for key in sorted(groups):
        rows = groups[key]
        errors = [safe_float(row["forecast_error_avg"]) for row in rows]
        hits = [row["target_hit"] for row in rows if row["target_hit"] is not None]
        avg_error = mean(errors)
        hit_rate = ((sum(hits) / len(hits)) * 100.0) if hits else 0.0
        print(
            f"{str(key):35s} "
            f"trades={len(rows):3d} "
            f"avg_error={avg_error if avg_error is not None else 'n/a'} "
            f"target_hit={hit_rate:5.1f}%"
        )


def main():
    rows = fetch_rows()
    if not rows:
        print("No temperature settlement analysis rows found.")
        return

    resolved = [row for row in rows if row["actual_source_status"] == "resolved" and row["actual_temperature"] is not None]
    unresolved = [row for row in rows if row["actual_source_status"] != "resolved"]

    print(f"Total analysis rows: {len(rows)}")
    print(f"Resolved actual temperatures: {len(resolved)}")
    print(f"Unresolved rows: {len(unresolved)}")

    overall_errors = [safe_float(row["forecast_error_avg"]) for row in resolved]
    overall_hits = [row["target_hit"] for row in resolved if row["target_hit"] is not None]
    print(f"Overall average forecast error: {mean(overall_errors)}")
    print(f"Overall target hit rate: {(sum(overall_hits) / len(overall_hits) * 100.0) if overall_hits else 0.0:.1f}%")

    by_city = defaultdict(list)
    by_station = defaultdict(list)
    by_timezone = defaultdict(list)
    by_target = defaultdict(list)
    per_source_errors = defaultdict(list)

    for row in resolved:
        by_city[row["city"] or "Unknown"].append(row)
        by_station[row["station_name"] or "Unknown"].append(row)
        by_timezone[row["timezone"] or "Unknown"].append(row)
        by_target[row["target_type"] or "unknown"].append(row)

        raw_errors = row["forecast_error_by_source_json"]
        if raw_errors:
            try:
                payload = json.loads(raw_errors)
                for source, value in payload.items():
                    numeric = safe_float(value)
                    if numeric is not None:
                        per_source_errors[source].append(numeric)
            except json.JSONDecodeError:
                pass

    print_group_stats("By City", by_city)
    print_group_stats("By Station", by_station)
    print_group_stats("By Timezone", by_timezone)
    print_group_stats("By Target Type", by_target)

    print("\n=== Per-Source Forecast Error ===")
    for source in sorted(per_source_errors):
        avg_error = mean(per_source_errors[source])
        print(f"{source:20s} samples={len(per_source_errors[source]):3d} avg_error={avg_error}")

    if unresolved:
        status_counts = defaultdict(int)
        for row in unresolved:
            status_counts[row["actual_source_status"] or "unknown"] += 1
        print("\n=== Unresolved Statuses ===")
        for status in sorted(status_counts):
            print(f"{status:30s} {status_counts[status]}")


if __name__ == "__main__":
    main()
