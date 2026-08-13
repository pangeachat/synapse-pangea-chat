"""Shared builder for the course join link handed out in emails."""

from __future__ import annotations


def build_join_url(app_base_url: str, access_code: str) -> str:
    """Join link for a course space, from its access code.

    The link is the bare short code ``<app>/<code>`` — that path *is* the app
    URL on web and native alike, with no redirect hop. The older
    ``/#/join_with_link?classcode=`` spelling is retired: the client has no such
    route, and web runs a path URL strategy, so a fragment is not part of the
    route at all and such a link lands on the world map.

    Built on the environment's app host from ``app_base_url`` (ansible sets it
    per env: ``app.pangea.chat`` in prod, ``app.staging.pangea.chat`` on
    staging), never a hardcoded host — a staging course must not hand out a
    prod link.
    """
    return f"{app_base_url.rstrip('/')}/{access_code}"
