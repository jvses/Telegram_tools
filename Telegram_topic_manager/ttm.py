#!/usr/bin/env python3
"""Envia cada pasta de input/ para um topico (criado ou existente) de um supergrupo forum."""

import argparse
import json
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tg_common import (  # noqa: E402
    TransferProgress,
    ask_yes_no,
    choose_item,
    choose_many,
    connect,
    human_size,
    list_topics,
    logout,
    natural_key,
    run,
    unique_path,
    with_flood_retry,
)
from telethon import errors, functions, types  # noqa: E402

import video_tools  # noqa: E402


BASE_DIR = Path(__file__).resolve().parent
INPUT_DIR = BASE_DIR / "input"
OUTPUT_DIR = BASE_DIR / "output"
# Videos originais que foram substituidos por uma versao convertida.
ORIGINALS_DIR = BASE_DIR / "originais"
# Marca de pasta continua: guarda o grupo e o topico para os proximos envios.
STATE_FILE = ".ttm.json"

VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
INCOMPLETE_SUFFIXES = {".part", ".ytdl", ".tmp", ".crdownload"}
# Limite de upload do Telegram: 4000 partes de 512 KB (o dobro com Premium).
MAX_UPLOAD_BYTES = 2000 * 1024 * 1024
MAX_UPLOAD_BYTES_PREMIUM = 4000 * 1024 * 1024


def parse_args():
    parser = argparse.ArgumentParser(
        description="Envia as pastas de input/ para topicos de um supergrupo forum do Telegram."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Mostra o que seria enviado, sem conectar ao Telegram.",
    )
    parser.add_argument(
        "--incluir-csv",
        action="store_true",
        help="Envia tambem o CSV do curso (<pasta>.csv), que por padrao fica de fora.",
    )
    parser.add_argument(
        "--topicos-abertos",
        action="store_true",
        help="Deixa os topicos novos abertos. Por padrao eles sao fechados (so admins postam).",
    )
    parser.add_argument(
        "--sem-analise",
        action="store_true",
        help="Pula a analise dos videos com ffprobe/ffmpeg.",
    )
    parser.add_argument(
        "--crf",
        type=int,
        default=video_tools.X264_CRF,
        choices=range(15, 36),
        metavar="N",
        help=f"Qualidade da reconversao H.264 (padrao {video_tools.X264_CRF}; maior = arquivo menor).",
    )
    parser.add_argument(
        "--preset",
        default=video_tools.X264_PRESET,
        choices=video_tools.X264_PRESETS,
        help=f"Velocidade da reconversao (padrao {video_tools.X264_PRESET}; mais lento = arquivo menor).",
    )
    parser.add_argument(
        "--logout",
        action="store_true",
        help="Encerra a sessao do Telegram e apaga user.session.",
    )
    return parser.parse_args()


def is_video(path):
    return path.suffix.lower() in VIDEO_EXTENSIONS


def course_dirs():
    if not INPUT_DIR.exists():
        return []
    return sorted(
        [path for path in INPUT_DIR.iterdir() if path.is_dir() and not path.name.startswith(".")],
        key=lambda path: natural_key(path.name),
    )


# ===== Pastas continuas =====

def load_state(folder):
    try:
        state = json.loads((folder / STATE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def save_state(folder, group, topic_id, topic_title):
    state = {
        "grupo_id": group.id,
        "grupo": group.title,
        "topico_id": topic_id,
        "topico": topic_title,
    }
    (folder / STATE_FILE).write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def folder_label(folder):
    if not load_state(folder):
        return folder.name
    pending = sum(1 for path in folder.iterdir() if path.is_file() and not path.name.startswith("."))
    return f"{folder.name}  [continua: {pending} arquivo(s) novo(s)]"


def choose_folders_to_keep(plans):
    """Pergunta quais pastas novas ficam em input/ para receber aulas depois."""
    if not plans:
        return []
    print(
        "\nPasta continua: fica em input/ depois do envio, e as aulas novas que voce colocar"
        " nela vao para o mesmo topico na proxima execucao."
    )
    if len(plans) == 1:
        name = plans[0].folder.name
        return plans if ask_yes_no(f"Manter '{name}' em input/ para aulas futuras?") else []
    if not ask_yes_no("Manter alguma destas pastas em input/ para aulas futuras?"):
        return []
    return choose_many(
        plans,
        "Pastas para manter:",
        lambda plan: plan.folder.name,
        prompt="Quais? (Enter = todas, ex: 1,3, 0 = nenhuma): ",
    )


class CoursePlan:
    """Arquivos de uma pasta separados em: enviar, deixar de fora e incompletos."""

    def __init__(self, folder, include_csv, max_bytes):
        self.folder = folder
        self.state = load_state(folder)
        self.keep = bool(self.state)
        self.to_send = []
        self.extras = []
        self.too_big = []
        self.incomplete = []

        for path in sorted(folder.iterdir(), key=lambda item: natural_key(item.name)):
            if path.is_dir() or path.name.startswith("."):
                continue
            if path.suffix.lower() in INCOMPLETE_SUFFIXES:
                self.incomplete.append(path)
            elif path.name == f"{folder.name}.csv" and not include_csv:
                # Lista gerada pelo course_dl.py: links que expiram, nao e conteudo do curso.
                self.extras.append(path)
            elif path.stat().st_size > max_bytes:
                self.too_big.append(path)
            else:
                self.to_send.append(path)

    @property
    def total_size(self):
        return sum(path.stat().st_size for path in self.to_send)

    def describe(self):
        line = f"{self.folder.name}: {len(self.to_send)} arquivo(s), {human_size(self.total_size)}"
        notes = []
        if self.state:
            notes.append(f"pasta continua -> topico '{self.state.get('topico', '?')}'")
        if self.incomplete:
            notes.append(f"{len(self.incomplete)} download(s) INCOMPLETO(S)")
        if self.too_big:
            notes.append(f"{len(self.too_big)} acima do limite do Telegram")
        if self.extras:
            notes.append("CSV do curso fica de fora")
        return line + (f"  [{'; '.join(notes)}]" if notes else "")


def print_plan(plans):
    print("\nPlano de envio:")
    for plan in plans:
        print(f"- {plan.describe()}")
        for path in plan.incomplete:
            print(f"    incompleto: {path.name}")
        for path in plan.too_big:
            print(f"    grande demais: {path.name} ({human_size(path.stat().st_size)})")


# ===== Videos =====

def analyze_videos(plans, max_bytes):
    reports = []
    for plan in plans:
        for path in plan.to_send + plan.too_big:
            if is_video(path):
                reports.append(video_tools.analyze(path, max_bytes))
    return reports


def print_video_report(reports, show_commands=False):
    print("\nAnalise dos videos:")
    flagged = [report for report in reports if report.problems or report.suggestions or report.warnings]
    print(f"  {len(reports) - len(flagged)} de {len(reports)} video(s) ja estao prontos para o Telegram.")

    for report in flagged:
        print(f"\n- {report.path.parent.name}/{report.path.name}")
        print(f"    {video_tools.summary_line(report.info)}")
        for line in video_tools.describe(report):
            print(f"    {line}")
        if report.needs_conversion:
            print(f"    acao sugerida: {report.action}")
            if show_commands:
                if report.remux or report.reencode_video or report.reencode_audio:
                    output = report.path.with_name(f"{report.path.stem}.telegram.mp4")
                    print(f"    $ {video_tools.shell_command(report, output)}")
                note = video_tools.split_note(report)
                if note:
                    print(f"    ({note})")


def video_label(report):
    optional = "" if report.problems else " (opcional)"
    return f"{report.path.parent.name}/{report.path.name}: {report.action}{optional}"


def offer_conversion(reports):
    """Pergunta se os videos com sugestao devem ser convertidos. True se algo mudou."""
    candidates = [report for report in reports if report.needs_conversion]
    if not candidates:
        return False

    slow = [report for report in candidates if report.reencode_video]
    print(f"\n{len(candidates)} video(s) podem ser convertidos.")
    if slow:
        total = video_tools.format_duration(sum(report.info.duration for report in slow if report.info))
        print(
            f"{len(slow)} precisam reconverter a imagem: {total} de video com preset "
            f"{video_tools.X264_PRESET} e CRF {video_tools.X264_CRF}. O progresso aparece na tela."
        )
    else:
        print("Todos sao rapidos: a imagem do video nao e recodificada.")
    if any(report.info and report.info.size > report.max_bytes for report in candidates):
        print(
            f"Videos acima de {human_size(MAX_UPLOAD_BYTES)} sao divididos em ate {video_tools.MAX_PARTS} "
            "partes, com ' - PART1', ' - PART2'... no nome e na legenda."
        )
    print(f"Os originais vao para {ORIGINALS_DIR.relative_to(BASE_DIR)}/<pasta>/ e nada e apagado.")
    if not ask_yes_no("Converter agora, antes de enviar?"):
        return False

    selected = candidates
    if len(candidates) > 1:
        selected = choose_many(
            candidates,
            "Videos para converter:",
            video_label,
            prompt="Quais? (Enter = todos, ex: 1,3-5, 0 = nenhum): ",
        )

    changed = False
    for index, report in enumerate(selected, start=1):
        before = report.path.stat().st_size
        print(f"\n[{index}/{len(selected)}] {report.path.name}: {report.action}")
        try:
            targets, backup = video_tools.convert(
                report, ORIGINALS_DIR / report.path.parent.name, MAX_UPLOAD_BYTES
            )
        except RuntimeError as exc:
            print(f"  [erro] {exc}")
            continue
        changed = True
        after = ", ".join(f"{target.name} ({human_size(target.stat().st_size)})" for target in targets)
        print(f"  [ok] {human_size(before)} -> {after}")
        print(f"  Original em {backup.relative_to(BASE_DIR)}")
    return changed


def video_upload_options(path, temp_dir):
    """Miniatura e atributos (duracao, dimensoes) do video, se o ffmpeg estiver instalado."""
    if not video_tools.available():
        return {}
    info = video_tools.probe(path)
    options = {}
    attributes = video_tools.video_attributes(info, types)
    if attributes:
        options["attributes"] = attributes
        thumb = video_tools.make_thumbnail(path, info, Path(temp_dir) / "thumb.jpg")
        if thumb:
            options["thumb"] = str(thumb)
    return options


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


async def get_or_create_topic(client, group, title, topic_ids, close_new_topics):
    # topic_ids: titulo em minusculas -> ID, para reaproveitar topicos com o mesmo nome.
    existing_id = topic_ids.get(title.casefold())
    if existing_id:
        print(f"Topico existente: {title} (ID {existing_id})")
        return existing_id

    peer = await client.get_input_entity(group)
    result = await with_flood_retry(
        client,
        functions.messages.CreateForumTopicRequest(
            peer=peer,
            title=title,
            random_id=random.getrandbits(63),
        ),
    )
    topic_id = extract_created_topic_id(result)
    if not topic_id:
        raise RuntimeError("Nao foi possivel descobrir o ID do topico criado.")

    print(f"Topico criado: {title} (ID {topic_id})")
    topic_ids[title.casefold()] = topic_id

    if close_new_topics:
        await with_flood_retry(
            client,
            functions.messages.EditForumTopicRequest(
                peer=peer,
                topic_id=topic_id,
                closed=True,
            ),
        )
        print("Topico novo fechado: so admins podem postar nele.")

    return topic_id


def extract_created_topic_id(result):
    updates = getattr(result, "updates", [])
    # O ID do topico e o ID da mensagem de servico "topico criado".
    for update in updates:
        message = getattr(update, "message", None)
        if message and isinstance(getattr(message, "action", None), types.MessageActionTopicCreate):
            return message.id
    for update in updates:
        if isinstance(update, types.UpdateMessageID):
            return update.id
    return None


async def sent_file_names(client, group, topic_id):
    names = set()
    async for message in client.iter_messages(group, reply_to=topic_id, limit=None):
        if message.file and message.file.name:
            names.add(message.file.name)
    return names


async def upload_course(client, group, topic_id, plan, results):
    already_sent = await sent_file_names(client, group, topic_id)
    folder_name = plan.folder.name

    for path in plan.to_send:
        if path.name in already_sent:
            print(f"[skip] Ja enviado: {path.name}")
            move_to_output(path, folder_name)
            results["skipped"] += 1
            continue

        print(f"\nEnviando: {path.name} ({human_size(path.stat().st_size)})")
        try:
            with tempfile.TemporaryDirectory(prefix="ttm-") as temp_dir:
                extra = video_upload_options(path, temp_dir) if is_video(path) else {}
                await with_flood_retry(
                    client.send_file,
                    entity=group,
                    file=str(path),
                    caption=path.stem,
                    force_document=not is_video(path),
                    supports_streaming=is_video(path),
                    reply_to=topic_id,
                    progress_callback=TransferProgress(path.name),
                    **extra,
                )
        except (errors.RPCError, OSError, ValueError) as exc:
            # Falha em um arquivo nao derruba a pasta; ele continua em input/.
            print(f"\n[erro] {path.name}: {exc}")
            results["failed"] += 1
            continue

        print("\n[ok] Upload concluido.")
        move_to_output(path, folder_name)
        results["sent"] += 1

    for path in plan.extras:
        move_to_output(path, folder_name)
    if plan.keep:
        print(f"Pasta {folder_name} continua em input/: coloque as proximas aulas nela.")
    else:
        remove_if_empty(plan.folder)


def move_to_output(path, course_name):
    target_dir = OUTPUT_DIR / course_name
    target_dir.mkdir(parents=True, exist_ok=True)
    target = unique_path(target_dir / path.name)
    shutil.move(str(path), str(target))


def remove_if_empty(folder):
    try:
        folder.rmdir()
        print(f"Pasta {folder.name} concluida e removida de input/.")
    except OSError:
        print(f"Pasta {folder.name} ainda tem arquivos em input/ (falhas ou pendentes).")


async def main():
    args = parse_args()
    video_tools.configure(crf=args.crf, preset=args.preset)
    if args.logout:
        await logout()
        return

    folders = course_dirs()
    if not folders:
        print(f"Nenhuma pasta de curso encontrada em {INPUT_DIR}")
        return

    selected = choose_many(folders, "Pastas encontradas em input:", folder_label)
    if not selected:
        print("Operacao cancelada.")
        return

    plans = [CoursePlan(folder, args.incluir_csv, MAX_UPLOAD_BYTES) for folder in selected]
    print_plan(plans)

    # Analise e conversao rodam antes de conectar: nao dependem do Telegram e podem demorar.
    if not args.sem_analise:
        if not video_tools.available():
            print("\n[aviso] ffmpeg/ffprobe nao encontrados: analise dos videos desativada.")
        else:
            reports = analyze_videos(plans, MAX_UPLOAD_BYTES)
            if reports:
                print_video_report(reports, show_commands=args.dry_run)
                if not args.dry_run and offer_conversion(reports):
                    plans = [CoursePlan(folder, args.incluir_csv, MAX_UPLOAD_BYTES) for folder in selected]
                    print_plan(plans)

    if args.dry_run:
        print("\nModo dry-run: nada foi enviado nem convertido.")
        return

    if not any(plan.to_send or plan.extras or plan.too_big or plan.incomplete for plan in plans):
        print("\nNada para enviar: as pastas escolhidas nao tem arquivos novos.")
        return

    client, me = await connect()
    try:
        max_bytes = MAX_UPLOAD_BYTES_PREMIUM if getattr(me, "premium", False) else MAX_UPLOAD_BYTES
        if max_bytes != MAX_UPLOAD_BYTES:
            # Conta Premium: o limite maior pode liberar arquivos grandes.
            plans = [CoursePlan(folder, args.incluir_csv, max_bytes) for folder in selected]
            print_plan(plans)

        incomplete = [plan for plan in plans if plan.incomplete]
        if incomplete and not ask_yes_no(
            "\nHa pastas com downloads incompletos. Enviar essas pastas mesmo assim?"
        ):
            plans = [plan for plan in plans if not plan.incomplete]
            print("Pastas com downloads incompletos ficaram de fora.")

        for plan in plans:
            if plan.state and not (plan.to_send or plan.extras):
                print(f"{plan.folder.name}: nenhuma aula nova desde o ultimo envio.")
        plans = [plan for plan in plans if plan.to_send or plan.extras]
        if not plans:
            print("Nada para enviar.")
            return

        groups = await list_admin_forum_groups(client)
        group = None
        remembered = {plan.state.get("grupo_id") for plan in plans if plan.state}
        if len(remembered) == 1:
            # Pastas continuas: sugere o mesmo supergrupo do envio anterior.
            group_id = remembered.pop()
            match = next((item for item in groups if item.id == group_id), None)
            if match and ask_yes_no(
                f"\nUsar o supergrupo '{match.title}', o mesmo dos envios anteriores?", default=True
            ):
                group = match
        if group is None:
            group = choose_item(
                groups,
                "Supergrupos com topicos onde sua conta parece ter permissao:",
                lambda item: item.title,
            )
        if not group:
            print("Nenhum supergrupo escolhido.")
            return

        if not ask_yes_no(f"\nCriar/usar {len(plans)} topico(s) em '{group.title}' e enviar?"):
            print("Operacao cancelada.")
            return

        for plan in choose_folders_to_keep([plan for plan in plans if not plan.state]):
            plan.keep = True

        close_new_topics = not args.topicos_abertos
        if close_new_topics:
            print("Topicos novos serao fechados: so admins postam (use --topicos-abertos para mudar).")

        topics = await list_topics(client, group)
        topic_ids = {topic.title.casefold(): topic.id for topic in topics}
        topic_titles = {topic.id: topic.title for topic in topics}
        results = {"sent": 0, "skipped": 0, "failed": 0}
        for plan in plans:
            print(f"\n=== {plan.folder.name} ===")
            try:
                topic_id = None
                if plan.state and plan.state.get("grupo_id") == group.id:
                    saved_id = plan.state.get("topico_id")
                    if saved_id in topic_titles:
                        # Vale mesmo que o topico tenha sido renomeado no app.
                        topic_id = saved_id
                        print(f"Topico da pasta continua: {topic_titles[saved_id]} (ID {saved_id})")
                    else:
                        print("[aviso] O topico salvo para esta pasta nao existe mais; procurando pelo nome.")
                if topic_id is None:
                    topic_id = await get_or_create_topic(
                        client, group, plan.folder.name, topic_ids, close_new_topics
                    )
                    topic_titles.setdefault(topic_id, plan.folder.name)
                if plan.keep:
                    save_state(plan.folder, group, topic_id, topic_titles[topic_id])
                await upload_course(client, group, topic_id, plan, results)
            except errors.RPCError as exc:
                print(f"[erro] Falha em {plan.folder.name}: {exc}")
                results["failed"] += len(plan.to_send)

        print("\nResumo:")
        print(f"  Enviados: {results['sent']}")
        print(f"  Ja estavam no topico: {results['skipped']}")
        print(f"  Falharam: {results['failed']}")
    finally:
        await client.disconnect()


if __name__ == "__main__":
    run(main())
