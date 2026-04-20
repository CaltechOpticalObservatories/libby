"""
Test peer with group_id.
"""
import time
from typing import Dict, Any
from libby.daemon import LibbyDaemon


def handle_echo(p: Dict[str, Any]):
    return {"ok": True, "echo": p, "t": time.time()}


class GroupTestPeer(LibbyDaemon):
    peer_id = "group-test-peer"
    transport = "rabbitmq"
    rabbitmq_url = "amqp://localhost"
    group_id = "hsfei"

    services = {
        "echo": handle_echo,
    }

    def on_start(self, libby):
        print(f"[GroupTestPeer] Started with group_id='{self.group_id}'")
        print(f"[GroupTestPeer] Expected queue name: libby.group.{self.group_id}.peer.{self.peer_id}")


if __name__ == "__main__":
    GroupTestPeer().serve()
