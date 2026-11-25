# AI call auditor: sales stages and tone of voice checked by a bot

A Telegram bot for a sales lead. A curator sends in a recording of a manager's call with a prospective guest; the bot transcribes it and returns a short review: which stages of the conversation were skipped and where the manager broke the brand's tone. Demo: [@curator_audit_bot](https://t.me/curator_audit_bot).

This is a practice project. The retreat, "Know Yourself" («Познай себя»), is a training scenario of a retreat center, not a real organization. Calls and reports work in Russian and English. Case study: [tatidzufri.com/en/case/dialog-auditor](https://tatidzufri.com/en/case/dialog-auditor).

![The bot's report with skipped stages and tone violations](demo/audit-output-with-tone-violations.png)

## About the project

The bot checks every call against the same 11-point checklist, so the score doesn't depend on who listened and in what mood. Every check is saved to the database, and the history shows which points managers skip most often.

## The task

In retreat sales a manager has to lead the prospect through a clear sequence: warm contact, finding out what she needs, the program and its values, the price, handling doubts, the next step. Listening to and reviewing each call by hand is slow, and scores end up subjective and different from one reviewer to another.

## How it works

1. **A recording in the chat.** The curator sends the bot an audio or video file of the call.
2. **Transcription.** AssemblyAI turns speech into text. It handles long recordings and two speakers.
3. **Checklist audit.** An OpenAI model gets the transcript and the instruction from `prompt.md` and checks 11 points: greeting and rapport, finding the need, the retreat's values, program and format, whether the retreat suits this guest, the outcome after the retreat, handling objections, price and terms, the next step, tone without pressure, the guest's decision.
4. **A report in a fixed format.** Skipped points first, then brand tone violations with an exact quote from the manager. The wording is predefined, so reports on different managers are easy to compare. If all 11 points are covered and the tone holds, the bot replies: "A well-run conversation!"
5. **History in the database.** Who sent it, which file, the transcript, the final audit, success or error. All of it goes to PostgreSQL.

The checklist and tone rules live in `prompt.md`. Changing the audit logic doesn't require touching the code.

**Two languages.** A call can be in Russian or English. AssemblyAI detects the language of the recording (it expects `ru` or `en` and falls back to Russian when unsure), and the audit is written in the call's language from `prompt.md` or `prompt_en.md`. The English version has its own list of pressure words: "hurry", "discount", "today only", "only N spots left". The bot's service messages follow the user's language: from Telegram settings, the `?start=en` link or the `/language` command. A sample English call: `demo/audio_test_en.mp3` (generated with OpenAI TTS).

## Results

- Calls can be checked against the checklist without listening to them.
- Each manager gets specific feedback on their own conversation.
- Every manager is scored by the same checklist.
- The check history shows which points slip and how quality changes over time.

## Tech stack

| Technology | What it's for |
|---|---|
| **Python** | Bot language, 3.12 in the container |
| **python-telegram-bot** | Receiving recordings, downloading files, replies and progress messages in the chat |
| **AssemblyAI** | Speech recognition for audio and video |
| **OpenAI API** | Audits the transcript against the checklist. The model is set in `.env`, `gpt-4.1-mini` by default |
| **prompt.md / prompt_en.md** | The curator role, 11 criteria, tone rules and report templates |
| **PostgreSQL + psycopg2** | Check history for analytics |
| **Docker Compose** | Starts the bot and the database with one command |

## Repository structure

- `bot.py`: the whole bot, from receiving a file to saving to the database.
- `prompt.md`, `prompt_en.md`: instructions for the model in Russian and English.
- `demo/`: a sample report and test recordings `audio_test.mp3` and `audio_test_en.mp3`.

## Running it

```bash
cp .env.example .env      # TELEGRAM_BOT_TOKEN, ASSEMBLYAI_API_KEY, OPENAI_API_KEY
docker compose up -d --build
```

Without Docker you need your own PostgreSQL; the connection string goes in `DATABASE_URL`:

```bash
pip install -r requirements.txt
python bot.py
```

You can try the bot on `demo/audio_test.mp3` or `demo/audio_test_en.mp3`.

## Author

Tetiana Dziubas, AI automation engineer. I build Telegram bots, RAG assistants and LLM agents that take routine work off small teams. Case studies and contact: [tatidzufri.com/en](https://tatidzufri.com/en/).
