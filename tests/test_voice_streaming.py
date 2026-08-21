"""
Tests for streaming STT — agent/voice/deepgram_stream.py, the /api/transcribe
/stream relay, and the WebSocket hole it would have opened in the origin gate.

smooth-voice_2 Tier 2 assumes you can lean on the recognizer's own
end-of-utterance signal. Batch Deepgram has none, which is why index.html's
hands-free VAD infers "they're done" from microphone energy alone. These
cover the signal that replaces that inference.

Run from the project root:
    python -m unittest tests.test_voice_streaming
"""

import asyncio
import json
import os
import shutil
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import AioHTTPTestCase

import serve as serve_module
from agent.providers.base import BaseProvider, ProviderResponse, TextChunk, TokenUsage
from agent.security.origin import check_origin
from agent.tools.registry import ToolRegistry
from agent.voice.deepgram_stream import (
    DeepgramStream,
    StreamingTranscriptionError,
    normalize_message,
    stream_params,
)


class FakeProvider(BaseProvider):
    @property
    def model_name(self):
        return "fake-model"

    async def stream(self, messages, system, tools=None):
        yield TextChunk(text="ok")
        yield ProviderResponse(text="ok", tool_calls=[], usage=TokenUsage(), model=self.model_name)


class TestStreamParams(unittest.TestCase):
    def test_interim_results_are_on(self):
        # Not optional: Deepgram only emits UtteranceEnd when interims are on,
        # and UtteranceEnd is the half of the endpoint signal that survives a
        # noisy channel. Turning it off to save bandwidth would silently
        # disable the more reliable signal.
        self.assertEqual(stream_params()["interim_results"], "true")

    def test_utterance_end_and_endpointing_are_sent(self):
        params = stream_params(utterance_end_ms=1500, endpointing_ms=250)
        self.assertEqual(params["utterance_end_ms"], "1500")
        self.assertEqual(params["endpointing"], "250")

    def test_model_matches_the_batch_path(self):
        # The two paths must not quietly transcribe differently.
        self.assertEqual(stream_params()["model"], "nova-2")


class TestNormalizeMessage(unittest.TestCase):
    def test_a_final_transcript(self):
        event = normalize_message(json.dumps({
            "type": "Results",
            "channel": {"alternatives": [{"transcript": "hello there"}]},
            "is_final": True, "speech_final": True,
        }))
        self.assertEqual(event, {
            "type": "transcript", "text": "hello there",
            "is_final": True, "speech_final": True,
        })

    def test_an_interim_transcript(self):
        event = normalize_message(json.dumps({
            "type": "Results",
            "channel": {"alternatives": [{"transcript": "hel"}]},
            "is_final": False,
        }))
        self.assertEqual(event["is_final"], False)
        self.assertEqual(event["text"], "hel")

    def test_an_empty_interim_is_dropped(self):
        # Deepgram emits these constantly while listening.
        self.assertIsNone(normalize_message(json.dumps({
            "type": "Results",
            "channel": {"alternatives": [{"transcript": "  "}]},
            "is_final": False,
        })))

    def test_an_empty_result_carrying_speech_final_is_kept(self):
        # The endpoint signal arriving on a segment that happened to
        # transcribe to nothing. Swallowing it strands the turn open.
        event = normalize_message(json.dumps({
            "type": "Results",
            "channel": {"alternatives": [{"transcript": ""}]},
            "is_final": True, "speech_final": True,
        }))
        self.assertIsNotNone(event)
        self.assertTrue(event["speech_final"])

    def test_utterance_end(self):
        self.assertEqual(
            normalize_message(json.dumps({"type": "UtteranceEnd", "last_word_end": 1.5})),
            {"type": "utterance_end"},
        )

    def test_speech_started(self):
        self.assertEqual(
            normalize_message(json.dumps({"type": "SpeechStarted", "timestamp": 0.4})),
            {"type": "speech_started"},
        )

    def test_metadata_and_unknown_types_are_dropped(self):
        for body in ('{"type":"Metadata"}', '{"type":"SomethingNew"}', "{}"):
            with self.subTest(body=body):
                self.assertIsNone(normalize_message(body))

    def test_errors_are_surfaced(self):
        event = normalize_message(json.dumps({"type": "Error", "description": "bad audio"}))
        self.assertEqual(event["type"], "error")
        self.assertIn("bad audio", event["message"])

    def test_malformed_json_does_not_raise(self):
        self.assertIsNone(normalize_message("not json"))
        self.assertIsNone(normalize_message(b"\x00\x01"))

    def test_unexpected_shapes_do_not_raise(self):
        for body in ('{"type":"Results"}',
                     '{"type":"Results","channel":{}}',
                     '{"type":"Results","channel":{"alternatives":[]}}',
                     '{"type":"Results","channel":{"alternatives":["junk"]}}',
                     '[1,2,3]', 'null', '"a string"'):
            with self.subTest(body=body):
                normalize_message(body)  # must not raise

    def test_an_error_message_is_bounded(self):
        event = normalize_message(json.dumps({"type": "Error", "description": "x" * 5000}))
        self.assertLessEqual(len(event["message"]), 300)

    def test_a_dict_is_accepted_directly(self):
        self.assertEqual(
            normalize_message({"type": "UtteranceEnd"}), {"type": "utterance_end"}
        )


class TestDeepgramStreamConfig(unittest.TestCase):
    def test_a_missing_key_fails_loudly_at_construction(self):
        with self.assertRaises(StreamingTranscriptionError):
            DeepgramStream("")


class TestWebSocketOriginGate(unittest.TestCase):
    """
    A WebSocket upgrade arrives as a GET, and unlike fetch() it is not
    subject to the same-origin policy — any page can open a socket to
    localhost and the browser will not block it or ask for CORS. Ungated,
    /api/transcribe/stream is a stranger spending Sean's Deepgram credits and
    reading what his microphone hears.
    """

    WS_HEADERS = {
        "Upgrade": "websocket",
        "Connection": "keep-alive, Upgrade",
        "Host": "localhost:8123",
    }

    def _headers(self, **overrides):
        headers = dict(self.WS_HEADERS)
        headers.update(overrides)
        return headers

    def test_a_cross_site_upgrade_is_refused(self):
        reason = check_origin(
            "GET", "/api/transcribe/stream",
            self._headers(**{"Sec-Fetch-Site": "cross-site"}), "127.0.0.1",
        )
        self.assertIsNotNone(reason)

    def test_a_same_site_upgrade_is_refused(self):
        reason = check_origin(
            "GET", "/api/transcribe/stream",
            self._headers(**{"Sec-Fetch-Site": "same-site"}), "127.0.0.1",
        )
        self.assertIsNotNone(reason)

    def test_a_same_origin_upgrade_is_allowed(self):
        reason = check_origin(
            "GET", "/api/transcribe/stream",
            self._headers(**{"Sec-Fetch-Site": "same-origin"}), "127.0.0.1",
        )
        self.assertIsNone(reason)

    def test_an_upgrade_claiming_a_foreign_host_is_refused(self):
        reason = check_origin(
            "GET", "/api/transcribe/stream",
            self._headers(Host="evil.example"), "127.0.0.1",
        )
        self.assertIsNotNone(reason)

    def test_connection_header_is_parsed_as_a_token_list(self):
        # Real browsers send "keep-alive, Upgrade". Matching the whole header
        # value would miss every real handshake.
        for connection in ("Upgrade", "keep-alive, Upgrade", "upgrade", "KEEP-ALIVE, UPGRADE"):
            with self.subTest(connection=connection):
                reason = check_origin(
                    "GET", "/api/transcribe/stream",
                    self._headers(Connection=connection, **{"Sec-Fetch-Site": "cross-site"}),
                    "127.0.0.1",
                )
                self.assertIsNotNone(reason, f"{connection!r} was not recognised as an upgrade")

    def test_an_ordinary_cross_site_GET_is_still_ungated(self):
        # The exemption is for upgrades specifically, not a widening of the
        # rule to every GET.
        self.assertIsNone(
            check_origin("GET", "/api/usage",
                         {"Host": "localhost:8123", "Sec-Fetch-Site": "cross-site"},
                         "127.0.0.1")
        )


class TestStreamEndpointWiring(AioHTTPTestCase):
    async def get_application(self):
        self.tmp = tempfile.mkdtemp()
        self._prev_env = {
            key: os.environ.get(key)
            for key in (
                "TRILLION_FACTORY_DB", "TRILLION_NOTES_VAULT_PATH",
                "TRILLION_NOTES_INDEX_PATH", "TRILLION_HEARTBEAT_DB",
                "TRILLION_CSP_REPORT_DB", "DEEPGRAM_API_KEY",
            )
        }
        os.environ["TRILLION_FACTORY_DB"] = os.path.join(self.tmp, "factory.db")
        os.environ["TRILLION_NOTES_VAULT_PATH"] = os.path.join(self.tmp, "vault")
        os.environ["TRILLION_NOTES_INDEX_PATH"] = os.path.join(self.tmp, "notes.db")
        os.environ["TRILLION_HEARTBEAT_DB"] = os.path.join(self.tmp, "heartbeat.db")
        os.environ["TRILLION_CSP_REPORT_DB"] = os.path.join(self.tmp, "csp.db")
        os.environ.pop("DEEPGRAM_API_KEY", None)

        serve_module._provider = FakeProvider()
        serve_module._registry = ToolRegistry()
        serve_module._agent_sessions.clear()
        return serve_module.build_app()

    def tearDown(self):
        super().tearDown()
        serve_module._provider = None
        serve_module._registry = None
        serve_module._agent_sessions.clear()
        for key, prev in self._prev_env.items():
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_the_route_exists(self):
        paths = {getattr(r.resource, "canonical", None) for r in self.app.router.routes()}
        self.assertIn("/api/transcribe/stream", paths)

    async def test_an_unconfigured_key_reports_an_error_rather_than_hanging(self):
        # No DEEPGRAM_API_KEY: the socket must open, say why, and close —
        # not accept audio into a void or 500.
        async with self.client.ws_connect("/api/transcribe/stream") as ws:
            message = await ws.receive_json(timeout=5)
        self.assertEqual(message["type"], "error")
        self.assertIn("Deepgram", message["message"])

    async def test_close_flushes_and_the_server_hangs_up_without_waiting_for_us(self):
        """
        Regression, found live rather than in a unit test.

        The first version kept reading from the browser after "close", so the
        flushed tail was only collected once the *browser* hung up — and a
        browser that hangs up promptly loses the last segment, which is the
        one word a turn usually hinges on. The socket also just sat there:
        6s with no close instead of a prompt one.
        """
        import agent.voice.deepgram_stream as ds

        finished = asyncio.Event()

        class FakeStream:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return None

            async def send_audio(self, chunk):
                pass

            async def finish(self):
                finished.set()

            async def __aiter__(self):
                # Deepgram emits the flushed tail, then closes.
                await finished.wait()
                yield {"type": "transcript", "text": "the tail", "is_final": True,
                       "speech_final": True}

        original = ds.DeepgramStream
        ds.DeepgramStream = FakeStream
        os.environ["DEEPGRAM_API_KEY"] = "test-key"
        try:
            async with self.client.ws_connect("/api/transcribe/stream") as ws:
                await ws.send_bytes(b"audio")
                await ws.send_str("close")
                message = await ws.receive_json(timeout=5)
                self.assertEqual(message["text"], "the tail")
                # And the server closes on its own, without us hanging up first.
                closed = await ws.receive(timeout=5)
                self.assertIn(closed.type, (web.WSMsgType.CLOSE, web.WSMsgType.CLOSED,
                                            web.WSMsgType.CLOSING))
        finally:
            ds.DeepgramStream = original
            os.environ.pop("DEEPGRAM_API_KEY", None)

    async def test_the_batch_endpoint_is_untouched(self):
        # Streaming is additive. Push-to-talk must keep working exactly as it
        # did, and it is the fallback when the socket fails.
        resp = await self.client.request("POST", "/api/transcribe", data=b"not audio")
        self.assertEqual(resp.status, 400)


if __name__ == "__main__":
    unittest.main()
