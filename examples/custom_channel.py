"""Offline custom channel demonstration: no external messages or model credentials."""

import json
import tempfile
from pathlib import Path

from evomesh import AgentApp, Config
from evomesh.channels import ChannelDenied, ChannelHub, IncomingMessage


class LocalChannel:
    def receive(self, headers, body):
        # Demo only. Production adapters need platform-specific cryptographic authentication.
        if headers.get("x-demo-secret") != "offline-demo":
            raise ChannelDenied("Invalid demo credential")
        data = json.loads(body)
        return IncomingMessage(data["id"], "me", "local-chat", data["text"], {"chat": "local"})

    def send(self, target, text, identifier):
        print(f"Reply to {target['chat']}: {text}")


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    with AgentApp(
        Config(
            database_url=f"sqlite:///{root / 'ledger.db'}", workspace=root / "workspace", demo=True
        )
    ) as app:
        hub = ChannelHub(app)
        try:
            hub.register("local", LocalChannel(), {"allowed_senders": ["me"]})
            payload = json.dumps({"id": "message-1", "text": "List workspace files"}).encode()
            hub.receive("local", {"x-demo-secret": "offline-demo"}, payload)
            hub.receive("local", {"x-demo-secret": "offline-demo"}, payload)  # Deduplicated.
            app.work_once()
            print(hub.deliver_once())
            assert len(app.state.list_tasks()) == 1
        finally:
            hub.close()
