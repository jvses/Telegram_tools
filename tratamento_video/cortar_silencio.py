#!/usr/bin/env python3
"""Detecta pausas (silencio) em gravacoes de aula, ajuda a revisar e corta o video.

Fluxo:
  0. calibrar  -> (opcional, uma vez por curso) mostra quais regioes da tela sao ignoradas
                  na analise (cameras dos alunos, miniaturas de participantes...) e salva
                  essa escolha para todos os videos da pasta.
  1. detectar  -> gera "<video>.cortes.txt" ao lado do video, com cada pausa marcada como
                  CORTAR (silencio com a tela parada) ou REVISAR (silencio com o conteudo
                  mudando: demonstracao de software, simulacao, escrita na mesa digitalizadora).
  2. revisar   -> abre cada pausa REVISAR no player (mpv, VLC, MPlayer, ffplay...) e pergunta
                  o que fazer. Tambem da para editar o .cortes.txt a mao.
  3. cortar    -> gera o video sem os trechos CORTAR no lugar do original, que vai (com a lista
                  de cortes) para <pasta>/orig/. Se o video ja e H.264/AAC, corta sem reconverter
                  (mesma qualidade); senao (WebM etc.) converte para H.264/AAC/MP4.

Exemplos:
  python cortar_silencio.py calibrar "aula 07.mp4"
  python cortar_silencio.py detectar "aula 07.mp4"
  python cortar_silencio.py revisar "aula 07.mp4" --player vlc
  python cortar_silencio.py cortar "aula 07.mp4" --saida ../Telegram_topic_manager/input/turma_2026

So usa a biblioteca padrao do Python e os programas ffmpeg/ffprobe (nao precisa do venv).
"""

import argparse
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "Telegram_topic_manager"))
import video_tools  # noqa: E402  (mesmos parametros de conversao do ttm.py)


EXTENSOES = {".mp4", ".mkv", ".webm", ".mov", ".m4v", ".avi"}
SUFIXO_CORTES = ".cortes.txt"
PASTA_ORIGINAIS = "orig"                   # originais depois do corte
ARQUIVO_CONFIG = ".cortar_silencio.json"   # mascara da tela, por pasta (curso)
ARQUIVO_PREVIA = "mascara_tela.png"

# ----- Deteccao de silencio
JANELA = 0.25            # s: resolucao da medicao de volume
MINIMO = 10.0            # s: pausa minima para entrar na lista
# s: ruido curto (clique, mouse, batida na mesa) entre duas pausas e ignorado. Valores maiores
# passam a engolir palavras curtas ("ok", "sim") de quem responde a uma pergunta.
JUNTAR = 0.5
MARGEM = 0.5             # s: mantidos antes e depois de cada pausa, para nao cortar o fim da fala
SILENCIO_DIGITAL = -100  # dB: abaixo disso e silencio absoluto (supressao de ruido do app de reuniao)
ABAIXO_DA_FALA = 20      # dB: limiar automatico = nivel tipico da fala - 20 dB...
ACIMA_DO_RUIDO = 6       # dB: ...mas pelo menos 6 dB acima do ruido de fundo

# ----- Atividade de tela (grade de 16 x 9 celulas)
GRADE = (16, 9)
AMOSTRA_TELA = 2.0       # s entre quadros comparados
DIFERENCA_PIXEL = 24     # diferenca de brilho (0-255) para o pixel contar como alterado
CELULA_ATIVA = 0.01      # fracao dos pixels da celula alterados para ela contar como "mudou"
# Mascara automatica: celulas que mudam o tempo todo (cameras, miniaturas de participantes).
# Amostra JANELAS_MASCARA trechos espalhados pela aula e usa a MEDIANA da atividade por trecho:
# camera mexe em todo trecho (mediana 13-66% numa aula real); slide e demonstracao mudam em
# rajadas (mediana 0%), entao nao entram na mascara mesmo com muita troca de slide.
CRONICA = 0.10
JANELAS_MASCARA = 24
JANELAS_MINIMAS = 8      # video curto demais para estimar: sem mascara automatica
DURACAO_JANELA_MASCARA = 30.0
# Classificacao da pausa, olhando so fora da mascara:
TELA_PARADA = 10.0       # % maximo das amostras com alguma mudanca (troca de slide e relogio cabem)
MOVIMENTO_MIN = 0.10     # % do quadro mudando em 2 s para contar como movimento (cursor e icone nao)
MASCARA_MAXIMA = 0.70    # ignorando mais que isso da tela, a analise nao e confiavel

ACOES = ("CORTAR", "MANTER", "REVISAR")

# ----- Players: (nome, executavel, id flatpak). A ordem e a de preferencia.
PLAYERS = [
    ("mpv", "mpv", "io.mpv.Mpv"),
    ("vlc", "vlc", "org.videolan.VLC"),
    ("mplayer", "mplayer", None),
    ("ffplay", "ffplay", None),
]


# ===== Utilidades =====

def checar_ffmpeg():
    if not video_tools.available():
        sys.exit("ffmpeg/ffprobe nao encontrados. Instale o ffmpeg (ex.: sudo pacman -S ffmpeg).")


def fmt_tempo(segundos):
    ms = int(round(max(segundos, 0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h}:{m:02d}:{s:02d}.{ms:03d}"


def fmt_duracao(segundos):
    total = int(round(segundos))
    h, resto = divmod(total, 3600)
    m, s = divmod(resto, 60)
    if h:
        return f"{h}h{m:02d}min"
    if m:
        return f"{m}min{s:02d}s"
    return f"{s}s"


def ler_tempo(texto):
    """Aceita 1:02:03.5, 02:03, 123.4 ou 123,4."""
    texto = texto.strip().replace(",", ".")
    partes = texto.split(":")
    if not 1 <= len(partes) <= 3:
        raise ValueError(f"tempo invalido: {texto!r}")
    total = 0.0
    for parte in partes:
        total = total * 60 + float(parte)
    if total < 0:
        raise ValueError(f"tempo negativo: {texto!r}")
    return total


def perguntar(texto, padrao=""):
    resposta = input(texto).strip()
    return resposta or padrao


def sim_nao(texto, padrao=False):
    sufixo = " [S/n]: " if padrao else " [s/N]: "
    resposta = input(texto + sufixo).strip().lower()
    if not resposta:
        return padrao
    return resposta in {"s", "sim", "y", "yes"}


def arquivo_cortes(video):
    return video.with_name(video.name + SUFIXO_CORTES)


def video_de(caminho):
    """Aceita o video ou o .cortes.txt e devolve (video, arquivo de cortes)."""
    caminho = Path(caminho)
    if caminho.name.endswith(SUFIXO_CORTES):
        return caminho.with_name(caminho.name[: -len(SUFIXO_CORTES)]), caminho
    return caminho, arquivo_cortes(caminho)


def chave_natural(nome):
    return [int(parte) if parte.isdigit() else parte.casefold() for parte in re.split(r"(\d+)", nome)]


def coletar_videos(argumentos):
    videos = []
    for argumento in argumentos or ["."]:
        caminho = Path(argumento)
        if caminho.is_dir():
            videos += [
                filho for filho in caminho.iterdir()
                if filho.is_file() and filho.suffix.lower() in EXTENSOES and not filho.name.startswith(".")
            ]
        elif caminho.is_file() and caminho.suffix.lower() in EXTENSOES:
            videos.append(caminho)
        else:
            print(f"Ignorando (nao e um video suportado): {argumento}")
    return sorted(set(videos), key=lambda item: chave_natural(item.name))


def faixas_audio(video):
    resultado = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=index,codec_name,channels:stream_tags=language,title", "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.strip() or "ffprobe falhou")
    return json.loads(resultado.stdout).get("streams", [])


def ler_video(video):
    info = video_tools.probe(video)
    if info is None or not info.duration:
        raise RuntimeError("nao foi possivel ler o video (ou a duracao)")
    return info


# ===== 1. Volume e silencio =====

def medir_volume(video, faixa):
    """Nivel RMS (dB) a cada JANELA segundos de uma faixa de audio."""
    amostras = int(8000 * JANELA)
    filtro = (
        f"aformat=channel_layouts=mono,aresample=8000,asetnsamples=n={amostras}:p=0,"
        "astats=metadata=1:reset=1:measure_overall=RMS_level:measure_perchannel=none,"
        "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-"
    )
    processo = subprocess.Popen(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(video),
         "-map", f"0:a:{faixa}", "-vn", "-sn", "-dn", "-af", filtro, "-f", "null", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    niveis = []
    tempo = None
    for linha in processo.stdout:
        if linha.startswith("frame:"):
            achado = re.search(r"pts_time:(-?[\d.]+)", linha)
            tempo = float(achado.group(1)) if achado else None
        elif "RMS_level=" in linha and tempo is not None:
            valor = linha.split("=", 1)[1].strip()
            # Silencio digital vem como -inf.
            niveis.append((tempo, -120.0 if "inf" in valor else max(float(valor), -120.0)))
    erro = processo.stderr.read()
    processo.wait()
    if processo.returncode != 0:
        raise RuntimeError(erro.strip()[-300:] or "ffmpeg falhou ao medir o volume")
    return niveis


def otsu(valores):
    """Limiar que melhor separa dois grupos (fala x fundo) num histograma de 1 dB."""
    menor, maior = int(math.floor(min(valores))), int(math.ceil(max(valores)))
    histograma = [0] * (maior - menor + 1)
    for valor in valores:
        histograma[int(valor) - menor] += 1
    total = len(valores)
    soma_total = sum((indice + menor) * qtd for indice, qtd in enumerate(histograma))
    peso = soma = 0
    melhor = (-1.0, menor)
    for indice, qtd in enumerate(histograma):
        peso += qtd
        soma += (indice + menor) * qtd
        if peso in (0, total):
            continue
        media_baixa = soma / peso
        media_alta = (soma_total - soma) / (total - peso)
        variancia = peso * (total - peso) * (media_baixa - media_alta) ** 2
        if variancia > melhor[0]:
            melhor = (variancia, indice + menor)
    return melhor[1]


@dataclass
class Limiar:
    ruido: float
    fala: float
    valor: float
    digital: float          # % do audio em silencio absoluto
    automatico: bool


def calcular_limiar(niveis, fixo=None):
    """Limiar pela fala, nao pelo silencio.

    Apps de reuniao com supressao de ruido zeram o audio (-120 dB) nas pausas; um limiar
    "ruido + X" ficaria em -100 dB e qualquer ruido quebraria as pausas. Por isso: separa
    fala e fundo (Otsu, sem o silencio digital), pega o nivel tipico da fala e fica
    ABAIXO_DA_FALA dB abaixo dele, pelo menos ACIMA_DO_RUIDO dB acima do fundo.
    """
    valores = [nivel for _, nivel in niveis]
    digital = sum(1 for valor in valores if valor <= SILENCIO_DIGITAL) * 100 / len(valores)
    audiveis = [valor for valor in valores if valor > SILENCIO_DIGITAL] or [-120.0]
    corte = otsu(audiveis) if len(set(audiveis)) > 1 else audiveis[0]
    fala = sorted(valor for valor in audiveis if valor >= corte)
    fundo = sorted(valor for valor in audiveis if valor < corte)
    nivel_fala = fala[len(fala) // 2] if fala else -20.0
    nivel_fundo = fundo[len(fundo) // 5] if fundo else -120.0
    if fixo is not None:
        return Limiar(nivel_fundo, nivel_fala, fixo, digital, False)
    valor = max(nivel_fala - ABAIXO_DA_FALA, nivel_fundo + ACIMA_DO_RUIDO)
    return Limiar(nivel_fundo, nivel_fala, valor, digital, True)


def trechos_silenciosos(mascara, duracao):
    """Converte a mascara por janela em intervalos (inicio, fim)."""
    trechos = []
    inicio = None
    for indice, silencioso in enumerate(mascara):
        tempo = indice * JANELA
        if silencioso and inicio is None:
            inicio = tempo
        elif not silencioso and inicio is not None:
            trechos.append([inicio, tempo])
            inicio = None
    if inicio is not None:
        trechos.append([inicio, duracao])
    return trechos


def juntar_trechos(trechos, intervalo_maximo):
    """Une pausas separadas por um ruido curto (o problema das linhas 8 e 9 do .txt antigo)."""
    unidos = []
    contagem = []
    for inicio, fim in trechos:
        if unidos and inicio - unidos[-1][1] <= intervalo_maximo:
            unidos[-1][1] = fim
            contagem[-1] += 1
        else:
            unidos.append([inicio, fim])
            contagem.append(1)
    return unidos, contagem


# ===== 2. Tela: grade de atividade e mascara =====

def _celulas():
    return GRADE[0] * GRADE[1]


def _ler_grades(dados):
    tamanho = _celulas() * 2
    return [
        [valor / 65535 for valor in struct.unpack(f"<{_celulas()}H", dados[i:i + tamanho])]
        for i in range(0, len(dados) - tamanho + 1, tamanho)
    ]


def _binarizar_para_grade():
    largura, altura = GRADE
    return (
        f"lut=c0='if(gt(val,{DIFERENCA_PIXEL}),255,0)',format=gray16le,"
        f"scale={largura}:{altura}:flags=area"
    )


def amostrar_grade(video, inicio, duracao):
    """Para cada par de quadros (a cada AMOSTRA_TELA s): fracao alterada de cada celula."""
    if duracao < AMOSTRA_TELA * 2:
        return []
    filtro = (
        f"fps=1/{AMOSTRA_TELA},scale=640:360,format=gray,tblend=all_mode=difference,"
        + _binarizar_para_grade()
    )
    resultado = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-ss", f"{inicio:.3f}", "-t", f"{duracao:.3f}", "-i", str(video),
         "-map", "0:v:0", "-an", "-sn", "-vf", filtro, "-f", "rawvideo", "-pix_fmt", "gray16le", "-"],
        capture_output=True,
    )
    return _ler_grades(resultado.stdout) if resultado.returncode == 0 else []


def dilatar(celulas):
    """Inclui as celulas vizinhas (bordas das cameras)."""
    largura, altura = GRADE
    resultado = set(celulas)
    for celula in celulas:
        x, y = celula % largura, celula // largura
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if 0 <= x + dx < largura and 0 <= y + dy < altura:
                    resultado.add((y + dy) * largura + x + dx)
    return resultado


def estimar_mascara(video, duracao):
    """Celulas que mudam a aula inteira (cameras): amostra trechos espalhados pela aula."""
    janelas = min(JANELAS_MASCARA, int(duracao // (DURACAO_JANELA_MASCARA * 2)))
    if janelas < JANELAS_MINIMAS:
        # Com poucos trechos, uma demonstracao longa pareceria "camera" e seria ignorada.
        print(f"  video curto ({fmt_duracao(duracao)}): sem mascara automatica")
        return set()
    passo = duracao / janelas
    taxas = [[] for _ in range(_celulas())]
    for numero in range(janelas):
        print(f"\r  mascara da tela: {numero + 1}/{janelas}", end="", flush=True)
        inicio = numero * passo + max(passo - DURACAO_JANELA_MASCARA, 0) / 2
        grades = amostrar_grade(video, inicio, min(DURACAO_JANELA_MASCARA, duracao - inicio))
        if not grades:
            continue
        for celula in range(_celulas()):
            ativas = sum(1 for grade in grades if grade[celula] > CELULA_ATIVA)
            taxas[celula].append(ativas / len(grades))
    print()
    if not taxas[0]:
        return set()
    cronicas = {
        celula for celula, valores in enumerate(taxas)
        if valores and sorted(valores)[(len(valores) - 1) // 2] >= CRONICA
    }
    return dilatar(cronicas)


def celulas_do_retangulo(texto):
    """'x,y,largura,altura' em % da tela -> celulas cujo centro esta dentro."""
    try:
        x, y, largura, altura = (float(parte) for parte in texto.split(","))
    except ValueError:
        raise argparse.ArgumentTypeError(f"use x,y,largura,altura em % (ex.: 65,5,35,60): {texto!r}")
    colunas, linhas = GRADE
    celulas = set()
    for linha in range(linhas):
        for coluna in range(colunas):
            cx, cy = (coluna + 0.5) * 100 / colunas, (linha + 0.5) * 100 / linhas
            if x <= cx <= x + largura and y <= cy <= y + altura:
                celulas.add(linha * colunas + coluna)
    return celulas


def montar_mascara_manual(ignorar, analisar):
    mascara = set()
    for texto in ignorar or []:
        mascara |= celulas_do_retangulo(texto)
    if analisar:
        dentro = set()
        for texto in analisar:
            dentro |= celulas_do_retangulo(texto)
        mascara |= set(range(_celulas())) - dentro
    return mascara


def carregar_config(pasta):
    caminho = pasta / ARQUIVO_CONFIG
    try:
        dados = json.loads(caminho.read_text(encoding="utf-8"))
        if list(dados.get("grade", [])) != list(GRADE):
            print(f"  [aviso] {caminho.name} usa outra grade; ignorado (rode 'calibrar' de novo).")
            return None
        return set(dados.get("ignorar", []))
    except (OSError, ValueError):
        return None


def salvar_config(pasta, mascara, origem):
    dados = {
        "grade": list(GRADE),
        "ignorar": sorted(mascara),
        "origem": origem,
        "salvo_em": time.strftime("%Y-%m-%d %H:%M"),
    }
    (pasta / ARQUIVO_CONFIG).write_text(json.dumps(dados, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def nome_regiao(celulas):
    """Descreve onde fica um conjunto de celulas: 'centro', 'sup. dir.'..."""
    if not celulas:
        return ""
    colunas, linhas = GRADE
    cx = sum(celula % colunas for celula in celulas) / len(celulas) / (colunas - 1)
    cy = sum(celula // colunas for celula in celulas) / len(celulas) / (linhas - 1)
    vertical = "sup." if cy < 0.34 else ("inf." if cy > 0.66 else "")
    horizontal = "esq." if cx < 0.34 else ("dir." if cx > 0.66 else "")
    return " ".join(parte for parte in (vertical, horizontal) if parte) or "centro"


@dataclass
class Tela:
    atividade: float        # % das amostras com alguma mudanca fora da mascara
    movimento: float        # % das amostras com mudanca grande (>= MOVIMENTO_MIN do quadro)
    onde: str

    @property
    def categoria(self):
        # Demonstracao, terminal e vista de galeria das cameras dao "mov": numa aula real os
        # numeros se sobrepoem, entao quem decide e a revisao (o 'f' do revisar mostra fotos).
        if self.atividade <= TELA_PARADA:
            return "parada"
        if self.movimento > TELA_PARADA:
            return "mov"
        return "pequena"

    def rotulo(self):
        categoria = self.categoria
        if categoria == "parada":
            return "parada"
        valor = self.atividade if categoria == "pequena" else self.movimento
        return f"{categoria}:{valor:.0f}%"


def analisar_tela(video, inicio, fim, mascara):
    """Compara quadros a cada AMOSTRA_TELA s. Um evento isolado (trocar o slide, o relogio
    mudar o minuto) e uma amostra so; escrita, demonstracao e cameras mudam em muitas."""
    livres = [celula for celula in range(_celulas()) if celula not in mascara]
    if not livres:
        return None
    amostras = amostrar_grade(video, inicio, fim - inicio)
    if not amostras:
        return None
    total = _celulas()
    ativas = com_movimento = 0
    contagem = [0] * total
    for grade in amostras:
        mudaram = [celula for celula in livres if grade[celula] > CELULA_ATIVA]
        if mudaram:
            ativas += 1
            for celula in mudaram:
                contagem[celula] += 1
        if sum(grade[celula] for celula in livres) / total * 100 > MOVIMENTO_MIN:
            com_movimento += 1
    onde = nome_regiao({celula for celula in livres if contagem[celula] >= max(2, ativas * 0.3)})
    quantidade = len(amostras)
    return Tela(ativas * 100 / quantidade, com_movimento * 100 / quantidade, onde)


def gerar_previa(video, mascara, momento, destino):
    """Quadro do video com a grade e as celulas ignoradas em vermelho."""
    colunas, linhas = GRADE
    caixas = "".join(
        f",drawbox=x=iw*{celula % colunas}/{colunas}:y=ih*{celula // colunas}/{linhas}"
        f":w=iw/{colunas}+1:h=ih/{linhas}+1:color=red@0.45:t=fill"
        for celula in sorted(mascara)
    )
    filtro = f"scale=1280:-2{caixas},drawgrid=w=iw/{colunas}:h=ih/{linhas}:t=1:c=white@0.7"
    resultado = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{momento:.3f}",
         "-i", str(video), "-frames:v", "1", "-vf", filtro, str(destino)],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.strip()[-300:] or "ffmpeg falhou ao gerar a previa")


def abrir_arquivo(caminho):
    for programa in ("xdg-open", "gio"):
        if shutil.which(programa):
            comando = [programa, str(caminho)] if programa == "xdg-open" else [programa, "open", str(caminho)]
            subprocess.Popen(comando, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
    return False


# ===== Lista de cortes =====

@dataclass
class Trecho:
    acao: str
    inicio: float
    fim: float
    tela: str = "?"         # parada | mov:NN% | mudou | ? (nao analisada)
    nota: str = ""

    @property
    def duracao(self):
        return self.fim - self.inicio


LEGENDA = [
    "CORTAR  = o trecho sai do video",
    "MANTER  = o trecho fica",
    "REVISAR = ainda nao decidido (no corte, conta como MANTER)",
    "TELA (fora das areas ignoradas; NN% = parte do tempo):",
    "  parada      = nada mudou (uma troca de slide ou o relogio nao contam)",
    "  pequena:NN% = mudancas pequenas e frequentes: escrita na mesa, cursor, icone piscando",
    "  mov:NN%     = mudancas grandes: demonstracao, terminal, simulacao, rolagem ou cameras em galeria",
    "  ?           = nao analisada",
    "Pode trocar a acao, apagar linhas ou ajustar INICIO/FIM (h:mm:ss.mmm). O resto e informacao.",
    "Os tempos ja incluem a margem: e exatamente o que sera removido.",
]


def escrever_cortes(caminho, video, duracao, cabecalho, trechos):
    linhas = [
        "# cortar_silencio.py - lista de cortes",
        f"# video: {video.name}",
        f"# duracao: {fmt_tempo(duracao)}",
        *[f"# {linha}" for linha in cabecalho],
        "#",
        *[f"# {linha}" for linha in LEGENDA],
        "#",
        f"#{'ACAO':<8} {'INICIO':<12} {'FIM':<12} {'DURACAO':>9}  {'TELA':<9} NOTA",
    ]
    for trecho in trechos:
        linhas.append(
            f"{trecho.acao:<8}  {fmt_tempo(trecho.inicio):<12} {fmt_tempo(trecho.fim):<12} "
            f"{fmt_duracao(trecho.duracao):>9}  {trecho.tela:<9} {trecho.nota}".rstrip()
        )
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_text("\n".join(linhas) + "\n", encoding="utf-8")
    os.replace(temporario, caminho)


def ler_cortes(caminho, duracao):
    """Le o .cortes.txt (editado ou nao). Retorna (cabecalho, trechos ordenados)."""
    cabecalho = []
    trechos = []
    for numero, linha in enumerate(caminho.read_text(encoding="utf-8").splitlines(), start=1):
        if not linha.strip():
            continue
        if linha.startswith("#"):
            cabecalho.append(linha)
            continue
        partes = linha.split(maxsplit=5)
        if len(partes) < 3:
            raise ValueError(f"linha {numero}: esperado 'ACAO INICIO FIM': {linha!r}")
        acao = partes[0].upper()
        if acao not in ACOES:
            raise ValueError(f"linha {numero}: acao {partes[0]!r} (use {', '.join(ACOES)})")
        inicio, fim = ler_tempo(partes[1]), ler_tempo(partes[2])
        if fim <= inicio:
            raise ValueError(f"linha {numero}: FIM antes do INICIO")
        if duracao and inicio >= duracao:
            raise ValueError(f"linha {numero}: INICIO depois do fim do video ({fmt_tempo(duracao)})")
        tela = partes[4] if len(partes) > 4 else "?"
        nota = partes[5] if len(partes) > 5 else ""
        # Formato da versao anterior: "parada 0%" / "mudando 100%".
        if tela in ("parada", "mudando"):
            achado = re.match(r"(\d+)%\s*(.*)", nota)
            if achado:
                tela = "parada" if tela == "parada" else f"mov:{achado.group(1)}%"
                nota = achado.group(2)
        trechos.append(Trecho(acao, inicio, min(fim, duracao) if duracao else fim, tela, nota.strip()))
    trechos.sort(key=lambda trecho: trecho.inicio)
    return cabecalho, trechos


def regravar(caminho, video, duracao, cabecalho_original, trechos):
    """Regrava a lista mantendo as informacoes da deteccao que estavam no cabecalho."""
    fixas = ["cortar_silencio.py", "video:", "duracao:", "ACAO"] + [linha.split()[0] for linha in LEGENDA]
    fixas += ["parada", "pequena", "mov:", "?"]
    cabecalho = []
    for linha in cabecalho_original:
        texto = linha.lstrip("#").strip()
        if texto and not any(texto.startswith(prefixo) for prefixo in fixas):
            cabecalho.append(texto)
    escrever_cortes(caminho, video, duracao, cabecalho, trechos)


# ===== Comando: calibrar =====

def calibrar(args):
    checar_ffmpeg()
    video = Path(args.video)
    info = ler_video(video)
    pasta = video.parent
    manual = montar_mascara_manual(args.ignorar, args.analisar)
    if args.mostrar:
        mascara = carregar_config(pasta)
        if mascara is None:
            sys.exit(f"Nenhuma mascara salva em {pasta / ARQUIVO_CONFIG}.")
    else:
        mascara = set(manual)
        if not args.sem_auto:
            print("Procurando as regioes que mudam a aula inteira (cameras, participantes)...")
            mascara |= estimar_mascara(video, info.duration)
    momento = ler_tempo(args.momento) if args.momento else info.duration / 2
    previa = pasta / ARQUIVO_PREVIA
    gerar_previa(video, mascara, momento, previa)
    fracao = len(mascara) / _celulas()
    print(f"\nPrevia: {previa}")
    print(f"Em vermelho: {len(mascara)} de {_celulas()} celulas ({fracao:.0%} da tela) ignoradas na analise.")
    if abrir_arquivo(previa):
        print("(a imagem foi aberta no visualizador padrao)")
    if fracao > MASCARA_MAXIMA:
        print(f"[aviso] Mais de {MASCARA_MAXIMA:.0%} da tela ignorada: a analise da tela vai ser pouco util.")
    print(
        "\nPara ajustar, rode de novo com --ignorar x,y,largura,altura (em % da tela; pode repetir)\n"
        "ou --analisar x,y,largura,altura para olhar SO a area do conteudo (ex.: o slide)."
    )
    if args.mostrar:
        return
    if sim_nao(f"Salvar esta mascara para os videos de {pasta.name or pasta}/?", padrao=True):
        origem = "manual" if args.sem_auto else ("automatica + manual" if manual else "automatica")
        salvar_config(pasta, mascara, origem)
        print(f"Salvo em {pasta / ARQUIVO_CONFIG}. O 'detectar' vai usa-la nos videos desta pasta.")


# ===== Comando: detectar =====

def detectar(args):
    checar_ffmpeg()
    if args.sem_tela:
        args.tela = "nao"
    videos = coletar_videos(args.videos)
    if not videos:
        print("Nenhum video encontrado.")
        return
    for video in videos:
        print(f"\n=== {video.name}")
        try:
            detectar_video(video, args)
        except (RuntimeError, ValueError) as erro:
            print(f"  [erro] {erro}")


def detectar_video(video, args):
    destino = arquivo_cortes(video)
    if destino.exists() and not args.forcar:
        if not sim_nao(f"  {destino.name} ja existe (talvez com suas decisoes). Sobrescrever?"):
            print("  Mantido.")
            return

    info = ler_video(video)
    duracao = info.duration
    faixas = faixas_audio(video)
    if not faixas:
        raise RuntimeError("o video nao tem audio")
    if args.faixa is not None:
        if not 1 <= args.faixa <= len(faixas):
            raise RuntimeError(f"--faixa {args.faixa}: o video tem {len(faixas)} faixa(s) de audio")
        escolhidas = [args.faixa - 1]
    else:
        escolhidas = list(range(len(faixas)))
    print(f"  {fmt_duracao(duracao)}, {len(faixas)} faixa(s) de audio")

    # Silencio = todas as faixas escolhidas abaixo do limiar delas ao mesmo tempo
    # (ex.: microfone em silencio, mas o som do software da demonstracao tocando nao e pausa).
    janelas = math.ceil(duracao / JANELA)
    mascara_audio = None
    cabecalho = []
    for faixa in escolhidas:
        print(f"  medindo o volume da faixa {faixa + 1}...", flush=True)
        niveis = medir_volume(video, faixa)
        if not niveis:
            raise RuntimeError(f"faixa {faixa + 1} sem audio decodificavel")
        janelas = max(janelas, int(niveis[-1][0] / JANELA) + 1)
        limiar = calcular_limiar(niveis, args.limiar)
        tipo = "automatico" if limiar.automatico else "fixo"
        resumo = (
            f"audio faixa {faixa + 1}: fala {limiar.fala:.1f} dB | fundo {limiar.ruido:.1f} dB | "
            f"silencio absoluto {limiar.digital:.0f}% | limiar {limiar.valor:.1f} dB ({tipo})"
        )
        print(f"  {resumo}")
        cabecalho.append(resumo)
        if limiar.fala - limiar.ruido < 12:
            aviso = (
                f"AVISO faixa {faixa + 1}: fala e fundo a menos de 12 dB um do outro; "
                "a deteccao pode errar (use --limiar)"
            )
            print(f"  {aviso}")
            cabecalho.append(aviso)
        silencio = [False] * janelas
        for tempo, nivel in niveis:
            indice = int(round(tempo / JANELA))
            if 0 <= indice < janelas:
                silencio[indice] = nivel < limiar.valor
        mascara_audio = silencio if mascara_audio is None else [a and b for a, b in zip(mascara_audio, silencio)]

    brutos = trechos_silenciosos(mascara_audio, duracao)
    unidos, contagem = juntar_trechos(brutos, args.juntar)
    cabecalho.append(
        f"parametros: minimo {args.minimo:g} s | juntar pausas separadas por ate {args.juntar:g} s | "
        f"margem {args.margem:g} s"
    )

    trechos = []
    for (inicio, fim), partes in zip(unidos, contagem):
        if fim - inicio < args.minimo:
            continue
        nota = []
        no_inicio = inicio <= JANELA
        no_fim = fim >= duracao - JANELA
        # A margem preserva o fim/comeco da fala; nas pontas da gravacao corta tudo.
        corte_inicio = 0.0 if no_inicio else inicio + args.margem
        corte_fim = duracao if no_fim else fim - args.margem
        if corte_fim - corte_inicio < 1:
            continue
        if no_inicio:
            nota.append("inicio da gravacao")
        if no_fim:
            nota.append("fim da gravacao")
        if partes > 1:
            nota.append(f"juntou {partes} pausas")
        trechos.append(Trecho("REVISAR", corte_inicio, corte_fim, "?", "; ".join(nota)))

    if trechos and args.tela != "nao":
        analisar_pausas(video, duracao, trechos, args, cabecalho)
    elif trechos:
        cabecalho.append("tela: nao analisada (--tela nao); todas as pausas ficam REVISAR")

    if not trechos:
        print(f"  Nenhuma pausa de {args.minimo:g} s ou mais.")
    escrever_cortes(destino, video, duracao, cabecalho, trechos)
    cortar_total = sum(trecho.duracao for trecho in trechos if trecho.acao == "CORTAR")
    revisar_lista = [trecho for trecho in trechos if trecho.acao == "REVISAR"]
    print(
        f"  {len(trechos)} pausa(s): {len(trechos) - len(revisar_lista)} para CORTAR ({fmt_duracao(cortar_total)}), "
        f"{len(revisar_lista)} para REVISAR ({fmt_duracao(sum(t.duracao for t in revisar_lista))})"
    )
    print(f"  Lista salva em: {destino}")


def analisar_pausas(video, duracao, trechos, args, cabecalho):
    manual = montar_mascara_manual(args.ignorar, args.analisar)
    salva = None if manual else carregar_config(video.parent)
    if manual:
        mascara, origem = manual, "--ignorar/--analisar"
    elif salva is not None:
        mascara, origem = salva, f"{ARQUIVO_CONFIG} (calibrar)"
    else:
        mascara, origem = estimar_mascara(video, duracao), "automatica (confira com 'calibrar')"
    fracao = len(mascara) / _celulas()
    regiao = f" ({nome_regiao(mascara)})" if mascara else ""
    cabecalho.append(f"tela: ignorando {fracao:.0%} da tela{regiao}, mascara {origem}")
    print(f"  {cabecalho[-1]}")

    modo = args.tela
    if modo == "auto" and fracao > MASCARA_MAXIMA:
        aviso = f"tela: mais de {MASCARA_MAXIMA:.0%} ignorada; nada e marcado CORTAR automaticamente"
        print(f"  [aviso] {aviso}")
        cabecalho.append(aviso)
        modo = "dica"
    if modo == "dica":
        cabecalho.append("tela: modo dica (a analise so informa; todas as pausas ficam REVISAR)")

    print(f"  analisando a tela em {len(trechos)} pausa(s)...", flush=True)
    for numero, trecho in enumerate(trechos, start=1):
        print(f"\r  tela: {numero}/{len(trechos)}", end="", flush=True)
        tela = analisar_tela(video, trecho.inicio, trecho.fim, mascara)
        if tela is None:
            continue
        trecho.tela = tela.rotulo()
        if tela.categoria != "parada" and tela.onde:
            trecho.nota = "; ".join(parte for parte in (trecho.nota, f"mudou: {tela.onde}") if parte)
        if modo == "auto" and tela.categoria == "parada":
            trecho.acao = "CORTAR"
    print()


# ===== Comando: revisar =====

def _flatpak_instalado(app_id):
    if not app_id or not shutil.which("flatpak"):
        return False
    resultado = subprocess.run(["flatpak", "info", app_id], capture_output=True)
    return resultado.returncode == 0


def encontrar_player(preferido=None):
    """Retorna (nome, prefixo do comando) do primeiro player disponivel, ou ('sistema', None)."""
    preferido = preferido or os.environ.get("CORTAR_SILENCIO_PLAYER")
    candidatos = [item for item in PLAYERS if not preferido or item[0] == preferido]
    for nome, executavel, flatpak in candidatos:
        if shutil.which(executavel):
            return nome, [executavel]
        if _flatpak_instalado(flatpak):
            return nome, ["flatpak", "run", flatpak]
    if preferido and preferido != "sistema":
        print(f"[aviso] player '{preferido}' nao encontrado; usando o player padrao do sistema.")
    return "sistema", None


def comando_player(nome, prefixo, video, comeco, fim, titulo):
    duracao = max(fim - comeco, 1)
    if nome == "mpv":
        return prefixo + [f"--start={comeco:.3f}", f"--end={fim:.3f}", "--force-window=yes",
                          f"--title={titulo}", "--osd-level=3", str(video)]
    if nome == "vlc":
        return prefixo + [f"--start-time={comeco:.3f}", f"--stop-time={fim:.3f}", "--play-and-exit",
                          f"--meta-title={titulo}", str(video)]
    if nome == "mplayer":
        return prefixo + ["-ss", f"{comeco:.3f}", "-endpos", f"{duracao:.3f}", str(video)]
    if nome == "ffplay":
        return prefixo + ["-hide_banner", "-loglevel", "error", "-ss", f"{comeco:.3f}",
                          "-t", f"{duracao:.3f}", "-autoexit", "-window_title", titulo, str(video)]
    raise ValueError(nome)


def tocar(player, video, inicio, fim, contexto):
    nome, prefixo = player
    comeco = max(inicio - contexto, 0)
    if nome == "sistema":
        # Player sem controle de tempo pela linha de comando: abre e mostra para onde ir.
        print(f"  Abrindo no player padrao. Avance ate {fmt_tempo(comeco)} (a pausa vai ate {fmt_tempo(fim)}).")
        if not abrir_arquivo(video):
            print("  [aviso] nao foi possivel abrir um player; confira os tempos manualmente.")
        input("  Enter quando terminar de assistir... ")
        return
    titulo = f"{video.name}  {fmt_tempo(inicio)} - {fmt_tempo(fim)}"
    comando = comando_player(nome, prefixo, video, comeco, fim + contexto, titulo)
    try:
        subprocess.run(comando, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as erro:
        print(f"  [erro] nao foi possivel abrir o {nome}: {erro}")


def fotos_da_pausa(video, inicio, fim, destino, quantidade=6):
    """Junta `quantidade` quadros espalhados pela pausa numa imagem (3 x 2)."""
    passo = (fim - inicio) / quantidade
    entradas = []
    for numero in range(quantidade):
        entradas += ["-ss", f"{inicio + passo * (numero + 0.5):.3f}", "-i", str(video)]
    rotulos = "".join(f"[{numero}:v:0]scale=640:-2,setsar=1[f{numero}];" for numero in range(quantidade))
    juntos = "".join(f"[f{numero}]" for numero in range(quantidade))
    layout = "0_0|w0_0|w0+w1_0|0_h0|w0_h0|w0+w1_h0"
    resultado = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *entradas,
         "-filter_complex", f"{rotulos}{juntos}xstack=inputs={quantidade}:layout={layout}[o]",
         "-map", "[o]", "-frames:v", "1", "-q:v", "4", str(destino)],
        capture_output=True, text=True,
    )
    if resultado.returncode != 0:
        raise RuntimeError(resultado.stderr.strip()[-300:] or "ffmpeg falhou")


def revisar(args):
    video, lista = video_de(args.alvo)
    if not lista.exists():
        sys.exit(f"Lista de cortes nao encontrada: {lista}. Rode 'detectar' antes.")
    info = video_tools.probe(video) if video.exists() else None
    duracao = info.duration if info else 0
    cabecalho, trechos = ler_cortes(lista, duracao)
    player = encontrar_player(args.player)

    pendentes = [t for t in trechos if args.todos or t.acao == "REVISAR"]
    if not pendentes:
        print("Nada para revisar. (Use --todos para passar por todas as pausas.)")
        return
    print(f"{len(pendentes)} pausa(s) para revisar em {video.name}.")
    if player[0] == "sistema":
        print("Nenhum player com controle de tempo (mpv, vlc, mplayer, ffplay): o video abre no player padrao.")
    else:
        origem = " (flatpak)" if player[1][0] == "flatpak" else ""
        print(f"Player: {player[0]}{origem}. Ele abre {args.contexto:g} s antes da pausa; feche-o para responder.")

    em_lote = {}  # categoria da tela -> acao escolhida com C/M maiusculo
    for numero, trecho in enumerate(pendentes, start=1):
        categoria = trecho.tela.split(":")[0]
        if categoria in em_lote:
            trecho.acao = em_lote[categoria]
            print(f"[{numero}/{len(pendentes)}] {fmt_tempo(trecho.inicio)} ({fmt_duracao(trecho.duracao)}, "
                  f"tela {trecho.tela}): {trecho.acao} (em lote)")
            regravar(lista, video, duracao, cabecalho, trechos)
            continue
        while True:
            print(
                f"\n[{numero}/{len(pendentes)}] {trecho.acao}  {fmt_tempo(trecho.inicio)} - {fmt_tempo(trecho.fim)}"
                f"  ({fmt_duracao(trecho.duracao)}; tela {trecho.tela}){'  ' + trecho.nota if trecho.nota else ''}"
            )
            resposta = perguntar(
                "(v)er, (f)otos, (c)ortar, (m)anter, (a)justar, (p)ular, (s)air | C/M = esta e as "
                f"proximas com tela '{categoria}' [v]: ", "v",
            )[:1]
            if resposta in ("C", "M"):
                em_lote[categoria] = "CORTAR" if resposta == "C" else "MANTER"
                resposta = resposta.lower()
            resposta = resposta.lower()
            if resposta == "v":
                tocar(player, video, trecho.inicio, trecho.fim, args.contexto)
                continue
            if resposta == "f":
                imagem = video.with_name(".fotos_pausa.jpg")
                try:
                    fotos_da_pausa(video, trecho.inicio, trecho.fim, imagem)
                    if not abrir_arquivo(imagem):
                        print(f"  Imagem salva em {imagem}")
                except RuntimeError as erro:
                    print(f"  [erro] {erro}")
                continue
            if resposta == "c":
                trecho.acao = "CORTAR"
            elif resposta == "m":
                trecho.acao = "MANTER"
            elif resposta == "a":
                try:
                    novo_inicio = perguntar(f"  inicio [{fmt_tempo(trecho.inicio)}]: ")
                    novo_fim = perguntar(f"  fim    [{fmt_tempo(trecho.fim)}]: ")
                    inicio = ler_tempo(novo_inicio) if novo_inicio else trecho.inicio
                    fim = ler_tempo(novo_fim) if novo_fim else trecho.fim
                    if fim <= inicio:
                        raise ValueError("fim antes do inicio")
                    trecho.inicio, trecho.fim = inicio, fim
                except ValueError as erro:
                    print(f"  [erro] {erro}")
                continue
            elif resposta == "s":
                regravar(lista, video, duracao, cabecalho, trechos)
                print(f"Salvo em {lista}.")
                return
            elif resposta != "p":
                continue
            # Salva a cada decisao: sair no meio nao perde o que ja foi revisado.
            regravar(lista, video, duracao, cabecalho, trechos)
            break
    video.with_name(".fotos_pausa.jpg").unlink(missing_ok=True)
    print(f"\nRevisao concluida. Salvo em {lista}.")


# ===== Comando: cortar =====

def trechos_mantidos(cortes, duracao):
    """Complemento dos trechos CORTAR (unidos se encostarem)."""
    mantidos = []
    posicao = 0.0
    for inicio, fim in cortes:
        if inicio > posicao:
            mantidos.append((posicao, inicio))
        posicao = max(posicao, fim)
    if posicao < duracao:
        mantidos.append((posicao, duracao))
    # Pedacos minusculos entre dois cortes so viram um "piscar" no video.
    return [(inicio, fim) for inicio, fim in mantidos if fim - inicio >= 0.5]


def escolher_modo(relatorio, pedido):
    """'copia' (sem perda) quando o video ja serve para o Telegram; 'converter' quando precisa."""
    motivos = relatorio.problems + relatorio.suggestions
    precisa = relatorio.reencode_video or relatorio.reencode_audio
    if pedido == "copia":
        if precisa:
            print("[aviso] --modo copia num video que precisaria de conversao: "
                  "o resultado pode nao tocar bem no Telegram.")
        return "copia", "pedido com --modo copia"
    if pedido == "converter":
        return "converter", "pedido com --modo converter"
    if precisa:
        return "converter", "; ".join(motivos) or "formato nao compativel"
    return "copia", "ja e H.264 + AAC, adequado para o Telegram"


def keyframes_video(video):
    resultado = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "packet=pts_time,flags", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    tempos = []
    for linha in resultado.stdout.splitlines():
        partes = linha.strip().split(",")
        if len(partes) >= 2 and partes[1].startswith("K") and partes[0] not in ("", "N/A"):
            tempos.append(float(partes[0]))
    return sorted(tempos)


def planejar_copia(mantidos, keyframes):
    """Sem reconverter, cada trecho mantido so pode comecar num keyframe: recua o inicio ate o
    keyframe anterior (o que sobra e silencio da pausa, nunca fala). Se o keyframe cair dentro
    do trecho anterior, os dois trechos viram um so (aquela pausa fica inteira)."""
    import bisect

    plano = []
    perdidos = 0
    for inicio, fim in mantidos:
        posicao = bisect.bisect_right(keyframes, inicio + 0.001) - 1
        chave = keyframes[posicao] if posicao >= 0 else 0.0
        if plano and chave <= plano[-1][1]:
            plano[-1] = (plano[-1][0], fim)
            perdidos += 1
        else:
            plano.append((chave, fim))
    return plano, perdidos


def _rodar(comando, erro):
    resultado = subprocess.run(comando, capture_output=True, text=True)
    if resultado.returncode != 0:
        raise RuntimeError(f"{erro}: {resultado.stderr.strip()[-300:]}")


def cortar_copiando(video, plano, faixa, destino):
    """Corta com -c copy: mesma qualidade e tamanho proporcional ao original."""
    pasta = destino.parent
    partes = []
    lista = pasta / f".{destino.stem}.partes.txt"
    try:
        for numero, (inicio, fim) in enumerate(plano, start=1):
            print(f"\r  copiando trecho {numero}/{len(plano)}", end="", flush=True)
            parte = pasta / f".{destino.stem}.parte{numero:03d}.mp4"
            partes.append(parte)
            busca = ["-ss", f"{inicio + 0.001:.3f}"] if inicio > 0 else []
            _rodar(
                ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *busca, "-i", str(video),
                 "-t", f"{fim - inicio:.3f}", "-map", "0:v:0", "-map", f"0:a:{faixa}?", "-c", "copy",
                 "-avoid_negative_ts", "make_zero", "-f", "mp4", str(parte)],
                "ffmpeg falhou ao copiar um trecho",
            )
        print()
        lista.write_text(
            "".join("file '" + str(parte.resolve()).replace("'", "'\\''") + "'\n" for parte in partes),
            encoding="utf-8",
        )
        print("  juntando os trechos...", flush=True)
        _rodar(
            ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
             "-i", str(lista), "-map", "0", "-c", "copy", "-map_metadata", "-1",
             "-movflags", "+faststart", "-f", "mp4", str(destino)],
            "ffmpeg falhou ao juntar os trechos",
        )
    finally:
        for parte in partes:
            parte.unlink(missing_ok=True)
        lista.unlink(missing_ok=True)


def duracoes_faixas(video):
    resultado = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration", "-of", "csv=p=0", str(video)],
        capture_output=True, text=True,
    )
    duracoes = {}
    for linha in resultado.stdout.splitlines():
        tipo, _, valor = linha.partition(",")
        if tipo in ("video", "audio") and tipo not in duracoes:
            try:
                duracoes[tipo] = float(valor)
            except ValueError:
                pass
    return duracoes


def cortar_convertendo(video, info, relatorio, mantidos, faixa, destino, duracao_final):
    """Reconverte so os trechos mantidos. Cada trecho e recortado a parte (trim/atrim) e o
    filtro concat realinha audio e video no inicio de cada um: o erro fica em 1 quadro e nao
    se acumula de um corte para o outro (com select/aselect num video de taxa variavel,
    3 cortes ja davam 175 ms de atraso)."""
    roteiro = destino.with_name(f".{destino.stem}.filtro.txt")
    fps = video_tools.output_fps(info)
    filtros = [filtro for filtro in relatorio.video_filters if not filtro.startswith("fps=")]
    if not any(filtro.startswith("scale=") for filtro in filtros):
        filtros.append("scale=trunc(iw/2)*2:trunc(ih/2)*2")  # H.264 yuv420p exige dimensoes pares
    extras = "".join(f",{filtro}" for filtro in filtros)
    tem_audio = info.audio_streams > 0
    quantidade = len(mantidos)
    linhas = [f"[0:v:0]fps={fps}{extras},split={quantidade}" + "".join(f"[v{n}]" for n in range(quantidade))]
    if tem_audio:
        linhas.append(f"[0:a:{faixa}]asplit={quantidade}" + "".join(f"[a{n}]" for n in range(quantidade)))
    juntar = ""
    for numero, (inicio, fim) in enumerate(mantidos):
        # Desloca pelo tempo exato do corte (nao pelo 1o quadro): preserva a fracao de quadro.
        linhas.append(f"[v{numero}]trim=start={inicio:.6f}:end={fim:.6f},setpts=PTS-{inicio:.6f}/TB[vt{numero}]")
        juntar += f"[vt{numero}]"
        if tem_audio:
            linhas.append(f"[a{numero}]atrim=start={inicio:.6f}:end={fim:.6f},asetpts=PTS-{inicio:.6f}/TB[at{numero}]")
            juntar += f"[at{numero}]"
    saidas = "[v][a]" if tem_audio else "[v]"
    linhas.append(f"{juntar}concat=n={quantidade}:v=1:a={1 if tem_audio else 0}{saidas}")
    roteiro.write_text(";\n".join(linhas), encoding="utf-8")
    comando = [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-i", str(video),
        "-/filter_complex", str(roteiro), "-map", "[v]",
        "-c:v", "libx264", "-preset", video_tools.X264_PRESET, "-crf", str(video_tools.X264_CRF),
        "-profile:v", "high", "-pix_fmt", "yuv420p", "-fps_mode", "cfr",
        "-force_key_frames", f"expr:gte(t,n_forced*{video_tools.KEYFRAME_SECONDS})",
    ]
    if tem_audio:
        comando += ["-map", "[a]", "-c:a", "aac", "-b:a", video_tools.AAC_BITRATE,
                    "-ac", str(min(info.channels or 2, 2))]
    comando += ["-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", str(destino)]
    try:
        video_tools.run_ffmpeg(comando, duracao_final, "convertendo")
    finally:
        roteiro.unlink(missing_ok=True)


def conferir_resultado(caminho, esperado, folga):
    """Duracao total e sincronia (video e audio com a mesma duracao)."""
    resultado = video_tools.probe(caminho)
    if resultado is None:
        raise RuntimeError("o resultado nao pode ser lido")
    if abs(resultado.duration - esperado) > folga:
        raise RuntimeError(
            f"duracao do resultado ({fmt_tempo(resultado.duration)}) nao bate com a esperada ({fmt_tempo(esperado)})"
        )
    faixas = duracoes_faixas(caminho)
    if "video" in faixas and "audio" in faixas and abs(faixas["video"] - faixas["audio"]) > folga:
        raise RuntimeError(
            f"video ({fmt_tempo(faixas['video'])}) e audio ({fmt_tempo(faixas['audio'])}) com duracoes diferentes"
        )


def guardar_original(video, lista):
    """Move o original e a lista de cortes para <pasta>/orig/."""
    pasta = video.parent / PASTA_ORIGINAIS
    pasta.mkdir(exist_ok=True)
    destino = pasta / video.name
    contador = 1
    while destino.exists() or arquivo_cortes(destino).exists():
        destino = pasta / f"{video.stem}_{contador}{video.suffix}"
        contador += 1
    shutil.move(str(video), str(destino))
    if lista.exists():
        shutil.move(str(lista), str(arquivo_cortes(destino)))
    return destino


def cortar(args):
    checar_ffmpeg()
    video, lista = video_de(args.alvo)
    if not video.exists():
        sys.exit(f"Video nao encontrado: {video}")
    if not lista.exists():
        sys.exit(f"Lista de cortes nao encontrada: {lista}. Rode 'detectar' antes.")
    video_tools.configure(crf=args.crf, preset=args.preset)

    relatorio = video_tools.analyze(video, 10 ** 15)
    info = relatorio.info
    if info is None:
        sys.exit("Nao foi possivel ler o video.")
    duracao = info.duration
    _, trechos = ler_cortes(lista, duracao)

    revisar_restantes = [t for t in trechos if t.acao == "REVISAR"]
    if revisar_restantes:
        print(f"{len(revisar_restantes)} pausa(s) ainda estao como REVISAR e vao ficar no video.")
        if not args.sim and not sim_nao("Continuar mesmo assim?"):
            print(f"Use: python {Path(__file__).name} revisar \"{video}\"")
            return

    cortes = sorted((t.inicio, min(t.fim, duracao)) for t in trechos if t.acao == "CORTAR")
    if not cortes:
        print("Nenhum trecho marcado como CORTAR. Nada a fazer.")
        return
    mantidos = trechos_mantidos(cortes, duracao)
    exato = sum(fim - inicio for inicio, fim in mantidos)
    faixa = (args.faixa - 1) if args.faixa else 0

    modo, motivo = escolher_modo(relatorio, args.modo)
    plano = None
    if modo == "copia":
        plano, perdidos = planejar_copia(mantidos, keyframes_video(video))
        final = sum(fim - inicio for inicio, fim in plano)
    else:
        final = exato

    saida_dir = Path(args.saida) if args.saida else video.parent
    saida = saida_dir / f"{video.stem}.mp4"
    print(f"\n{video.name}: {fmt_duracao(duracao)} -> {fmt_duracao(final)} "
          f"({len(cortes)} corte(s), {fmt_duracao(duracao - final)} removidos)")
    if modo == "copia":
        print(f"Modo copia ({motivo}): sem reconverter, mesma qualidade, leva poucos segundos.")
        sobra = final - exato
        if sobra > 0.5:
            print(f"  Os cortes caem em keyframes: ficam {fmt_duracao(sobra)} a mais de pausa no total "
                  "(nunca fala). Para corte exato: --modo converter.")
        if perdidos:
            print(f"  [aviso] {perdidos} pausa(s) curtas demais para cortar sem reconverter ficam inteiras.")
    else:
        print(f"Modo converter ({motivo}): H.264 preset {video_tools.X264_PRESET}, "
              f"CRF {video_tools.X264_CRF}, AAC. Demora, com progresso na tela.")
    print(f"Editado:  {saida}")
    print(f"Original: vai para {video.parent / PASTA_ORIGINAIS}/ junto com a lista de cortes.")
    if saida.exists() and saida.resolve() != video.resolve() and not args.sim:
        if not sim_nao(f"{saida} ja existe e nao e o original. Substituir?"):
            return
    if not args.sim and not sim_nao("Gerar o video editado agora?", padrao=True):
        return

    saida_dir.mkdir(parents=True, exist_ok=True)
    temporario = saida_dir / f".{video.stem}.cortando.mp4"
    try:
        if modo == "copia":
            try:
                cortar_copiando(video, plano, faixa, temporario)
                conferir_resultado(temporario, final, max(1.0, 0.3 * len(plano)))
            except RuntimeError as erro:
                print(f"  [aviso] o corte sem reconverter falhou ({erro}).")
                print("  Fazendo pelo modo converter.")
                modo, final = "converter", exato
                temporario.unlink(missing_ok=True)
        if modo == "converter":
            cortar_convertendo(video, info, relatorio, mantidos, faixa, temporario, exato)
            conferir_resultado(temporario, exato, max(2.0, exato * 0.002))
        # Tudo certo: original para orig/ e o editado no lugar (ou em --saida).
        guardado = guardar_original(video, lista)
        os.replace(temporario, saida)
    except RuntimeError as erro:
        sys.exit(f"[erro] {erro}. O original nao foi alterado.")
    finally:
        temporario.unlink(missing_ok=True)

    tamanho = saida.stat().st_size
    original = guardado.stat().st_size
    print(f"[ok] {saida} ({fmt_duracao(final)}, {tamanho / 1024 ** 2:.0f} MB; "
          f"original {original / 1024 ** 2:.0f} MB)")
    print(f"     Original e lista de cortes em {guardado.parent}/")
    if modo == "converter" and tamanho > original * final / duracao * 1.1:
        print("  [aviso] a conversao ficou maior que o original (proporcionalmente).")
    if tamanho > 2000 * 1024 ** 2:
        print("Acima de 2000 MB: o ttm.py vai dividir em partes (PART1, PART2...) na hora de enviar.")


# ===== Linha de comando =====

def main():
    parser = argparse.ArgumentParser(
        description="Detecta pausas em gravacoes de aula, ajuda a revisar e corta o video.",
        epilog="Fluxo: [calibrar] -> detectar -> revisar (ou editar o .cortes.txt) -> cortar",
    )
    comandos = parser.add_subparsers(dest="comando", required=True)
    retangulo = "x,y,largura,altura em %% da tela (pode repetir)"

    p = comandos.add_parser("calibrar", help="Mostra e salva as regioes da tela ignoradas (cameras...).")
    p.add_argument("video", help="Um video representativo do curso.")
    p.add_argument("--ignorar", action="append", metavar="RET", help=f"Ignora tambem esta area: {retangulo}.")
    p.add_argument("--analisar", action="append", metavar="RET",
                   help=f"Analisa SO esta area (ex.: o slide): {retangulo}.")
    p.add_argument("--sem-auto", action="store_true", help="Nao detecta as areas automaticamente.")
    p.add_argument("--momento", help="Instante do quadro da previa (padrao: meio do video).")
    p.add_argument("--mostrar", action="store_true", help="So mostra a mascara ja salva na pasta.")
    p.set_defaults(func=calibrar)

    p = comandos.add_parser("detectar", help="Gera a lista de pausas (<video>.cortes.txt).")
    p.add_argument("videos", nargs="*", help="Videos ou pastas (padrao: pasta atual).")
    p.add_argument("--minimo", type=float, default=MINIMO, help=f"Pausa minima em segundos (padrao {MINIMO:g}).")
    p.add_argument("--juntar", type=float, default=JUNTAR,
                   help=f"Une pausas separadas por ruido de ate N s (padrao {JUNTAR:g}).")
    p.add_argument("--margem", type=float, default=MARGEM,
                   help=f"Segundos mantidos antes/depois de cada pausa (padrao {MARGEM:g}).")
    p.add_argument("--limiar", type=float, default=None,
                   help="Limiar fixo em dB (ex.: -35). Padrao: automatico pelo nivel da fala.")
    p.add_argument("--faixa", type=int, default=None,
                   help="Usa so esta faixa de audio (1, 2...). Padrao: silencio em todas.")
    p.add_argument("--tela", choices=["auto", "dica", "nao"], default="auto",
                   help="auto: tela parada vira CORTAR; dica: so informa (tudo REVISAR); nao: nao analisa.")
    p.add_argument("--sem-tela", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--ignorar", action="append", metavar="RET",
                   help=f"Ignora esta area nesta execucao (no lugar da mascara salva): {retangulo}.")
    p.add_argument("--analisar", action="append", metavar="RET", help=f"Analisa SO esta area: {retangulo}.")
    p.add_argument("--forcar", action="store_true", help="Sobrescreve listas existentes sem perguntar.")
    p.set_defaults(func=detectar)

    p = comandos.add_parser("revisar", help="Assiste as pausas REVISAR e decide cortar ou manter.")
    p.add_argument("alvo", help="Video ou o .cortes.txt dele.")
    p.add_argument("--todos", action="store_true", help="Passa por todas as pausas, nao so as REVISAR.")
    p.add_argument("--player", choices=[item[0] for item in PLAYERS] + ["sistema"],
                   help="Player (padrao: o primeiro disponivel entre mpv, vlc, mplayer, ffplay; "
                        "'sistema' = player padrao, sem pular para o tempo).")
    p.add_argument("--contexto", type=float, default=3.0, help="Segundos antes/depois mostrados (padrao 3).")
    p.set_defaults(func=revisar)

    p = comandos.add_parser("cortar", help="Gera o video sem os trechos CORTAR.")
    p.add_argument("alvo", help="Video ou o .cortes.txt dele.")
    p.add_argument("--saida", help="Pasta do editado (padrao: o lugar do original, que vai para orig/).")
    p.add_argument("--modo", choices=["auto", "copia", "converter"], default="auto",
                   help="auto: corta sem reconverter se o video ja for H.264/AAC, senao converte; "
                        "copia: sempre sem reconverter (corte no keyframe); converter: sempre reconverte "
                        "(corte exato).")
    p.add_argument("--faixa", type=int, default=None, help="Faixa de audio do resultado (padrao 1).")
    p.add_argument("--crf", type=int, default=video_tools.X264_CRF, choices=range(15, 36), metavar="N",
                   help=f"Qualidade H.264 no modo converter (padrao {video_tools.X264_CRF}).")
    p.add_argument("--preset", default=video_tools.X264_PRESET, choices=video_tools.X264_PRESETS,
                   help=f"Velocidade H.264 no modo converter (padrao {video_tools.X264_PRESET}).")
    p.add_argument("--sim", action="store_true", help="Nao pede confirmacao (gera e substitui um editado que ja exista).")
    p.set_defaults(func=cortar)

    args = parser.parse_args()
    try:
        args.func(args)
    except (KeyboardInterrupt, EOFError):
        # Ctrl+C ou Ctrl+D: o que ja foi decidido no revisar esta salvo.
        print("\nInterrompido.")
        sys.exit(130)


if __name__ == "__main__":
    main()
