"""Notice delivery asks the CMS whether a contact refused the kind of notice
(``notice_delivery/contact_refusal.py``). Tests about other behaviour use this
stand-in CMS that refuses nothing; a test about refusals patches its own."""

from unittest import mock

NOTHING_REFUSED = (200, b'{"refused": false, "suppressed": false}')
_patcher = mock.patch(
    "synapse_pangea_chat.notice_delivery.contact_refusal.cms_request",
    new=mock.AsyncMock(return_value=NOTHING_REFUSED),
)


def start() -> None:
    _patcher.start()


def stop() -> None:
    _patcher.stop()
