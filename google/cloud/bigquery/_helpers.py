# Copyright 2015 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Shared helper functions for BigQuery API classes."""

import base64
import datetime
import decimal
import math
import re
import os
from typing import Optional, Union

from dateutil import relativedelta
from google.cloud._helpers import UTC  # type: ignore
from google.cloud._helpers import _date_from_iso8601_date
from google.cloud._helpers import _datetime_from_microseconds
from google.cloud._helpers import _RFC3339_MICROS
from google.cloud._helpers import _RFC3339_NO_FRACTION
from google.cloud._helpers import _to_bytes

import packaging.version

_RFC3339_MICROS_NO_ZULU = "%Y-%m-%dT%H:%M:%S.%f"
_TIMEONLY_WO_MICROS = "%H:%M:%S"
_TIMEONLY_W_MICROS = "%H:%M:%S.%f"
_PROJECT_PREFIX_PATTERN = re.compile(
    r"""
    (?P<project_id>\S+\:[^.]+)\.(?P<dataset_id>[^.]+)(?:$|\.(?P<custom_id>[^.]+)$)
""",
    re.VERBOSE,
)

# BigQuery sends INTERVAL data in "canonical format"
# https://cloud.google.com/bigquery/docs/reference/standard-sql/data-types#interval_type
_INTERVAL_PATTERN = re.compile(
    r"(?P<calendar_sign>-?)(?P<years>\d+)-(?P<months>\d+) "
    r"(?P<days>-?\d+) "
    r"(?P<time_sign>-?)(?P<hours>\d+):(?P<minutes>\d+):(?P<seconds>\d+)\.?(?P<fraction>\d*)?$"
)

_BQ_STORAGE_OPTIONAL_READ_SESSION_VERSION = packaging.version.Version("2.6.0")

BIGQUERY_EMULATOR_HOST = "BIGQUERY_EMULATOR_HOST"
"""Environment variable defining host for emulator."""

_DEFAULT_HOST = "https://bigquery.googleapis.com"
"""Default host for JSON API."""


def _get_host():
    return os.environ.get(BIGQUERY_EMULATOR_HOST, _DEFAULT_HOST)


class BQStorageVersions:
    """Version comparisons for google-cloud-bigqueyr-storage package."""

    def __init__(self):
        self._installed_version = None

    @property
    def installed_version(self) -> packaging.version.Version:
        """Return the parsed version of google-cloud-bigquery-storage."""
        if self._installed_version is None:
            from google.cloud import bigquery_storage

            self._installed_version = packaging.version.parse(
                # Use 0.0.0, since it is earlier than any released version.
                # Legacy versions also have the same property, but
                # creating a LegacyVersion has been deprecated.
                # https://github.com/pypa/packaging/issues/321
                getattr(bigquery_storage, "__version__", "0.0.0")
            )

        return self._installed_version

    @property
    def is_read_session_optional(self) -> bool:
        """True if read_session is optional to rows().

        See: https://github.com/googleapis/python-bigquery-storage/pull/228
        """
        return self.installed_version >= _BQ_STORAGE_OPTIONAL_READ_SESSION_VERSION


class PyarrowVersions:
    """Version comparisons for pyarrow package."""

    def __init__(self):
        self._installed_version = None

    @property
    def installed_version(self) -> packaging.version.Version:
        """Return the parsed version of pyarrow."""
        if self._installed_version is None:
            import pyarrow  # type: ignore

            self._installed_version = packaging.version.parse(
                # Use 0.0.0, since it is earlier than any released version.
                # Legacy versions also have the same property, but
                # creating a LegacyVersion has been deprecated.
                # https://github.com/pypa/packaging/issues/321
                getattr(pyarrow, "__version__", "0.0.0")
            )

        return self._installed_version

    @property
    def use_compliant_nested_type(self) -> bool:
        return self.installed_version.major >= 4


BQ_STORAGE_VERSIONS = BQStorageVersions()
PYARROW_VERSIONS = PyarrowVersions()


def _not_null(value, field):
    """Check whether 'value' should be coerced to 'field' type."""
    return value is not None or (field is not None and field.mode != "NULLABLE")


def _int_from_json(value, field):
    """Coerce 'value' to an int, if set or not nullable."""
    if _not_null(value, field):
        return int(value)


def _interval_from_json(
    value: Optional[str], field
) -> Optional[relativedelta.relativedelta]:
    """Coerce 'value' to an interval, if set or not nullable."""
    if not _not_null(value, field):
        return None
    if value is None:
        raise TypeError(f"got {value} for REQUIRED field: {repr(field)}")

    parsed = _INTERVAL_PATTERN.match(value)
    if parsed is None:
        raise ValueError(f"got interval: '{value}' with unexpected format")

    calendar_sign = -1 if parsed.group("calendar_sign") == "-" else 1
    years = calendar_sign * int(parsed.group("years"))
    months = calendar_sign * int(parsed.group("months"))
    days = int(parsed.group("days"))
    time_sign = -1 if parsed.group("time_sign") == "-" else 1
    hours = time_sign * int(parsed.group("hours"))
    minutes = time_sign * int(parsed.group("minutes"))
    seconds = time_sign * int(parsed.group("seconds"))
    fraction = parsed.group("fraction")
    microseconds = time_sign * int(fraction.ljust(6, "0")[:6]) if fraction else 0

    return relativedelta.relativedelta(
        years=years,
        months=months,
        days=days,
        hours=hours,
        minutes=minutes,
        seconds=seconds,
        microseconds=microseconds,
    )


def _float_from_json(value, field):
    """Coerce 'value' to a float, if set or not nullable."""
    if _not_null(value, field):
        return float(value)


def _decimal_from_json(value, field):
    """Coerce 'value' to a Decimal, if set or not nullable."""
    if _not_null(value, field):
        return decimal.Decimal(value)


def _bool_from_json(value, field):
    """Coerce 'value' to a bool, if set or not nullable."""
    if _not_null(value, field):
        return value.lower() in ["t", "true", "1"]


def _string_from_json(value, _):
    """NOOP string -> string coercion"""
    return value


def _bytes_from_json(value, field):
    """Base64-decode value"""
    if _not_null(value, field):
        return base64.standard_b64decode(_to_bytes(value))


def _timestamp_from_json(value, field):
    """Coerce 'value' to a datetime, if set or not nullable."""
    if _not_null(value, field):
        # value will be a integer in seconds, to microsecond precision, in UTC.
        return _datetime_from_microseconds(int(value))


def _timestamp_query_param_from_json(value, field):
    """Coerce 'value' to a datetime, if set or not nullable.

    Args:
        value (str): The timestamp.

        field (google.cloud.bigquery.schema.SchemaField):
            The field corresponding to the value.

    Returns:
        Optional[datetime.datetime]:
            The parsed datetime object from
            ``value`` if the ``field`` is not null (otherwise it is
            :data:`None`).
    """
    if _not_null(value, field):
        # Canonical formats for timestamps in BigQuery are flexible. See:
        # g.co/cloud/bigquery/docs/reference/standard-sql/data-types#timestamp-type
        # The separator between the date and time can be 'T' or ' '.
        value = value.replace(" ", "T", 1)
        # The UTC timezone may be formatted as Z or +00:00.
        value = value.replace("Z", "")
        value = value.replace("+00:00", "")

        if "." in value:
            # YYYY-MM-DDTHH:MM:SS.ffffff
            return datetime.datetime.strptime(value, _RFC3339_MICROS_NO_ZULU).replace(
                tzinfo=UTC
            )
        else:
            # YYYY-MM-DDTHH:MM:SS
            return datetime.datetime.strptime(value, _RFC3339_NO_FRACTION).replace(
                tzinfo=UTC
            )
    else:
        return None


def _datetime_from_json(value, field):
    """Coerce 'value' to a datetime, if set or not nullable.

    Args:
        value (str): The timestamp.
        field (google.cloud.bigquery.schema.SchemaField):
            The field corresponding to the value.

    Returns:
        Optional[datetime.datetime]:
            The parsed datetime object from
            ``value`` if the ``field`` is not null (otherwise it is
            :data:`None`).
    """
    if _not_null(value, field):
        if "." in value:
            # YYYY-MM-DDTHH:MM:SS.ffffff
            return datetime.datetime.strptime(value, _RFC3339_MICROS_NO_ZULU)
        else:
            # YYYY-MM-DDTHH:MM:SS
            return datetime.datetime.strptime(value, _RFC3339_NO_FRACTION)
    else:
        return None


def _date_from_json(value, field):
    """Coerce 'value' to a datetime date, if set or not nullable"""
    if _not_null(value, field):
        # value will be a string, in YYYY-MM-DD form.
        return _date_from_iso8601_date(value)


def _time_from_json(value, field):
    """Coerce 'value' to a datetime date, if set or not nullable"""
    if _not_null(value, field):
        if len(value) == 8:  # HH:MM:SS
            fmt = _TIMEONLY_WO_MICROS
        elif len(value) == 15:  # HH:MM:SS.micros
            fmt = _TIMEONLY_W_MICROS
        else:
            raise ValueError("Unknown time format: {}".format(value))
        return datetime.datetime.strptime(value, fmt).time()


def _record_from_json(value, field):
    """Coerce 'value' to a mapping, if set or not nullable."""
    if _not_null(value, field):
        record = {}
        record_iter = zip(field.fields, value["f"])
        for subfield, cell in record_iter:
            converter = _CELLDATA_FROM_JSON[subfield.field_type]
            if subfield.mode == "REPEATED":
                value = [converter(item["v"], subfield) for item in cell["v"]]
            else:
                value = converter(cell["v"], subfield)
            record[subfield.name] = value
        return record


_CELLDATA_FROM_JSON = {
    "INTEGER": _int_from_json,
    "INT64": _int_from_json,
    "INTERVAL": _interval_from_json,
    "FLOAT": _float_from_json,
    "FLOAT64": _float_from_json,
    "NUMERIC": _decimal_from_json,
    "BIGNUMERIC": _decimal_from_json,
    "BOOLEAN": _bool_from_json,
    "BOOL": _bool_from_json,
    "STRING": _string_from_json,
    "GEOGRAPHY": _string_from_json,
    "BYTES": _bytes_from_json,
    "TIMESTAMP": _timestamp_from_json,
    "DATETIME": _datetime_from_json,
    "DATE": _date_from_json,
    "TIME": _time_from_json,
    "RECORD": _record_from_json,
}

_QUERY_PARAMS_FROM_JSON = dict(_CELLDATA_FROM_JSON)
_QUERY_PARAMS_FROM_JSON["TIMESTAMP"] = _timestamp_query_param_from_json


def _field_to_index_mapping(schema):
    """Create a mapping from schema field name to index of field."""
    return {f.name: i for i, f in enumerate(schema)}


def _field_from_json(resource, field):
    converter = _CELLDATA_FROM_JSON.get(field.field_type, lambda value, _: value)
    if field.mode == "REPEATED":
        return [converter(item["v"], field) for item in resource]
    else:
        return converter(resource, field)


def _row_tuple_from_json(row, schema):
    """Convert JSON row data to row with appropriate types.

    Note:  ``row['f']`` and ``schema`` are presumed to be of the same length.

    Args:
        row (Dict): A JSON response row to be converted.
        schema (Sequence[Union[ \
                :class:`~google.cloud.bigquery.schema.SchemaField`, \
                Mapping[str, Any] \
        ]]):  Specification of the field types in ``row``.

    Returns:
        Tuple: A tuple of data converted to native types.
    """
    from google.cloud.bigquery.schema import _to_schema_fields

    schema = _to_schema_fields(schema)

    row_data = []
    for field, cell in zip(schema, row["f"]):
        row_data.append(_field_from_json(cell["v"], field))
    return tuple(row_data)


def _rows_from_json(values, schema):
    """Convert JSON row data to rows with appropriate types.

    Args:
        values (Sequence[Dict]): The list of responses (JSON rows) to convert.
        schema (Sequence[Union[ \
                :class:`~google.cloud.bigquery.schema.SchemaField`, \
                Mapping[str, Any] \
        ]]):
            The table's schema. If any item is a mapping, its content must be
            compatible with
            :meth:`~google.cloud.bigquery.schema.SchemaField.from_api_repr`.

    Returns:
        List[:class:`~google.cloud.bigquery.Row`]
    """
    from google.cloud.bigquery import Row
    from google.cloud.bigquery.schema import _to_schema_fields

    schema = _to_schema_fields(schema)
    field_to_index = _field_to_index_mapping(schema)
    return [Row(_row_tuple_from_json(r, schema), field_to_index) for r in values]


def _int_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, int):
        value = str(value)
    return value


def _float_to_json(value) -> Union[None, str, float]:
    """Coerce 'value' to an JSON-compatible representation."""
    if value is None:
        return None

    if isinstance(value, str):
        value = float(value)

    return str(value) if (math.isnan(value) or math.isinf(value)) else float(value)


def _decimal_to_json(value):
    """Coerce 'value' to a JSON-compatible representation."""
    if isinstance(value, decimal.Decimal):
        value = str(value)
    return value


def _bool_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, bool):
        value = "true" if value else "false"
    return value


def _bytes_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, bytes):
        value = base64.standard_b64encode(value).decode("ascii")
    return value


def _timestamp_to_json_parameter(value):
    """Coerce 'value' to an JSON-compatible representation.

    This version returns the string representation used in query parameters.
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo not in (None, UTC):
            # Convert to UTC and remove the time zone info.
            value = value.replace(tzinfo=None) - value.utcoffset()
        value = "%s %s+00:00" % (value.date().isoformat(), value.time().isoformat())
    return value


def _timestamp_to_json_row(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, datetime.datetime):
        # For naive datetime objects UTC timezone is assumed, thus we format
        # those to string directly without conversion.
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        value = value.strftime(_RFC3339_MICROS)
    return value


def _datetime_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, datetime.datetime):
        # For naive datetime objects UTC timezone is assumed, thus we format
        # those to string directly without conversion.
        if value.tzinfo is not None:
            value = value.astimezone(UTC)
        value = value.strftime(_RFC3339_MICROS_NO_ZULU)
    return value


def _date_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, datetime.date):
        value = value.isoformat()
    return value


def _time_to_json(value):
    """Coerce 'value' to an JSON-compatible representation."""
    if isinstance(value, datetime.time):
        value = value.isoformat()
    return value


# Converters used for scalar values marshalled as row data.
_SCALAR_VALUE_TO_JSON_ROW = {
    "INTEGER": _int_to_json,
    "INT64": _int_to_json,
    "FLOAT": _float_to_json,
    "FLOAT64": _float_to_json,
    "NUMERIC": _decimal_to_json,
    "BIGNUMERIC": _decimal_to_json,
    "BOOLEAN": _bool_to_json,
    "BOOL": _bool_to_json,
    "BYTES": _bytes_to_json,
    "TIMESTAMP": _timestamp_to_json_row,
    "DATETIME": _datetime_to_json,
    "DATE": _date_to_json,
    "TIME": _time_to_json,
    # Make sure DECIMAL and BIGDECIMAL are handled, even though
    # requests for them should be converted to NUMERIC.  Better safe
    # than sorry.
    "DECIMAL": _decimal_to_json,
    "BIGDECIMAL": _decimal_to_json,
}


# Converters used for scalar values marshalled as query parameters.
_SCALAR_VALUE_TO_JSON_PARAM = _SCALAR_VALUE_TO_JSON_ROW.copy()
_SCALAR_VALUE_TO_JSON_PARAM["TIMESTAMP"] = _timestamp_to_json_parameter


def _scalar_field_to_json(field, row_value):
    """Maps a field and value to a JSON-safe value.

    Args:
        field (google.cloud.bigquery.schema.SchemaField):
            The SchemaField to use for type conversion and field name.
        row_value (Any):
            Value to be converted, based on the field's type.

    Returns:
        Any: A JSON-serializable object.
    """
    converter = _SCALAR_VALUE_TO_JSON_ROW.get(field.field_type)
    if converter is None:  # STRING doesn't need converting
        return row_value
    return converter(row_value)


def _repeated_field_to_json(field, row_value):
    """Convert a repeated/array field to its JSON representation.

    Args:
        field (google.cloud.bigquery.schema.SchemaField):
            The SchemaField to use for type conversion and field name. The
            field mode must equal ``REPEATED``.
        row_value (Sequence[Any]):
            A sequence of values to convert to JSON-serializable values.

    Returns:
        List[Any]: A list of JSON-serializable objects.
    """
    values = []
    for item in row_value:
        values.append(_single_field_to_json(field, item))
    return values


def _record_field_to_json(fields, row_value):
    """Convert a record/struct field to its JSON representation.

    Args:
        fields (Sequence[google.cloud.bigquery.schema.SchemaField]):
            The :class:`~google.cloud.bigquery.schema.SchemaField`s of the
            record's subfields to use for type conversion and field names.
        row_value (Union[Tuple[Any], Mapping[str, Any]):
            A tuple or dictionary to convert to JSON-serializable values.

    Returns:
        Mapping[str, Any]: A JSON-serializable dictionary.
    """
    isdict = isinstance(row_value, dict)

    # If row is passed as a tuple, make the length sanity check to avoid either
    # uninformative index errors a few lines below or silently omitting some of
    # the values from the result (we cannot know exactly which fields are missing
    # or redundant, since we don't have their names).
    if not isdict and len(row_value) != len(fields):
        msg = "The number of row fields ({}) does not match schema length ({}).".format(
            len(row_value), len(fields)
        )
        raise ValueError(msg)

    record = {}

    if isdict:
        processed_fields = set()

    for subindex, subfield in enumerate(fields):
        subname = subfield.name
        subvalue = row_value.get(subname) if isdict else row_value[subindex]

        # None values are unconditionally omitted
        if subvalue is not None:
            record[subname] = _field_to_json(subfield, subvalue)

        if isdict:
            processed_fields.add(subname)

    # Unknown fields should not be silently dropped, include them. Since there
    # is no schema information available for them, include them as strings
    # to make them JSON-serializable.
    if isdict:
        not_processed = set(row_value.keys()) - processed_fields

        for field_name in not_processed:
            value = row_value[field_name]
            if value is not None:
                record[field_name] = str(value)

    return record


def _single_field_to_json(field, row_value):
    """Convert a single field into JSON-serializable values.

    Ignores mode so that this can function for ARRAY / REPEATING fields
    without requiring a deepcopy of the field. See:
    https://github.com/googleapis/python-bigquery/issues/6

    Args:
        field (google.cloud.bigquery.schema.SchemaField):
            The SchemaField to use for type conversion and field name.

        row_value (Any):
            Scalar or Struct to be inserted. The type
            is inferred from the SchemaField's field_type.

    Returns:
        Any: A JSON-serializable object.
    """
    if row_value is None:
        return None

    if field.field_type == "RECORD":
        return _record_field_to_json(field.fields, row_value)

    return _scalar_field_to_json(field, row_value)


def _field_to_json(field, row_value):
    """Convert a field into JSON-serializable values.

    Args:
        field (google.cloud.bigquery.schema.SchemaField):
            The SchemaField to use for type conversion and field name.

        row_value (Union[Sequence[List], Any]):
            Row data to be inserted. If the SchemaField's mode is
            REPEATED, assume this is a list. If not, the type
            is inferred from the SchemaField's field_type.

    Returns:
        Any: A JSON-serializable object.
    """
    if row_value is None:
        return None

    if field.mode == "REPEATED":
        return _repeated_field_to_json(field, row_value)

    return _single_field_to_json(field, row_value)


def _snake_to_camel_case(value):
    """Convert snake case string to camel case."""
    words = value.split("_")
    return words[0] + "".join(map(str.capitalize, words[1:]))


def _get_sub_prop(container, keys, default=None):
    """Get a nested value from a dictionary.

    This method works like ``dict.get(key)``, but for nested values.

    Args:
        container (Dict):
            A dictionary which may contain other dictionaries as values.
        keys (Iterable):
            A sequence of keys to attempt to get the value for. If ``keys`` is a
            string, it is treated as sequence containing a single string key. Each item
            in the sequence represents a deeper nesting. The first key is for
            the top level. If there is a dictionary there, the second key
            attempts to get the value within that, and so on.
        default (Optional[object]):
            Value to returned if any of the keys are not found.
            Defaults to ``None``.

    Examples:
        Get a top-level value (equivalent to ``container.get('key')``).

        >>> _get_sub_prop({'key': 'value'}, ['key'])
        'value'

        Get a top-level value, providing a default (equivalent to
        ``container.get('key', default='default')``).

        >>> _get_sub_prop({'nothere': 123}, ['key'], default='not found')
        'not found'

        Get a nested value.

        >>> _get_sub_prop({'key': {'subkey': 'value'}}, ['key', 'subkey'])
        'value'

    Returns:
        object: The value if present or the default.
    """
    if isinstance(keys, str):
        keys = [keys]

    sub_val = container
    for key in keys:
        if key not in sub_val:
            return default
        sub_val = sub_val[key]
    return sub_val


def _set_sub_prop(container, keys, value):
    """Set a nested value in a dictionary.

    Args:
        container (Dict):
            A dictionary which may contain other dictionaries as values.
        keys (Iterable):
            A sequence of keys to attempt to set the value for. If ``keys`` is a
            string, it is treated as sequence containing a single string key. Each item
            in the sequence represents a deeper nesting. The first key is for
            the top level. If there is a dictionary there, the second key
            attempts to get the value within that, and so on.
        value (object): Value to set within the container.

    Examples:
        Set a top-level value (equivalent to ``container['key'] = 'value'``).

        >>> container = {}
        >>> _set_sub_prop(container, ['key'], 'value')
        >>> container
        {'key': 'value'}

        Set a nested value.

        >>> container = {}
        >>> _set_sub_prop(container, ['key', 'subkey'], 'value')
        >>> container
        {'key': {'subkey': 'value'}}

        Replace a nested value.

        >>> container = {'key': {'subkey': 'prev'}}
        >>> _set_sub_prop(container, ['key', 'subkey'], 'new')
        >>> container
        {'key': {'subkey': 'new'}}
    """
    if isinstance(keys, str):
        keys = [keys]

    sub_val = container
    for key in keys[:-1]:
        if key not in sub_val:
            sub_val[key] = {}
        sub_val = sub_val[key]
    sub_val[keys[-1]] = value


def _del_sub_prop(container, keys):
    """Remove a nested key fro a dictionary.

    Args:
        container (Dict):
            A dictionary which may contain other dictionaries as values.
        keys (Iterable):
            A sequence of keys to attempt to clear the value for. Each item in
            the sequence represents a deeper nesting. The first key is for
            the top level. If there is a dictionary there, the second key
            attempts to get the value within that, and so on.

    Examples:
        Remove a top-level value (equivalent to ``del container['key']``).

        >>> container = {'key': 'value'}
        >>> _del_sub_prop(container, ['key'])
        >>> container
        {}

        Remove a nested value.

        >>> container = {'key': {'subkey': 'value'}}
        >>> _del_sub_prop(container, ['key', 'subkey'])
        >>> container
        {'key': {}}
    """
    sub_val = container
    for key in keys[:-1]:
        if key not in sub_val:
            sub_val[key] = {}
        sub_val = sub_val[key]
    if keys[-1] in sub_val:
        del sub_val[keys[-1]]


def _int_or_none(value):
    """Helper: deserialize int value from JSON string."""
    if isinstance(value, int):
        return value
    if value is not None:
        return int(value)


def _str_or_none(value):
    """Helper: serialize value to JSON string."""
    if value is not None:
        return str(value)


def _split_id(full_id):
    """Helper: split full_id into composite parts.

    Args:
        full_id (str): Fully-qualified ID in standard SQL format.

    Returns:
        List[str]: ID's parts separated into components.
    """
    with_prefix = _PROJECT_PREFIX_PATTERN.match(full_id)
    if with_prefix is None:
        parts = full_id.split(".")
    else:
        parts = with_prefix.groups()
        parts = [part for part in parts if part]
    return parts


def _parse_3_part_id(full_id, default_project=None, property_name="table_id"):
    output_project_id = default_project
    output_dataset_id = None
    output_resource_id = None
    parts = _split_id(full_id)

    if len(parts) != 2 and len(parts) != 3:
        raise ValueError(
            "{property_name} must be a fully-qualified ID in "
            'standard SQL format, e.g., "project.dataset.{property_name}", '
            "got {}".format(full_id, property_name=property_name)
        )

    if len(parts) == 2 and not default_project:
        raise ValueError(
            "When default_project is not set, {property_name} must be a "
            "fully-qualified ID in standard SQL format, "
            'e.g., "project.dataset_id.{property_name}", got {}'.format(
                full_id, property_name=property_name
            )
        )

    if len(parts) == 2:
        output_dataset_id, output_resource_id = parts
    else:
        output_project_id, output_dataset_id, output_resource_id = parts

    return output_project_id, output_dataset_id, output_resource_id


def _build_resource_from_properties(obj, filter_fields):
    """Build a resource based on a ``_properties`` dictionary, filtered by
    ``filter_fields``, which follow the name of the Python object.
    """
    partial = {}
    for filter_field in filter_fields:
        api_field = obj._PROPERTY_TO_API_FIELD.get(filter_field)
        if api_field is None and filter_field not in obj._properties:
            raise ValueError("No property %s" % filter_field)
        elif api_field is not None:
            partial[api_field] = obj._properties.get(api_field)
        else:
            # allows properties that are not defined in the library
            # and properties that have the same name as API resource key
            partial[filter_field] = obj._properties[filter_field]

    return partial


def _verify_job_config_type(job_config, expected_type, param_name="job_config"):
    if not isinstance(job_config, expected_type):
        msg = (
            "Expected an instance of {expected_type} class for the {param_name} parameter, "
            "but received {param_name} = {job_config}"
        )
        raise TypeError(
            msg.format(
                expected_type=expected_type.__name__,
                param_name=param_name,
                job_config=job_config,
            )
        )
