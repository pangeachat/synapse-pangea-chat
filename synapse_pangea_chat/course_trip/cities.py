"""The listed cities and the one rule that names a point by them.

The list is CMS content (the ``cities`` collection: every city of a million or
more plus the destinations added by hand). A point belongs to the nearest
listed city within 250 miles and to no city beyond that: the same rule as
admin-dash-api's ``app/plan/cities.py`` and the app's ``city_index.dart``,
checked against the same example set (``tests/fixtures/nearest_city.json``).
"""

from __future__ import annotations

import json
import logging
import math
from io import BytesIO
from typing import Any, Dict, Iterable, List, Mapping, NamedTuple, Optional
from urllib.parse import urlencode

from synapse.http.client import read_body_with_max_size
from synapse.logging.context import make_deferred_yieldable
from synapse.module_api import ModuleApi
from twisted.web.http_headers import Headers

logger = logging.getLogger("synapse.module.synapse_pangea_chat.course_trip.cities")

#: A point further than this from every listed city belongs to no city.
CAP_MILES = 250.0
EARTH_RADIUS_MILES = 3958.7613
# The cap as the cosine of its central angle: two unit vectors' dot product is
# the cosine of the angle between them, so a city is within the cap exactly
# when its dot product with the point is at least this.
_CAP_COS = math.cos(CAP_MILES / EARTH_RADIUS_MILES)
PAGE = 1000
MAX_PAGES = 5
MAX_BODY_BYTES = 16_000_000


def _unit(lat: float, lng: float) -> "tuple[float, float, float]":
    phi, lam = math.radians(lat), math.radians(lng)
    return (math.cos(phi) * math.cos(lam), math.cos(phi) * math.sin(lam), math.sin(phi))


class City(NamedTuple):
    key: str
    name: str
    country: str
    lat: float
    lng: float
    x: float
    y: float
    z: float

    def as_mission_city(self) -> Dict[str, Any]:
        """The record's ``city``: name and position (pangeachat/.github#780).
        Matrix event content is canonical JSON, which has no floats, so the
        position is integer microdegrees (``lat_e6``, ``lng_e6``)."""
        return {
            "name": self.name,
            "lat_e6": round(self.lat * 1_000_000),
            "lng_e6": round(self.lng * 1_000_000),
        }


def city(key: str, name: str, country: str, lat: float, lng: float) -> City:
    return City(key, name, country, lat, lng, *_unit(lat, lng))


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def parse_city(row: Any) -> Optional[City]:
    """A city from one ``cities`` row; None for a row that cannot be placed or
    named, which is dropped rather than drawn at 0,0."""
    if not isinstance(row, Mapping):
        return None
    key = row.get("key") or row.get("id")
    name = row.get("name")
    country = row.get("country")
    lat = _number(row.get("lat"))
    lng = _number(row.get("lng"))
    if (
        not isinstance(key, str)
        or not key
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(country, str)
        or lat is None
        or lng is None
        or not -90 <= lat <= 90
        or not -180 <= lng <= 180
    ):
        return None
    return city(key, name.strip(), country.strip(), lat, lng)


class CityIndex:
    """The listed cities and the rule that names a point by them."""

    def __init__(self, cities: Iterable[City]) -> None:
        self.cities: List[City] = list(cities)
        self._answers: Dict["tuple[float, float]", Optional[City]] = {}

    @classmethod
    def from_rows(cls, rows: Iterable[Any]) -> "CityIndex":
        """An index over the parseable rows, each key once (the first wins)."""
        seen: set = set()
        cities: List[City] = []
        for row in rows:
            parsed = parse_city(row)
            if parsed is not None and parsed.key not in seen:
                seen.add(parsed.key)
                cities.append(parsed)
        return cls(cities)

    def __len__(self) -> int:
        return len(self.cities)

    def nearest(self, lat: float, lng: float) -> Optional[City]:
        """The listed city nearest the point when it is within the cap (on the
        sphere), else None. Points are rounded to about 100 m so activities
        sharing a place share one lookup."""
        point = (round(lat, 3), round(lng, 3))
        if point in self._answers:
            return self._answers[point]
        x, y, z = _unit(*point)
        best: Optional[City] = None
        best_dot = _CAP_COS
        for candidate in self.cities:
            dot = candidate.x * x + candidate.y * y + candidate.z * z
            if dot > best_dot:
                best, best_dot = candidate, dot
        if len(self._answers) < 50_000:
            self._answers[point] = best
        return best


def miles_between(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance, for the example set's cap edges."""
    x1, y1, z1 = _unit(lat1, lng1)
    x2, y2, z2 = _unit(lat2, lng2)
    dot = max(-1.0, min(1.0, x1 * x2 + y1 * y2 + z1 * z2))
    return math.acos(dot) * EARTH_RADIUS_MILES


class CityListError(RuntimeError):
    """The CMS could not supply the city list."""


class CityListReader:
    """The ``cities`` collection, read whole with the module's service key and
    kept for ``cache_ttl_seconds``. A read that fails raises
    :class:`CityListError`; a caller that needs the list refuses rather than
    name nothing."""

    def __init__(
        self,
        api: ModuleApi,
        cms_base_url: str,
        cms_api_key: str,
        cache_ttl_seconds: int = 3600,
    ) -> None:
        self._http = api.http_client
        self._clock = api._hs.get_clock()
        self._base = cms_base_url.rstrip("/") + "/api/"
        self._headers = Headers(
            {"Authorization": [f"service-users API-Key {cms_api_key}"]}
        )
        self._ttl_ms = cache_ttl_seconds * 1000
        self._cached: Optional[CityIndex] = None
        self._cached_at_ms = 0

    async def _get(self, path: str) -> Any:
        response = await self._http.request(
            "GET", self._base + path, headers=self._headers
        )
        buffer = BytesIO()
        await make_deferred_yieldable(
            read_body_with_max_size(response, buffer, MAX_BODY_BYTES)
        )
        if response.code != 200:
            raise CityListError(f"CMS {path.split('?')[0]} answered {response.code}")
        return json.loads(buffer.getvalue())

    async def index(self) -> CityIndex:
        now = self._clock.time_msec()
        if self._cached is not None and now - self._cached_at_ms < self._ttl_ms:
            return self._cached
        rows: List[Any] = []
        for page in range(1, MAX_PAGES + 1):
            query = urlencode(
                [
                    ("limit", str(PAGE)),
                    ("page", str(page)),
                    ("sort", "key"),
                    ("depth", "0"),
                ]
            )
            found = await self._get("cities?" + query)
            docs = found.get("docs") if isinstance(found, Mapping) else None
            if not isinstance(docs, list):
                raise CityListError("cities read returned no docs list")
            rows.extend(docs)
            if found.get("hasNextPage") is not True:
                break
        index = CityIndex.from_rows(rows)
        if len(index) == 0:
            logger.warning("The city list is empty: no Can-Do can be given a city")
        self._cached, self._cached_at_ms = index, now
        return index


_shared: Optional[CityListReader] = None


def shared_city_list(api: ModuleApi, config: Any) -> CityListReader:
    """One reader per process, so every writer and the pass share one cache."""
    global _shared
    if _shared is None:
        _shared = CityListReader(
            api,
            config.cms_base_url,
            config.cms_service_api_key,
            getattr(config, "course_trip_cities_cache_ttl_seconds", 3600),
        )
    return _shared
