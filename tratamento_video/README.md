# Tratamento de vídeo

Ferramentas para preparar gravações de aula antes de enviá-las com o [Telegram_topic_manager](../Telegram_topic_manager/README.md). Usam só a biblioteca padrão do Python e o `ffmpeg`, então não é preciso ativar o venv.

## cortar_silencio.py: tirar as pausas da gravação

Remove as pausas longas: intervalo com a gravação rodando, esquecer de pausar, começo e fim da gravação. O **original nunca é alterado**.

```bash
python tratamento_video/cortar_silencio.py calibrar "aula 07.mp4"   # 0. uma vez por curso (opcional)
python tratamento_video/cortar_silencio.py detectar "aula 07.mp4"   # 1. gera "aula 07.mp4.cortes.txt"
python tratamento_video/cortar_silencio.py revisar  "aula 07.mp4"   # 2. decide o que está em dúvida
python tratamento_video/cortar_silencio.py cortar   "aula 07.mp4"   # 3. edita; o original vai para orig/
```

`detectar` também aceita pastas e vários arquivos de uma vez.

### 0. calibrar (uma vez por curso)

Em gravações de reunião (Meet, Teams…), parte da tela se mexe o tempo todo: as câmeras das salas, as miniaturas dos participantes. Para a análise não confundir isso com conteúdo, essas regiões são ignoradas.

O `calibrar` procura essas regiões em 24 trechos espalhados pela aula. Ele usa a **mediana** da atividade de cada parte da tela: câmera se mexe em praticamente todo trecho, enquanto slide e demonstração mudam em rajadas. Em seguida, salva `mascara_tela.png`, um quadro do vídeo com as áreas ignoradas em vermelho, e abre a imagem. Se estiver certo, confirme, e a escolha fica salva em `.cortar_silencio.json` para todos os vídeos da pasta.

Para ajustar à mão, use retângulos em % da tela (`x,y,largura,altura`). As opções podem ser repetidas:

```bash
calibrar "aula.mp4" --ignorar 65,0,35,100           # ignora também a faixa da direita
calibrar "aula.mp4" --analisar 0,15,65,70 --sem-auto  # olha só a área do slide
calibrar "aula.mp4" --mostrar                       # mostra a máscara já salva
```

Rode de novo se o layout da reunião mudar entre os cursos. Sem calibrar, o `detectar` estima a máscara sozinho em cada vídeo (vídeos com menos de 8 minutos ficam sem máscara).

### 1. detectar

**Áudio.** O volume é medido a cada 0,25 s. O limiar é calculado pelo **nível da sua fala**: 20 dB abaixo do volume típico dela, e pelo menos 6 dB acima do ruído de fundo. Ele não depende do silêncio, porque apps de reunião com supressão de ruído zeram o áudio nas pausas. Numa aula real, isso deu −37 dB. Pausas separadas por ruídos de até 0,5 s (clique, mouse, batida na mesa) viram uma só. Ficam na lista as pausas de 10 s ou mais.

**Tela.** Fora das áreas ignoradas, quadros a cada 2 s são comparados. A pausa recebe uma categoria:

| TELA | O que significa | Ação |
|---|---|---|
| `parada` | Nada mudou em 90% do tempo ou mais. Uma troca de slide ou o relógio mudando o minuto não contam. | `CORTAR` |
| `pequena:NN%` | Mudanças pequenas e frequentes: escrita na mesa digitalizadora, cursor, ícone piscando. | `REVISAR` |
| `mov:NN%` | Mudanças grandes: demonstração, terminal, simulação, rolagem **ou câmeras em vista de galeria**. | `REVISAR` |

A demonstração e a vista de galeria ficam na mesma categoria de propósito: medidas numa aula real, elas se confundem. Quem decide é a revisão, que tem atalhos para isso.

A lista fica em `<video>.cortes.txt`:

```
#ACAO     INICIO       FIM            DURACAO  TELA      NOTA
CORTAR    0:57:09.250  1:24:42.250   27min33s  parada    juntou 4 pausas
REVISAR   1:45:06.250  1:45:22.750        16s  mov:100%  juntou 4 pausas; mudou: centro
REVISAR   1:51:50.750  1:52:03.250        12s  pequena:33%
```

Você pode trocar a ação, apagar linhas ou ajustar os tempos direto no arquivo. Os tempos já incluem a margem, então o arquivo mostra exatamente o que será removido.

| Opção | Padrão | Efeito |
|---|---|---|
| `--minimo N` | 10 | Pausa mínima, em segundos. |
| `--juntar N` | 0.5 | Une pausas separadas por ruído de até N s. Valores maiores engolem palavras curtas ("ok", "sim"). |
| `--margem N` | 0.5 | Segundos mantidos antes e depois de cada pausa, para não cortar o fim de uma frase. |
| `--limiar dB` | automático | Limiar fixo, por exemplo `-35`. |
| `--faixa N` | todas | Usa só uma faixa de áudio. Por padrão, só conta como pausa o trecho em silêncio em todas as faixas. |
| `--tela auto\|dica\|nao` | auto | `auto`: tela parada vira CORTAR. `dica`: só informa, e tudo fica REVISAR. `nao`: não analisa a tela (mais rápido). |
| `--ignorar` / `--analisar` | | Áreas só para esta execução, no lugar da máscara salva. |

### 2. revisar

Passa por cada pausa `REVISAR`, ou por todas com `--todos`:

| Tecla | Faz |
|---|---|
| `v` (Enter) | Abre o trecho no player, 3 s antes da pausa. Feche o player para responder. |
| `f` | **Fotos:** 6 quadros espalhados pela pausa numa imagem. Leva segundos, mesmo numa pausa de 27 min. |
| `c` / `m` | Corta / mantém esta pausa. |
| `C` / `M` | Corta / mantém esta **e todas as próximas com a mesma TELA** (ex.: todas as `mov`). |
| `a` | Ajusta início e fim. |
| `p` / `s` | Pula / sai. Cada decisão é salva na hora. |

**Players:** o script usa o primeiro disponível, nesta ordem: **mpv → VLC → MPlayer → ffplay**. Ele também encontra o mpv e o VLC instalados por **Flatpak** (`io.mpv.Mpv`, `org.videolan.VLC`), comuns em computadores sem acesso de administrador. Se nenhum estiver instalado, o vídeo abre no player padrão do sistema, e o script mostra para qual tempo avançar. Para escolher um player: `--player vlc`, ou a variável `CORTAR_SILENCIO_PLAYER=vlc`.

Se o VLC do Flatpak não conseguir abrir o arquivo, libere o acesso aos arquivos uma vez (não exige administrador):

```bash
flatpak override --user --filesystem=home org.videolan.VLC
```

### 3. cortar

Gera o vídeo sem os trechos `CORTAR`. Ele **fica no lugar do original**, com o mesmo nome e extensão `.mp4`. O original e a lista de cortes vão para `orig/`, na mesma pasta:

```
aulas/aula 07.mp4                      <- editado
aulas/orig/aula 07.mp4                 <- original, intacto
aulas/orig/aula 07.mp4.cortes.txt
```

Com `--saida PASTA`, o editado vai para outro lugar, por exemplo direto para a pasta contínua do ttm. O original vai para `orig/` do mesmo jeito.

**Modo de corte (`--modo`, padrão `auto`):**

| Modo | Quando o `auto` escolhe | Como corta |
|---|---|---|
| `copia` | O vídeo já é H.264 + AAC/MP3 (MP4, ou MKV com esses codecs) e não precisa de mais nada. | `-c copy`: **mesma qualidade e tamanho proporcional ao original**, leva segundos. Cada trecho mantido começa no keyframe anterior (de 2 em 2 a 10 em 10 s, conforme a gravação), então sobram alguns segundos de cada pausa, nunca fala. |
| `converter` | WebM, VP8/VP9, Opus, HEVC, 10 bits, bitrate alto, acima de 1080p… | Reconverte com os parâmetros do `ttm.py` (H.264 preset `fast`, CRF 23, AAC, taxa constante). Corte exato no quadro, mas demora. |

- Use `--modo converter` para forçar o corte exato num MP4, ou `--modo copia` para nunca reconverter.
- Antes de começar, o script mostra o modo, o motivo e quanto vai ser removido. No modo cópia, mostra também quanto de pausa sobra por causa dos keyframes.
- Ao terminar, confere a duração e se áudio e vídeo têm o mesmo comprimento. Se o corte por cópia falhar nessa conferência, o script refaz pelo modo converter.
- Nos testes com taxa de quadros variável e B-frames, a sincronia ficou em 1 quadro nos dois modos.
- Pausas ainda em `REVISAR` ficam no vídeo, e o script avisa antes.
- Se o resultado passar de 2000 MB, o `ttm.py` divide em PART1, PART2… no envio.
- Rodar `cortar` de novo no editado não corta duas vezes: a lista de cortes foi para `orig/`.

Os tempos de capítulos que você escrever depois devem usar o vídeo **editado**.

### Problemas do `detectar_silencio.py` antigo e como foram resolvidos

| Problema | O que acontecia | Solução |
|---|---|---|
| Ruído curto quebra a pausa (linhas 8–9, 10–11 e 14–15 do exemplo) | Um clique de 17 ms dividia uma pausa de 61 s em duas. | Pausas separadas por até 0,5 s de ruído são unidas. |
| Começo da pausa perdido | Com `d=10`, se o ruído vinha aos 8 s, o trecho de 0–8 s não aparecia. | A detecção é feita por janela de 0,25 s, as pausas são unidas e só depois o mínimo é aplicado. |
| Limiar fixo de −30 dB | Dependia do microfone e da sala. | O limiar é calculado pela sua fala em cada gravação e aparece no cabeçalho. Funciona também com a supressão de ruído (silêncio absoluto). |
| Demonstração ou escrita sem fala | Aparecia como pausa igual a um intervalo. | Análise da tela fora das câmeras; esses casos ficam `REVISAR`. |
| Várias faixas de áudio analisadas separadamente | Microfone mudo com o som do software tocando contava como pausa. | Só conta como pausa o trecho em silêncio em todas as faixas. |
| `silence_start` negativo | A regex `[0-9.]+` não lia `-0.02`, e a pausa do início sumia. | Os tempos são lidos com sinal. As pausas nas pontas são marcadas como "início/fim da gravação". |
| WebM do navegador sem duração | A pausa final terminava em "início + 10 s". | A duração é medida lendo o arquivo. |
| Corte no meio da frase | Os tempos eram os do limiar. | A margem de 0,5 s é mantida de cada lado. |

`detectar_silencio.py` continua na pasta como referência.
