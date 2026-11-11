#!/usr/bin/env python3

import asyncio
import getpass
import json
import random
import re
import shutil
from pathlib import Path

from telethon import TelegramClient, functions, types


BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
SESSION_FILE = BASE_DIR / "user.session"
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}


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


def is_video(path):
    return path.suffix.lower() in VIDEO_EXTENSIONS


def course_dirs():
    if not INPUT_DIR.exists():
        return []
    return sorted(
        [path for path in INPUT_DIR.iterdir() if path.is_dir()],
        key=lambda path: natural_key(path.name),
    )


def ordered_files(folder):
    return sorted(
        [path for path in folder.iterdir() if path.is_file()],
        key=lambda path: natural_key(path.name),
    )


async def list_admin_forum_groups(client):
    groups = []
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        if not isinstance(entity, types.Channel):
            continue
        if not getattr(entity, "megagroup", False):
            continue
        if not getattr(entity, "forum", False):
            continue
        if getattr(entity, "creator", False) or getattr(entity, "admin_rights", None):
            groups.append(entity)

    return sorted(groups, key=lambda group: group.title.casefold())


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


async def get_or_create_topic(client, group, title, close_new_topics):
    existing = await find_topic(client, group, title)
    if existing:
        print(f"Topico existente: {existing.title} (ID {existing.id})")
        return existing.id

    peer = await client.get_input_entity(group)
    result = await client(
        functions.messages.CreateForumTopicRequest(
            peer=peer,
            title=title,
            random_id=random.getrandbits(63),
        )
    )
    topic_id = extract_created_topic_id(result)
    if not topic_id:
        raise RuntimeError("Nao foi possivel descobrir o ID do topico criado.")

    print(f"Topico criado: {title} (ID {topic_id})")

    if close_new_topics:
        await client(
            functions.messages.EditForumTopicRequest(
                peer=peer,
                topic_id=topic_id,
                closed=True,
            )
        )
        print("Topico novo fechado.")

    return topic_id


async def find_topic(client, group, title):
    expected = title.casefold()
    for topic in await list_topics(client, group):
        if topic.title.casefold() == expected:
            return topic
    return None


def extract_created_topic_id(result):
    for update in getattr(result, "updates", []):
        message = getattr(update, "message", None)
        if message and isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
            return message.id
        if isinstance(update, types.UpdateNewChannelMessage):
            message = update.message
            if isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
                return message.id
        if hasattr(update, "id") and isinstance(update.id, int):
            return update.id
    return None


async def sent_file_names(client, group, topic_id):
    names = set()
    async for message in client.iter_messages(group, reply_to=topic_id, limit=None):
        if message.file and message.file.name:
            names.add(message.file.name)
    return names


async def upload_course_folder(client, group, topic_id, folder):
    already_sent = await sent_file_names(client, group, topic_id)
    files = ordered_files(folder)
    if not files:
        print(f"Nenhum arquivo em {folder.name}.")
        return

    for path in files:
        if path.name in already_sent:
            print(f"[skip] Ja enviado: {path.name}")
            move_to_output(path, folder.name)
            continue

        label = path.stem
        print(f"\nEnviando: {path.name}")
        await client.send_file(
            entity=group,
            file=str(path),
            caption=label,
            force_document=not is_video(path),
            supports_streaming=is_video(path),
            reply_to=topic_id,
            progress_callback=TransferProgress(path.name),
        )
        print("\n[ok] Upload concluido.")
        move_to_output(path, folder.name)


def move_to_output(path, course_name):
    target_dir = OUTPUT_DIR / course_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(target_dir / path.name)
    shutil.move(str(path), str(target))


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


async def main():
    api_id, api_hash = ask_config()
    client = TelegramClient(str(SESSION_FILE), api_id, api_hash)

    try:
        await client.start()
        print("\nLogin concluido.")

        groups = await list_admin_forum_groups(client)
        group = choose_item(
            groups,
            "Supergrupos com topicos onde sua conta parece ter permissao:",
            lambda item: item.title,
        )
        if not group:
            print("Nenhum supergrupo com topicos encontrado.")
            return

        folders = course_dirs()
        if not folders:
            print(f"Nenhuma pasta de curso encontrada em {INPUT_DIR}")
            return

        print("\nPastas encontradas em input:")
        for folder in folders:
            print(f"- {folder.name}")

        if not ask_yes_no("Criar/usar topicos com esses nomes e enviar os arquivos?"):
            print("Operacao cancelada.")
            return

        close_new_topics = ask_yes_no("Fechar topicos novos depois de criar?", default=False)

        for folder in folders:
            topic_title = folder.name
            print(f"\n=== {topic_title} ===")
            try:
                topic_id = await get_or_create_topic(
                    client,
                    group,
                    topic_title,
                    close_new_topics,
                )
                await upload_course_folder(client, group, topic_id, folder)
            except Exception as exc:
                print(f"[erro] Falha em {topic_title}: {exc}")

    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
