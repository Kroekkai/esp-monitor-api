import os
from typing import Optional

from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "myorg")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "sensors")

_client = InfluxDBClient(url=INFLUX_URL, token=INFLUX_TOKEN, org=INFLUX_ORG)
_write_api = _client.write_api(write_options=SYNCHRONOUS)
_query_api = _client.query_api()


def write_sensor_point(device_id: str, temperature: float, humidity: float) -> None:
    """Called directly inside the POST /api/sensor handler - no separate service call."""
    point = (
        Point("sensor_data")
        .tag("device_id", device_id)
        .field("temperature", float(temperature))
        .field("humidity", float(humidity))
    )
    _write_api.write(bucket=INFLUX_BUCKET, org=INFLUX_ORG, record=point)


def query_recent(
    hours: int = 24,
    device_id: Optional[str] = None,
    device_ids: Optional[list] = None,
    start: Optional[str] = None,
    stop: Optional[str] = None,
):
    """start/stop, when given, are RFC3339 UTC timestamps (e.g.
    "2026-09-15T00:00:00Z") already validated by the caller - used for the
    calendar "pick a day" view. Otherwise falls back to the last `hours`."""
    if device_id:
        device_filter = f'|> filter(fn: (r) => r.device_id == "{device_id}")'
    elif device_ids:
        if len(device_ids) == 0:
            return []
        conditions = " or ".join(f'r.device_id == "{d}"' for d in device_ids)
        device_filter = f'|> filter(fn: (r) => {conditions})'
    else:
        device_filter = ""

    range_clause = f"range(start: {start}, stop: {stop})" if start and stop else f"range(start: -{hours}h)"

    flux = f'''
    from(bucket: "{INFLUX_BUCKET}")
      |> {range_clause}
      |> filter(fn: (r) => r._measurement == "sensor_data")
      {device_filter}
      |> pivot(rowKey:["_time"], columnKey: ["_field"], valueColumn: "_value")
      |> sort(columns: ["_time"], desc: true)
    '''
    tables = _query_api.query(flux)
    results = []
    for table in tables:
        for record in table.records:
            results.append(
                {
                    "time": record.get_time().isoformat(),
                    "device_id": record.values.get("device_id"),
                    "temperature": record.values.get("temperature"),
                    "humidity": record.values.get("humidity"),
                }
            )
    return results
