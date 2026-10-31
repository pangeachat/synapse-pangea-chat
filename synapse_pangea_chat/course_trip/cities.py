"""The listed cities, by key.

The list is CMS content (the ``cities`` collection: every city of a million or
more plus the destinations added by hand). The CMS names each activity's city
by the one rule and stores its key on the activity (``city_key``; cms
cities.instructions.md), so this module matches no point: it turns a stored
key into the city a course record names.
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

PAGE = 1000
MAX_PAGES = 5
MAX_BODY_BYTES = 16_000_000


class City(NamedTuple):
    key: str
    name: str
    country: str
    lat: float
    lng: float

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
    return City(key, name, country, lat, lng)


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
    """The listed cities, each key once."""

    def __init__(self, cities: Iterable[City]) -> None:
        self.cities: List[City] = list(cities)
        self._by_key: Dict[str, City] = {}
        for listed in self.cities:
            self._by_key.setdefault(listed.key, listed)

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

    def city(self, key: Optional[str]) -> Optional[City]:
        """The listed city with ``key``, or None (no key, or a key the list
        no longer holds)."""
        return self._by_key.get(key) if key else None


class CityListError(RuntimeError):
    """The CMS could not supply the city list."""


class CityListReader:
    """The ``cities`` collection, read whole with the module's service key and
    kept for ``cache_ttl_seconds``. A read that fails, stops short or finds no
    city raises :class:`CityListError`; a caller that needs the list refuses
    rather than name nothing."""

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
        else:
            # Part of the list would name some points by the wrong city.
            raise CityListError(f"cities read did not finish within {MAX_PAGES} pages")
        index = CityIndex.from_rows(rows)
        if len(index) == 0:
            # An empty list would write trips with no cities that the one-time
            # pass then never revisits: the same as an unreadable list.
            raise CityListError("the city list is empty")
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
