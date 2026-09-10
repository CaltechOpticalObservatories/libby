import threading
from typing import Callable, Optional
import pika
from pika.exceptions import AMQPError

from ._transport_errors import describe_exception


DestStr = str
SrcStr = str

class RabbitMQTransport:
    """
    RabbitMQ-based transport for Bamboo Protocol.

    Architecture:
    - Each peer gets a unique queue: "libby.peer.<peer_id>"
    - Direct exchange "libby.direct" for peer-to-peer messages
    - Fanout exchange "libby.fanout" for broadcast messages
    - Each peer binds its queue to both exchanges

    No address book needed - RabbitMQ broker handles all routing.
    """

    def __init__(self, peer_id: str, rabbitmq_url: str = "amqp://localhost", group_id: Optional[str] = None):
        """
        Initialize RabbitMQ transport.

        Args:
            peer_id: Unique identifier for this peer
            rabbitmq_url: RabbitMQ connection URL (e.g., "amqp://user:pass@host:5672/")
            group_id: Optional group identifier. If provided, included in queue name
                      for future group functionality.
        """
        self._peer_id = peer_id
        self._url = rabbitmq_url
        self._group_id = group_id
        self._queue_name = self._build_queue_name()
        self._cb: Optional[Callable[[SrcStr, bytes], None]] = None

        # Send connection (used in main thread)
        self._send_connection: Optional[pika.BlockingConnection] = None
        self._send_channel: Optional[pika.channel.Channel] = None
        self._send_lock = threading.Lock()

        # Receive connection created in background thread (not stored as instance vars)
        self._stop = threading.Event()
        # Set once the receive queue is declared, bound, and consuming. start()
        # blocks on this so callers never publish a request before this peer
        # can actually receive the reply -- otherwise a fast responder's ACK/RESP
        # can arrive before our queue exists and get silently dropped by the
        # direct exchange, surfacing as a spurious timeout.
        self._ready = threading.Event()
        self._rx_thread: Optional[threading.Thread] = None
        # Pump heartbeats so an idle send connection doesn't get reset by the broker
        self._hb_thread: Optional[threading.Thread] = None
        self._hb_interval_s: float = 30.0

        # Setup exchanges and queue
        self._setup()

    def _setup(self) -> None:
        """Setup RabbitMQ connection, exchanges, and queues."""
        try:
            # Create send connection
            params = pika.URLParameters(self._url)
            params.heartbeat = 600  # 10 minute heartbeat
            self._send_connection = pika.BlockingConnection(params)
            self._send_channel = self._send_connection.channel()

            # The send connection only publishes; it doesn't need its own
            # queue, so it just makes sure the exchanges exist.
            self._setup_topology(self._send_channel, declare_queue=False)

        except AMQPError as e:
            raise RuntimeError(
                f"Failed to setup RabbitMQ transport: {describe_exception(e)}"
            ) from e

    def _build_queue_name(self) -> str:
        """Build queue name, including group_id if provided."""
        if self._group_id:
            return f"libby.group.{self._group_id}.peer.{self._peer_id}"
        return f"libby.peer.{self._peer_id}"

    def _setup_topology(self, channel, *, declare_queue: bool) -> None:
        """
        Declare exchanges on a given channel, and this peer's queue and
        bindings if `declare_queue` is set.

        The queue is exclusive to the connection that declares it, since a
        transient, non-exclusive queue is a deprecated RabbitMQ combination
        (removed outright in newer broker versions). Only the receive
        connection consumes from it, so only that connection should declare
        it; the send connection only needs the exchanges to exist.
        """
        # Declare exchanges
        channel.exchange_declare(
            exchange='libby.direct',
            exchange_type='direct',
            durable=False
        )

        # Fanout exchange for broadcasts
        channel.exchange_declare(
            exchange='libby.fanout',
            exchange_type='fanout',
            durable=False
        )

        if not declare_queue:
            return

        # Create this peer's queue, exclusive to this (receive) connection
        channel.queue_declare(
            queue=self._queue_name,
            durable=False,
            exclusive=True
        )

        # Bind queue to direct exchange with peer_id as routing key
        channel.queue_bind(
            queue=self._queue_name,
            exchange='libby.direct',
            routing_key=self._peer_id
        )

        # Bind queue to fanout exchange (for broadcasts)
        channel.queue_bind(
            queue=self._queue_name,
            exchange='libby.fanout'
        )

    @property
    def group_id(self) -> Optional[str]:
        return self._group_id

    @property
    def mtu(self) -> int:
        """Maximum transmission unit - RabbitMQ can handle large messages."""
        return 512 * 1024

    def start(self, ready_timeout_s: float = 5.0) -> None:
        """Start consuming messages from RabbitMQ.

        Blocks (up to ``ready_timeout_s``) until the receive queue is
        declared, bound, and consuming, so callers can publish a request
        immediately after this returns without racing their own reply queue.
        """
        if self._rx_thread and self._rx_thread.is_alive():
            return

        # Create separate receive connection in the background thread
        self._stop.clear()
        self._ready.clear()
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True)
        self._rx_thread.start()

        if self._hb_thread is None or not self._hb_thread.is_alive():
            self._hb_thread = threading.Thread(
                target=self._hb_loop, daemon=True, name="libby-rmq-hb"
            )
            self._hb_thread.start()

        self._ready.wait(timeout=ready_timeout_s)

    def stop(self) -> None:
        """Stop consuming and close RabbitMQ connection."""
        self._stop.set()

        # Wait for receive thread to finish (it will cleanup its own connection)
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)
        if self._hb_thread:
            self._hb_thread.join(timeout=2.0)

        # Close send connection
        if self._send_channel and self._send_channel.is_open:
            try:
                self._send_channel.close()
            except Exception:
                pass

        if self._send_connection and self._send_connection.is_open:
            try:
                self._send_connection.close()
            except Exception:
                pass

    def on_receive(self, cb: Callable[[SrcStr, bytes], None]) -> None:
        """
        Register callback for incoming messages.

        Args:
            cb: Callback function that receives (source, frame) where
                source is "peer:<peer_id>" and frame is raw bytes
        """
        self._cb = cb

    def send(self, dest: DestStr, frame: bytes) -> None:
        """
        Send a frame to a destination.

        Args:
            dest: Destination string, either:
                  - ``"peer:<peer_id>"`` for direct peer-to-peer
                  - ``"broadcast:*"`` for fanout to all peers
            frame: Raw bytes to send (already serialized by Bamboo)
        """
        if not self._send_channel or not self._send_channel.is_open:
            return  # Silently drop if channel not ready

        try:
            with self._send_lock:
                properties = pika.BasicProperties(
                    app_id=self._peer_id,  # Identify sender
                    delivery_mode=1  # Non-persistent (faster)
                )

                if dest.startswith("peer:"):
                    # Direct peer-to-peer message
                    peer_id = dest.split(":", 1)[1]
                    self._send_channel.basic_publish(
                        exchange='libby.direct',
                        routing_key=peer_id,
                        body=frame,
                        properties=properties
                    )

                elif dest.startswith("broadcast:"):
                    # Broadcast to all peers
                    self._send_channel.basic_publish(
                        exchange='libby.fanout',
                        routing_key='',
                        body=frame,
                        properties=properties
                    )

        except AMQPError:
            # Silently drop on error to match Bamboo's no-NACK policy
            pass

    def _hb_loop(self) -> None:
        """Drive heartbeats on the send connection during idle periods."""
        while not self._stop.wait(timeout=self._hb_interval_s):
            try:
                with self._send_lock:
                    conn = self._send_connection
                    if conn is not None and conn.is_open:
                        conn.process_data_events(time_limit=0)
            except Exception:
                pass

    def _rx_loop(self) -> None:
        """Message receive loop - runs in background thread with its own connection."""
        recv_conn = None
        recv_ch = None

        def message_callback(ch, method, properties, body):
            """Handle incoming message."""
            if not self._cb:
                ch.basic_ack(delivery_tag=method.delivery_tag)
                return

            # Extract sender from app_id property
            src_peer = properties.app_id if properties.app_id else "unknown"

            # Call Bamboo's callback with expected format
            try:
                self._cb(f"peer:{src_peer}", body)
            except Exception:
                pass  # Silently ignore callback errors

            # Acknowledge message
            ch.basic_ack(delivery_tag=method.delivery_tag)

        try:
            # Create separate receive connection (not shared with send thread)
            params = pika.URLParameters(self._url)
            params.heartbeat = 600
            recv_conn = pika.BlockingConnection(params)
            recv_ch = recv_conn.channel()

            # Ensure topology is set up on this channel
            self._setup_topology(recv_ch, declare_queue=True)

            # Start consuming
            recv_ch.basic_consume(
                queue=self._queue_name,
                on_message_callback=message_callback,
                auto_ack=False  # Manual ack for reliability
            )
            self._ready.set()

            # Consume until stopped
            while not self._stop.is_set():
                try:
                    recv_conn.process_data_events(time_limit=0.1)
                except Exception:
                    if self._stop.is_set():
                        break
                    # Connection lost, try to reconnect
                    self._ready.clear()
                    try:
                        if recv_ch and recv_ch.is_open:
                            recv_ch.close()
                        if recv_conn and recv_conn.is_open:
                            recv_conn.close()

                        # Reconnect
                        recv_conn = pika.BlockingConnection(params)
                        recv_ch = recv_conn.channel()

                        # Re-declare exchanges, queue, and bindings (idempotent)
                        self._setup_topology(recv_ch, declare_queue=True)

                        recv_ch.basic_consume(
                            queue=self._queue_name,
                            on_message_callback=message_callback,
                            auto_ack=False
                        )
                        self._ready.set()
                    except Exception:
                        # Failed to reconnect, wait and try again
                        if not self._stop.is_set():
                            self._stop.wait(1.0)

        except Exception:
            pass  # Exit gracefully
        finally:
            # Cleanup receive connection
            if recv_ch:
                try:
                    recv_ch.stop_consuming()
                except Exception:
                    pass
                try:
                    recv_ch.close()
                except Exception:
                    pass
            if recv_conn:
                try:
                    recv_conn.close()
                except Exception:
                    pass
