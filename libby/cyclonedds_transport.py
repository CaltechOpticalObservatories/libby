"""Cyclone DDS transport: bamboo envelopes as typed DDS samples, peer to peer.

No ``from __future__ import annotations``: cyclonedds resolves IDL field
types from live annotations.
"""
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

from bamboo import Envelope, Message, MsgType
from cyclonedds.core import (DDSException, GuardCondition, InstanceState, Policy, Qos,
                             ReadCondition, SampleState, ViewState, WaitSet)
from cyclonedds.domain import Domain, DomainParticipant
from cyclonedds.idl import IdlStruct, types
from cyclonedds.pub import DataWriter, Publisher
from cyclonedds.sub import DataReader, Subscriber
from cyclonedds.topic import Topic
from cyclonedds.util import duration

from ._transport_errors import describe_exception

logger = logging.getLogger(__name__)

DestStr = str
SrcStr = str

DIRECT_TOPIC = "libby_direct"
BROADCAST_TOPIC = "libby_broadcast"
MTU_BYTES = 512 * 1024
MAX_BLOCKING_S = 1
TAKE_BATCH = 64
RX_WAKE_S = 1
# A first write to a peer waits this long for its reader, so it is not lost to discovery
MATCH_TIMEOUT_S = 1.0
# start() returns once no new peer has been matched for SETTLE_S, or after DISCOVERY_MAX_S
DISCOVERY_SETTLE_S = 0.1
DISCOVERY_MAX_S = 1.0
POLL_S = 0.01


@dataclass
class DdsEnvelope(IdlStruct, typename="libby.Envelope"):  # pylint: disable=too-many-instance-attributes
    """bamboo's ``Envelope`` plus the message's binary part, as one DDS sample.

    Absent optional strings travel as ``""`` and an absent binary as empty.
    """

    version: types.int32
    type: str
    transid: str
    key: str
    payload: str
    time: str
    destid: str
    sourceid: str
    traceparent: str
    tracestate: str
    binary: types.sequence[types.uint8]


def to_sample(msg: Message) -> DdsEnvelope:
    """Encode a bamboo message as a DDS sample."""
    env = msg.env
    return DdsEnvelope(
        version=env.version,
        type=str(env.type),
        transid=env.transid,
        key=env.key or "",
        payload=json.dumps(env.payload, separators=(",", ":")),
        time=env.time,
        destid=env.destid or "",
        sourceid=env.sourceid,
        traceparent=env.traceparent or "",
        tracestate=env.tracestate or "",
        binary=msg.binary or b"",
    )


def to_message(sample: DdsEnvelope) -> Message:
    """Decode a DDS sample back into a bamboo message."""
    envelope = Envelope(
        version=sample.version,
        type=MsgType(sample.type),
        transid=sample.transid,
        key=sample.key or None,
        payload=json.loads(sample.payload),
        time=sample.time,
        destid=sample.destid or None,
        sourceid=sample.sourceid,
        traceparent=sample.traceparent or None,
        tracestate=sample.tracestate or None,
    )
    # An octet sequence reads back as a list of ints
    return Message(envelope, bytes(sample.binary) if sample.binary else None)


def _qos(*partitions: str) -> Qos:
    """Reliable and lossless, optionally restricted to partitions."""
    policies = [Policy.Reliability.Reliable(duration(seconds=MAX_BLOCKING_S)),
                Policy.History.KeepAll]
    if partitions:
        policies.append(Policy.Partition(list(partitions)))
    return Qos(*policies)


def _matched_readers(writer: DataWriter) -> int:
    """Count the readers a writer is matched with.

    ``get_matched_subscriptions()`` raises IndexError when a reader matches
    between its sizing and filling calls (cyclonedds-python 11.0.1).
    """
    return writer.get_publication_matched_status().current_count


class CycloneDdsTransport:  # pylint: disable=too-many-instance-attributes
    """
    Cyclone DDS transport for bamboo, with no broker and no address book.

    Carries ``Message`` objects rather than byte frames (``pack_frames``).

    - Each peer reads ``libby_direct`` in a partition named by its own id; a
      direct message is written into the partition named by its ``destid``,
      so DDS delivers it only to that peer.
    - Every peer reads ``libby_broadcast``; broadcasts reach the sender too,
      as on RabbitMQ.
    - Peers find each other through DDS discovery. ``dds_config`` is Cyclone
      XML (or a path/URI to it), e.g. to list unicast peers where multicast
      is blocked; when unset, Cyclone reads ``CYCLONEDDS_URI``.
    """

    pack_frames = False

    def __init__(self, peer_id: str, domain_id: int = 0, dds_config: Optional[str] = None,
                 group_id: Optional[str] = None):
        self._peer_id = peer_id
        self._group_id = group_id
        self._cb: Optional[Callable[[SrcStr, Message], None]] = None
        self._stop = threading.Event()
        self._closed = False
        self._rx_thread: Optional[threading.Thread] = None
        self._writers: Dict[str, Tuple[DataWriter, threading.Event]] = {}
        self._writers_lock = threading.Lock()
        try:
            # Kept alive for the participant's lifetime when a config is given
            self._domain = Domain(domain_id, dds_config) if dds_config else None
            self._participant = DomainParticipant(domain_id)
            self._direct_topic = Topic(self._participant, DIRECT_TOPIC, DdsEnvelope)
            broadcast_topic = Topic(self._participant, BROADCAST_TOPIC, DdsEnvelope)
            self._direct_reader = DataReader(
                Subscriber(self._participant, qos=_qos(peer_id)), self._direct_topic, qos=_qos())
            self._broadcast_reader = DataReader(
                Subscriber(self._participant), broadcast_topic, qos=_qos())
            self._broadcast_writer = DataWriter(
                Publisher(self._participant), broadcast_topic, qos=_qos())
            self._waitset = WaitSet(self._participant)
            self._wake = GuardCondition(self._participant)
            any_sample = SampleState.Any | ViewState.Any | InstanceState.Any
            for reader in (self._direct_reader, self._broadcast_reader):
                self._waitset.attach(ReadCondition(reader, any_sample))
            self._waitset.attach(self._wake)
        except DDSException as e:
            raise RuntimeError(
                f"Failed to setup Cyclone DDS transport: {describe_exception(e)}"
            ) from e

    @property
    def group_id(self) -> Optional[str]:
        """Group this peer belongs to, as given at construction."""
        return self._group_id

    @property
    def mtu(self) -> int:
        """Maximum message size; DDS fragments large samples itself."""
        return MTU_BYTES

    def start(self) -> None:
        """Start receiving, then wait (bounded) for discovery to settle.

        Returning only once already-running peers are matched means a first
        broadcast, such as a ``keys.list`` sweep, reaches them. A stopped
        transport cannot be restarted; build a new one.
        """
        if self._closed:
            raise RuntimeError("CycloneDdsTransport was stopped; create a new transport")
        if self._rx_thread and self._rx_thread.is_alive():
            return
        self._rx_thread = threading.Thread(target=self._rx_loop, daemon=True,
                                           name="libby-dds-rx")
        self._rx_thread.start()
        self._settle_discovery()

    def stop(self) -> None:
        """Stop receiving, drop later sends, and leave the DDS domain.

        Deleting the participant deletes its readers and writers too, so
        other peers stop matching this one.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        self._wake.set(True)
        if self._rx_thread:
            self._rx_thread.join(timeout=2.0)
        # cyclonedds-python has no public close, and `del` would not delete while
        # child entities hold references; __del__ deletes the entity now
        self._participant.__del__()  # pylint: disable=unnecessary-dunder-call

    def on_receive(self, cb: Callable[[SrcStr, Message], None]) -> None:
        """Register ``cb(source, message)``; ``source`` is ``"peer:<sourceid>"``."""
        self._cb = cb

    def send(self, dest: DestStr, msg: Message) -> None:
        """Send to ``"peer:<id>"`` or ``"broadcast:*"``; dropped once stopped.

        Delivery errors are dropped silently, matching bamboo's no-NACK policy.
        """
        if self._stop.is_set():
            return
        try:
            writer = (self._broadcast_writer if dest.startswith("broadcast:")
                      else self._direct_writer(dest.split(":", 1)[-1]))
            writer.write(to_sample(msg))
        except DDSException:
            pass  # includes a send racing stop() as the participant is deleted

    def _direct_writer(self, peer_id: str) -> DataWriter:
        """Return the cached writer for a peer, creating it on first use.

        Only the first use waits for the peer's reader, and it waits outside
        the lock so sends to other peers are not held up; concurrent first
        sends to the same peer wait on that same match.
        """
        with self._writers_lock:
            entry = self._writers.get(peer_id)
            created = entry is None
            if created:
                writer = DataWriter(Publisher(self._participant, qos=_qos(peer_id)),
                                    self._direct_topic, qos=_qos())
                entry = self._writers[peer_id] = (writer, threading.Event())
        writer, matched = entry
        if created:
            try:
                deadline = time.monotonic() + MATCH_TIMEOUT_S
                while not _matched_readers(writer) and time.monotonic() < deadline:
                    time.sleep(POLL_S)
            finally:
                matched.set()
        else:
            matched.wait(MATCH_TIMEOUT_S)
        return writer

    def _settle_discovery(self) -> None:
        start = time.monotonic()
        matched = _matched_readers(self._broadcast_writer)
        changed = start
        while (now := time.monotonic()) - start < DISCOVERY_MAX_S:
            if now - changed >= DISCOVERY_SETTLE_S:
                return
            time.sleep(POLL_S)
            current = _matched_readers(self._broadcast_writer)
            if current != matched:
                matched, changed = current, time.monotonic()

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            self._waitset.wait(duration(seconds=RX_WAKE_S))
            for reader in (self._direct_reader, self._broadcast_reader):
                for sample in reader.take(N=TAKE_BATCH):
                    if self._stop.is_set():
                        return
                    if not isinstance(sample, DdsEnvelope) or self._cb is None:
                        continue
                    try:
                        message = to_message(sample)
                    except ValueError:
                        continue  # malformed payload or type, dropped like any undeliverable frame
                    try:
                        self._cb(f"peer:{message.env.sourceid}", message)
                    except Exception:  # pylint: disable=broad-exception-caught
                        # One bad message must not stop this peer receiving
                        logger.exception("receive callback failed for a message from %s",
                                         message.env.sourceid)
