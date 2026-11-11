#!/usr/bin/env python3

import asyncio
import getpass
import json
import re
from pathlib import Path

from telethon import TelegramClient, functions, types


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
SESSION_FILE = BASE_DIR / "user.session"
DOWNLOADS_DIR = BASE_DIR / "downloads"


class TransferProgress:
    def __init__(self, label):
        self.label = label
        self.last_percent = -1

    def __call__(self, current, total):
        if not total:
            return

        percent = int(current * 100 / total)
        if percent == self.last_percent:
            return

        self.last_percent = percent
        current_mb = current / (1024 * 1024)
        total_mb = total / (1024 * 1024)
        print(
            f"\r{self.label}: {percent:3d}% "
            f"({current_mb:.1f}/{total_mb:.1f} MB)",
            end="",
            flush=True,
        )


def load_config():
    if not CONFIG_FILE.exists():
        return None, None

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            config = json.load(file)
    except (json.JSONDecodeError, OSError):
        return None, None

    return config.get("api_id"), config.get("api_hash")


def save_config(api_id, api_hash):
    with CONFIG_FILE.open("w", encoding="utf-8") as file:
        json.dump({"api_id": int(api_id), "api_hash": api_hash}, file, indent=2)
    CONFIG_FILE.chmod(0o600)


def ask_config():
    api_id, api_hash = load_config()
    if api_id and api_hash:
        return api_id, api_hash

    print("=== Configuracao inicial ===")
    api_id = input("API ID do Telegram: ").strip()
    api_hash = getpass.getpass("API Hash: ").strip()
    save_config(api_id, api_hash)
    print(f"Configuracao salva em {CONFIG_FILE}")
    return int(api_id), api_hash


def ask_yes_no(question, default=False):
    suffix = " [S/n]: " if default else " [s/N]: "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"s", "sim", "y", "yes"}


def choose_item(items, title, label_func):
    if not items:
        return None

    print(f"\n{title}")
    for index, item in enumerate(items, start=1):
        print(f"{index}. {label_func(item)}")

    while True:
        try:
            choice = int(input("\nDigite o numero escolhido: ").strip())
            return items[choice - 1]
        except (ValueError, IndexError):
            print("Numero invalido. Tente novamente.")


def natural_key(value):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


def safe_name(value):
    value = " ".join(str(value or "").split())
    value = re.sub(r'[\\/:*?"<>|]+', "_", value)
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:128] or "sem_nome"


def source_label(entity):
    if isinstance(entity, types.Channel):
        if getattr(entity, "broadcast", False):
            kind = "canal"
        elif getattr(entity, "forum", False):
            kind = "supergrupo forum"
        else:
            kind = "supergrupo"
    elif isinstance(entity, types.Chat):
        kind = "grupo"
    else:
        kind = "chat"
    return f"[{kind}] {entity.title}"


async def list_sources(client):
    sources = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if isinstance(entity, types.Channel):
            if getattr(entity, "broadcast", False) or getattr(entity, "megagroup", False):
                sources.append(entity)
        elif isinstance(entity, types.Chat):
            sources.append(entity)

    return sorted(sources, key=lambda item: source_label(item).casefold())


async def list_topics(client, group):
    peer = await client.get_input_entity(group)
    topics = []
    seen = set()
    offset_date = None
    offset_id = 0
    offset_topic = 0

    while True:
        result = await client(
            functions.messages.GetForumTopicsRequest(
                peer=peer,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100,
                q=None,
            )
        )

        valid_topics = [
            topic
            for topic in result.topics
            if isinstance(topic, types.ForumTopic) and topic.id not in seen
        ]
        if not valid_topics:
            break

        topics.extend(valid_topics)
        seen.update(topic.id for topic in valid_topics)

        last_topic = valid_topics[-1]
        messages_by_id = {message.id: message for message in result.messages}
        last_message = messages_by_id.get(last_topic.top_message)
        offset_date = last_message.date if last_message else None
        offset_id = last_topic.top_message
        offset_topic = last_topic.id

        if len(topics) >= result.count:
            break

    return sorted(topics, key=lambda topic: topic.title.casefold())


def media_base_name(message):
    if message.file and message.file.name:
        return Path(message.file.name).stem

    text = (message.message or "").strip()
    if text:
        return text.splitlines()[0]

    return f"midia_{message.id}"


def media_extension(message):
    if message.file and message.file.name:
        suffix = Path(message.file.name).suffix
        if suffix:
            return suffix

    if message.file and message.file.ext:
        return message.file.ext

    return ".bin"


def unique_path(path):
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    for counter in range(1, 1000):
        candidate = path.with_name(f"{stem}_{counter}{suffix}")
        if not candidate.exists():
            return candidate

    raise RuntimeError(f"Nao foi possivel criar nome unico para {path}")


async def download_media_from_scope(client, source, reply_to, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    downloaded = 0
    skipped = 0
    failed = 0
    index = 0

    print(f"\nBaixando para: {output_dir}")
    async for message in client.iter_messages(
        source,
        reply_to=reply_to,
        limit=None,
        reverse=True,
    ):
        if not message.media:
            continue

        index += 1
        base = safe_name(media_base_name(message))
        extension = media_extension(message)
        target = output_dir / f"{index:04d}-{base}{extension}"

        if target.exists() and target.stat().st_size > 1024:
            skipped += 1
            print(f"[skip] {target.name}")
            continue

        target = unique_path(target)
        try:
            print(f"\nBaixando: {target.name}")
            result = await client.download_media(
                message,
                file=str(target),
                progress_callback=TransferProgress(target.name),
            )
            print()

            if not result or not target.exists() or target.stat().st_size == 0:
                raise RuntimeError("arquivo nao foi salvo corretamente")

            downloaded += 1
            print(f"[ok] {target.name}")
        except Exception as exc:
            failed += 1
            print(f"[erro] {target.name}: {exc}")

    print("\nResumo:")
    print(f"  Baixados: {downloaded}")
    print(f"  Ignorados: {skipped}")
    print(f"  Falharam: {failed}")


async def choose_download_scope(client, source):
    source_dir = DOWNLOADS_DIR / safe_name(source.title)

    if isinstance(source, types.Channel) and getattr(source, "forum", False):
        if ask_yes_no("Esse supergrupo tem topicos. Baixar um topico especifico?", default=True):
            topics = await list_topics(client, source)
            topic = choose_item(
                topics,
                "Topicos disponiveis:",
                lambda item: f"{item.title} (ID {item.id})",
            )
            if not topic:
                return None, None
            return topic.id, source_dir / safe_name(topic.title)

    return None, source_dir / "chat_inteiro"


async def main():
    api_id, api_hash = ask_config()
    client = TelegramClient(str(SESSION_FILE), api_id, api_hash)

    try:
        await client.start()
        print("\nLogin concluido.")

        sources = await list_sources(client)
        source = choose_item(sources, "Canais, grupos e supergrupos:", source_label)
        if not source:
            print("Nenhum canal ou grupo encontrado.")
            return

        reply_to, output_dir = await choose_download_scope(client, source)
        if output_dir is None:
            print("Operacao cancelada.")
            return

        await download_media_from_scope(client, source, reply_to, output_dir)

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
