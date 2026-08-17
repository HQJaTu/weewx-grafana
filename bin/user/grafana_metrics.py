# Copyright 2026 - Distributed under the terms of the GNU Public License (GPLv3)
"""Naming, unit, and payload-building logic shared by the live OTLP uploader
(user.grafana) and the historical backfill converter (user.grafana_backfill).

Keeping this logic in one place guarantees that a metric backfilled from the
weewx database has exactly the same name and labels as the same observation
published live, so the two data paths stitch together into one continuous
series in Grafana Cloud.

Metric naming follows the OpenTelemetry/Prometheus convention of baking the
unit into the metric name, e.g. outTemp in degrees Celsius becomes
'weewx_outdoor_temperature_celsius'. The station, model, and unit_system are
published as static labels/resource attributes rather than baked into the
metric name, since they identify the *source*, not the *measurement*.
"""

import re

import weewx.units

VERSION = "0.1.0"

# Observation types that carry no numeric weather data and are never
# published as metrics.
DEFAULT_SKIP_FIELDS = frozenset(('usUnits', 'interval', 'dateTime'))

# weewx observation key -> canonical metric base name. Anything not listed
# here falls back to a snake_case rendering of the weewx key (see
# _snake_case()), so unmapped/custom/extra-sensor observation types still get
# a sensible, unique name.
OBS_METRIC_NAMES = {
    'outTemp': 'outdoor_temperature',
    'inTemp': 'indoor_temperature',
    'outHumidity': 'outdoor_humidity',
    'inHumidity': 'indoor_humidity',
    'barometer': 'barometric_pressure',
    'pressure': 'absolute_pressure',
    'altimeter': 'altimeter_pressure',
    'windSpeed': 'wind_speed',
    'windGust': 'wind_gust_speed',
    'windDir': 'wind_direction',
    'windGustDir': 'wind_gust_direction',
    'windrun': 'wind_run',
    'rain': 'rain',
    'rainRate': 'rain_rate',
    'dewpoint': 'dew_point',
    'inDewpoint': 'indoor_dew_point',
    'windchill': 'wind_chill',
    'heatindex': 'heat_index',
    'radiation': 'solar_radiation',
    'UV': 'uv_index',
    'ET': 'evapotranspiration',
    'appTemp': 'apparent_temperature',
    'cloudbase': 'cloud_base',
    'humidex': 'humidex',
    'THSW': 'thsw_index',
    'consBatteryVoltage': 'console_battery_voltage',
    'heatingVoltage': 'heating_voltage',
    'supplyVoltage': 'supply_voltage',
    'referenceVoltage': 'reference_voltage',
    'rxCheckPercent': 'rx_check',
    'signal1': 'signal_strength',
    'hourRain': 'rain_hour_total',
    'rain24': 'rain_24h_total',
    'dayRain': 'rain_day_total',
    'monthRain': 'rain_month_total',
    'yearRain': 'rain_year_total',
}

# weewx unit type -> human-readable metric name suffix.
UNIT_METRIC_SUFFIX = {
    'degree_F': 'fahrenheit',
    'degree_C': 'celsius',
    'degree_K': 'kelvin',
    'inHg': 'inches_mercury',
    'mbar': 'millibars',
    'hPa': 'hectopascals',
    'mmHg': 'millimeters_mercury',
    'kPa': 'kilopascals',
    'inHg_per_hour': 'inches_mercury_per_hour',
    'mbar_per_hour': 'millibars_per_hour',
    'hPa_per_hour': 'hectopascals_per_hour',
    'kPa_per_hour': 'kilopascals_per_hour',
    'inch': 'inches',
    'cm': 'centimeters',
    'mm': 'millimeters',
    'inch_per_hour': 'inches_per_hour',
    'cm_per_hour': 'centimeters_per_hour',
    'mm_per_hour': 'millimeters_per_hour',
    'mile_per_hour': 'miles_per_hour',
    'mile_per_hour2': 'miles_per_hour_squared',
    'km_per_hour': 'kilometers_per_hour',
    'km_per_hour2': 'kilometers_per_hour_squared',
    'meter_per_second': 'meters_per_second',
    'meter_per_second2': 'meters_per_second_squared',
    'knot': 'knots',
    'knot2': 'knots_squared',
    'percent': 'percent',
    'degree_compass': 'degrees',
    'watt_per_meter_squared': 'watts_per_square_meter',
    'uv_index': 'index',
    'unix_epoch': 'seconds',
    'volt': 'volts',
    'amp': 'amps',
    'watt': 'watts',
    'watt_hour': 'watt_hours',
    'kilowatt_hour': 'kilowatt_hours',
    'ohm': 'ohms',
    'foot': 'feet',
    'mile': 'miles',
    'km': 'kilometers',
    'meter': 'meters',
    'count': 'count',
    'minute': 'minutes',
    'second': 'seconds',
    'hour': 'hours',
    'day': 'days',
    'NONE': None,
}

# weewx unit type -> UCUM-ish unit symbol used for the OTLP metric's
# informational 'unit' field. Purely cosmetic (Grafana Cloud does not require
# it); observations with no entry are published without a 'unit' field.
OTLP_UNIT_SYMBOL = {
    'degree_F': '[degF]',
    'degree_C': 'Cel',
    'degree_K': 'K',
    'inHg': '[in_i]Hg',
    'mbar': 'mbar',
    'hPa': 'hPa',
    'mmHg': 'mm[Hg]',
    'kPa': 'kPa',
    'inch': '[in_i]',
    'cm': 'cm',
    'mm': 'mm',
    'mile_per_hour': '[mi_i]/h',
    'km_per_hour': 'km/h',
    'meter_per_second': 'm/s',
    'knot': '[kn_i]',
    'percent': '%',
    'degree_compass': 'deg',
    'watt_per_meter_squared': 'W/m2',
    'volt': 'V',
    'amp': 'A',
    'watt': 'W',
    'watt_hour': 'W.h',
    'ohm': 'Ohm',
    'foot': '[ft_i]',
    'meter': 'm',
}


def _snake_case(obs_key):
    """Render a weewx camelCase observation key as snake_case.

    e.g. 'extraTemp1' -> 'extra_temp_1', 'outTemp' -> 'out_temp'.
    """
    s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', obs_key)
    s = re.sub(r'([A-Za-z])([0-9])', r'\1_\2', s)
    return s.lower()


def metric_base_name(obs_key):
    """Return the canonical metric base name (no 'weewx_' prefix, no unit
    suffix) for a weewx observation key."""
    return OBS_METRIC_NAMES.get(obs_key, _snake_case(obs_key))


def metric_name_for(obs_key, unit_type, name_override=None):
    """Return the full metric name for an observation.

    If 'name_override' is given, it is used verbatim (the admin has taken
    full control of the name, mirroring the 'inputs' override in
    weewx-mqtt). Otherwise the name is built as 'weewx_<base>_<unit-suffix>',
    e.g. 'weewx_outdoor_temperature_celsius'.
    """
    if name_override:
        return name_override
    base = metric_base_name(obs_key)
    suffix = UNIT_METRIC_SUFFIX.get(unit_type)
    if suffix:
        return "weewx_%s_%s" % (base, suffix)
    return "weewx_%s" % base


def unit_system_name(usUnits):
    """Return the configured nickname (US, METRIC, METRICWX) for a weewx
    standard unit system constant, falling back to its raw value."""
    return weewx.units.unit_nicknames.get(usUnits, str(usUnits))


def static_labels(station, model, unit_system):
    """Return the static station/model/unit_system labels as an ordered
    dict-like mapping. These are stable for the life of a weewx
    configuration and describe the *source* of the data, not the individual
    measurement."""
    return {
        'station': station,
        'model': model,
        'unit_system': unit_system,
    }


def iter_observations(record, inputs=None, skip_fields=None):
    """Yield (metric_name, unit_type, value) for every numeric, publishable
    observation in a record.

    Args:
        record: A weewx record dict. Must contain 'usUnits'.
        inputs: Optional dict of obs_key -> {'name': ..., 'unit': ...}
            overrides, mirroring weewx-mqtt's 'inputs' option. 'unit'
            converts the value before publishing; 'name' replaces the
            generated metric name outright.
        skip_fields: Optional iterable of obs_keys to exclude, in addition
            to the always-excluded DEFAULT_SKIP_FIELDS.
    """
    inputs = inputs or {}
    skip = DEFAULT_SKIP_FIELDS | frozenset(skip_fields or ())
    usUnits = record['usUnits']

    for obs_key, raw_value in record.items():
        if obs_key in skip or raw_value is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue

        override = inputs.get(obs_key, {})
        to_unit = override.get('unit')
        if to_unit:
            from_unit, from_group = weewx.units.getStandardUnitType(usUnits, obs_key)
            value = weewx.units.convert((value, from_unit, from_group), to_unit)[0]
            unit_type = to_unit
        else:
            unit_type, _group = weewx.units.getStandardUnitType(usUnits, obs_key)

        name = metric_name_for(obs_key, unit_type, override.get('name'))
        yield name, unit_type, value


def build_otlp_metrics(record, inputs=None, skip_fields=None):
    """Build the list of OTLP Metric objects (as plain dicts, ready for JSON
    serialization) for a single record. Each observation becomes a Gauge
    metric with a single data point timestamped at record['dateTime'].
    """
    time_unix_nano = str(int(record['dateTime']) * 1000000000)
    metrics = []
    for name, unit_type, value in iter_observations(record, inputs, skip_fields):
        metric = {
            'name': name,
            'gauge': {
                'dataPoints': [{
                    'timeUnixNano': time_unix_nano,
                    'asDouble': value,
                }],
            },
        }
        unit_symbol = OTLP_UNIT_SYMBOL.get(unit_type)
        if unit_symbol:
            metric['unit'] = unit_symbol
        metrics.append(metric)
    return metrics


def build_otlp_payload(record, station, model, unit_system, inputs=None, skip_fields=None):
    """Build a full OTLP ExportMetricsServiceRequest (as a plain dict, ready
    for JSON serialization) for a single record.

    station/model/unit_system are published as resource attributes: they are
    constant for every metric this station ever publishes, which is exactly
    what an OTLP Resource represents.
    """
    metrics = build_otlp_metrics(record, inputs, skip_fields)
    return {
        'resourceMetrics': [{
            'resource': {
                'attributes': [
                    {'key': 'station', 'value': {'stringValue': station}},
                    {'key': 'model', 'value': {'stringValue': model}},
                    {'key': 'unit_system', 'value': {'stringValue': unit_system}},
                ],
            },
            'scopeMetrics': [{
                'scope': {'name': 'weewx-grafana', 'version': VERSION},
                'metrics': metrics,
            }],
        }],
    }


# ----------------------------------------------------------------------------
# OpenMetrics text format, used by the backfill converter. promtool's
# 'tsdb create-blocks-from openmetrics' command consumes exactly this format.
# ----------------------------------------------------------------------------

_ESCAPE_TABLE = str.maketrans({'\\': '\\\\', '"': '\\"', '\n': '\\n'})


def _escape_label_value(value):
    return value.translate(_ESCAPE_TABLE)


def format_label_set(labels):
    """Render a dict of labels as a Prometheus/OpenMetrics label-set, e.g.
    '{station="Home",model="VantagePro2"}'."""
    if not labels:
        return ''
    parts = ['%s="%s"' % (k, _escape_label_value(str(v))) for k, v in labels.items()]
    return '{%s}' % ','.join(parts)


def write_openmetrics(fileobj, families):
    """Write a mapping of metric_name -> family to 'fileobj' in OpenMetrics
    text exposition format.

    Args:
        fileobj: A text-mode file-like object to write to.
        families: dict of metric_name -> {
            'help': str (optional),
            'samples': list of (labels_dict, timestamp, value) tuples.
        }
        OpenMetrics requires every sample for a metric family to be
        contiguous, so families/samples are written out fully grouped; the
        caller is responsible for collecting them that way first.
    """
    for name in sorted(families):
        family = families[name]
        fileobj.write('# TYPE %s gauge\n' % name)
        help_text = family.get('help')
        if help_text:
            fileobj.write('# HELP %s %s\n' % (name, help_text.replace('\n', ' ')))
        for labels, timestamp, value in sorted(family['samples'], key=lambda s: s[1]):
            fileobj.write('%s%s %s %d\n' % (
                name, format_label_set(labels), repr(float(value)), int(timestamp)))
    fileobj.write('# EOF\n')