import base64
import hashlib
import hmac
import json
import time
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from sqlalchemy import select, update

from evomesh import AgentApp, Config
from evomesh.channel_store import ChannelStore, deliveries
from evomesh.channels import (
    ChannelDenied,
    ChannelHub,
    DeliveryRejected,
    DingTalkAdapter,
    FeishuAdapter,
    IncomingMessage,
    TelegramAdapter,
    reply_text,
)
from evomesh.runtime_store import RuntimeStore, tasks
from evomesh.store import LeaseLost


@pytest.fixture
def credentials(monkeypatch):
    values = {
        "APP_ID": "cli_test",
        "APP_SECRET": "app-secret",
        "VERIFY": "verify-secret",
        "ENCRYPT": "encrypt-secret",
        "BOT_TOKEN": "123:fake",
        "WEBHOOK": "hook-secret",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return values


def feishu_config():
    return dict(
        kind="feishu",
        app_id_env="APP_ID",
        app_secret_env="APP_SECRET",
        verification_token_env="VERIFY",
        encrypt_key_env="ENCRYPT",
        allowed_senders=["ou_user"],
    )


def encrypted_feishu(payload):
    raw = json.dumps(payload).encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    iv = bytes(range(16))
    cipher = Cipher(algorithms.AES(hashlib.sha256(b"encrypt-secret").digest()), modes.CBC(iv))
    encryptor = cipher.encryptor()
    encrypted = iv + encryptor.update(padded) + encryptor.finalize()
    body = json.dumps({"encrypt": base64.b64encode(encrypted).decode()}).encode()
    stamp = str(int(time.time()))
    signature = hashlib.sha256((stamp + "nonce" + "encrypt-secret").encode() + body).hexdigest()
    return {
        "x-lark-request-timestamp": stamp,
        "x-lark-request-nonce": "nonce",
        "x-lark-signature": signature,
    }, body


def feishu_event():
    return {
        "schema": "2.0",
        "header": {
            "app_id": "cli_test",
            "token": "verify-secret",
            "event_type": "im.message.receive_v1",
        },
        "event": {
            "sender": {"sender_type": "user", "sender_id": {"open_id": "ou_user"}},
            "message": {
                "message_id": "om_1",
                "chat_id": "oc_1",
                "chat_type": "p2p",
                "message_type": "text",
                "content": '{"text":"list files"}',
            },
        },
    }


def telegram_config():
    return dict(
        kind="telegram",
        bot_token_env="BOT_TOKEN",
        webhook_secret_env="WEBHOOK",
        allowed_senders=["42"],
    )


def telegram_event(update=1, group=False):
    return {
        "update_id": update,
        "message": {
            "message_id": 2,
            "from": {"id": 42, "is_bot": False},
            "chat": {"id": 7, "type": "group" if group else "private"},
            "text": "List files",
        },
    }


def test_feishu_encryption_signature_handshake_and_reply(credentials):
    requests = []

    def respond(request):
        requests.append(request)
        if "/auth/" in str(request.url):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "tenant-token"})
        return httpx.Response(200, json={"code": 0})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = FeishuAdapter(feishu_config(), client)
        headers, body = encrypted_feishu(feishu_event())
        msg = adapter.receive(headers, body)
        assert msg.sender == "ou_user" and msg.text == "list files"
        adapter.send(msg.reply, "回答", "reply-id")
        assert requests[-1].url.path == "/open-apis/im/v1/messages/om_1/reply"
        assert requests[-1].headers["Authorization"] == "Bearer tenant-token"
        assert json.loads(requests[-1].content)["uuid"] == "reply-id"
        assert json.loads(json.loads(requests[-1].content)["content"]) == {"text": "回答"}
        challenge = {"type": "url_verification", "token": "verify-secret", "challenge": "hello"}
        assert adapter.receive({}, encrypted_feishu(challenge)[1]) == {"challenge": "hello"}
        challenge["token"] = "bad"
        with pytest.raises(ChannelDenied):
            adapter.receive({}, encrypted_feishu(challenge)[1])
        with pytest.raises(ChannelDenied):
            adapter.receive({**headers, "x-lark-signature": "wrong"}, body)
        with pytest.raises(ChannelDenied):
            adapter.receive({**headers, "x-lark-request-timestamp": "0"}, body)
        with pytest.raises(ValueError):
            adapter.receive(headers, b'{"encrypt":"invalid"}')


def test_dingtalk_signed_text_reply_and_destination_restriction(credentials):
    stamp, secret = str(int(time.time() * 1000)), credentials["APP_SECRET"]
    headers = {
        "timestamp": stamp,
        "sign": base64.b64encode(
            hmac.digest(secret.encode(), (stamp + "\n" + secret).encode(), "sha256")
        ).decode(),
    }
    event = {
        "msgId": "message1",
        "senderId": "user1",
        "conversationId": "chat1",
        "conversationType": "1",
        "msgtype": "text",
        "text": {"content": "hi"},
        "sessionWebhook": "https://oapi.dingtalk.com/robot/sendBySession?session=secret",
        "sessionWebhookExpiredTime": int((time.time() + 300) * 1000),
    }
    sent = []

    def respond(req):
        sent.append(req)
        return httpx.Response(200, json={"errcode": 0})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = DingTalkAdapter({"app_secret_env": "APP_SECRET"}, client)
        msg = adapter.receive(headers, json.dumps(event).encode())
        adapter.send(msg.reply, "Hello", "id")
        assert json.loads(sent[0].content)["text"]["content"] == "Hello"
        with pytest.raises(ChannelDenied):
            adapter.receive({**headers, "sign": "wrong"}, json.dumps(event).encode())
        with pytest.raises(ChannelDenied):
            adapter.receive({**headers, "timestamp": "0"}, json.dumps(event).encode())
        for bad in [
            "https://localhost/robot/sendBySession?session=x",
            "https://oapi.dingtalk.com.evil/robot/sendBySession?session=x",
            "https://oapi.dingtalk.com@localhost/robot/sendBySession?session=x",
            "http://oapi.dingtalk.com/robot/sendBySession?session=x",
        ]:
            with pytest.raises(ValueError):
                adapter.receive(headers, json.dumps({**event, "sessionWebhook": bad}).encode())
        with pytest.raises(DeliveryRejected, match="expired"):
            adapter.send({**msg.reply, "expires": 0}, "hello", "id")
        assert len(sent) == 1


def test_telegram_auth_update_filter_and_thread_reply(credentials):
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        adapter = TelegramAdapter(telegram_config(), client)
        headers = {"x-telegram-bot-api-secret-token": "hook-secret"}
        data = telegram_event()
        data["message"]["message_thread_id"] = 3
        msg = adapter.receive(headers, json.dumps(data).encode())
        adapter.send(msg.reply, "hello", "id")
        assert json.loads(sent[0].content)["message_thread_id"] == 3
        assert json.loads(sent[0].content)["reply_parameters"] == {"message_id": 2}
        assert adapter.receive(headers, b'{"update_id":2}') is None
        with pytest.raises(ChannelDenied):
            adapter.receive({}, json.dumps(data).encode())


def test_multireplica_duplicate_receipt_creates_one_task_and_one_reply(
    store, project, tmp_path, credentials
):
    cfg = Config(
        project=project, workspace=tmp_path, demo=True, channels={"telegram": telegram_config()}
    )
    app = AgentApp(cfg, store=store, initialize=False)
    sent = []

    def respond(req):
        sent.append(req)
        return httpx.Response(200, json={"ok": True})

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        hubs = [ChannelHub(app, client=client) for _ in range(4)]

        def receive(index):
            return hubs[index % 4].receive(
                "telegram",
                {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"},
                json.dumps(telegram_event()).encode(),
            )

        with ThreadPoolExecutor(max_workers=4) as pool:
            assert all(r == {"ok": True} for r in pool.map(receive, range(12)))
        assert len(app.state.list_tasks()) == 1
        assert app.work_once()["status"] == "succeeded"
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda hub: hub.deliver_once(), hubs))
        assert len(sent) == 1
        assert hubs[0].state.list()[0]["status"] == "sent"
        public = hubs[0].state.list()[0]
        assert "reply" not in public and "token" not in public
        with pytest.raises(ChannelDenied):
            hubs[0].receive(
                "telegram",
                {"X-Telegram-Bot-Api-Secret-Token": "hook-secret"},
                json.dumps(telegram_event(2, group=True)).encode(),
            )
        assert len(app.state.list_tasks()) == 1
        cfg.channels["telegram"]["allowed_senders"] = []
        with pytest.raises(ChannelDenied):
            receive(0)
        assert "hook-secret" not in json.dumps(hubs[0].status())


def test_uncertain_reply_requires_manual_retry_and_never_reruns_task(
    store, project, tmp_path, credentials
):
    app = AgentApp(
        Config(
            project=project, workspace=tmp_path, demo=True, channels={"telegram": telegram_config()}
        ),
        store=store,
        initialize=False,
    )
    attempts = []

    def timeout(req):
        attempts.append(req)
        raise httpx.ReadTimeout("secret provider token in URL", request=req)

    with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
        hub = ChannelHub(app, client=client)
        hub.receive(
            "telegram",
            {"x-telegram-bot-api-secret-token": "hook-secret"},
            json.dumps(telegram_event()).encode(),
        )
        app.work_once()
        assert hub.deliver_once()["status"] == "uncertain"
        assert hub.deliver_once() is None
        row = hub.state.list()[0]
        assert row["error"] == "ReadTimeout"
        assert hub.state.retry_reply(row["id"])
        assert hub.deliver_once()["status"] == "uncertain"
        assert len(attempts) == 2 and app.state.task(row["task_id"])["attempts"] == 1


def test_crashed_reply_worker_fencing_and_owner_scope(store, project):
    state = RuntimeStore(store, project, "alice")
    inbox = ChannelStore(state)
    message = IncomingMessage("event", "sender", "chat", "Hello", {"secret": "private"})
    task = inbox.accept("custom", message)
    with store.engine.begin() as conn:
        conn.execute(
            update(tasks).where(tasks.c.id == task).values(status="succeeded", result="Hi")
        )
    row = inbox.claim_reply()
    assert row["status"] == "sending"
    with store.engine.begin() as conn:
        conn.execute(update(deliveries).where(deliveries.c.id == row["id"]).values(lease_until=0))
    assert inbox.claim_reply() is None
    assert inbox.list()[0]["status"] == "uncertain"
    with pytest.raises(LeaseLost):
        inbox.finish_reply(row["id"], row["token"], "sent")
    bob = ChannelStore(RuntimeStore(store, project, "bob"))
    assert bob.list() == [] and not bob.retry_reply(row["id"])
    assert bob.accept("custom", message) != task
    with store.engine.connect() as conn:
        assert (
            len(conn.execute(select(deliveries).where(deliveries.c.project == project)).all()) == 2
        )


def test_configuration_fails_closed_and_bounds_unicode_reply(credentials):
    Config(channels={"telegram": telegram_config()}).validate()
    with pytest.raises(ValueError):
        Config(channels={"telegram": {"bot_token": "raw-secret"}}).validate()
    with pytest.raises(ValueError):
        Config(channels={"bad/name": telegram_config()}).validate()
    with pytest.raises(ValueError):
        Config(channels={"telegram": {**telegram_config(), "enabled": "yes"}}).validate()
    assert len(reply_text("😀" * 4000).encode("utf-16-le")) < 8192
