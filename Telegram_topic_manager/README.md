# Telegram Topic Manager

Script para enviar arquivos locais para topicos de um supergrupo forum do Telegram.

## Instalacao

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Uso

Coloque os arquivos em pastas dentro de `input/`. Cada pasta vira um topico, ou reutiliza um topico existente com o mesmo nome.

```bash
python ttm_modern.py
```

Na primeira execucao o script pede `api_id` e `api_hash` do Telegram e salva em `config.json`. O login cria/reutiliza `user.session`.

Depois do upload, os arquivos enviados sao movidos para `output/`.
