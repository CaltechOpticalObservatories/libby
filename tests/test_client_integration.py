"""Integration test: Client against a live LibbyDaemon over RabbitMQ.

Skipped automatically if no broker is reachable at amqp://localhost - this
suite needs a real RabbitMQ instance, unlike the rest of tests/.
"""
import threading
import time
import unittest

from libby import Client, KeywordError
from libby.daemon import LibbyDaemon

RABBITMQ_URL = "amqp://localhost"


def _broker_available() -> bool:
    try:
        from libby.rabbitmq_transport import RabbitMQTransport
        probe = RabbitMQTransport(peer_id="libby-test-probe", rabbitmq_url=RABBITMQ_URL)
        probe.stop()
        return True
    except Exception:
        return False


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class ClientIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class _TestDaemon(LibbyDaemon):
            peer_id = "pickofftest"
            group_id = "hsfei"
            transport = "rabbitmq"
            discovery_enabled = False

            def on_start(self, libby):
                state = {"pos": 10.0}
                self.keyword_registry.float(
                    "positionvalue",
                    getter=lambda: state["pos"],
                    setter=lambda v: state.update(pos=v),
                    units="mm",
                )
                self.keyword_registry.bool("isreferenced", getter=lambda: True)

        cls.daemon = _TestDaemon()
        cls.daemon.start()
        time.sleep(2.0)  # let the receive connection/queue finish setting up
        cls.client = Client.rabbitmq(rabbitmq_url=RABBITMQ_URL)
        time.sleep(1.5)  # same warm-up the daemon connection needed

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.daemon.stop()

    def _name(self, keyword: str) -> str:
        return f"hsfei.pickofftest.{keyword}"

    def test_get_returns_current_value(self):
        self.assertEqual(self.client.get(self._name("positionvalue"), timeout_s=6.0), 10.0)

    def test_set_then_get_round_trips(self):
        self.assertEqual(self.client.set(self._name("positionvalue"), 42.0, timeout_s=6.0), 42.0)
        self.assertEqual(self.client.get(self._name("positionvalue"), timeout_s=6.0), 42.0)

    def test_show_includes_units(self):
        resp = self.client.show(self._name("positionvalue"), timeout_s=6.0)
        self.assertTrue(resp["ok"])
        self.assertEqual(resp["units"], "mm")

    def test_set_on_read_only_keyword_raises_keyword_error(self):
        with self.assertRaises(KeywordError):
            self.client.set(self._name("isreferenced"), False, timeout_s=6.0)

    def test_uptime_is_a_small_positive_integer(self):
        uptime = self.client.get(self._name("uptime"), timeout_s=6.0)
        self.assertIsInstance(uptime, int)
        self.assertGreaterEqual(uptime, 0)


@unittest.skipUnless(_broker_available(), "no RabbitMQ broker reachable at amqp://localhost")
class ShutdownKeywordTests(unittest.TestCase):
    """Separate daemon: shutdown stops it, so it can't share the class fixture above."""

    def test_daemon_registered_shutdown_trigger_stops_the_daemon(self):
        class _TestDaemon(LibbyDaemon):
            """Daemon that wires its own shutdown keyword to request_stop."""

            peer_id = "shutdowntest"
            group_id = "hsfei"
            transport = "rabbitmq"
            discovery_enabled = False

            def on_start(self, libby):
                self.keyword_registry.trigger(
                    "shutdown",
                    action=self.request_stop,
                    description="Gracefully stop this daemon.",
                )

        daemon = _TestDaemon()
        serve_thread = threading.Thread(target=daemon.serve, daemon=True)
        serve_thread.start()
        time.sleep(2.0)
        client = Client.rabbitmq(rabbitmq_url=RABBITMQ_URL)
        time.sleep(1.5)
        try:
            client.set("hsfei.shutdowntest.shutdown", 1, timeout_s=6.0)
            serve_thread.join(timeout=5.0)
            self.assertFalse(serve_thread.is_alive())
        finally:
            client.close()
            daemon.stop()


if __name__ == "__main__":
    unittest.main()
