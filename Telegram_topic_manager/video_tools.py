"""Analise e conversao de videos com ffprobe/ffmpeg para assistir bem no Telegram.

O que o app do Telegram reproduz direto (streaming), em todos os clientes:
  - container MP4 com o indice (atomo "moov") no inicio do arquivo ("faststart");
  - video H.264 com pixel format yuv420p (8 bits);
  - audio AAC (ou MP3), estereo ou mono.
Fora disso o video ate pode ser enviado, mas vira download ou nao toca em alguns apps.

Videos acima do limite de upload sao divididos em ate MAX_PARTS partes ("... - PART1.mp4"),
cortadas em keyframes e sem reconverter.

Usa os programas ffmpeg e ffprobe do sistema (nao ha pacote Python a instalar).
"""

import dataclasses
import json
import math
import shlex
import shutil
import struct
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


FFMPEG = shutil.which("ffmpeg")
FFPROBE = shutil.which("ffprobe")

MP4_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
GOOD_AUDIO = {"aac", "mp3"}
# Bitrate de video (bits/s) acima do qual vale reconverter, pelo lado menor da imagem.
HIGH_BITRATE = [(480, 2_500_000), (720, 5_000_000), (1080, 8_000_000), (99999, 16_000_000)]
MAX_SHORT_SIDE = 1080
MAX_FPS = 60
# Taxa de saida quando a gravacao nao informa uma taxa confiavel.
DEFAULT_FPS = 30

# ----- Reconversao H.264 (ajustavel por --crf / --preset no ttm.py)
# CRF menor = melhor qualidade e arquivo maior. 23 e o padrao do x264; ate 25 a perda e pequena.
X264_CRF = 23
X264_PRESET = "fast"
# Usado so quando nem dividindo em MAX_PARTS o video caberia (veja convert()).
FALLBACK_CRF = 25
# Keyframe pelo menos a cada N segundos: avancar o video no app e cortar as partes com precisao.
KEYFRAME_SECONDS = 10
AAC_BITRATE = "128k"
AAC_BPS = 128_000

# ----- Divisao em partes
MAX_PARTS = 3
# Cada parte mira ate 95% do limite (o corte so acontece em keyframes).
PART_FILL = 0.95
PART_LABEL = " - PART{n}"
# Diferenca de duracao aceita em cada corte (com taxa constante fica perto de 0,05 s).
MAX_CUT_LOSS = 0.3
# Margem do bitrate maximo calculado no ultimo recurso (container, variacao do encoder).
SIZE_SAFETY = 0.93
MIN_VIDEO_BPS = 300_000

X264_PRESETS = (
    "ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow",
)


def available():
    return bool(FFMPEG and FFPROBE)


def configure(crf=None, preset=None):
    global X264_CRF, X264_PRESET
    if crf is not None:
        X264_CRF = int(crf)
    if preset is not None:
        X264_PRESET = preset


class SplitGapError(RuntimeError):
    """O corte sem reconverter perdeu trechos (video com taxa de quadros variavel)."""


@dataclass
class VideoInfo:
    format_names: set
    duration: float
    size: int
    vindex: int = 0
    vcodec: str | None = None
    vprofile: str | None = None
    pix_fmt: str | None = None
    width: int = 0
    height: int = 0
    fps: float = 0.0          # 0 = taxa variavel/desconhecida (comum em gravacao de tela)
    vbitrate: int = 0
    field_order: str | None = None
    acodec: str | None = None
    channels: int = 0
    audio_streams: int = 0
    faststart: bool | None = None

    @property
    def is_mp4(self):
        return bool(self.format_names & MP4_FORMATS)

    @property
    def short_side(self):
        return min(self.width, self.height)


@dataclass
class VideoReport:
    path: Path
    info: VideoInfo | None
    max_bytes: int = 0
    problems: list = field(default_factory=list)      # impede/atrapalha o envio ou o streaming
    suggestions: list = field(default_factory=list)   # melhora, mas e opcional
    warnings: list = field(default_factory=list)      # ffmpeg nao resolve; so avisar
    reencode_video: bool = False
    reencode_audio: bool = False
    remux: bool = False
    parts: int = 1                                    # partes previstas sem reconverter
    video_filters: list = field(default_factory=list)

    @property
    def needs_conversion(self):
        return self.remux or self.reencode_video or self.reencode_audio or self.parts > 1

    @property
    def action(self):
        steps = []
        if self.reencode_video:
            steps.append("reconverter video" + (" e audio" if self.reencode_audio else ""))
        elif self.reencode_audio:
            steps.append("reconverter so o audio")
        elif self.remux:
            steps.append("reorganizar o arquivo (sem perda, rapido)")
        if self.parts > 1 and not self.reencode_video:
            steps.append(f"dividir em {self.parts} partes (sem perda)")
        elif self.reencode_video and self.info and self.info.size > self.max_bytes:
            steps.append(f"dividir em ate {MAX_PARTS} partes se ainda passar do limite")
        return ", depois ".join(steps) or "nada"


# ===== Leitura =====

def probe(path):
    """Le as propriedades do video com ffprobe. Retorna None se nao for um video legivel."""
    if not FFPROBE:
        return None
    result = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    video = next(
        (
            stream for stream in streams
            if stream.get("codec_type") == "video"
            and not stream.get("disposition", {}).get("attached_pic")
        ),
        None,
    )
    if not video:
        return None
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    audio = audios[0] if audios else {}

    info = VideoInfo(
        format_names=set(fmt.get("format_name", "").split(",")),
        duration=_float(fmt.get("duration")) or _float(video.get("duration")),
        size=int(_float(fmt.get("size")) or path.stat().st_size),
        vindex=int(video.get("index", 0)),
        vcodec=video.get("codec_name"),
        vprofile=video.get("profile"),
        pix_fmt=video.get("pix_fmt"),
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_frame_rate(video),
        vbitrate=int(_float(video.get("bit_rate"))),
        field_order=video.get("field_order"),
        acodec=audio.get("codec_name"),
        channels=int(audio.get("channels") or 0),
        audio_streams=len(audios),
    )
    if abs(_rotation(video)) % 180 == 90:
        # Gravado em pe no celular: o quadro e salvo deitado com uma marca de rotacao.
        info.width, info.height = info.height, info.width
    if not info.duration:
        # Gravacoes de navegador (WebM do MediaRecorder) nao gravam a duracao no cabecalho.
        info.duration = scan_duration(path)
    if not info.vbitrate and info.duration:
        # MKV/WebM nao informam o bitrate por faixa: estima pelo arquivo inteiro.
        info.vbitrate = int(info.size * 8 / info.duration)
    if info.is_mp4:
        info.faststart = is_faststart(path)
    return info


def scan_duration(path):
    """Duracao lendo o arquivo inteiro sem decodificar (rapido, limitado pelo disco)."""
    if not FFMPEG:
        return 0.0
    result = subprocess.run(
        [FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(path),
         "-map", "0", "-c", "copy", "-f", "null", "-progress", "pipe:1", "-nostats", "-"],
        capture_output=True,
        text=True,
    )
    duration = 0.0
    for line in result.stdout.splitlines():
        key, _, value = line.partition("=")
        if key == "out_time_us" and value.strip().isdigit():
            duration = int(value) / 1e6
    return duration


def is_faststart(path):
    """True se o atomo 'moov' (indice) vem antes do 'mdat' (dados) no MP4."""
    try:
        with open(path, "rb") as handle:
            while True:
                header = handle.read(8)
                if len(header) < 8:
                    return None
                size, kind = struct.unpack(">I4s", header)
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    size = struct.unpack(">Q", handle.read(8))[0]
                    handle.seek(size - 16, 1)
                elif size < 8:
                    return None
                else:
                    handle.seek(size - 8, 1)
    except (OSError, struct.error):
        return None


def _frame_rate(stream):
    # Gravacao de tela tem taxa variavel; valores absurdos (ex.: 1000/1) sao a base de tempo.
    for key in ("avg_frame_rate", "r_frame_rate"):
        rate = _fraction(stream.get(key))
        if 0 < rate <= 240:
            return rate
    return 0.0


def _rotation(stream):
    for item in stream.get("side_data_list") or []:
        if "rotation" in item:
            return int(_float(item["rotation"]))
    return int(_float((stream.get("tags") or {}).get("rotate")))


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fraction(value):
    try:
        num, den = str(value).split("/")
        return float(num) / float(den) if float(den) else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


def parts_needed(size, max_bytes):
    if size <= max_bytes:
        return 1
    return math.ceil(size / (max_bytes * PART_FILL))


# ===== Analise =====

def analyze(path, max_bytes):
    info = probe(path)
    report = VideoReport(path=path, info=info, max_bytes=max_bytes)
    if info is None:
        report.warnings.append("ffprobe nao conseguiu ler o video (arquivo corrompido?)")
        return report

    # --- Container
    if not info.is_mp4:
        report.problems.append(
            f"container {path.suffix.lower() or '?'} nao toca direto no app (precisa ser MP4)"
        )
        report.remux = True
    elif info.faststart is False:
        report.problems.append("indice do MP4 no fim do arquivo: o video so toca depois de baixar inteiro")
        report.remux = True

    # --- Video
    if info.vcodec == "hevc":
        report.suggestions.append("video em H.265/HEVC: alguns apps (desktop/web antigos) nao reproduzem")
        report.reencode_video = True
    elif info.vcodec != "h264":
        report.problems.append(f"codec de video {info.vcodec} nao e reproduzido por todos os apps (use H.264)")
        report.reencode_video = True
    if info.pix_fmt and info.pix_fmt not in {"yuv420p", "yuvj420p"}:
        report.problems.append(f"pixel format {info.pix_fmt} (10 bits/4:4:4) falha em muitos celulares")
        report.reencode_video = True
    if info.field_order not in {None, "progressive", "unknown"}:
        report.suggestions.append("video entrelacado: aparecem 'listras' em movimento")
        report.reencode_video = True
        report.video_filters.append("yadif")

    if info.short_side > MAX_SHORT_SIDE:
        report.suggestions.append(
            f"resolucao {info.width}x{info.height}: acima de 1080p pesa muito para assistir no celular"
        )
        report.reencode_video = True
        if info.width >= info.height:
            report.video_filters.append(f"scale=-2:{MAX_SHORT_SIDE}")
        else:
            report.video_filters.append(f"scale={MAX_SHORT_SIDE}:-2")
    if info.fps > MAX_FPS + 1:
        report.suggestions.append(f"{info.fps:.0f} fps: acima de {MAX_FPS} fps so aumenta o arquivo")
        report.reencode_video = True

    limit = next(rate for side, rate in HIGH_BITRATE if info.short_side <= side)
    if info.vbitrate > limit * 1.5:
        report.suggestions.append(
            f"bitrate alto ({info.vbitrate / 1e6:.1f} Mb/s): da para reduzir bem o tamanho "
            "sem perda visivel"
        )
        report.reencode_video = True

    # --- Tamanho
    if not info.duration:
        report.warnings.append("duracao desconhecida: o progresso e a divisao em partes nao funcionam")
    if info.size > max_bytes:
        parts = parts_needed(info.size, max_bytes)
        limit_text = _size(max_bytes)
        if not report.reencode_video and parts <= MAX_PARTS:
            report.parts = parts
            report.problems.append(
                f"{_size(info.size)} passa do limite de {limit_text}: "
                f"vai em {parts} partes (PART1..PART{parts}), sem reconverter"
            )
        elif not report.reencode_video:
            report.problems.append(
                f"{_size(info.size)} precisaria de {parts} partes de {limit_text}: "
                f"reconverter (CRF {X264_CRF}) para caber em ate {MAX_PARTS}"
            )
            report.reencode_video = True
        else:
            report.problems.append(
                f"{_size(info.size)} passa do limite de {limit_text}: o tamanho final so se sabe "
                f"depois de converter; se ainda passar, vai em ate {MAX_PARTS} partes"
            )

    # --- Audio
    if not info.audio_streams:
        report.warnings.append("SEM AUDIO: confira se a gravacao/download pegou a faixa de audio")
    else:
        if info.acodec not in GOOD_AUDIO:
            report.problems.append(f"audio {info.acodec} nao toca em todos os apps (use AAC)")
            report.reencode_audio = True
        if info.channels > 2:
            report.suggestions.append(f"audio com {info.channels} canais: estereo basta no celular")
            report.reencode_audio = True
        if info.audio_streams > 1:
            report.suggestions.append(f"{info.audio_streams} faixas de audio: o Telegram so usa a primeira")
            report.remux = True

    if report.reencode_video and (info.width % 2 or info.height % 2) and not any(
        item.startswith("scale=") for item in report.video_filters
    ):
        # H.264 em yuv420p exige largura e altura pares (comum em gravacao de janela).
        report.video_filters.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")

    return report


def describe(report):
    lines = []
    for text in report.problems:
        lines.append(f"problema: {text}")
    for text in report.suggestions:
        lines.append(f"sugestao: {text}")
    for text in report.warnings:
        lines.append(f"aviso: {text}")
    return lines


def summary_line(info):
    if info is None:
        return "?"
    audio = f"{info.acodec} {info.channels}ch" if info.audio_streams else "sem audio"
    fps = f"{info.fps:.0f}fps" if info.fps else "fps variavel"
    return (
        f"{info.vcodec} {info.width}x{info.height} {fps} {info.vbitrate / 1e6:.1f}Mb/s, "
        f"{audio}, {format_duration(info.duration)}, {_size(info.size)}"
    )


def _size(size):
    if size >= 1024 ** 3:
        return f"{size / 1024 ** 3:.2f} GB"
    return f"{size / 1024 ** 2:.0f} MB"


def format_duration(seconds):
    hours, rest = divmod(int(seconds), 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}"


# ===== Conversao =====

def ffmpeg_args(report, output, crf=None, max_rate=None):
    args = [
        FFMPEG or "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-i", str(report.path),
        "-map", "0:v:0", "-map", "0:a:0?",
        "-map_metadata", "0", "-map_chapters", "-1",
    ]
    if report.reencode_video:
        args += [
            "-c:v", "libx264", "-preset", X264_PRESET, "-crf", str(crf or X264_CRF),
            "-profile:v", "high", "-pix_fmt", "yuv420p",
            # Taxa constante: gravacao de tela tem taxa variavel, e os "buracos" nos timestamps
            # fazem o corte em partes perder audio. Quadro parado repetido quase nao ocupa espaco.
            "-fps_mode", "cfr", "-r", output_fps(report.info),
            "-force_key_frames", f"expr:gte(t,n_forced*{KEYFRAME_SECONDS})",
        ]
        if max_rate:
            args += ["-maxrate", str(max_rate), "-bufsize", str(max_rate * 2)]
        if report.video_filters:
            args += ["-vf", ",".join(report.video_filters)]
    else:
        args += ["-c:v", "copy"]

    if report.reencode_audio:
        args += ["-c:a", "aac", "-b:a", AAC_BITRATE, "-ac", str(min(report.info.channels or 2, 2))]
    else:
        args += ["-c:a", "copy"]

    args += ["-movflags", "+faststart", "-f", "mp4", str(output)]
    return args


def output_fps(info):
    fps = info.fps if info else 0
    if fps > MAX_FPS + 1:
        fps = MAX_FPS
    elif not fps:
        fps = DEFAULT_FPS
    return f"{fps:.3f}".rstrip("0").rstrip(".")


def shell_command(report, output):
    """Comando equivalente para copiar e rodar no terminal."""
    args = ffmpeg_args(report, output)
    args[0] = "ffmpeg"
    return shlex.join(args)


def split_note(report):
    if report.parts > 1 and not report.reencode_video:
        return (
            f"a divisao em {report.parts} partes e feita pelo ttm.py (cortes em keyframes, "
            "com tamanhos parecidos)"
        )
    return None


def convert(report, originals_dir, max_bytes):
    """Converte e, se preciso, divide o video em partes.

    O original vai para originals_dir e o(s) MP4 novo(s) ficam no lugar dele.
    Retorna (lista de arquivos novos, copia do original). Se algo falhar, ou com Ctrl+C,
    o original fica intacto e os temporarios sao apagados.
    """
    source = report.path
    duration = report.info.duration if report.info else 0
    temps = []
    try:
        work = source
        if report.reencode_video or report.reencode_audio or report.remux:
            work = _temp(source, "convertendo")
            temps.append(work)
            _encode(report, work, duration, X264_CRF, None)
            parts = parts_needed(work.stat().st_size, max_bytes)
            if parts > MAX_PARTS:
                work = _fallback_encode(report, work, duration, max_bytes, parts, temps)

        pieces = [work]
        parts = parts_needed(work.stat().st_size, max_bytes)
        if parts > MAX_PARTS:
            raise RuntimeError(
                f"{_size(work.stat().st_size)} precisaria de {parts} partes (maximo {MAX_PARTS}). "
                "Reconverta com CRF maior (--crf) ou corte o video"
            )
        if parts > 1:
            try:
                pieces = _split(work, duration, parts, max_bytes, temps)
            except SplitGapError:
                if work is not source:
                    raise
                print("  O corte sem reconverter perderia trechos (taxa de quadros variavel). Reconvertendo.")
                forced = dataclasses.replace(report, reencode_video=True, reencode_audio=True)
                work = _temp(source, "convertendo")
                temps.append(work)
                _encode(forced, work, duration, X264_CRF, None)
                parts = parts_needed(work.stat().st_size, max_bytes)
                if parts > MAX_PARTS:
                    work = _fallback_encode(forced, work, duration, max_bytes, parts, temps)
                    parts = parts_needed(work.stat().st_size, max_bytes)
                pieces = _split(work, duration, parts, max_bytes, temps) if parts > 1 else [work]

        # Tudo certo: guarda o original e coloca os arquivos novos no lugar.
        originals_dir.mkdir(parents=True, exist_ok=True)
        backup = _unique(originals_dir / source.name)
        shutil.move(str(source), str(backup))
        targets = []
        for number, piece in enumerate(pieces, start=1):
            label = PART_LABEL.format(n=number) if len(pieces) > 1 else ""
            target = _unique(source.with_name(f"{source.stem}{label}.mp4"))
            piece.replace(target)
            targets.append(target)
        return targets, backup
    finally:
        for temp in temps:
            temp.unlink(missing_ok=True)


def _temp(source, tag):
    path = source.with_name(f".{source.stem}.{tag}.mp4")
    path.unlink(missing_ok=True)
    return path


def _encode(report, output, duration, crf, max_rate):
    args = ffmpeg_args(report, output, crf=crf, max_rate=max_rate)
    _run_ffmpeg(args, duration, "convertendo")
    result = probe(output)
    if result is None or (duration and result.duration < duration * 0.995 - 1):
        raise RuntimeError("o arquivo convertido ficou incompleto; o original foi mantido")


def _fallback_encode(report, first, duration, max_bytes, parts, temps):
    """Ultimo recurso: CRF maior + bitrate maximo calculado para caber em MAX_PARTS partes."""
    if not duration:
        raise RuntimeError("duracao desconhecida: nao da para calcular o bitrate para caber nas partes")
    budget = MAX_PARTS * max_bytes * PART_FILL * SIZE_SAFETY * 8 / duration
    rate = int(budget - (AAC_BPS if report.info.audio_streams else 0))
    if rate < MIN_VIDEO_BPS:
        raise RuntimeError(f"video longo demais para caber em {MAX_PARTS} partes; corte o video")
    crf = max(X264_CRF, FALLBACK_CRF)
    print(
        f"  Com CRF {X264_CRF} ficou com {_size(first.stat().st_size)} ({parts} partes). "
        f"Reconvertendo com CRF {crf} e no maximo {rate / 1e6:.1f} Mb/s para caber em {MAX_PARTS}."
    )
    retry = _temp(report.path, "convertendo2")
    temps.append(retry)
    forced = dataclasses.replace(report, reencode_video=True)
    _encode(forced, retry, duration, crf, rate)
    first.unlink(missing_ok=True)
    return retry


def _run_ffmpeg(args, duration, label):
    args = list(args)
    args[1:1] = ["-progress", "pipe:1", "-nostats", "-loglevel", "error"]
    started = time.monotonic()
    process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in process.stdout:
            key, _, value = line.strip().partition("=")
            if key == "out_time_us" and value.isdigit() and duration:
                done = int(value) / 1e6
                percent = min(done * 100 / duration, 100)
                elapsed = time.monotonic() - started
                speed = done / elapsed if elapsed else 0
                eta = (duration - done) / speed if speed else 0
                print(
                    f"\r  {label}: {percent:5.1f}%  ({speed:.1f}x, faltam ~{format_duration(eta)})  ",
                    end="",
                    flush=True,
                )
        stderr = process.stderr.read()
        process.wait()
    except BaseException:
        # Ctrl+C ou erro: nao deixa o ffmpeg rodando.
        process.kill()
        process.wait()
        raise
    print()
    if process.returncode != 0:
        raise RuntimeError(f"ffmpeg falhou: {stderr.strip()[-500:] or process.returncode}")


# ===== Divisao em partes =====

def split_points(path, video_index, parts):
    """Tempos de corte (em keyframes) que deixam as partes com tamanhos parecidos."""
    process = subprocess.Popen(
        [FFPROBE, "-v", "error", "-show_entries", "packet=stream_index,pts_time,size,flags",
         "-of", "compact=p=0", str(path)],
        stdout=subprocess.PIPE,
        text=True,
    )
    keyframes = []  # (tempo, bytes acumulados antes do keyframe)
    total = 0
    try:
        for line in process.stdout:
            fields = dict(item.split("=", 1) for item in line.strip().split("|") if "=" in item)
            size = int(fields.get("size", 0) or 0)
            if (
                fields.get("stream_index") == str(video_index)
                and fields.get("flags", "").startswith("K")
                and fields.get("pts_time", "N/A") != "N/A"
            ):
                keyframes.append((float(fields["pts_time"]), total))
            total += size
    finally:
        process.wait()

    cuts = []
    for number in range(1, parts):
        target = total * number / parts
        candidates = [item for item in keyframes if not cuts or item[0] > cuts[-1] + 1]
        if not candidates:
            break
        cuts.append(min(candidates, key=lambda item: abs(item[1] - target))[0])
    return [cut for cut in cuts if cut > 1]


def _split(work, duration, parts, max_bytes, temps):
    work_info = probe(work)
    while parts <= MAX_PARTS:
        print(f"  dividindo em {parts} partes...")
        cuts = split_points(work, work_info.vindex if work_info else 0, parts)
        if len(cuts) != parts - 1:
            raise RuntimeError("nao ha keyframes suficientes para dividir o video")
        pattern = work.with_name(f".{work.stem.lstrip('.')}.parte%d.mp4")
        args = [
            FFMPEG, "-hide_banner", "-nostdin", "-y", "-i", str(work),
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-map_metadata", "0",
            "-f", "segment",
            # Um centesimo antes do keyframe escolhido, para o corte cair exatamente nele.
            "-segment_times", ",".join(f"{cut - 0.01:.3f}" for cut in cuts),
            "-reset_timestamps", "1",
            "-segment_format", "mp4", "-segment_format_options", "movflags=+faststart",
            "-segment_start_number", "1",
            str(pattern),
        ]
        pieces = [Path(str(pattern) % number) for number in range(1, parts + 1)]
        temps.extend(pieces)
        _run_ffmpeg(args, duration, "dividindo")

        missing = [piece for piece in pieces if not piece.exists()]
        if missing:
            raise RuntimeError("o ffmpeg nao gerou todas as partes")
        total = sum((probe(piece) or VideoInfo(set(), 0, 0)).duration for piece in pieces)
        expected = work_info.duration if work_info and work_info.duration else duration
        if expected and total < expected - MAX_CUT_LOSS * (parts - 1) - 0.2:
            for piece in pieces:
                piece.unlink(missing_ok=True)
            raise SplitGapError(
                f"as partes somam {total:.1f}s de {expected:.1f}s; o original foi mantido"
            )
        if all(piece.stat().st_size <= max_bytes for piece in pieces):
            return pieces
        # Uma parte ficou grande (trecho com muito movimento): tenta com uma parte a mais.
        for piece in pieces:
            piece.unlink(missing_ok=True)
        parts += 1
    raise RuntimeError(f"nao foi possivel dividir em ate {MAX_PARTS} partes dentro do limite")


def _unique(path):
    if not path.exists():
        return path
    for counter in range(1, 1000):
        candidate = path.with_name(f"{path.stem}_{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Nao foi possivel criar nome unico para {path}")


# ===== Envio =====

def make_thumbnail(path, info, output):
    """Gera a miniatura JPEG (ate 320 px) que aparece no chat antes de dar play."""
    if not FFMPEG or info is None:
        return None
    moment = min(info.duration * 0.1, 60) if info.duration else 0
    result = subprocess.run(
        [
            FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
            "-ss", f"{moment:.2f}", "-i", str(path), "-frames:v", "1",
            "-vf", "scale='if(gte(iw,ih),320,-2)':'if(gte(iw,ih),-2,320)'",
            "-q:v", "5", str(output),
        ],
        capture_output=True,
    )
    if result.returncode != 0 or not output.exists() or output.stat().st_size > 200 * 1024:
        return None
    return output


def video_attributes(info, types):
    """Duracao e dimensoes exatas para o Telegram (sem depender do hachoir)."""
    if info is None or not info.width or not info.height:
        return None
    return [
        types.DocumentAttributeVideo(
            duration=info.duration,
            w=info.width,
            h=info.height,
            supports_streaming=True,
        )
    ]
