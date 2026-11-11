"""Codigo compartilhado pelas ferramentas: credenciais, sessao, login e menus.

config.json e user.session ficam na raiz do repositorio e valem para os dois scripts.
"""

import asyncio
import getpass
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

try:
    from telethon import TelegramClient, errors, functions, types, utils
except ImportError:
    sys.exit(
        "Telethon nao encontrado. Ative o ambiente virtual da raiz do projeto:\n"
        "  source .venv/bin/activate\n"
        "ou instale as dependencias: pip install -r requirements.txt"
    )


ROOT_DIR = Path(__file__).resolve().parent
CONFIG_FILE = ROOT_DIR / "config.json"
SESSION_FILE = ROOT_DIR / "user.session"
PRIVATE_MODE = 0o600
API_HASH_PATTERN = re.compile(r"[0-9a-f]{32}")


# ===== Credenciais (api_id / api_hash) =====

def validate_config(api_id, api_hash):
    try:
        api_id = int(str(api_id).strip())
    except ValueError:
        raise ValueError("o API ID deve ser um numero") from None

    api_hash = str(api_hash or "").strip().lower()
    if not API_HASH_PATTERN.fullmatch(api_hash):
        raise ValueError("o API Hash deve ter 32 caracteres hexadecimais")

    return api_id, api_hash


def load_config():
    env_id, env_hash = os.getenv("TG_API_ID"), os.getenv("TG_API_HASH")
    if env_id and env_hash:
        return validate_config(env_id, env_hash)

    if not CONFIG_FILE.exists():
        return None

    secure_file(CONFIG_FILE)
    try:
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        return validate_config(data.get("api_id"), data.get("api_hash"))
    except (OSError, json.JSONDecodeError, AttributeError, ValueError) as exc:
        print(f"[aviso] {CONFIG_FILE.name} invalido ({exc}); vamos recriar.")
        return None


def save_config(api_id, api_hash):
    # Cria o arquivo ja com permissao 600, sem janela em que ele fique legivel.
    tmp_path = CONFIG_FILE.with_name(CONFIG_FILE.name + ".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, PRIVATE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        json.dump({"api_id": api_id, "api_hash": api_hash}, file, indent=2)
    os.replace(tmp_path, CONFIG_FILE)


def ask_config():
    config = load_config()
    if config:
        return config

    print("=== Configuracao inicial ===")
    print("Crie as credenciais em https://my.telegram.org > API development tools.")
    while True:
        api_id = input("API ID: ").strip()
        api_hash = getpass.getpass("API Hash (nao aparece ao digitar): ").strip()
        try:
            config = validate_config(api_id, api_hash)
            break
        except ValueError as exc:
            print(f"Invalido: {exc}. Tente de novo.\n")

    save_config(*config)
    print(f"Configuracao salva em {CONFIG_FILE} (somente seu usuario pode ler).")
    return config


# ===== Sessao e login =====

def session_files():
    return [SESSION_FILE, SESSION_FILE.with_name(SESSION_FILE.name + "-journal")]


def secure_file(path):
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        return
    if mode & 0o077:
        path.chmod(PRIVATE_MODE)
        print(f"[seguranca] Permissao de {path.name} corrigida para 600.")


async def connect():
    """Abre a sessao compartilhada e faz login se preciso. Retorna (client, me)."""
    api_id, api_hash = ask_config()
    # Arquivos novos (sessao, journal do SQLite) nascem legiveis so pelo dono.
    os.umask(0o077)
    for path in session_files():
        secure_file(path)

    try:
        client = TelegramClient(str(SESSION_FILE), api_id, api_hash)
        # Pede telefone, codigo e senha de 2 etapas (sem eco) quando necessario.
        await client.start()
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            sys.exit(
                "A sessao esta em uso por outro script. "
                "Feche a outra ferramenta e tente de novo."
            )
        raise
    except errors.ApiIdInvalidError:
        sys.exit(f"API ID/Hash recusados pelo Telegram. Apague {CONFIG_FILE.name} e refaca.")

    for path in session_files():
        secure_file(path)

    me = await client.get_me()
    username = f" (@{me.username})" if me.username else ""
    print(f"\nConectado como {utils.get_display_name(me)}{username}.")
    return client, me


async def logout():
    """Encerra a sessao no Telegram (equivale a remover o dispositivo) e apaga user.session."""
    if not SESSION_FILE.exists():
        print("Nenhuma sessao salva.")
        return

    api_id, api_hash = ask_config()
    client = TelegramClient(str(SESSION_FILE), api_id, api_hash)
    await client.connect()
    try:
        if await client.is_user_authorized() and await client.log_out():
            print("Sessao encerrada no Telegram.")
    finally:
        await client.disconnect()

    for path in session_files():
        path.unlink(missing_ok=True)
    print(f"{SESSION_FILE.name} removido.")
    if CONFIG_FILE.exists() and ask_yes_no(f"Apagar tambem {CONFIG_FILE.name} (API ID/Hash)?"):
        CONFIG_FILE.unlink()
        print(f"{CONFIG_FILE.name} removido.")


def run(main_coroutine):
    try:
        asyncio.run(main_coroutine)
    except KeyboardInterrupt:
        print("\n\nInterrompido pelo usuario. Arquivos ja concluidos foram mantidos.")
        sys.exit(130)


async def with_flood_retry(func, *args, attempts=3, **kwargs):
    # Esperas curtas o Telethon ja faz sozinho; aqui tratamos as longas.
    for attempt in range(1, attempts + 1):
        try:
            return await func(*args, **kwargs)
        except errors.FloodWaitError as exc:
            if attempt == attempts:
                raise
            wait = exc.seconds + 1
            print(f"\n[pausa] O Telegram pediu {wait}s de espera (limite de requisicoes)...")
            await asyncio.sleep(wait)


# ===== Menus =====

def ask_yes_no(question, default=False):
    suffix = " [S/n]: " if default else " [s/N]: "
    answer = input(question + suffix).strip().lower()
    if not answer:
        return default
    return answer in {"s", "sim", "y", "yes"}


def choose_item(items, title, label_func):
    """Menu numerado. Digitar texto filtra a lista; Enter mostra tudo; 0 cancela."""
    if not items:
        return None

    shown = items
    while True:
        print(f"\n{title}")
        for index, item in enumerate(shown, start=1):
            print(f"{index}. {label_func(item)}")

        answer = input("\nNumero (texto filtra, Enter mostra tudo, 0 cancela): ").strip()
        if answer == "0":
            return None
        if not answer:
            shown = items
            continue
        if answer.isdigit():
            choice = int(answer)
            if 1 <= choice <= len(shown):
                return shown[choice - 1]
            print("Numero invalido. Tente novamente.")
            continue

        term = fold_text(answer)
        filtered = [item for item in items if term in fold_text(label_func(item))]
        if filtered:
            shown = filtered
        else:
            print(f"Nada encontrado com '{answer}'.")


def choose_many(items, title, label_func, prompt="Quais? (Enter = todos, ex: 1,3-5, 0 cancela): "):
    """Selecao multipla: 'todos', '1,3' ou '2-5'. Retorna lista (vazia = cancelar)."""
    print(f"\n{title}")
    for index, item in enumerate(items, start=1):
        print(f"{index}. {label_func(item)}")

    while True:
        answer = input("\n" + prompt).strip().lower()
        if answer == "0":
            return []
        if answer in {"", "t", "todos"}:
            return list(items)
        try:
            indexes = parse_selection(answer, len(items))
            return [items[index] for index in indexes]
        except ValueError as exc:
            print(f"Selecao invalida: {exc}")


def parse_selection(text, total):
    indexes = []
    for part in text.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
        else:
            start = end = int(part)
        if not 1 <= start <= end <= total:
            raise ValueError(f"'{part}' fora de 1-{total}")
        indexes.extend(index - 1 for index in range(start, end + 1) if index - 1 not in indexes)
    if not indexes:
        raise ValueError("nada selecionado")
    return indexes


# ===== Topicos de forum =====

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


# ===== Arquivos e textos =====

class TransferProgress:
    def __init__(self, label):
        self.label = label
        self.last_percent = -1
        self.started = time.monotonic()

    def __call__(self, current, total):
        if not total:
            return

        percent = int(current * 100 / total)
        if percent == self.last_percent:
            return

        self.last_percent = percent
        elapsed = max(time.monotonic() - self.started, 0.001)
        speed = current / elapsed
        print(
            f"\r{self.label}: {percent:3d}% "
            f"({human_size(current)}/{human_size(total)}, {human_size(speed)}/s)",
            end="",
            flush=True,
        )


def human_size(size):
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


def natural_key(value):
    return [
        int(part) if part.isdigit() else part.casefold()
        for part in re.split(r"(\d+)", str(value))
    ]


def fold_text(value):
    normalized = unicodedata.normalize("NFKD", str(value))
    return normalized.encode("ascii", "ignore").decode("ascii").casefold()


def safe_name(value):
    value = " ".join(str(value or "").split())
    value = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", value)
    value = re.sub(r"_+", "_", value).strip("._ ")
    return value[:128] or "sem_nome"


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
