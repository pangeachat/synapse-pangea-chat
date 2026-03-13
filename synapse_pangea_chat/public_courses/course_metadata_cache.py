"""Cache for CMS course plan metadata (language, CEFR level).

Fetches course plan data from Payload CMS and caches per-UUID with a
configurable TTL.  Two entry points:

* ``get_course_metadata`` – unfiltered lookup for a set of UUIDs
* ``get_filtered_course_ids`` – passes client-side filters (l2, l1,
  cefr_level) directly to CMS ``where`` clauses so only matching
  course plans are returned
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple, TypedDict
from urllib.parse import urlencode

logger = logging.getLogger("synapse_pangea_chat.course_metadata_cache")


class FilteredCourseMetadataLookupError(RuntimeError):
    """Raised when filtered CMS metadata lookup fails."""


class CourseMeta(TypedDict):
    l2: str
    l1: str
    cefr_level: str


# Per-UUID in-memory cache: {uuid: (CourseMeta, timestamp)}
_meta_cache: Dict[str, Tuple[CourseMeta, float]] = {}
_DEFAULT_TTL_SECONDS = 5  # short burst cache; refresh frequently


def _is_fresh(timestamp: float, ttl: int) -> bool:
    return time.time() - timestamp < ttl


def _store(uuid: str, meta: CourseMeta) -> None:
    _meta_cache[uuid] = (meta, time.time())


def _parse_docs(docs: List[Dict[str, Any]]) -> Dict[str, CourseMeta]:
    result: Dict[str, CourseMeta] = {}
    for doc in docs:
        uuid = doc.get("id")
        if not uuid:
            continue
        meta = CourseMeta(
            l2=doc.get("l2", ""),
            l1=doc.get("originalL1", ""),
            cefr_level=doc.get("cefrLevel", ""),
        )
        result[str(uuid)] = meta
        _store(str(uuid), meta)
    return result


async def _cms_get(
    cms_base_url: str,
    cms_api_key: str,
    query_params: Dict[str, str],
) -> List[Dict[str, Any]]:
    """Execute a GET against ``/api/course-plans`` with *query_params*."""
    from twisted.internet import reactor
    from twisted.web.client import Agent, readBody
    from twisted.web.http_headers import Headers

    agent = Agent(reactor)

    qs = urlencode(query_params, doseq=False)
    url = f"{cms_base_url}/api/course-plans?{qs}"

    response = await agent.request(
        b"GET",
        url.encode("utf-8"),
        Headers(
            {
                b"Authorization": [f"users API-Key {cms_api_key}".encode("utf-8")],
            }
        ),
    )

    body = await readBody(response)
    if response.code >= 400:
        raise RuntimeError(
            f"CMS course-plans query failed ({response.code}): "
            f"{body.decode('utf-8', errors='replace')}"
        )

    data = json.loads(body)
    return data.get("docs", [])


def _build_where_params(
    course_uuids: List[str],
    target_language: Optional[str] = None,
    language_of_instructions: Optional[str] = None,
    cefr_level: Optional[str] = None,
) -> Dict[str, str]:
    """Build Payload CMS ``where`` query parameters."""
    params: Dict[str, str] = {
        "where[id][in]": ",".join(course_uuids),
        "limit": str(len(course_uuids)),
        "depth": "0",
    }
    if target_language:
        params["where[l2][equals]"] = target_language
    if language_of_instructions:
        params["where[originalL1][equals]"] = language_of_instructions
    if cefr_level:
        params["where[cefrLevel][equals]"] = cefr_level
    return params


async def get_course_metadata(
    course_uuids: List[str],
    cms_base_url: str,
    cms_api_key: str,
    cache_ttl: int = _DEFAULT_TTL_SECONDS,
) -> Dict[str, CourseMeta]:
    """Return metadata for *course_uuids*, using cache where possible.

    UUIDs with a fresh cache entry are served from memory; only stale /
    missing UUIDs are fetched from CMS in a single request.
    """
    if not course_uuids:
        return {}

    result: Dict[str, CourseMeta] = {}
    to_fetch: List[str] = []

    for uuid in course_uuids:
        cached = _meta_cache.get(uuid)
        if cached and _is_fresh(cached[1], cache_ttl):
            result[uuid] = cached[0]
        else:
            to_fetch.append(uuid)

    if not to_fetch:
        return result

    try:
        params = _build_where_params(to_fetch)
        docs = await _cms_get(cms_base_url, cms_api_key, params)
        fetched = _parse_docs(docs)
        result.update(fetched)
    except Exception:
        logger.warning(
            "Failed to fetch course metadata from CMS; returning stale/partial cache",
            exc_info=True,
        )
        # Fall back to stale cache entries if available
        for uuid in to_fetch:
            stale = _meta_cache.get(uuid)
            if stale:
                result[uuid] = stale[0]

    return result


async def get_filtered_course_ids(
    course_uuids: List[str],
    cms_base_url: str,
    cms_api_key: str,
    target_language: Optional[str] = None,
    language_of_instructions: Optional[str] = None,
    cefr_level: Optional[str] = None,
) -> Dict[str, CourseMeta]:
    """Return metadata only for courses matching the given filters.

    Delegates filtering to CMS via ``where`` clauses so only matching
    documents are returned.  Results are cached per-UUID for later
    unfiltered lookups.
    """
    if not course_uuids:
        return {}

    try:
        params = _build_where_params(
            course_uuids,
            target_language=target_language,
            language_of_instructions=language_of_instructions,
            cefr_level=cefr_level,
        )
        docs = await _cms_get(cms_base_url, cms_api_key, params)
        return _parse_docs(docs)
    except Exception as exc:
        logger.warning(
            "Failed to fetch filtered course metadata from CMS",
            exc_info=True,
        )
        raise FilteredCourseMetadataLookupError(
            "Filtered CMS course metadata lookup failed"
        ) from exc
