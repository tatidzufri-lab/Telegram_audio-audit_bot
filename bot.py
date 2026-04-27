import asyncio
import logging
import mimetypes
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import psycopg2
import requests
from dotenv import load_dotenv
from openai import OpenAI
from telegram import Message, Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

ASSEMBLYAI_UPLOAD_URL = "https://api.assemblyai.com/v2/upload"
ASSEMBLYAI_TRANSCRIPT_URL = "https://api.assemblyai.com/v2/transcript"
ASSEMBLYAI_SPEECH_MODELS = ["universal-3-pro", "universal-2"]
ASSEMBLYAI_LANGUAGE_CODE = "ru"
MESSAGE_CHUNK_SIZE = 3900
DB_CONNECTION_RETRIES = 30
DB_CONNECTION_RETRY_DELAY_SECONDS = 2


class AssemblyAIError(RuntimeError):
    """Raised when AssemblyAI returns an error state."""


@dataclass
class Settings:
    telegram_bot_token: str
    assemblyai_api_key: str
    openai_api_key: str
    database_url: str
    openai_model: str = "gpt-4.1-mini"

    @classmethod
    def from_env(cls) -> "Settings":
        telegram_bot_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        assemblyai_api_key = os.getenv("ASSEMBLYAI_API_KEY", "").strip()
        openai_api_key = os.getenv("OPENAI_API_KEY", "").strip()
        database_url = os.getenv("DATABASE_URL", "").strip()
        openai_model = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip() or "gpt-4.1-mini"

        missing = []
        if not telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not assemblyai_api_key:
            missing.append("ASSEMBLYAI_API_KEY")
        if not openai_api_key:
            missing.append("OPENAI_API_KEY")
        if not database_url:
            missing.append("DATABASE_URL")
        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            telegram_bot_token=telegram_bot_token,
            assemblyai_api_key=assemblyai_api_key,
            openai_api_key=openai_api_key,
            database_url=database_url,
            openai_model=openai_model,
        )


def init_database(database_url: str) -> None:
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS video_audits (
                    id BIGSERIAL PRIMARY KEY,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    chat_id BIGINT NOT NULL,
                    user_id BIGINT NULL,
                    username TEXT NULL,
                    file_id TEXT NOT NULL,
                    file_unique_id TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    transcript TEXT NULL,
                    analysis TEXT NULL,
                    status TEXT NOT NULL,
                    error_message TEXT NULL
                );
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_audits_chat_id
                ON video_audits (chat_id);
                """
            )
            cursor.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_video_audits_created_at
                ON video_audits (created_at DESC);
                """
            )
        connection.commit()


def init_database_with_retry(database_url: str) -> None:
    for attempt in range(1, DB_CONNECTION_RETRIES + 1):
        try:
            init_database(database_url)
            logging.info("Postgres is ready. Database schema initialized.")
            return
        except Exception as error:
            logging.warning(
                "Postgres is not ready yet (attempt %s/%s): %s",
                attempt,
                DB_CONNECTION_RETRIES,
                error,
            )
            time.sleep(DB_CONNECTION_RETRY_DELAY_SECONDS)

    raise RuntimeError("Could not initialize Postgres schema after multiple attempts.")


def save_audit_result(
    database_url: str,
    chat_id: int,
    user_id: Optional[int],
    username: Optional[str],
    file_id: str,
    file_unique_id: str,
    filename: str,
    transcript: Optional[str],
    analysis: Optional[str],
    status: str,
    error_message: Optional[str],
) -> None:
    with psycopg2.connect(database_url) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO video_audits (
                    chat_id,
                    user_id,
                    username,
                    file_id,
                    file_unique_id,
                    filename,
                    transcript,
                    analysis,
                    status,
                    error_message
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s);
                """,
                (
                    chat_id,
                    user_id,
                    username,
                    file_id,
                    file_unique_id,
                    filename,
                    transcript,
                    analysis,
                    status,
                    error_message,
                ),
            )
        connection.commit()


def load_audit_prompt(prompt_path: Path) -> str:
    if not prompt_path.exists():
        raise FileNotFoundError(f"Prompt file was not found: {prompt_path}")

    content = prompt_path.read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"Prompt file is empty: {prompt_path}")
    return content


def _raise_assemblyai_error(response: requests.Response, action: str) -> None:
    try:
        details = response.json()
    except ValueError:
        details = response.text
    raise AssemblyAIError(
        f"AssemblyAI {action} failed with status {response.status_code}: {details}"
    )


def upload_to_assemblyai(file_path: Path, api_key: str) -> str:
    headers = {"authorization": api_key}
    with file_path.open("rb") as audio_file:
        response = requests.post(
            ASSEMBLYAI_UPLOAD_URL,
            headers=headers,
            data=audio_file,
            timeout=1200,
        )
    if not response.ok:
        _raise_assemblyai_error(response, action="upload")

    upload_url = response.json().get("upload_url")
    if not upload_url:
        raise AssemblyAIError("AssemblyAI did not return upload_url.")
    return upload_url


def create_assemblyai_transcript(upload_url: str, api_key: str) -> str:
    headers = {"authorization": api_key, "content-type": "application/json"}
    payload = {
        "audio_url": upload_url,
        "speech_models": ASSEMBLYAI_SPEECH_MODELS,
        "language_code": ASSEMBLYAI_LANGUAGE_CODE,
    }
    response = requests.post(
        ASSEMBLYAI_TRANSCRIPT_URL,
        headers=headers,
        json=payload,
        timeout=60,
    )
    if not response.ok:
        _raise_assemblyai_error(response, action="transcript creation")

    transcript_id = response.json().get("id")
    if not transcript_id:
        raise AssemblyAIError("AssemblyAI did not return transcript id.")
    return transcript_id


def poll_assemblyai_transcript(transcript_id: str, api_key: str, timeout_seconds: int = 3600) -> str:
    headers = {"authorization": api_key}
    deadline = time.monotonic() + timeout_seconds
    endpoint = f"{ASSEMBLYAI_TRANSCRIPT_URL}/{transcript_id}"

    while time.monotonic() < deadline:
        response = requests.get(endpoint, headers=headers, timeout=60)
        if not response.ok:
            _raise_assemblyai_error(response, action="transcript polling")
        payload = response.json()
        status = payload.get("status")

        if status == "completed":
            text = (payload.get("text") or "").strip()
            if not text:
                raise AssemblyAIError("AssemblyAI returned empty transcript.")
            return text

        if status == "error":
            raise AssemblyAIError(payload.get("error") or "Unknown AssemblyAI error.")

        time.sleep(3)

    raise TimeoutError("Timed out while waiting for AssemblyAI transcription.")


def transcribe_video(file_path: Path, api_key: str) -> str:
    upload_url = upload_to_assemblyai(file_path=file_path, api_key=api_key)
    transcript_id = create_assemblyai_transcript(upload_url=upload_url, api_key=api_key)
    return poll_assemblyai_transcript(transcript_id=transcript_id, api_key=api_key)


def analyze_with_openai(transcript: str, audit_prompt: str, settings: Settings) -> str:
    client = OpenAI(api_key=settings.openai_api_key)

    response = client.chat.completions.create(
        model=settings.openai_model,
        temperature=0.1,
        messages=[
            {
                "role": "system",
                "content": (
                    "Ты — Куратор качества продаж ретрита «Познай себя». "
                    "Проводишь аудит диалога менеджера с потенциальной участницей строго по правилам "
                    "архитектурного документа из сообщения пользователя. "
                    "Следуй правилам и шаблонам формулировок дословно, не добавляй лишних пояснений, "
                    "не придумывай содержание сверх транскрипта."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"{audit_prompt}\n\n"
                    "Ниже транскрипт диалога менеджера с клиенткой для аудита.\n"
                    "<transcript>\n"
                    f"{transcript}\n"
                    "</transcript>"
                ),
            },
        ],
    )

    content = response.choices[0].message.content
    if isinstance(content, str):
        result = content.strip()
    else:
        result = str(content).strip()

    if not result:
        raise RuntimeError("OpenAI returned empty analysis.")
    return result


def extract_media_file_data(message: Message) -> Optional[tuple[str, str, str]]:
    if message.video:
        mime = message.video.mime_type or "video/mp4"
        extension = mimetypes.guess_extension(mime) or ".mp4"
        filename = f"{message.video.file_unique_id}{extension}"
        return message.video.file_id, message.video.file_unique_id, filename

    if message.document and (message.document.mime_type or "").startswith("video/"):
        original_name = message.document.file_name or ""
        extension = Path(original_name).suffix
        if not extension:
            extension = mimetypes.guess_extension(message.document.mime_type or "") or ".mp4"
        safe_name = re.sub(r"[^\w.\-]", "_", original_name) if original_name else ""
        filename = safe_name or f"{message.document.file_unique_id}{extension}"
        return message.document.file_id, message.document.file_unique_id, filename

    if message.audio:
        mime = message.audio.mime_type or "audio/mpeg"
        original_name = message.audio.file_name or ""
        extension = Path(original_name).suffix
        if not extension:
            extension = mimetypes.guess_extension(mime) or ".mp3"
        safe_name = re.sub(r"[^\w.\-]", "_", original_name) if original_name else ""
        filename = safe_name or f"{message.audio.file_unique_id}{extension}"
        return message.audio.file_id, message.audio.file_unique_id, filename

    if message.document and (message.document.mime_type or "").startswith("audio/"):
        original_name = message.document.file_name or ""
        extension = Path(original_name).suffix
        if not extension:
            extension = mimetypes.guess_extension(message.document.mime_type or "") or ".mp3"
        safe_name = re.sub(r"[^\w.\-]", "_", original_name) if original_name else ""
        filename = safe_name or f"{message.document.file_unique_id}{extension}"
        return message.document.file_id, message.document.file_unique_id, filename

    return None


def split_for_telegram(text: str, chunk_size: int = MESSAGE_CHUNK_SIZE) -> list[str]:
    if len(text) <= chunk_size:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= chunk_size:
            chunks.append(remaining)
            break

        split_at = remaining.rfind("\n", 0, chunk_size)
        if split_at == -1:
            split_at = chunk_size

        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()

    return chunks


async def start_handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Привет! Я — куратор качества продаж ретрита «Познай себя».\n\n"
        "Пришлите аудио или видеозапись разговора менеджера с потенциальной участницей. "
        "Я сделаю транскрипт и проверю диалог по 11 ключевым блокам, "
        "после чего пришлю краткие рекомендации по доработке."
    )


async def help_handler(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    await update.message.reply_text(
        "Как пользоваться:\n"
        "1) Пришлите запись разговора менеджера с клиенткой (аудио или видео).\n"
        "2) Дождитесь, пока я расшифрую запись и проверю диалог по 11 ключевым блокам.\n"
        "3) Получите итоговый аудит с рекомендациями прямо в чате.\n\n"
        "Длинные записи могут обрабатываться несколько минут — это нормально."
    )


async def handle_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    message = update.message
    file_data = extract_media_file_data(message)
    if not file_data:
        await message.reply_text(
            "Кажется, во вложении нет подходящей записи. "
            "Пришлите аудио или видео разговора менеджера с клиенткой."
        )
        return

    file_id, file_unique_id, filename = file_data
    settings: Settings = context.application.bot_data["settings"]
    audit_prompt: str = context.application.bot_data["audit_prompt"]
    user_id = message.from_user.id if message.from_user else None
    username = message.from_user.username if message.from_user else None
    transcript: Optional[str] = None
    analysis: Optional[str] = None

    progress_message = await message.reply_text("Запись получена. Скачиваю...")
    await context.bot.send_chat_action(chat_id=message.chat_id, action=ChatAction.TYPING)

    try:
        with tempfile.TemporaryDirectory(prefix="tg_sales_audit_") as tmp_dir:
            local_path = Path(tmp_dir) / filename
            telegram_file = await context.bot.get_file(file_id)
            await telegram_file.download_to_drive(custom_path=str(local_path))

            await progress_message.edit_text("Запись скачана. Делаю транскрипт...")
            transcript = await asyncio.to_thread(transcribe_video, local_path, settings.assemblyai_api_key)

        await progress_message.edit_text("Транскрипт готов. Провожу аудит диалога...")
        analysis = await asyncio.to_thread(analyze_with_openai, transcript, audit_prompt, settings)

        try:
            await asyncio.to_thread(
                save_audit_result,
                settings.database_url,
                message.chat_id,
                user_id,
                username,
                file_id,
                file_unique_id,
                filename,
                transcript,
                analysis,
                "success",
                None,
            )
        except Exception as db_error:
            logging.exception("Failed to save successful audit to Postgres: %s", db_error)

        await progress_message.edit_text("Аудит готов. Отправляю результат...")
        for chunk in split_for_telegram(analysis):
            await message.reply_text(chunk)

        await progress_message.delete()

    except Exception as error:
        logging.exception("Failed to process media file: %s", error)

        try:
            await asyncio.to_thread(
                save_audit_result,
                settings.database_url,
                message.chat_id,
                user_id,
                username,
                file_id,
                file_unique_id,
                filename,
                transcript,
                analysis,
                "failed",
                str(error),
            )
        except Exception as db_error:
            logging.exception("Failed to save failed audit to Postgres: %s", db_error)

        await progress_message.edit_text(
            "Не удалось обработать запись. Попробуйте отправить файл ещё раз чуть позже."
        )


async def handle_unsupported_message(update: Update, _: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return
    await update.message.reply_text(
        "Я работаю только с записями разговоров. "
        "Пришлите, пожалуйста, аудио или видео — и я проведу аудит."
    )


def build_application(settings: Settings, audit_prompt: str) -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()
    app.bot_data["settings"] = settings
    app.bot_data["audit_prompt"] = audit_prompt

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("help", help_handler))
    app.add_handler(
        MessageHandler(
            filters.VIDEO | filters.Document.VIDEO | filters.AUDIO | filters.Document.AUDIO,
            handle_media,
        )
    )
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_unsupported_message))

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    load_dotenv()

    settings = Settings.from_env()
    init_database_with_retry(settings.database_url)
    prompt_path = Path(__file__).with_name("prompt.md")
    audit_prompt = load_audit_prompt(prompt_path)

    app = build_application(settings=settings, audit_prompt=audit_prompt)
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
