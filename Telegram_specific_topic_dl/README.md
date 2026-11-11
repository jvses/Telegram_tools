# Telegram Specific Topic Downloader

Script para baixar midias de um canal, grupo inteiro ou topico especifico de um supergrupo forum do Telegram.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

```bash
python tldlsp_modern.py
```

Na primeira execucao o script pede `api_id` e `api_hash` do Telegram e salva em `config.json`. O login cria/reutiliza `user.session`.

Os downloads sao salvos em `downloads/<canal_ou_grupo>/<topico_ou_chat_inteiro>/`.
