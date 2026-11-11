#!/usr/bin/env python3

import asyncio
import re
import os
import json
import copy
from pathlib import Path
from telethon import TelegramClient, sync
from telethon.utils import get_extension
from telethon.tl.types import DocumentAttributeFilename
from telethon.tl.functions.messages import GetDialogsRequest, GetHistoryRequest
from telethon.tl.types import InputPeerEmpty, InputChannel
from telethon.tl.types import ForumTopic, ForumTopicDeleted #para checagem de tópicos deletados e mais informações
from telethon.tl.functions.channels import GetForumTopicsRequest
from telethon.tl.types import Channel, Chat
from getpass import getpass

SESSION_FILE = "user"
CONFIG_FILE = 'config.json'

def load_config():
    config_path = Path(CONFIG_FILE)
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                config = json.load(f)
                return config['api_id'], config['api_hash']
        except (json.JSONDecodeError, KeyError):
            print("Configuração inválida. Vamos recriar...")
    return None, None

def save_config(api_id, api_hash):
    config = {'api_id': api_id, 'api_hash': api_hash}
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f)
    Path(CONFIG_FILE).chmod(0o600)  # Permissões restritas
    
def sanitize_filename(name):
    """Remove caracteres inválidos e normaliza underscores"""
    # Substitui caracteres especiais por um único underscore
    name = re.sub(r'[\\/*?:"<>|]', '_', name)
    # Remove múltiplos underscores consecutivos
    name = re.sub(r'_+', '_', name)
    # Remove espaços extras e underscores no início/fim
    return name.strip(' _')[:150]  # Limite seguro para sistemas de arquivos   

async def download_topic_media(client, group, topic):
    # Cria diretório com o nome do tópico
    dir_name = sanitize_filename(topic.title)
    os.makedirs(dir_name, exist_ok=True)
    print(f"\n📥 Baixando mídias para '{dir_name}'...")
    # Busca todas as mensagens do tópico
    messages = await client.get_messages(
        entity=group,
        reply_to=topic.id,  # Filtra pelo tópico
        limit=None,
        reverse=True        # Ordena do mais antigo ao mais novo
    )
    downloaded = 0
    skipped = 0
    
    for msg in messages:
        if not msg.media:
            continue
            
        # Determina o nome base do arquivo
        if msg.text:
            base_name = msg.text.strip()
        else:
            # Tenta obter o nome original do arquivo
            if hasattr(msg.media, 'document') and msg.media.document:
                for attr in msg.media.document.attributes:
                    if isinstance(attr, DocumentAttributeFilename):
                        base_name = attr.file_name
                        break
                else:
                    base_name = f"arquivo_{msg.id}"
            else:
                base_name = f"midia_{msg.id}"

        # Extrai metadados do arquivo principal
        # Sanitização e construção do caminho
        clean_name = sanitize_filename(base_name)
        ext = get_extension(msg.media)
        file_name = f"{clean_name}.{ext}"
        file_path = os.path.join(dir_name, file_name)
        # Verificação de arquivo existente
        if os.path.exists(file_path):
            # Verifica se o arquivo existente está completo
            if os.path.getsize(file_path) > 1024:
                skipped += 1
                continue
            
        # Download com tratamento de erros
        try:
            # Download do arquivo principal (não thumbnail)
            await client.download_media(
                message=msg,
                file=file_path,
                thumb=None          # Ignora thumbnail
            )
            # Validação pós-download
            if not os.path.exists(file_path) or os.path.getsize(file_path) < 1024:
                raise ValueError("Arquivo vazio ou inválido")
            
            downloaded += 1
            print(f"✅ {file_name} ({media.size // 1024} KB)")
            
        except Exception as e:
            print(f"❌ Erro em {file_name}: {str(e)}")
    
    print(f"\nConcluído! Baixados: {downloaded}, Ignorados: {skipped}")

async def list_topics(client, group):
    # Converter para InputChannel
    input_channel = InputChannel(group.id, group.access_hash)
    all_topics = []
    offset_topic = 0  # Controle de paginação
    limit = 100  # Valor máximo permitido pela API
    date, offset, offset_topic, total = 0, 0, 0, 0
    
    try:
        while True:
            result = await client(GetForumTopicsRequest(
                channel=input_channel,
                offset_date=date,
                offset_id=offset,
                offset_topic=offset_topic,
                limit=limit
            ))
            # Processa os tópicos válidos
            if not total:
                total = result.count
                
            topic_list=result.topics
            if not topic_list or len(all_topics) >= total:
                break
            
            valid_topics = [t for t in result.topics if isinstance(t, ForumTopic)]
            all_topics.extend(valid_topics)
            last = topic_list[-1]
            
            offset_topic, offset = last.id, last.top_message
            date = {m.id: m.date for m in result.messages}.get(offset, 0)
        
        return total, all_topics
        
    except Exception as e:
        print(f"Erro: {str(e)}")
        return []

async def main():
    api_id, api_hash = load_config()
    
    if not api_id or not api_hash:
        print("=== Primeira configuração ===")
        api_id = input("API ID (my.telegram.org): ").strip()
        api_hash = getpass("API Hash: ").strip()
        save_config(int(api_id), api_hash)
        print("\nCredenciais salvas com segurança!\n")
        
    print("=== Entrando na conta ===")
    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    
    try:
        await client.start()
        print("\nAutenticação bem-sucedida!\n")
        
        print("Carregando grupos...")
        dialogs = await client.get_dialogs()
        
        groups = []
        for dialog in dialogs:
            if isinstance(dialog.entity, Channel) and dialog.entity.megagroup and dialog.entity.forum:
                groups.append(dialog.entity)
        
        print("\nGrupos disponíveis:")
        for i, group in enumerate(groups, 1):
            print(f"{i}. {group.title}")
            print(f"""
                Título: {group.title}
                ID: {group.id}
                Access Hash: {group.access_hash}
                É supergrupo: {group.megagroup}
                Tem tópicos: {group.forum}
                """)
        
        choice = int(input("\nDigite o número do grupo: ")) - 1
        selected_group = copy.copy(groups[choice])
        
        total, topics = await list_topics(client, selected_group)
        print(f"Esse grupo tem o total de {total} grupos")
        # Na parte da listagem:
        for i, topic in enumerate(topics, 1):
            # Verificação adicional para segurança
            if not isinstance(topic, ForumTopic):
                print(f"Tópico-{i}: [DELETADO OU INVÁLIDO] (ID: {topic.id})")
                continue
                
            print(f"Tópico-{i}: {topic.title} (ID: {topic.id})")
       
        topic_choice = int(input("\nDigite o número do tópico: ")) - 1
        selected_topic=copy.copy(topics[topic_choice])
        
        await download_topic_media(client, selected_group, selected_topic)
        
    except Exception as e:
        print(f"Erro: {str(e)}")
    finally:
        await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())