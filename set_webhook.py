"""Run this ONCE from your own PC after every deploy URL change.

    python set_webhook.py https://YOUR-PROJECT.vercel.app

Reads BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from your local .env file
(copy .env.example to .env and fill both in; never commit .env).
"""
import json
import os
import sys
import urllib.parse
import urllib.request


def load_env(path=".env"):
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())


def call(token, method, payload):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/{method}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def main():
    if len(sys.argv) != 2 or not sys.argv[1].startswith("https://"):
        sys.exit("Usage: python set_webhook.py https://YOUR-PROJECT.vercel.app")
    load_env()
    token, secret = os.getenv("BOT_TOKEN"), os.getenv("TELEGRAM_WEBHOOK_SECRET")
    if not token or not secret:
        sys.exit("Put BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET in your .env file first.")

    url = sys.argv[1].rstrip("/") + "/api/webhook"
    print("setWebhook   ->", call(token, "setWebhook", {
        "url": url,
        "secret_token": secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": True,
    }))
    print("setCommands  ->", call(token, "setMyCommands", {
        "commands": [{"command": "start", "description": "How to use this bot"}]
    }))
    info = call(token, "getWebhookInfo", {})["result"]
    print("\nWebhook is now:", info.get("url"))
    print("Pending updates:", info.get("pending_update_count"), "| last error:", info.get("last_error_message"))


if __name__ == "__main__":
    main()
