from __future__ import annotations
from typing import Dict, Optional
from .daemon import LibbyDaemon
from .libby import Libby


class RabbitMQDaemon(LibbyDaemon):
    """
    LibbyDaemon subclass that uses RabbitMQ transport instead of ZMQ.

    Usage:
        class MyPeer(RabbitMQDaemon):
            peer_id = "my-peer"
            rabbitmq_url = "amqp://localhost"  # optional, defaults to this

            services = {
                "echo": lambda payload: {"echo": payload}
            }

            topics = {
                "alerts": lambda payload: print(payload)
            }

        if __name__ == "__main__":
            MyPeer().serve()

    Note: No need to set `bind` or `address_book` - RabbitMQ
    handles all routing automatically.
    """

    # RabbitMQ-specific config
    rabbitmq_url: Optional[str] = None

    def config_rabbitmq_url(self) -> str:
        """Override to customize RabbitMQ connection URL."""
        return self.rabbitmq_url or "amqp://localhost"

    def build_libby(self) -> Libby:
        """Build Libby instance with RabbitMQ transport."""
        return Libby.rabbitmq(
            self_id=self.config_peer_id(),
            rabbitmq_url=self.config_rabbitmq_url(),
            keys=[],
            callback=None,
            discover=self.config_discovery_enabled(),
            discover_interval_s=self.config_discovery_interval_s(),
            hello_on_start=True,
        )
