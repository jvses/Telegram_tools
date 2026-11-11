# Telegram Specific Topic Downloader

Baixa as mídias de um canal, de um grupo inteiro ou de um tópico específico de um supergrupo fórum do Telegram do qual a sua conta participa.

Instalação, credenciais e login são comuns às duas ferramentas: veja o [README da raiz](../README.md).

## Uso

```bash
source .venv/bin/activate                         # na raiz do repositório
python Telegram_specific_topic_dl/tldlsp.py
```

1. Escolha o canal ou grupo. Digitar parte do nome filtra a lista, e acentos não importam.
2. Se for um supergrupo com tópicos, escolha um tópico ou o chat inteiro.

Os arquivos são salvos em `Telegram_specific_topic_dl/downloads/<canal_ou_grupo>/<tópico_ou_chat_inteiro>/`, numerados na ordem em que foram postados: `0001-nome.ext`, `0002-...`.

| Opção | Efeito |
|---|---|
| `--saida PASTA` | Salva em outra pasta no lugar de `downloads/`. |
| `--logout` | Encerra a sessão do Telegram e apaga o `user.session`. |

## Como os downloads são tratados

- **Rodar de novo é seguro:** arquivos que já existem com o tamanho certo são pulados. Um arquivo incompleto é baixado outra vez.
- **Downloads interrompidos:** cada download é gravado como `.part` e só recebe o nome final quando termina. Um download interrompido nunca é confundido com um arquivo completo.
- **Nomes seguros:** o nome vem do arquivo original ou da primeira linha da legenda. Os nomes são limpos (sem `/`, `..` ou caracteres inválidos), e extensões estranhas viram `.bin`.
- **Prévias de link e mensagens sem arquivo** são ignoradas.
- **Tópico "General":** também funciona. As mensagens dos outros tópicos são filtradas.
- **Limites do Telegram:** quando o Telegram pede uma pausa (FloodWait), o script espera e tenta de novo.
