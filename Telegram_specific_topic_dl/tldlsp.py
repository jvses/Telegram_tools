#!/usr/bin/env python3
"""Baixa as midias de um canal, grupo ou topico especifico de um supergrupo forum."""

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tg_common import (  # noqa: E402
    TransferProgress,
    ask_yes_no,
    choose_item,
    connect,
    human_size,
    list_topics,
    logout,
    run,
    safe_name,
    with_flood_retry,
)
from telethon import errors, types  # noqa: E402


BASE_DIR = Path(__file__).resolve().parent
DOWNLOADS_DIR = BASE_DIR / "downloads"
GENERAL_TOPIC_ID = 1
SAFE_EXTENSION = re.compile(r"\.[A-Za-z0-9]{1,10}")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Baixa midias de um canal, grupo ou topico de supergrupo do Telegram."
    )
    parser.add_argument(
        "--saida",
        type=Path,
        default=DOWNLOADS_DIR,
        help=f"Pasta de destino. Padrao: {DOWNLOADS_DIR}",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Encerra a sessao do Telegram e apaga user.session.",
    )
    return parser.parse_args()


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


def media_base_name(message):
    if message.file and message.file.name:
        return Path(message.file.name).stem

    text = (message.message or "").strip()
    if text:
        return text.splitlines()[0]

    return f"midia_{message.id}"


def media_extension(message):
    # Nome e extensao vem de quem postou: so aceitamos extensoes simples.
    candidates = []
    if message.file and message.file.name:
        candidates.append(Path(message.file.name).suffix)
    if message.file and message.file.ext:
        candidates.append(message.file.ext)

    for candidate in candidates:
        if candidate and SAFE_EXTENSION.fullmatch(candidate):
            return candidate.lower()
    return ".bin"


def in_general_topic(message):
    # No topico "General" as mensagens nao trazem cabecalho de topico.
    reply = message.reply_to
    return not (reply and getattr(reply, "forum_topic", False))


async def download_media_from_scope(client, source, topic_id, output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {"downloaded": 0, "skipped": 0, "failed": 0}
    index = 0

    general = topic_id == GENERAL_TOPIC_ID
    reply_to = None if general else topic_id

    print(f"\nBaixando para: {output_dir}")
    async for message in client.iter_messages(
        source,
        reply_to=reply_to,
        limit=None,
        reverse=True,
    ):
        # message.file e None para previas de link e outras midias sem arquivo.
        if not message.file:
            continue
        if general and not in_general_topic(message):
            continue

        index += 1
        base = safe_name(media_base_name(message))
        target = output_dir / f"{index:04d}-{base}{media_extension(message)}"
        expected_size = message.file.size

        if target.exists() and (
            target.stat().st_size == expected_size
            if expected_size
            else target.stat().st_size > 0
        ):
            results["skipped"] += 1
            print(f"[skip] {target.name}")
            continue

        # Baixa em .part: um download interrompido nunca parece completo.
        part = target.with_name(target.name + ".part")
        # Sobra de execucao anterior: se existir, o Telethon criaria "nome (1).part".
        part.unlink(missing_ok=True)
        try:
            size_label = f" ({human_size(expected_size)})" if expected_size else ""
            print(f"\nBaixando: {target.name}{size_label}")
            result = await with_flood_retry(
                client.download_media,
                message,
                file=str(part),
                progress_callback=TransferProgress(target.name),
            )
            print()

            saved = Path(result) if result else None
            if not saved or not saved.exists() or saved.stat().st_size == 0:
                raise RuntimeError("arquivo nao foi salvo corretamente")
            saved.replace(target)

            results["downloaded"] += 1
            print(f"[ok] {target.name}")
        except (errors.RPCError, OSError, RuntimeError, ValueError) as exc:
            part.unlink(missing_ok=True)
            results["failed"] += 1
            print(f"\n[erro] {target.name}: {exc}")
        except BaseException:
            part.unlink(missing_ok=True)
            raise

    print("\nResumo:")
    print(f"  Baixados: {results['downloaded']}")
    print(f"  Ja existiam: {results['skipped']}")
    print(f"  Falharam: {results['failed']}")


async def choose_download_scope(client, source, downloads_dir):
    source_dir = downloads_dir / safe_name(source.title)

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
    args = parse_args()
    if args.logout:
        await logout()
        return

    client, _me = await connect()
    try:
        sources = await list_sources(client)
        source = choose_item(sources, "Canais, grupos e supergrupos:", source_label)
        if not source:
            print("Nenhum canal ou grupo escolhido.")
            return

        topic_id, output_dir = await choose_download_scope(client, source, args.saida.expanduser())
        if output_dir is None:
            print("Operacao cancelada.")
            return

        await download_media_from_scope(client, source, topic_id, output_dir)
    finally:
        await client.disconnect()


if __name__ == "__main__":
    run(main())
