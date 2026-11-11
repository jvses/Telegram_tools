# Telegram Topic Manager

Envia arquivos locais para tópicos de um supergrupo fórum do Telegram onde a sua conta é criadora ou admin. Cada pasta dentro de `input/` vira um tópico. Se já existir um tópico com o mesmo nome, ele é reaproveitado.

Instalação, credenciais e login são comuns às duas ferramentas: veja o [README da raiz](../README.md).

## Uso

```bash
source .venv/bin/activate                         # na raiz do repositório
python Telegram_topic_manager/ttm.py
```

1. Coloque cada curso numa pasta dentro de `Telegram_topic_manager/input/`, por exemplo `input/meu_curso/01-Aula.mp4`.
2. Escolha quais pastas enviar: Enter para todas, ou algo como `1,3-5`.
3. Confira o plano mostrado (quantidade de arquivos, tamanho e avisos) e a análise dos vídeos. Se algum vídeo puder ser melhorado, o script oferece a conversão (veja [Vídeos](#vídeos)).
4. Escolha o supergrupo. Digitar parte do nome filtra a lista.
5. Confirme. Os tópicos novos são criados **fechados**: só admins postam neles, e os membros apenas assistem.

| Opção | Efeito |
|---|---|
| `--dry-run` | Mostra o plano de envio e a análise dos vídeos, com o comando `ffmpeg` sugerido para cada um. Não conecta ao Telegram nem converte nada. |
| `--incluir-csv` | Envia também o `<pasta>.csv`, que por padrão fica de fora. |
| `--topicos-abertos` | Deixa os tópicos novos abertos para todos os membros. |
| `--sem-analise` | Pula a análise e a conversão dos vídeos. |
| `--crf N` | Qualidade da reconversão H.264. Padrão 23; até 25 a perda é pequena; maior = arquivo menor. |
| `--preset NOME` | Velocidade da reconversão. Padrão `fast`; `medium`/`slow` gera arquivos um pouco menores e demora mais. |
| `--logout` | Encerra a sessão do Telegram e apaga o `user.session`. |

## Como os arquivos são tratados

- **Ordem:** os arquivos são enviados em ordem natural (`2-...` antes de `10-...`). A legenda é o nome do arquivo sem a extensão.
- **Vídeos e documentos:** vídeos (`.mp4`, `.mkv`, `.mov`…) vão como vídeo reproduzível no app, com miniatura, duração e resolução. O resto vai como documento.
- **Tópicos:** os novos são fechados logo depois de criados. Um tópico que já existia é reaproveitado como está.
- **Arquivos já enviados:** se um arquivo com o mesmo nome já está no tópico, ele não é reenviado. Por isso dá para rodar de novo depois de uma interrupção.
- **Depois de enviar:** cada arquivo enviado vai para `output/<pasta>/`. Quando a pasta de `input/` fica vazia, ela é removida, a menos que seja uma [pasta contínua](#pastas-contínuas-aulas-enviadas-aos-poucos).
- **Falhas:** se um arquivo falhar, ele continua em `input/` e os outros seguem normalmente.
- **Ficam de fora:**
  - downloads incompletos (`.part`, `.ytdl`…). O script pergunta se a pasta deve ser enviada mesmo assim.
  - arquivos ocultos.
  - arquivos acima do limite do Telegram: 2000 MB, ou 4000 MB com Premium.
  - o `<pasta>.csv` gerado pelo downloader de cursos, que contém links com validade e vai direto para `output/`.

## Pastas contínuas (aulas enviadas aos poucos)

Para um curso que ainda está sendo gravado, como aulas de um ano letivo, a pasta pode ficar em `input/` e receber as aulas novas conforme elas são gravadas:

1. No primeiro envio, responda **sim** a "Manter '<pasta>' em input/ para aulas futuras?".
2. O script cria o arquivo oculto `input/<pasta>/.ttm.json`, que guarda o supergrupo e o ID do tópico. As aulas enviadas continuam indo para `output/<pasta>/`, então a pasta fica vazia e pronta para as próximas.
3. A cada aula nova, coloque o vídeo na pasta e rode o `ttm.py`. Na lista, a pasta aparece como `[continua: N arquivo(s) novo(s)]`. O script sugere o mesmo supergrupo e envia só o que é novo para o mesmo tópico, que continua fechado.

Detalhes:

- O tópico é encontrado pelo ID. Você pode renomeá-lo no app, por exemplo para "Turma 2026", sem perder o vínculo.
- Se o tópico for apagado, ou se você escolher outro supergrupo, o script procura um tópico com o nome da pasta ou cria um novo, e atualiza o `.ttm.json`.
- Sem aulas novas, o script avisa e não chega a conectar ao Telegram.
- Para a legenda ficar em ordem e com a data da aula, comece o nome do arquivo pela data no formato ano-mês-dia: `2026-03-16 Aula 03 - Exercícios.mp4`. A ordem natural dos nomes vira a ordem de envio.
- Para encerrar o curso, apague a pasta de `input/`. Ela só contém o `.ttm.json`, e as aulas já estão em `output/`.
## Vídeos

Antes de conectar ao Telegram, o script lê cada vídeo com o `ffprobe` e compara com o que o app reproduz direto em todos os clientes: **MP4 com o índice no início (faststart), vídeo H.264 yuv420p e áudio AAC**. Isso exige o `ffmpeg` instalado no sistema (veja o [README da raiz](../README.md#requisitos-e-compatibilidade)). Sem ele, o script avisa e envia normalmente.

| O que é verificado | Tipo | O que a conversão faz |
|---|---|---|
| Container que não é MP4 (`.mkv`, `.webm`, `.avi`…) | problema | Reorganiza para MP4. |
| Índice do MP4 no fim do arquivo | problema | Move o índice para o início: o vídeo começa a tocar sem baixar tudo. |
| Codec que não é H.264 (VP9, AV1…) ou pixel format 10 bits / 4:4:4 | problema | Reconverte para H.264 yuv420p. |
| Áudio que não é AAC/MP3 (Opus, AC3, FLAC…) | problema | Reconverte o áudio para AAC 128 kb/s. |
| Arquivo acima do limite de upload (2000 MB, conta gratuita) | problema | Divide em até 3 partes. Veja [Vídeos longos](#vídeos-longos-divisão-em-partes). |
| H.265/HEVC | sugestão | Reconverte para H.264 (apps de desktop e web antigos não tocam HEVC). |
| Acima de 1080p, acima de 60 fps, bitrate alto, vídeo entrelaçado | sugestão | Reduz para 1080p / 60 fps, comprime e desentrelaça. |
| Áudio com mais de 2 canais ou várias faixas | sugestão | Estéreo, primeira faixa. |
| Vídeo **sem áudio** | aviso | Nada. Geralmente é download ou gravação sem a faixa de áudio. |

Como a conversão funciona:

- O script mostra a lista e pergunta se deve converter e quais vídeos. A resposta padrão é **não**.
- Quando só o container, o índice ou o áudio mudam, a conversão leva segundos e não perde qualidade de imagem. Reconverter a imagem demora mais, e o progresso aparece na tela com a velocidade (`3.2x` = 3,2 vezes o tempo real) e o tempo restante.
- **Nada é apagado.** O original vai para `Telegram_topic_manager/originais/<pasta>/`, e o MP4 novo (ou as partes) fica no lugar dele em `input/`. Apague os originais quando conferir o resultado.
- O resultado é verificado antes de substituir o original: a duração tem de bater, tanto na conversão quanto na soma das partes. Se falhar, ou com Ctrl+C, o original continua intacto e os temporários são removidos.
- Para converter manualmente, rode `ttm.py --dry-run` e copie o comando `ffmpeg` mostrado.
- Na hora do envio, o `ffmpeg` também gera a miniatura (o quadro que aparece antes do play) e informa a duração e a resolução exatas ao Telegram. Vídeos de celular gravados em pé são reconhecidos pela marca de rotação.

### Parâmetros da reconversão

```
-c:v libx264 -preset fast -crf 23 -profile:v high -pix_fmt yuv420p
-fps_mode cfr -r <fps da gravação, ou 30>
-force_key_frames "expr:gte(t,n_forced*10)"
-c:a aac -b:a 128k -ac <1 ou 2>
-movflags +faststart
```

- **CRF 23, preset `fast`:** o CRF controla a qualidade, e o tamanho varia conforme o conteúdo. Slides e quadro parado ficam bem menores que simulações com movimento. Use `--crf 25` para arquivos menores.
- **Taxa de quadros constante:** gravações de tela no navegador têm taxa variável, com poucos quadros quando a tela fica parada. Isso cria buracos nos timestamps, e o corte em partes passa a perder segundos de áudio. Na saída, a taxa é constante. Quadros parados repetidos quase não ocupam espaço, e o arquivo não cresce.
- **Keyframe a cada 10 s, no máximo:** avançar e voltar no app fica preciso, e há pontos de corte suficientes para dividir em partes de tamanhos parecidos.
- **Gravações WebM do navegador** (VP8/VP9 + Opus) são sempre reconvertidas. Elas costumam vir sem a duração no cabeçalho, e o script mede a duração lendo o arquivo, sem decodificar.

Os valores padrão ficam no topo do `video_tools.py` (`X264_CRF`, `X264_PRESET`, `MAX_PARTS`, `KEYFRAME_SECONDS`…).

## Vídeos longos: divisão em partes

O limite de upload da conta gratuita é 2000 MB por arquivo (4000 MB com Premium). Um vídeo acima disso é dividido em **até 3 partes**:

```
2026-03-23 Aula 04 - Simulação - PART1.mp4
2026-03-23 Aula 04 - Simulação - PART2.mp4
```

A legenda de cada parte é o nome do arquivo, então "PART1", "PART2"… aparecem também no Telegram. As partes são enviadas em ordem.

O que o script faz, nesta ordem:

1. **O vídeo já é compatível (H.264/AAC) e só passa do limite:** ele é cortado **sem reconverter**, em segundos e sem perda. Os cortes caem em keyframes e são escolhidos para as partes terem tamanhos parecidos. Se o corte sem reconversão perderia trechos (MP4 com taxa variável), o script reconverte e corta de novo.
2. **O vídeo precisa ser reconvertido** (WebM do navegador, bitrate alto…): ele é convertido com o CRF escolhido. Se o resultado ainda passar de 2000 MB, é dividido.
3. **Último recurso:** se nem em 3 partes coubesse, o script reconverte uma segunda vez com CRF 25 e um bitrate máximo calculado para caber em 3 partes. Isso é raro: uma aula de 6 h só chega a esse ponto acima de cerca de 2,2 Mb/s de média, e gravação de tela com CRF 23 costuma ficar bem abaixo disso.
4. Se nem assim couber, o script avisa e mantém o original: o vídeo precisa ser cortado antes.

Para ter uma ideia: 3h30 de aula a 1,5 Mb/s dá cerca de 2,4 GB, ou seja, 2 partes. Uma aula de 6 h a 2 Mb/s dá cerca de 5,4 GB, ou seja, 3 partes.
