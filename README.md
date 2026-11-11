# Telegram Tools

Duas ferramentas de linha de comando, em Python, para gerenciar conteúdo em supergrupos do Telegram com tópicos (fórum). Elas usam a sua conta de usuário, não um bot.

| Ferramenta | O que faz |
|---|---|
| [Telegram_topic_manager](Telegram_topic_manager/README.md) | Envia cada pasta de `input/` para um tópico (criado ou já existente) de um supergrupo onde você é admin. |
| [Telegram_specific_topic_dl](Telegram_specific_topic_dl/README.md) | Baixa as mídias de um canal, de um grupo ou de um tópico específico de um supergrupo. |

As duas são independentes: cada uma é executada sozinha. Elas compartilham o ambiente virtual, as credenciais e a sessão de login, que ficam na raiz do repositório.

```
telegram_tools/
├── .venv/                  ambiente virtual compartilhado (não vai para o Git)
├── config.json             api_id / api_hash (não vai para o Git)
├── user.session            sessão logada do Telegram (não vai para o Git)
├── requirements.txt
├── tg_common.py            código comum: credenciais, login, menus
├── Telegram_topic_manager/
│   ├── ttm.py
│   └── video_tools.py      análise e conversão de vídeos com ffmpeg
└── Telegram_specific_topic_dl/
    └── tldlsp.py
```

## Requisitos e compatibilidade

| Componente | Versão | Observação |
|---|---|---|
| Python | **3.11 ou mais novo** (testado no 3.14.8) | O mínimo vem do `cryptg` 0.6.1. Versões abaixo de 3.14 não foram testadas. |
| [Telethon](https://docs.telethon.dev) | `>=1.45,<2` (testado na 1.45.0) | Biblioteca principal. |
| [cryptg](https://pypi.org/project/cryptg/) | `>=0.6` (testado na 0.6.1) | Opcional, mas recomendado: criptografia em C que acelera muito uploads e downloads. |
| [hachoir](https://pypi.org/project/hachoir/) | `>=3.4` (testado na 3.4.0) | Lê a duração e a resolução dos vídeos quando o ffmpeg não está instalado. Sem nenhum dos dois, o Telegram mostra o vídeo sem duração. |
| [FFmpeg](https://ffmpeg.org) (`ffmpeg` e `ffprobe`) | testado no 9.0.2 | Programa do sistema, fora do pip (Arch: `sudo pacman -S ffmpeg`; Debian/Ubuntu: `sudo apt install ffmpeg`). O `ttm.py` usa para analisar, converter e dividir vídeos e gerar miniaturas. Opcional: sem ele, essa etapa é pulada. |

Sobre o Telethon (situação em novembro de 2026):

- **O projeto saiu do GitHub.** O repositório antigo (`LonamiWebs/Telethon`) foi arquivado, e o desenvolvimento continua no [Codeberg](https://codeberg.org/Lonami/Telethon). O pacote no PyPI continua o mesmo e recebe versões normalmente (1.43 em abril, 1.44 em junho e 1.45 em setembro de 2026).
- **A série 1.x é a estável.** A 2.x está em desenvolvimento numa branch separada, nunca foi publicada no PyPI e muda a API. Por isso o `requirements.txt` limita a `<2`.
- **Python 3.14:** o suporte foi corrigido nas versões 1.42 e 1.43. Versões anteriores do Telethon podem falhar no 3.14.
- **Ignore o "Python >=3.5" do PyPI.** Os metadados do pacote não são atualizados há anos (os classificadores param no 3.8). Use a tabela acima como referência.
- Mudanças relevantes desde a 1.42: correção do `iter_messages` em ordem reversa, que o downloader usa (1.43); menos timeouts e menos uso de CPU quando a conexão cai (1.44); reconexão mais robusta (1.45). O [changelog oficial](https://docs.telethon.dev/en/stable/misc/changelog.html) traz os detalhes.

## Instalação

```bash
cd telegram_tools
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Se você rodar um script sem o venv ativo, ele avisa que o Telethon não foi encontrado.

### Atualizar as dependências

```bash
source .venv/bin/activate
pip install -U -r requirements.txt
pip freeze            # confira as versões instaladas
```

Antes de subir o mínimo no `requirements.txt`, leia o changelog do Telethon e teste as duas ferramentas. Para trocar de versão do Python, apague o `.venv` e recrie-o com o novo interpretador (`python3.X -m venv .venv`). Um venv não sobrevive à troca de versão do Python.

## Credenciais e login

1. Crie um `api_id` e um `api_hash` em <https://my.telegram.org> → *API development tools*.
2. Na primeira execução de qualquer uma das ferramentas, informe os dois. O hash não aparece enquanto você digita. Eles ficam salvos em `config.json`, na raiz.
   - Como alternativa, defina as variáveis de ambiente `TG_API_ID` e `TG_API_HASH`. Elas têm prioridade sobre o arquivo.
3. Em seguida, o Telethon pede o seu telefone, o código enviado pelo app e, se você tiver, a senha de duas etapas. O login fica salvo em `user.session` e vale para as duas ferramentas.

Não rode as duas ferramentas ao mesmo tempo: a sessão é um banco SQLite único, e a segunda execução avisa que ela está em uso.

## Segurança

- **`user.session` é a sua conta logada.** Quem tiver esse arquivo acessa o seu Telegram sem senha nem código. O `config.json` contém o `api_hash`. Os dois estão no `.gitignore`. Rode `git status` antes de cada commit para conferir.
- As ferramentas criam esses arquivos com permissão `600` (só o seu usuário lê) e corrigem a permissão se ela estiver aberta. Por padrão, o Telethon cria a sessão com permissão `644`.
- Para encerrar a sessão (o mesmo que remover o dispositivo no app) e apagar o `user.session`:
  ```bash
  python Telegram_topic_manager/ttm.py --logout
  ```
- Se a sessão vazar, encerre-a também pelo app: *Configurações → Dispositivos*. Apagar o arquivo de um commit não basta, porque ele continua no histórico do Git.
- O `.gitignore` também barra as pastas de conteúdo (`input/`, `output/`, `originais/`, `downloads/`) e mídias soltas.
