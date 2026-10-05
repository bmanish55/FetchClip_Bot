# FetchClip (Vercel edition)

Telegram bot: send a link, get the video (low quality, <= 48 MB) or just the audio.

## Files you need (nothing else)

```
app.py            <- the whole bot + web app (Vercel loads the variable `app`)
pyproject.toml    <- dependencies
vercel.json       <- gives the function 300 s
set_webhook.py    <- run once on your PC
.env.example
```

**Delete these from your old project:** `api/telegram.py`, `requirements.txt`.

## Deploy

1. Push this folder to a GitHub repo (the `.gitignore` keeps secrets out) and import it in Vercel.
2. In Vercel > Project > Settings > Environment Variables add:
   - `BOT_TOKEN`
   - `TELEGRAM_WEBHOOK_SECRET` (any long random text, 32+ characters, letters/numbers/`_`/`-`)
3. Deploy. Open `https://YOUR-PROJECT.vercel.app/` - you should see
   `{"ok":true,"bot_token_set":true,"webhook_secret_set":true,"ffmpeg":true}`.
4. On your PC: copy `.env.example` to `.env`, fill the same two values, then run

   `python set_webhook.py https://YOUR-PROJECT.vercel.app`

5. Send `/start` to the bot.

If you change the environment variables later, redeploy (Vercel only applies them to new deployments).

## If something goes wrong

- `/` shows a false value -> that environment variable is missing in Vercel.
- Webhook set but bot silent -> Vercel > Logs, and `getWebhookInfo` (printed by set_webhook.py) shows `last_error_message`.
- Add `INLINE_PROCESSING=1` in Vercel to make the function finish its work before replying to Telegram (easier to debug in Logs). Remove it afterwards.
- `OWNER_ID=<your telegram id>` makes the bot show you the raw yt-dlp error in chat.
