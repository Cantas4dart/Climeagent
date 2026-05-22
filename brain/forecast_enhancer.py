import math
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


class ForecastEnhancer:
    """
    Lightweight forecast correction layer.

    This module only improves the upstream station forecast input using METAR
    anchoring, source-priority weighting, and small bias corrections.
    """

    US_SOURCE_WEIGHTS = {
        "noaa": 1.0,
        "hrrr": 0.92,
        "openmeteo": 0.60,
        "gfs": 0.52,
    }
    NON_US_SOURCE_WEIGHTS = {
        "ecmwf": 1.0,
        "gfs": 0.68,
        "openmeteo": 0.48,
    }
    SOURCE_ANCHOR_WEIGHTS = {
        "noaa": 0.90,
        "hrrr": 1.00,
        "ecmwf": 0.92,
        "gfs": 0.72,
        "openmeteo": 0.62,
    }

    def enhance(self, forecast_bundle, market_date, is_us, timezone_name=None, reference_now=None):
        market_date = self._coerce_date(market_date)
        if market_date is None:
            return self._empty_result()

        timezone_name = (
            timezone_name
            or forecast_bundle.get("timezone")
            or "UTC"
        )
        now_local = self._resolve_now(timezone_name, reference_now)
        source_series = self._extract_source_series(forecast_bundle, market_date, is_us, now_local)
        if not source_series:
            return self._empty_result()

        metar = forecast_bundle.get("metar") or {}
        metar_temp = self._extract_metar_temperature(metar)
        metar_time = self._extract_metar_time(metar)
        metar_freshness = self._metar_freshness_factor(metar_time, now_local)

        corrected_sources = {}
        source_adjustments = {}
        source_weights = self.US_SOURCE_WEIGHTS if is_us else self.NON_US_SOURCE_WEIGHTS

        for source_name, source_data in source_series.items():
            high_temp = float(source_data["high"])
            current_temp = source_data.get("current")
            delta = self._source_adjustment(
                source_name=source_name,
                current_temp=current_temp,
                metar_temp=metar_temp,
                market_date=market_date,
                now_local=now_local,
                metar_freshness=metar_freshness,
                is_us=is_us,
            )
            corrected_high = high_temp + delta

            # METAR is the truth anchor and must not be undercut once observed.
            if metar_temp is not None:
                corrected_high = max(corrected_high, metar_temp)

            corrected_high = round(corrected_high, 2)
            corrected_sources[source_name] = corrected_high
            source_adjustments[source_name] = round(corrected_high - high_temp, 2)

        consensus_sources = self._select_integer_consensus_sources(corrected_sources, source_weights)

        weighted_pairs = [
            (consensus_sources[name], source_weights.get(name, 0.35))
            for name in consensus_sources
        ]
        adjusted_high = self._weighted_average(weighted_pairs)
        if metar_temp is not None:
            adjusted_high = max(adjusted_high, metar_temp)
        adjusted_high = round(adjusted_high, 2)

        confidence_score = self._confidence_score(
            corrected_sources=consensus_sources,
            metar_temp=metar_temp,
            metar_freshness=metar_freshness,
        )

        return {
            "adjusted_high_temperature": adjusted_high,
            "confidence_score": round(confidence_score, 4),
            "source_adjustments": source_adjustments,
            "source_temperatures": consensus_sources,
            "metar_anchor_temperature": None if metar_temp is None else round(metar_temp, 2),
        }

    def _empty_result(self):
        return {
            "adjusted_high_temperature": None,
            "confidence_score": 0.0,
            "source_adjustments": {},
            "source_temperatures": {},
            "metar_anchor_temperature": None,
        }

    def _extract_source_series(self, forecast_bundle, market_date, is_us, now_local):
        source_series = {}

        if is_us:
            noaa_series = self._extract_noaa_series(forecast_bundle.get("noaa"), market_date, now_local)
            if noaa_series:
                source_series["noaa"] = noaa_series

            hrrr_series = self._extract_open_meteo_series(
                forecast_bundle.get("hrrr"),
                "temperature_2m_hrrr",
                market_date,
                now_local,
            )
            if hrrr_series:
                source_series["hrrr"] = hrrr_series

            gfs_series = self._extract_open_meteo_series(
                forecast_bundle.get("gfs"),
                "temperature_2m_gfs_seamless",
                market_date,
                now_local,
            )
            if gfs_series:
                source_series["gfs"] = gfs_series

            baseline_series = self._extract_open_meteo_series(
                forecast_bundle.get("open_meteo"),
                "temperature_2m",
                market_date,
                now_local,
            )
            if baseline_series:
                source_series["openmeteo"] = baseline_series
        else:
            ecmwf_series = self._extract_open_meteo_series(
                forecast_bundle.get("ecmwf"),
                "temperature_2m_ecmwf_ifs025",
                market_date,
                now_local,
            )
            if ecmwf_series:
                source_series["ecmwf"] = ecmwf_series

            gfs_series = self._extract_open_meteo_series(
                forecast_bundle.get("gfs"),
                "temperature_2m_gfs_seamless",
                market_date,
                now_local,
            )
            if gfs_series:
                source_series["gfs"] = gfs_series

            baseline_series = self._extract_open_meteo_series(
                forecast_bundle.get("open_meteo"),
                "temperature_2m",
                market_date,
                now_local,
            )
            if baseline_series:
                source_series["openmeteo"] = baseline_series

        return source_series

    def _extract_noaa_series(self, payload, market_date, now_local):
        if not isinstance(payload, dict):
            return None
        periods = payload.get("hourly_periods", [])
        if not isinstance(periods, list):
            return None

        day_periods = []
        best_current = None
        best_delta = None
        for period in periods:
            if not isinstance(period, dict):
                continue
            start_time = self._parse_dt(period.get("startTime"))
            if start_time is None:
                continue
            if start_time.date() == market_date and period.get("temperature") is not None:
                day_periods.append(float(period["temperature"]))

            if period.get("temperature") is None:
                continue
            delta = abs((start_time - now_local).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_current = float(period["temperature"])

        if not day_periods:
            return None

        return {"high": max(day_periods), "current": best_current}

    def _extract_open_meteo_series(self, payload, field_name, market_date, now_local):
        if not isinstance(payload, dict):
            return None
        hourly = payload.get("hourly", {})
        if not isinstance(hourly, dict):
            return None
        times = hourly.get("time", [])
        values = hourly.get(field_name, [])
        if not values and field_name != "temperature_2m":
            values = hourly.get("temperature_2m", [])
        if not isinstance(times, list) or not isinstance(values, list):
            return None

        day_values = []
        best_current = None
        best_delta = None
        for index, time_value in enumerate(times):
            if index >= len(values):
                break
            dt_value = self._parse_dt(time_value, now_local.tzinfo)
            if dt_value is None:
                continue
            try:
                temp_value = float(values[index])
            except (TypeError, ValueError):
                continue
            if dt_value.date() == market_date:
                day_values.append(temp_value)
            delta = abs((dt_value - now_local).total_seconds())
            if best_delta is None or delta < best_delta:
                best_delta = delta
                best_current = temp_value

        if not day_values:
            return None

        return {"high": max(day_values), "current": best_current}

    def _source_adjustment(self, source_name, current_temp, metar_temp, market_date, now_local, metar_freshness, is_us):
        if metar_temp is None or current_temp is None:
            return 0.0

        raw_bias = float(metar_temp) - float(current_temp)
        capped_bias = max(-4.0, min(4.0, raw_bias))
        anchor_weight = self.SOURCE_ANCHOR_WEIGHTS.get(source_name, 0.60)
        horizon_weight = self._horizon_weight(market_date, now_local, is_us)
        trend_bias = self._trend_bias_boost(capped_bias, market_date, now_local, is_us)
        return (capped_bias * anchor_weight * metar_freshness * horizon_weight) + trend_bias

    def _horizon_weight(self, market_date, now_local, is_us):
        if market_date > now_local.date():
            return 0.32 if is_us else 0.28
        if market_date < now_local.date():
            return 0.90
        if now_local.hour < 11:
            return 0.58 if is_us else 0.50
        if now_local.hour < 16:
            return 0.76 if is_us else 0.70
        return 0.92

    def _trend_bias_boost(self, capped_bias, market_date, now_local, is_us):
        if market_date != now_local.date():
            return 0.0
        if capped_bias <= 0:
            return 0.0

        peak_start = 13 if not is_us else 14
        peak_end = 17 if not is_us else 18
        if now_local.hour < peak_start:
            return 0.18 * capped_bias
        if now_local.hour <= peak_end:
            return 0.10 * capped_bias
        return 0.0

    def _confidence_score(self, corrected_sources, metar_temp, metar_freshness):
        values = list(corrected_sources.values())
        if not values:
            return 0.0

        spread = max(values) - min(values)
        agreement = max(0.0, 1.0 - min(spread / 4.0, 1.0))
        source_coverage = min(len(values) / 3.0, 1.0)
        metar_component = metar_freshness if metar_temp is not None else 0.18
        return max(0.05, min(0.99, 0.20 + (0.35 * metar_component) + (0.25 * agreement) + (0.20 * source_coverage)))

    def _select_integer_consensus_sources(self, corrected_sources, source_weights):
        if len(corrected_sources) < 3:
            return dict(corrected_sources)

        buckets = {}
        for source_name, temp in corrected_sources.items():
            integer_bucket = math.floor(float(temp))
            buckets.setdefault(integer_bucket, []).append(source_name)

        best_bucket_sources = max(
            buckets.values(),
            key=lambda names: (
                len(names),
                sum(source_weights.get(name, 0.35) for name in names),
            ),
        )
        if len(best_bucket_sources) <= len(corrected_sources) / 2:
            return dict(corrected_sources)

        return {
            source_name: corrected_sources[source_name]
            for source_name in corrected_sources
            if source_name in set(best_bucket_sources)
        }

    def _extract_metar_temperature(self, metar):
        for key in ("temp", "tempC", "temp_c", "temperature", "temperature_c"):
            value = metar.get(key)
            try:
                if value is not None:
                    return float(value)
            except (TypeError, ValueError):
                continue
        return None

    def _extract_metar_time(self, metar):
        for key in ("obsTime", "observation_time", "timestamp", "date"):
            value = metar.get(key)
            parsed = self._parse_dt(value)
            if parsed is not None:
                return parsed
        return None

    def _metar_freshness_factor(self, metar_time, now_local):
        if metar_time is None:
            return 0.25
        delta_hours = abs((now_local - metar_time).total_seconds()) / 3600.0
        if delta_hours <= 1.5:
            return 1.0
        if delta_hours <= 3.0:
            return 0.82
        if delta_hours <= 6.0:
            return 0.60
        return 0.35

    def _weighted_average(self, pairs):
        total_weight = 0.0
        total_value = 0.0
        for value, weight in pairs:
            total_value += float(value) * float(weight)
            total_weight += float(weight)
        if total_weight <= 0:
            return 0.0
        return total_value / total_weight

    def _resolve_now(self, timezone_name, reference_now=None):
        tz = ZoneInfo(timezone_name)
        if reference_now is None:
            return datetime.now(tz)
        if reference_now.tzinfo is None:
            return reference_now.replace(tzinfo=tz)
        return reference_now.astimezone(tz)

    def _coerce_date(self, value):
        if isinstance(value, date):
            return value
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value)).date()
        except ValueError:
            return None

    def _parse_dt(self, value, default_tz=None):
        if isinstance(value, datetime):
            if value.tzinfo is not None:
                return value
            return value.replace(tzinfo=default_tz or timezone.utc)
        if isinstance(value, (int, float)):
            try:
                numeric = float(value)
                if numeric > 1_000_000_000_000:
                    numeric /= 1000.0
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        if not value:
            return None
        raw_value = str(value).strip()
        if raw_value.replace(".", "", 1).isdigit():
            try:
                numeric = float(raw_value)
                if numeric > 1_000_000_000_000:
                    numeric /= 1000.0
                return datetime.fromtimestamp(numeric, tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            normalized = raw_value.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed
        return parsed.replace(tzinfo=default_tz or timezone.utc)
