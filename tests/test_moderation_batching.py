"""Tier 2 batching: one provider call carrying many messages.

Each worker blocks about two seconds on the provider whether the call
carries one text or thirty-two, so batching is the only lever that turns
that latency into throughput. The whole of the risk is in the mapping: the
wire contract is POSITIONAL, and a result attributed to the wrong message
redacts an innocent learner and leaves a harmful message standing.

So the property these tests exist for is not "the mapping is usually right".
It is that **no redaction is ever taken on a batched verdict at all**. The
batch is a SCREEN; anything it flags is re-checked with a single-text call
carrying exactly that message, and it is that call's verdict which decides
the disposition. A response to a one-element request has no index to get
wrong, so misattribution is not merely unlikely - it is not expressible.

What remains is the screen's own mapping, and it is closed from both ends:
`check_batch` derives the request order and the response pairing from ONE
immutable tuple, and pairs them with `zip(..., strict=True)`, which raises
rather than truncating; a response whose length differs from the request's
is refused before any pairing happens.
"""

import json
import unittest
from typing import Any, Dict, List, Optional, Sequence, Tuple

from twisted.internet import defer
from twisted.python.failure import Failure
from twisted.web.iweb import IBodyProducer

from synapse_pangea_chat.moderation.breaker import CircuitBreaker
from synapse_pangea_chat.moderation.choreo_client import (
    KIND_BATCH_UNSUPPORTED,
    KIND_SHAPE,
    ChoreoChecker,
    ModerationCheckError,
    moderate_texts,
)
from synapse_pangea_chat.moderation.metrics import TIER2_BATCH_UNSUPPORTED
from tests.moderation_doubles import MetricReader


class _Timer:
    def __init__(self) -> None:
        self._active = True

    def active(self) -> bool:
        return self._active

    def cancel(self) -> None:
        self._active = False


class FakeClock:
    """Enough clock for the deadline machinery, with a timer that never fires.

    These tests are about the batch CONTRACT, not about the deadlines - those
    have their own suite. The timer is real enough to be scheduled and
    cancelled, which is what the exchange does with it on every call; a clock
    without `call_later` at all made every failure come back as `transport`,
    because the missing attribute was caught by the exchange's own handler.
    """

    def __init__(self) -> None:
        self._now = 1000.0
        self.timers: List[_Timer] = []

    def time(self) -> float:
        return self._now

    def call_later(self, _delay: Any, _callback: Any, *args: Any) -> _Timer:
        timer = _Timer()
        self.timers.append(timer)
        return timer

    def advance(self, seconds: float) -> None:
        self._now += seconds


class _Transport:
    def __init__(self) -> None:
        self.aborted = False

    def abortConnection(self) -> None:
        self.aborted = True

    def stopProducing(self) -> None:
        self.aborted = True

    def loseConnection(self) -> None:
        self.aborted = True


class _Response:
    version = (b"HTTP", 1, 1)
    phrase = b"OK"

    def __init__(self, code: int = 200, body: bytes = b"{}") -> None:
        self.code = code
        self._body = body
        self.transport = _Transport()

    def deliverBody(self, protocol: Any) -> None:
        protocol.makeConnection(self.transport)
        protocol.dataReceived(self._body)
        from twisted.web.client import ResponseDone

        protocol.connectionLost(Failure(ResponseDone()))


class _Agent:
    """Records the request body, so a test can assert what went on the wire.

    A double that accepts anything and answers a canned body tests the
    response handling and nothing else - the request could carry `text`
    where it meant `texts` and every test would still pass.
    """

    def __init__(self, responses: Sequence[Any]) -> None:
        self._responses = list(responses)
        self.bodies: List[Dict[str, Any]] = []
        self.calls = 0

    def request(
        self, method: bytes, uri: bytes, headers: Any = None, bodyProducer: Any = None
    ) -> Any:
        self.calls += 1
        self.bodies.append(json.loads(_consume(bodyProducer)))
        outcome = self._responses.pop(0) if self._responses else _Response(200, b"{}")
        if isinstance(outcome, Exception):
            return defer.fail(outcome)
        return defer.succeed(outcome)


def _consume(producer: Any) -> bytes:
    """The bytes the agent would put on the wire, and the interface check.

    `IBodyProducer.providedBy` rather than a duck-type test: that is what
    `Agent.request` actually requires, so a raw `BytesIO` passed by mistake
    fails here as it would fail there.

    Read from the producer's file rather than by driving `startProducing`,
    which cooperates through the global reactor - it would leave a pending
    delayed call behind in a test with no reactor running, and it returns
    nothing synchronously, which read as an empty request body and made every
    failure come back classified as `transport`.
    """
    if producer is None:
        raise TypeError("Agent.request was given no body producer")
    if not IBodyProducer.providedBy(producer):
        raise TypeError(
            f"Agent.request wants an IBodyProducer, got {type(producer).__name__}"
        )
    source = producer._inputFile
    position = source.tell()
    try:
        source.seek(0)
        return bytes(source.read())
    finally:
        source.seek(position)


def _body(results: Sequence[Any]) -> bytes:
    """A batch response body. `Any`, not `Dict`, on purpose: several tests put
    something that is NOT a verdict object in the list, which is precisely the
    shape the validator has to refuse."""
    return json.dumps({"results": list(results)}).encode("utf-8")


def _verdict(flagged: bool = False, categories: Optional[List[str]] = None) -> Dict:
    return {
        "flagged": flagged,
        "categories": categories if categories is not None else [],
        "evaluated": True,
    }


def _run(coro: Any) -> Any:
    out: List[Any] = []
    defer.ensureDeferred(coro).addBoth(out.append)
    assert len(out) == 1, "the call did not complete synchronously"
    if isinstance(out[0], Failure):
        out[0].raiseException()
    return out[0]


class BatchTransportTestCase(unittest.TestCase):
    """`moderate_texts`: what goes on the wire and what is refused coming back."""

    def setUp(self) -> None:
        self.clock = FakeClock()

    def _call(self, texts: Sequence[str], agent: _Agent) -> Any:
        return _run(
            moderate_texts(
                list(texts),
                base_url="http://choreo.invalid",
                access_token="syt_x",
                agent=agent,
                clock=self.clock,
                timeout_seconds=15.0,
            )
        )

    def test_the_request_carries_texts_in_order(self) -> None:
        agent = _Agent([_Response(200, _body([_verdict(), _verdict()]))])
        self._call(["first", "second"], agent)
        self.assertEqual(
            agent.bodies[0],
            {"texts": ["first", "second"], "mock": False},
            "the batch request is not the contract the choreo handler " "implements",
        )

    def test_results_are_returned_in_request_order(self) -> None:
        agent = _Agent(
            [
                _Response(
                    200,
                    _body(
                        [
                            _verdict(False),
                            _verdict(True, ["harassment"]),
                            _verdict(False),
                        ]
                    ),
                )
            ]
        )
        results = self._call(["a", "b", "c"], agent)
        self.assertEqual([r["flagged"] for r in results], [False, True, False])

    def test_a_short_result_list_is_refused_rather_than_zipped(self) -> None:
        """The defect this whole file exists for.

        Two texts, one result. Truncating the pairing would attribute the one
        verdict to the first message and silently leave the second unchecked;
        worse, a list shifted by one attributes a `flagged` to the wrong
        learner. A length that does not match the request is not a partial
        answer, it is no answer.
        """
        agent = _Agent([_Response(200, _body([_verdict(True, ["harassment"])]))])
        with self.assertRaises(ModerationCheckError) as caught:
            self._call(["a", "b"], agent)
        self.assertEqual(caught.exception.kind, KIND_SHAPE)

    def test_a_long_result_list_is_refused_too(self) -> None:
        agent = _Agent([_Response(200, _body([_verdict(), _verdict(), _verdict()]))])
        with self.assertRaises(ModerationCheckError) as caught:
            self._call(["a", "b"], agent)
        self.assertEqual(caught.exception.kind, KIND_SHAPE)

    def test_each_result_is_shape_checked_like_a_single_verdict(self) -> None:
        """One malformed item invalidates the batch, not just that item.

        A batch is one answer to one question. An element we cannot read
        means the endpoint is not doing what the contract says, and the
        ELEMENT we cannot read may be the one whose position is wrong - so
        reading the others positionally is exactly the move that misattributes
        a verdict.
        """
        for bad in (
            {"categories": []},
            {"flagged": "yes"},
            {"flagged": True, "categories": None},
            {"flagged": True, "categories": [1]},
            {"flagged": False, "evaluated": 0},
            "not-an-object",
        ):
            with self.subTest(bad=bad):
                agent = _Agent([_Response(200, _body([_verdict(), bad]))])
                with self.assertRaises(ModerationCheckError) as caught:
                    self._call(["a", "b"], agent)
                self.assertEqual(caught.exception.kind, KIND_SHAPE)

    def test_a_response_without_a_results_list_reads_as_batch_unsupported(
        self,
    ) -> None:
        """An endpoint that answers something else did not understand `texts`.

        Kept distinct from an ordinary shape failure because the two want
        opposite things: a shape failure is a provider misbehaving and the
        messages are left alone, while this one means "ask again one text at
        a time", and never asking again would stop moderating.
        """
        for body in (b"{}", b'{"results": null}', b'{"flagged": false}', b"[]"):
            with self.subTest(body=body):
                agent = _Agent([_Response(200, body)])
                with self.assertRaises(ModerationCheckError) as caught:
                    self._call(["a", "b"], agent)
                self.assertEqual(caught.exception.kind, KIND_BATCH_UNSUPPORTED)

    def test_a_422_reads_as_batch_unsupported(self) -> None:
        """What an un-upgraded choreo actually answers.

        `ModerationRequest.text` is a required Pydantic field, so FastAPI
        rejects a body carrying `texts` and no `text` with 422 before the
        handler runs. 400 is included for a gateway that rewrites it.
        """
        for code in (400, 422):
            with self.subTest(code=code):
                agent = _Agent([_Response(code, b'{"detail": "field required"}')])
                with self.assertRaises(ModerationCheckError) as caught:
                    self._call(["a", "b"], agent)
                self.assertEqual(caught.exception.kind, KIND_BATCH_UNSUPPORTED)

    def test_other_statuses_keep_their_ordinary_meaning(self) -> None:
        """A 401 is a bad token and a 500 is a provider outage. Reading either
        as "batching is unsupported" would demote a healthy deployment to
        single-text calls for the life of the process on one transient
        error."""
        for code in (401, 403, 404, 429, 500, 503):
            with self.subTest(code=code):
                agent = _Agent([_Response(code, b"{}")])
                with self.assertRaises(ModerationCheckError) as caught:
                    self._call(["a", "b"], agent)
                self.assertNotEqual(caught.exception.kind, KIND_BATCH_UNSUPPORTED)


class BatchCheckerTestCase(unittest.TestCase):
    """`ChoreoChecker.check_batch`: the screen, the fallback and the counters."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = CircuitBreaker(
            clock=self.clock,
            failure_threshold=3,
            cooldown_seconds=30.0,
            max_cooldown_seconds=120.0,
        )
        self.reader = MetricReader()
        # Process-global, like every prometheus series here. Reset so a test
        # asserting the demotion is not reading a demotion another test made.
        TIER2_BATCH_UNSUPPORTED.set(0)
        self.addCleanup(TIER2_BATCH_UNSUPPORTED.set, 0)

    def _checker(self, agent: _Agent) -> ChoreoChecker:
        return ChoreoChecker(
            agent=agent,
            clock=self.clock,
            base_url="http://choreo.invalid",
            access_token="syt_x",
            breaker=self.breaker,
            timeout_seconds=15.0,
        )

    def test_the_returned_list_always_matches_the_request_length(self) -> None:
        """The structural guarantee the caller relies on.

        `check_batch` never returns a short list, on any path - a clean batch,
        a refused batch, an open breaker, a fallback to single-text calls.
        The caller pairs positionally, so a short return is the one thing that
        could silently shift every verdict by one.
        """
        cases: List[Tuple[str, List[Any]]] = [
            ("clean", [_Response(200, _body([_verdict()] * 3))]),
            ("shape", [_Response(200, _body([_verdict()]))]),
            ("server error", [_Response(500, b"{}")]),
            ("unsupported then singles", [_Response(422, b"{}")]),
        ]
        for name, responses in cases:
            with self.subTest(case=name):
                agent = _Agent(responses + [_Response(200, b"{}")] * 8)
                verdicts = _run(self._checker(agent).check_batch(["a", "b", "c"]))
                self.assertEqual(
                    len(verdicts.results), 3, f"{name} returned a short list"
                )

    def test_an_unsupported_endpoint_falls_back_to_single_text_calls(self) -> None:
        """Staging runs an un-upgraded choreo. It must keep moderating.

        Never wedge and never fail closed: the batch is refused, every message
        in it is immediately re-asked one at a time, and the verdicts come
        back as if batching had never been attempted.
        """
        agent = _Agent(
            [
                _Response(422, b'{"detail": "field required"}'),
                _Response(200, json.dumps(_verdict(True, ["harassment"])).encode()),
                _Response(200, json.dumps(_verdict(False)).encode()),
            ]
        )
        checker = self._checker(agent)
        verdicts = _run(checker.check_batch(["bad", "fine"]))
        self.assertEqual([r and r["flagged"] for r in verdicts.results], [True, False])
        self.assertTrue(
            verdicts.confirmed,
            "the fallback's answers were reported as a screen, so every "
            "flagged message would be asked about twice",
        )
        self.assertEqual(agent.bodies[0], {"texts": ["bad", "fine"], "mock": False})
        # The single-text body is unchanged by this work: `mock` defaults to
        # false on the endpoint's request model, and moving a request that is
        # already in production to match the batched shape would be risk with
        # no return.
        self.assertEqual(agent.bodies[1], {"text": "bad"})
        self.assertEqual(agent.bodies[2], {"text": "fine"})

    def test_the_demotion_is_remembered_so_it_is_paid_once(self) -> None:
        """One rejected batch per process, not one per batch.

        Re-probing on every batch would spend an extra round trip on every
        group of messages for the whole life of a staging deployment.
        """
        agent = _Agent(
            [_Response(422, b"{}")] + [_Response(200, b'{"flagged": false}')] * 10
        )
        checker = self._checker(agent)
        _run(checker.check_batch(["a", "b"]))
        batch_attempts = sum(1 for body in agent.bodies if "texts" in body)
        _run(checker.check_batch(["c", "d"]))
        self.assertEqual(
            sum(1 for body in agent.bodies if "texts" in body),
            batch_attempts,
            "a second batch was attempted against an endpoint already known "
            "not to understand one",
        )

    def test_the_demotion_is_visible_on_a_gauge(self) -> None:
        agent = _Agent([_Response(422, b"{}")] + [_Response(200, b"{}")] * 4)
        checker = self._checker(agent)
        self.assertEqual(
            self.reader.value("pangea_moderation_tier2_batch_unsupported"), 0.0
        )
        _run(checker.check_batch(["a", "b"]))
        self.assertEqual(
            self.reader.value("pangea_moderation_tier2_batch_unsupported"),
            1.0,
            "a deployment that cannot batch is invisible to its operator",
        )

    def test_an_ordinary_failure_does_not_demote_batching(self) -> None:
        """A 500 is the provider, not the contract.

        Demoting on it would mean one provider blip cost the deployment its
        throughput until the next restart - and the fallback's own retry would
        turn one failed batch into thirty-two more calls against a service
        that is already failing.
        """
        agent = _Agent([_Response(500, b"{}")])
        checker = self._checker(agent)
        verdicts = _run(checker.check_batch(["a", "b"]))
        self.assertEqual(verdicts.results, [None, None])
        self.assertEqual(agent.calls, 1, "a failing provider was asked two more times")
        self.assertEqual(
            self.reader.value("pangea_moderation_tier2_batch_unsupported"), 0.0
        )

    def test_an_open_breaker_sheds_the_whole_batch_and_counts_every_message(
        self,
    ) -> None:
        agent = _Agent([_Response(500, b"{}")] * 3)
        checker = self._checker(agent)
        for _ in range(3):
            _run(checker.check_batch(["a"]))
        self.reader.snapshot(
            "pangea_moderation_tier2_dropped_total", cause="breaker_open"
        )
        calls = agent.calls
        verdicts = _run(checker.check_batch(["a", "b", "c", "d"]))
        self.assertEqual(verdicts.results, [None] * 4)
        self.assertEqual(agent.calls, calls, "a call was made while open")
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_dropped_total", cause="breaker_open"
            ),
            4.0,
            "a shed batch counted as one unmoderated message rather than four",
        )

    def test_a_flagged_screen_result_is_not_counted_as_a_decided_check(
        self,
    ) -> None:
        """`tier2_checks_total` stays one increment per message.

        A flagged screen result does not decide anything - the single-text
        confirmation does - so counting it here and again on the confirm would
        report two checks for one message.
        """
        agent = _Agent(
            [_Response(200, _body([_verdict(True, ["harassment"]), _verdict(False)]))]
        )
        self.reader.snapshot("pangea_moderation_tier2_checks_total", outcome="flagged")
        self.reader.snapshot("pangea_moderation_tier2_checks_total", outcome="clean")
        _run(self._checker(agent).check_batch(["bad", "fine"]))
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_checks_total", outcome="flagged"
            ),
            0.0,
        )
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_checks_total", outcome="clean"),
            1.0,
        )

    def test_the_screen_records_every_item_it_saw(self) -> None:
        agent = _Agent(
            [
                _Response(
                    200,
                    _body(
                        [
                            _verdict(True, ["harassment"]),
                            _verdict(False),
                            _verdict(False),
                        ]
                    ),
                )
            ]
        )
        self.reader.snapshot("pangea_moderation_tier2_screen_total", verdict="flagged")
        self.reader.snapshot("pangea_moderation_tier2_screen_total", verdict="clean")
        _run(self._checker(agent).check_batch(["a", "b", "c"]))
        self.assertEqual(
            self.reader.delta(
                "pangea_moderation_tier2_screen_total", verdict="flagged"
            ),
            1.0,
        )
        self.assertEqual(
            self.reader.delta("pangea_moderation_tier2_screen_total", verdict="clean"),
            2.0,
        )

    def test_an_empty_batch_makes_no_call(self) -> None:
        agent = _Agent([])
        self.assertEqual(_run(self._checker(agent).check_batch([])).results, [])
        self.assertEqual(agent.calls, 0)

    def test_a_single_message_does_not_pay_for_the_batch_contract(self) -> None:
        """One message goes out on the single-text endpoint.

        A batch of one is a batch an un-upgraded choreo would reject, and the
        fallback would then cost a wasted round trip on the commonest case in
        an unloaded system.
        """
        agent = _Agent([_Response(200, json.dumps(_verdict(False)).encode())])
        _run(self._checker(agent).check_batch(["only"]))
        self.assertEqual(agent.bodies, [{"text": "only"}])


class BatchOrderingTestCase(unittest.TestCase):
    """The mapping, attacked directly."""

    def setUp(self) -> None:
        self.clock = FakeClock()
        self.breaker = CircuitBreaker(
            clock=self.clock,
            failure_threshold=100,
            cooldown_seconds=30.0,
            max_cooldown_seconds=120.0,
        )

    def _checker(self, agent: _Agent) -> ChoreoChecker:
        return ChoreoChecker(
            agent=agent,
            clock=self.clock,
            base_url="http://choreo.invalid",
            access_token="syt_x",
            breaker=self.breaker,
            timeout_seconds=15.0,
        )

    def test_the_wire_order_is_the_order_the_caller_gave(self) -> None:
        """Thirty-two distinguishable texts, checked end to end.

        The request order and the result pairing are derived from the same
        immutable tuple, so there is no second ordering for them to disagree
        about. This asserts that end to end rather than trusting it.
        """
        texts = [f"message-{index}" for index in range(32)]
        flagged_at = {3, 17, 31}
        agent = _Agent(
            [
                _Response(
                    200,
                    _body(
                        [
                            _verdict(index in flagged_at, ["harassment"])
                            if index in flagged_at
                            else _verdict(False)
                            for index in range(32)
                        ]
                    ),
                )
            ]
        )
        verdicts = _run(self._checker(agent).check_batch(texts))
        self.assertEqual(agent.bodies[0]["texts"], texts)
        self.assertFalse(
            verdicts.confirmed,
            "a positionally mapped batch reported itself as confirmed, so the "
            "caller would redact on it without asking again",
        )
        self.assertEqual(
            {index for index, r in enumerate(verdicts.results) if r and r["flagged"]},
            flagged_at,
        )

    def test_a_result_list_one_short_never_produces_a_pairing(self) -> None:
        """Thirty-two texts, thirty-one results.

        Without the length refusal `zip` would pair 31 of them and drop the
        last - but every verdict from the point of the omission onwards would
        belong to the wrong message, which is a redaction of an innocent
        learner. Nothing is paired: the whole batch has no verdict.
        """
        agent = _Agent([_Response(200, _body([_verdict(False)] * 31))])
        verdicts = _run(self._checker(agent).check_batch([f"m{i}" for i in range(32)]))
        self.assertEqual(
            verdicts.results,
            [None] * 32,
            "a short result list produced verdicts, so 31 messages were "
            "judged by another message's answer",
        )
