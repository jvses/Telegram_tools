#!/usr/bin/env python3
import json
import re
import subprocess
import sys
from pathlib import Path

# Extensões que serão processadas quando nenhum arquivo for passado
EXTENSOES = {".mp4", ".mkv", ".webm"}

# Limiar de ruído para considerar silêncio.
# -30dB costuma funcionar bem. Ajuste se necessário:
# -35dB é mais sensível; -25dB é mais restritivo.
NOISE_DB = "-30dB"


def rodar(cmd):
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding='utf-8',
        errors='replace'
    )


def obter_faixas_audio(video):
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "a",
        "-show_entries",
        "stream=index,codec_name,channels,duration:stream_tags=language,title:format=duration",
        "-of", "json",
        str(video)
    ]
    p = rodar(cmd)
    if p.returncode != 0:
        raise RuntimeError(p.stderr.strip())

    dados = json.loads(p.stdout)
    faixas = dados.get("streams", [])
    dur_format = dados.get("format", {}).get("duration")

    try:
        dur_format = float(dur_format) if dur_format else None
    except ValueError:
        dur_format = None

    for f in faixas:
        f["_duracao_format"] = dur_format

    return faixas


def detectar_silencio(video, stream_index, tolerancia, duracao):
    cmd = [
        "ffmpeg", "-hide_banner", "-nostats",
        "-i", str(video),
        "-map", f"0:{stream_index}",
        "-vn", "-sn", "-dn",
        "-af", f"silencedetect=noise={NOISE_DB}:d={tolerancia}",
        "-f", "null", "-"
    ]
    p = rodar(cmd)
    texto = p.stderr

    intervalos = []
    inicio_atual = None

    for linha in texto.splitlines():
        m = re.search(r"silence_start:\s*([0-9.]+)", linha)
        if m:
            inicio_atual = float(m.group(1))
            continue

        m = re.search(r"silence_end:\s*([0-9.]+)", linha)
        if m and inicio_atual is not None:
            fim = float(m.group(1))
            if fim - inicio_atual >= tolerancia:
                intervalos.append((inicio_atual, fim))
            inicio_atual = None

    # Silêncio que começa e vai até o final do áudio
    if inicio_atual is not None:
        fim = duracao if duracao is not None else inicio_atual + tolerancia
        if fim - inicio_atual >= tolerancia:
            intervalos.append((inicio_atual, fim))

    return intervalos


def formatar_tempo(segundos):
    ms_total = int(round(segundos * 1000))
    h = ms_total // 3600000
    ms_total %= 3600000
    m = ms_total // 60000
    ms_total %= 60000
    s = ms_total // 1000
    ms = ms_total % 1000
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"

def formatar_duracao(segundos):
    total = int(round(segundos))
    h = total // 3600
    total %= 3600
    m = total // 60
    s = total % 60

    if h > 0:
        return f"{h}h {m}min {s}s"
    elif m > 0:
        return f"{m}min {s}s"
    else:
        return f"{s}s"

def coletar_videos(args):
    caminhos = []

    if not args:
        args = ["."]

    for arg in args:
        p = Path(arg)

        if p.is_dir():
            for filho in p.iterdir():
                if filho.is_file() and filho.suffix.lower() in EXTENSOES:
                    caminhos.append(filho)

        elif p.is_file() and p.suffix.lower() in EXTENSOES:
            caminhos.append(p)

        else:
            print(f"Ignorando (não é vídeo suportado): {arg}")

    return sorted(set(caminhos))


def main():
    videos = coletar_videos(sys.argv[1:])

    if not videos:
        print("Nenhum vídeo .mp4, .mkv ou .webm encontrado.")
        return

    entrada = input("Tolerância de silêncio em segundos (inteiro): ").strip()

    try:
        tolerancia = int(entrada)
    except ValueError:
        print("Erro: informe um número inteiro de segundos.")
        return

    if tolerancia <= 0:
        print("Erro: a tolerância deve ser maior que zero.")
        return

    print(f"\nTolerância escolhida: {tolerancia} segundos")
    print(f"Limiar de ruído: {NOISE_DB}\n")

    for video in videos:
        print(f"Processando: {video}")

        try:
            faixas = obter_faixas_audio(video)
        except Exception as e:
            print(f"  Erro ao ler faixas de áudio: {e}")
            continue

        if not faixas:
            print("  Nenhuma faixa de áudio encontrada.")
            continue

        saida = video.with_name(
            f"{video.stem}_info_{tolerancia:02d}segundos.txt"
        )

        with saida.open("w", encoding="utf-8") as f:
            f.write(f"Arquivo: {video}\n")
            f.write(f"Tolerância de silêncio: {tolerancia} segundos\n")
            f.write(f"Limiar de ruído: {NOISE_DB}\n\n")

            for i, faixa in enumerate(faixas, start=1):
                idx = faixa["index"]
                codec = faixa.get("codec_name", "?")
                lang = faixa.get("tags", {}).get("language", "?")
                titulo = faixa.get("tags", {}).get("title", "")

                try:
                    duracao = faixa.get("duration")
                    if duracao in (None, "N/A"):
                        duracao = faixa.get("_duracao_format")
                    else:
                        duracao = float(duracao)
                except Exception:
                    duracao = faixa.get("_duracao_format")

                extras = f", título: {titulo}" if titulo else ""
                f.write(
                    f"Faixa de áudio #{i} "
                    f"(stream 0:{idx}, codec {codec}, idioma {lang}{extras})\n"
                )

                print(f"  Faixa #{i} (stream 0:{idx})...")

                try:
                    intervalos = detectar_silencio(
                        video, idx, tolerancia, duracao
                    )
                except Exception as e:
                    f.write(f"  ERRO ao analisar: {e}\n\n")
                    print(f"    Erro: {e}")
                    continue

                if not intervalos:
                    f.write(
                        f"  Nenhum silêncio >= {tolerancia}s encontrado.\n\n"
                    )
                else:
                    f.write(f"  Silêncios >= {tolerancia}s:\n")
                    for ini, fim in intervalos:
                        f.write(
                            f"  {formatar_tempo(ini)} -- {formatar_tempo(fim)} "
                            f"( {formatar_duracao(fim - ini)} )\n"
                        )
                    f.write("\n")

        print(f"  Informações salvas em: {saida}\n")


if __name__ == "__main__":
    main()
