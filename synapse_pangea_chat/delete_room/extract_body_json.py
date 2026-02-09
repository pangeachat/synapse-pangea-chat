import json
import logging
from typing import Any

from synapse.http.site import SynapseRequest

logger = logging.getLogger(
    "synapse.module.synapse_pangea_chat.delete_room.extract_body_json"
)


async def extract_body_json(request: SynapseRequest) -> Any:
    content_type = request.getHeader("Content-Type")
    if content_type is None:
        return None
    if not content_type.lower().strip().startswith("application/json"):
        return None
    try:
        if request.content is None:
            return None
        body = request.content.read()
        body_str = body.decode("utf-8")
        body_json = json.loads(body_str)
        return body_json
    except Exception as e:
        logger.error("Failed to parse JSON from request body: %s", e)
        return None
