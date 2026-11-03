# Messaging channels

Feishu/Lark, DingTalk and Telegram support incoming **text** messages and replies through the same durable task queue used by the CLI, web console and framework. No live platform credentials are shipped. Protocol/transport fixtures and HTTP end-to-end tests verify behavior; operators must perform real application setup and send a test message after deployment.

## Configure and run

Merge the relevant sections from [evomesh.channels.example.toml](../evomesh.channels.example.toml) into `evomesh.toml`. Set secrets in the environment variables named by `*_env`. Never put secret values in TOML, Git or browser forms. The CLI does not load `.env` automatically.

```bash
evomesh --config /path/to/evomesh.toml init
evomesh --config /path/to/evomesh.toml console
# Or: serve + task-worker, using the same config and credential environment.
```

Expose the server through a public HTTPS gateway. Each account has `/webhooks/{account-name}`; multiple accounts of the same kind are supported by choosing names such as `work_feishu` and setting `kind="feishu"`. The console's Channels page shows configured paths and missing environment-variable names. A ready label confirms local config only; it does not contact the platform.

`allowed_senders` is required for normal message acceptance: an empty list denies everyone. Private chats from these senders are accepted. Group messages additionally require their conversation ID in `allowed_groups`; enabling a sender never implicitly enables a group. Replies to group messages are visible to that group. No wildcards or allow-everyone setting are provided.

All accounts in one `AgentApp` share its configured owner, memory, workspace and tools. Conversations use a stable hash of account + chat + sender; they are separate histories, **not separate authorization boundaries**. Use separate owner/workspace/service deployments for unrelated people. Channel callers cannot select an arbitrary owner. Do not grant untrusted people access to a personal assistant's file/terminal/MCP tools.

## Feishu / international Lark

1. Create an enterprise self-built application and enable its bot capability.
2. Subscribe to `im.message.receive_v1`; grant appropriate message-receive permissions and `im:message:send_as_bot`. Publish/install the application as required by your organization.
3. Set `FEISHU_APP_ID`, `FEISHU_APP_SECRET`, `FEISHU_VERIFICATION_TOKEN`, `FEISHU_ENCRYPT_KEY` in the server and worker environment. Configure matching Verification Token and Encrypt Key in the platform event settings.
4. Set the HTTP event URL to `https://your-domain/webhooks/feishu`. The authenticated URL verification challenge is handled immediately.
5. Add your user's `open_id` to `allowed_senders`. Add group `chat_id` values only when you want group use. For international Lark, set `region="lark"`.

Normal event callbacks validate the token, app ID, timestamp/nonce and SHA-256 signature over the original body. AES-CBC encrypted event bodies are supported. Non-user/non-text events are acknowledged without task creation. Replies use a tenant token and reply-to-message API with a stable UUID; full message text is available in the task console.

Protocol references: [event subscription](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/event-subscription-guide/event-subscriptions), [official SDK callback implementation](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/event/dispatcher_handler.py), [reply request fields](https://github.com/larksuite/oapi-sdk-python/blob/v2_main/lark_oapi/api/im/v1/model/reply_message_request_body.py).

## DingTalk

1. Create an enterprise internal application robot; choose **HTTP** message reception (not a notification-only custom robot or Stream mode).
2. Set its callback URL to `https://your-domain/webhooks/dingtalk`.
3. Export `DINGTALK_APP_SECRET` using the application's AppSecret.
4. Populate `allowed_senders` with callback `senderId` values, not `senderStaffId`; group allowlists use `conversationId`.

The adapter verifies `timestamp` and the Base64 HMAC-SHA256 `sign` header according to DingTalk's HTTP robot protocol. The timestamp window is one hour. This platform signature covers the timestamp/secret, not the body; HTTPS at the trusted gateway is necessary. Replies use the authenticated event's `sessionWebhook`, restricted to the exact HTTPS DingTalk `sendBySession` endpoint. Expired session webhooks fail without sending. Queue/task delays beyond the provider's webhook lifetime require a fresh incoming message or reading the result in the console.

Protocol references: [receive messages](https://open.dingtalk.com/document/orgapp/receive-message), [reply/send messages](https://open.dingtalk.com/document/orgapp/robot-reply-and-send-messages).

## Telegram

1. Create a bot using BotFather; export its token as `TELEGRAM_BOT_TOKEN`.
2. Set `TELEGRAM_WEBHOOK_SECRET` to a random 1–256 character string using Telegram's permitted characters (`A-Z`, `a-z`, `0-9`, `_`, `-`).
3. Call Telegram's `setWebhook` with the public URL, matching secret token and `allowed_updates=["message"]`. For example, in a trusted local Python shell:

```python
import os
import httpx

response = httpx.post(
    f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/setWebhook",
    json={
        "url": "https://your-domain/webhooks/telegram",
        "secret_token": os.environ["TELEGRAM_WEBHOOK_SECRET"],
        "allowed_updates": ["message"],
    },
    timeout=15,
)
print(response.json().get("ok"))
```

4. Add numeric sender user IDs as **strings** to `allowed_senders`. Groups similarly use string chat IDs. Configure BotFather group privacy settings according to the events you intend to receive.

Every callback requires `X-Telegram-Bot-Api-Secret-Token`. Bot-originated messages, edited messages and non-text updates are ignored. The adapter preserves the original chat, message reply and optional topic/thread. See the [official Bot API](https://core.telegram.org/bots/api#setwebhook).

## Durability and recovery

Receipt and task insertion happen in one SQL transaction. A platform event/message ID is scoped by project/owner/account and deduplicated across replicas; retries acknowledge the same existing task. Session identity includes the sender. Different chat messages can be queued concurrently; this alpha does not promise strict global/channel ordering.

The work process executes the task and drains a SQL reply outbox. Only one replica wins a reply claim. Statuses are `waiting → sending → sent`, or `failed`/`uncertain`. Explicit provider rejections are failed; transport ambiguity or an expired reply-worker lease is uncertain. There is **no automatic replay** of an uncertain send. Even with provider deduplication where available, exactly-once external message delivery is not promised.

Use Channels → “重试回复” after checking the platform. This retries only the reply, never the model/tool task. Manual re-execution of a task is separate and does not reset an already-sent reply receipt. Outputs exceeding a conservative platform text limit are truncated with a notice; the full answer remains in the console. Failed tasks produce a generic failure-status reply, not raw exception details.

The private ledger contains callback reply targets, including DingTalk's temporary webhook URL. Protect SQL backups/access. API delivery listings omit targets and claim tokens. Platform HTTP exception strings are not exposed as logs; worker failures retain exception types. Keep clocks synchronized. Configuration must match across replicas; disabling an account prevents acceptance and sending but does not erase queued tasks or their previous effects.

Unsupported in v0.3: attachments, images/audio/video, cards, streaming text, long connections/Stream mode, OAuth onboarding, proactive pushes or schedule-to-channel routing. Custom adapters can add these without changing the agent loop.

## Framework extension

Implement `ChannelAdapter.receive(headers, body)` and `send(target, text, identifier)`. `receive` must authenticate the platform request and normalize it to `IncomingMessage`; return a dictionary for a verification response or `None` for ignored events. The hub applies allowlists before atomically enqueueing. `send` should use bounded timeouts, provider idempotency when available, and raise `DeliveryRejected` only for a definite rejection before/at the provider. Unknown failures remain uncertain.

```python
from evomesh.channels import ChannelHub
from evomesh.server import create_server

hub = ChannelHub(app)
hub.register(
    "company",
    MyCompanyAdapter(),
    {
        "allowed_senders": ["trusted-user"],
        "allowed_groups": [],
    },
)
server = create_server(
    "127.0.0.1",
    8765,
    server_token,
    app.agent,
    app=app,
    channels=hub,
    with_worker=True,
)
try:
    server.serve_forever()
finally:
    server.server_close()
    hub.close()  # Caller owns a hub supplied to create_server().
```

See [runnable offline adapter example](../examples/custom_channel.py) and [API reference](api.md). Custom adapters are registered in Python; TOML supports only the built-in kinds.
