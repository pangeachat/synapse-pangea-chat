"""One-shot CMS lookup of course-plan target languages, by plan id.

Used only by the ``l2`` backfill. There is deliberately no cache and no TTL
here: the read path makes no CMS call at all, and re-introducing a cached CMS
lookup inside ``public_courses`` is what the catalog contract set out to
remove. The backfill asks the CMS once per batch, writes the answer into Matrix
state, and never asks again.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Sequence
from urllib.parse import urlencode

# The API key is issued against the CMS ``service-users`` collection; the
# collection name is part of the Authorization scheme Payload expects.
CMS_AUTH_COLLECTION = "service-users"

# Where a course's target language lives, newest content model first.
#
# Course rooms reference v3 ``quest-plans`` — a quest id returns ZERO docs from
# the v1 ``course-plans`` collection, so querying only v1 makes the backfill a
# silent no-op for every real course. Legacy rooms may still hold a v1 id, so
# both are tried and the ids that resolve in the first are not re-asked.
#
# The two collections spell the language differently: v3 keeps generation
# inputs under ``req``, v1 has a flat ``l2``.
_PLAN_SOURCES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("quest-plans", ("req", "target_language")),
    ("course-plans", ("l2",)),
)


def _dig(doc: Dict[str, Any], path: Sequence[str]) -> Any:
    value: Any = doc
    for key in path:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.public_courses.course_plan_l2_lookup"
)


class CoursePlanLookupError(RuntimeError):
    """The CMS could not be asked. Distinct from 'the CMS has no such plan'."""


async def _cms_get_plans(
    collection: str,
    plan_ids: Sequence[str],
    cms_base_url: str,
    cms_api_key: str,
) -> List[Dict[str, Any]]:
    from twisted.internet import reactor
    from twisted.web.client import Agent, readBody
    from twisted.web.http_headers import Headers

    agent = Agent(reactor)

    query = urlencode(
        {
            "where[id][in]": ",".join(plan_ids),
            "limit": str(len(plan_ids)),
            "depth": "0",
        }
    )
    url = f"{cms_base_url}/api/{collection}?{query}"

    response = await agent.request(
        b"GET",
        url.encode("utf-8"),
        Headers(
            {
                b"Authorization": [
                    f"{CMS_AUTH_COLLECTION} API-Key {cms_api_key}".encode("utf-8")
                ],
            }
        ),
        None,
    )

    body = await readBody(response)
    if response.code >= 400:
        raise CoursePlanLookupError(
            f"CMS {collection} lookup failed ({response.code}): "
            f"{body.decode('utf-8', errors='replace')}"
        )

    data = json.loads(body)
    docs = data.get("docs", [])
    if not isinstance(docs, list):
        raise CoursePlanLookupError(
            f"CMS {collection} lookup returned no docs list: {docs!r}"
        )
    return docs


async def fetch_plan_languages(
    plan_ids: Sequence[str],
    cms_base_url: str,
    cms_api_key: str,
) -> Dict[str, str]:
    """Map plan id -> non-empty ``l2``, for the plan ids the CMS resolves.

    A plan id absent from the returned mapping did not resolve — either the
    CMS does not know it, or it carries no language. The caller must skip
    those rooms rather than guess a language for them.

    Raises ``CoursePlanLookupError`` if the CMS could not be reached or
    answered with an error, so a transport failure is never mistaken for
    "none of these plans have a language".
    """
    unique_ids = list(dict.fromkeys(plan_id for plan_id in plan_ids if plan_id))
    if not unique_ids:
        return {}

    languages: Dict[str, str] = {}
    outstanding = list(unique_ids)

    for collection, language_path in _PLAN_SOURCES:
        if not outstanding:
            break
        docs = await _cms_get_plans(collection, outstanding, cms_base_url, cms_api_key)
        for doc in docs:
            if not isinstance(doc, dict):
                continue
            plan_id = doc.get("id")
            l2 = _dig(doc, language_path)
            if not plan_id or not isinstance(l2, str) or not l2.strip():
                continue
            languages[str(plan_id)] = l2.strip()
        outstanding = [plan_id for plan_id in outstanding if plan_id not in languages]

    return languages
