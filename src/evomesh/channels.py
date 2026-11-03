"""Text channel adapters. Credentials stay in environment variables, never API responses."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


class ChannelDenied(ValueError):
    """Invalid authentication or a sender outside the configured trust boundary."""


class DeliveryRejected(RuntimeError):
    """The provider explicitly rejected a reply (or it was rejected before sending)."""


@dataclass(frozen=True)
class IncomingMessage:
    event_id: str
    sender: str
    conversation: str
    text: str
    reply: dict
    group: bool = False


class ChannelAdapter(Protocol):
    def receive(self, headers: dict[str, str], body: bytes) -> IncomingMessage | dict | None: ...

    def send(self, target: dict, text: str, identifier: str) -> None: ...


CREDENTIALS = {
    "feishu": ("app_id_env", "app_secret_env", "verification_token_env", "encrypt_key_env"),
    "dingtalk": ("app_secret_env",),
    "telegram": ("bot_token_env", "webhook_secret_env"),
}
COMMON = {"kind", "enabled", "allowed_senders", "allowed_groups"}


def validate_channels(channels):
    if not isinstance(channels, dict):
        raise ValueError("channels must be a table of named accounts")
    for name, cfg in channels.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name)
            or not isinstance(cfg, dict)
        ):
            raise ValueError("Channel names must be lowercase identifiers, at most 40 characters")
        kind = cfg.get("kind", name)
        if kind not in CREDENTIALS:
            raise ValueError("Built-in channel kind must be feishu, dingtalk or telegram")
        allowed = COMMON | set(CREDENTIALS[kind]) | ({"region"} if kind == "feishu" else set())
        if set(cfg) - allowed:
            raise ValueError(f"Unknown channel options for {name}")
        if type(cfg.get("enabled", True)) is not bool:
            raise ValueError("Channel enabled must be a boolean")
        for key in ("allowed_senders", "allowed_groups"):
            values = cfg.get(key, [])
            if not isinstance(values, list) or any(
                not isinstance(v, str) or not v or len(v) > 256 for v in values
            ):
                raise ValueError(f"{key} must contain string identifiers")
        for key in CREDENTIALS[kind]:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]*", cfg.get(key, "")):
                raise ValueError(f"{name}.{key} must name an environment variable")
        if cfg.get("region", "feishu") not in {"feishu", "lark"}:
            raise ValueError("region must be feishu or lark")


def equal(actual, expected):
    return (
        isinstance(actual, str)
        and isinstance(expected, str)
        and bool(expected)
        and hmac.compare_digest(actual.encode(), expected.encode())
    )


def fresh(value, scale=1, window=300):
    try:
        return abs(time.time() - int(value) / scale) <= window
    except (TypeError, ValueError, OverflowError):
        return False


def object_body(body):
    data = json.loads(body)
    if not isinstance(data, dict):
        raise ValueError("Expected JSON object")
    return data


def ding_url(value):
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.netloc != "oapi.dingtalk.com"
        or url.path != "/robot/sendBySession"
        or url.fragment
        or not parse_qs(url.query).get("session")
    ):
        raise ValueError("Invalid DingTalk session webhook")
    return value


class Adapter:
    def __init__(self, config, client):
        self.config, self.client = config, client

    def secret(self, key):
        value = os.getenv(self.config[key], "")
        if not value:
            raise ChannelDenied("Channel credentials unavailable")
        return value

    def post(self, url, **kwargs):
        response = self.client.post(url, **kwargs)
        # 5xx/timeouts can follow successful delivery: caller marks those uncertain.
        if 400 <= response.status_code < 500:
            raise DeliveryRejected("Provider rejected request")
        response.raise_for_status()
        return response.json()


class FeishuAdapter(Adapter):
    @property
    def base(self):
        return (
            "https://open.larksuite.com/open-apis"
            if self.config.get("region") == "lark"
            else "https://open.feishu.cn/open-apis"
        )

    def receive(self, headers, body):
        key = self.secret("encrypt_key_env")
        data = object_body(body)
        if "encrypt" in data:
            encrypted = base64.b64decode(data["encrypt"], validate=True)
            decryptor = Cipher(
                algorithms.AES(hashlib.sha256(key.encode()).digest()), modes.CBC(encrypted[:16])
            ).decryptor()
            padded = decryptor.update(encrypted[16:]) + decryptor.finalize()
            unpadder = padding.PKCS7(128).unpadder()
            data = object_body(unpadder.update(padded) + unpadder.finalize())
        header = data.get("header", {})
        if not equal(
            data.get("token") or header.get("token"), self.secret("verification_token_env")
        ):
            raise ChannelDenied("Invalid verification token")
        # Feishu's URL verification handshake is authenticated by the encrypted token.
        if data.get("type") == "url_verification":
            challenge = data.get("challenge")
            if not isinstance(challenge, str) or len(challenge) > 2048:
                raise ValueError("Invalid challenge")
            return {"challenge": challenge}
        stamp, nonce = headers.get("x-lark-request-timestamp"), headers.get("x-lark-request-nonce")
        if not fresh(stamp) or not isinstance(nonce, str) or not nonce:
            raise ChannelDenied("Missing or expired signature")
        signature = hashlib.sha256((stamp + nonce + key).encode() + body).hexdigest()
        if not equal(headers.get("x-lark-signature"), signature):
            raise ChannelDenied("Invalid signature")
        if header.get("app_id") != self.secret("app_id_env"):
            raise ChannelDenied("Wrong Feishu application")
        if header.get("event_type") != "im.message.receive_v1":
            return None
        event = data["event"]
        message, sender = event["message"], event["sender"]
        if sender.get("sender_type") != "user" or message.get("message_type") != "text":
            return None
        content = object_body(message["content"])["text"]
        for mention in message.get("mentions", []):
            content = content.replace(mention["key"], "").strip()
        return IncomingMessage(
            message["message_id"],
            sender["sender_id"]["open_id"],
            message["chat_id"],
            content,
            {"message_id": message["message_id"]},
            message.get("chat_type") != "p2p",
        )

    def send(self, target, text, identifier):
        auth = self.post(
            self.base + "/auth/v3/tenant_access_token/internal",
            json={"app_id": self.secret("app_id_env"), "app_secret": self.secret("app_secret_env")},
        )
        if auth.get("code") != 0 or not auth.get("tenant_access_token"):
            raise DeliveryRejected("Feishu authentication rejected")
        result = self.post(
            self.base + "/im/v1/messages/" + quote(target["message_id"], safe="") + "/reply",
            headers={"Authorization": "Bearer " + auth["tenant_access_token"]},
            json={
                "msg_type": "text",
                "content": json.dumps({"text": text}),
                "uuid": identifier[:50],
            },
        )
        if result.get("code") != 0:
            raise DeliveryRejected("Feishu reply rejected")


class DingTalkAdapter(Adapter):
    def receive(self, headers, body):
        stamp, secret = headers.get("timestamp"), self.secret("app_secret_env")
        if not fresh(stamp, scale=1000, window=3600):
            raise ChannelDenied("Missing or expired signature")
        signature = base64.b64encode(
            hmac.digest(secret.encode(), (stamp + "\n" + secret).encode(), "sha256")
        ).decode()
        if not equal(headers.get("sign"), signature):
            raise ChannelDenied("Invalid signature")
        data = object_body(body)
        if data.get("msgtype") != "text":
            return None
        return IncomingMessage(
            data["msgId"],
            data["senderId"],
            data["conversationId"],
            data["text"]["content"].strip(),
            {
                "webhook": ding_url(data["sessionWebhook"]),
                "expires": int(data["sessionWebhookExpiredTime"]) / 1000,
            },
            data.get("conversationType") != "1",
        )

    def send(self, target, text, identifier):
        if target["expires"] <= time.time():
            raise DeliveryRejected("DingTalk session webhook expired")
        result = self.post(
            ding_url(target["webhook"]), json={"msgtype": "text", "text": {"content": text}}
        )
        if result.get("errcode") != 0:
            raise DeliveryRejected("DingTalk reply rejected")


class TelegramAdapter(Adapter):
    def receive(self, headers, body):
        if not equal(
            headers.get("x-telegram-bot-api-secret-token"), self.secret("webhook_secret_env")
        ):
            raise ChannelDenied("Invalid webhook secret")
        data = object_body(body)
        message = data.get("message", {})
        if not message.get("text") or message.get("from", {}).get("is_bot", True):
            return None
        return IncomingMessage(
            str(data["update_id"]),
            str(message["from"]["id"]),
            str(message["chat"]["id"]),
            message["text"],
            {
                "chat_id": message["chat"]["id"],
                "message_id": message["message_id"],
                "thread_id": message.get("message_thread_id"),
            },
            message["chat"]["type"] != "private",
        )

    def send(self, target, text, identifier):
        payload = {
            "chat_id": target["chat_id"],
            "text": text,
            "reply_parameters": {"message_id": target["message_id"]},
        }
        if target.get("thread_id") is not None:
            payload["message_thread_id"] = target["thread_id"]
        result = self.post(
            "https://api.telegram.org/bot" + self.secret("bot_token_env") + "/sendMessage",
            json=payload,
        )
        if result.get("ok") is not True:
            raise DeliveryRejected("Telegram reply rejected")


BUILTINS = {"feishu": FeishuAdapter, "dingtalk": DingTalkAdapter, "telegram": TelegramAdapter}


def reply_text(text):
    """Leave room within Telegram UTF-16 and other platforms' text limits."""
    encoded = text.encode("utf-16-le")
    if len(encoded) <= 6000:
        return text
    return encoded[:5900].decode("utf-16-le", errors="ignore") + "\n[…完整结果见 EvoMesh 控制台]"


class ChannelHub:
    """Shared durable inbox/outbox; register() is the framework's custom adapter entry."""

    def __init__(self, app, *, client=None):
        from .channel_store import ChannelStore

        self.app, self.state = app, ChannelStore(app.state)
        self._owns_client = client is None
        self.client = client or httpx.Client(timeout=15, trust_env=False, follow_redirects=False)
        self.adapters, self.policies = {}, {}
        for name, cfg in app.config.channels.items():
            kind = cfg.get("kind", name)
            self.register(name, BUILTINS[kind](cfg, self.client), cfg)

    def register(self, name: str, adapter: ChannelAdapter, policy: dict):
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name) or name in self.adapters:
            raise ValueError("Invalid or duplicate channel name")
        self.adapters[name], self.policies[name] = adapter, policy

    def receive(self, name, headers, body):
        policy = self.policies.get(name, {})
        if name not in self.adapters or not policy.get("enabled", True):
            raise ChannelDenied("Channel disabled or unavailable")
        message = self.adapters[name].receive({k.lower(): v for k, v in headers.items()}, body)
        if isinstance(message, dict):
            return message
        if message is None:
            return {"ok": True}
        if not isinstance(message.text, str):
            raise ValueError("Expected message text")
        if not message.text.strip():
            return {"ok": True}
        if message.sender not in policy.get("allowed_senders", []):
            raise ChannelDenied("Sender not allowed")
        if message.group and message.conversation not in policy.get("allowed_groups", []):
            raise ChannelDenied("Group not allowed")
        self.state.accept(name, message)
        return {"ok": True}

    def status(self):
        result = []
        for name, cfg in self.policies.items():
            kind = cfg.get("kind", name)
            missing = [cfg[k] for k in CREDENTIALS.get(kind, ()) if not os.getenv(cfg[k])]
            result.append(
                {
                    "name": name,
                    "kind": kind,
                    "enabled": cfg.get("enabled", True),
                    "ready": not missing and bool(cfg.get("allowed_senders")),
                    "missing_env": missing,
                    "allowed_senders": cfg.get("allowed_senders", []),
                    "allowed_groups": cfg.get("allowed_groups", []),
                    "webhook_path": f"/webhooks/{name}",
                }
            )
        return result

    def deliver_once(self):
        row = self.state.claim_reply()
        if row is None:
            return None
        status, error = "sent", None
        try:
            if not self.policies.get(row["channel"], {}).get("enabled", True):
                raise DeliveryRejected("Channel disabled")
            task = self.app.state.task(row["task_id"])
            text = (
                task["result"]
                if task["status"] == "succeeded"
                else (f"任务未完成（{task['status']}）。请在 EvoMesh 控制台查看。")
            )
            self.adapters[row["channel"]].send(
                json.loads(row["reply"]), reply_text(text or "任务已完成。"), row["id"]
            )
        except DeliveryRejected as exc:
            status, error = "failed", str(exc)
        except Exception as exc:
            # Never put provider exception messages/URLs (tokens!) into durable logs.
            status, error = "uncertain", type(exc).__name__
        self.state.finish_reply(row["id"], row["token"], status, error)
        return {"id": row["id"], "status": status}

    def close(self):
        if self._owns_client:
            self.client.close()
