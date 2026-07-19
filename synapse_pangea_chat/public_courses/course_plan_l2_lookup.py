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

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.public_courses.course_plan_l2_lookup"
)


class CoursePlanLookupError(RuntimeError):
    """The CMS could not be asked. Distinct from 'the CMS has no such plan'."""


async def _cms_get_course_plans(
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
    url = f"{cms_base_url}/api/course-plans?{query}"

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
            f"CMS course-plans lookup failed ({response.code}): "
            f"{body.decode('utf-8', errors='replace')}"
        )

    data = json.loads(body)
    docs = data.get("docs", [])
    if not isinstance(docs, list):
        raise CoursePlanLookupError(
            f"CMS course-plans lookup returned no docs list: {docs!r}"
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

    docs = await _cms_get_course_plans(unique_ids, cms_base_url, cms_api_key)

    languages: Dict[str, str] = {}
    for doc in docs:
        if not isinstance(doc, dict):
            continue
        plan_id = doc.get("id")
        l2 = doc.get("l2")
        if not plan_id or not isinstance(l2, str) or not l2.strip():
            continue
        languages[str(plan_id)] = l2.strip()

    return languages
