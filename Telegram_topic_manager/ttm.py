#!/usr/bin/env python3

import json
from pathlib import Path
from telethon import TelegramClient, functions, types
from telethon.tl.types import UpdateChannelMessageViews
from telethon.tl.types import Channel
from natsort import natsorted  # Instale com: pip install natsort
import getpass
import asyncio
import os

SESSION_FILE = "user.session"
CONFIG_FILE = "config.json"
###################
class ProgressoUpload:
    def __init__(self, arquivo, tamanho_total):
        self.arquivo = arquivo
        self.tamanho_total = tamanho_total
        self.bytes_enviados = 0
        self.ultima_porcentagem = 0
        
    def atualizar(self, bytes_enviados, total):
        self.bytes_enviados = bytes_enviados
        porcentagem = int((bytes_enviados / self.tamanho_total) * 100)
        
        # Atualizar apenas se a porcentagem mudou
        if porcentagem != self.ultima_porcentagem:
            self.ultima_porcentagem = porcentagem
            self.exibir()
            
    def exibir(self):
        # Tamanho em MB
        enviado_mb = self.bytes_enviados / (1024 * 1024)
        total_mb = self.tamanho_total / (1024 * 1024)
        
        # Barra de progresso simples
        barra = '⬛' * (self.ultima_porcentagem // 5) + '⬜' * (20 - (self.ultima_porcentagem // 5))
        
        print(f"\r[{barra}] {self.ultima_porcentagem}% | {enviado_mb:.1f}/{total_mb:.1f} MB - {self.arquivo.name}", end='', flush=True)
#####################

def carregar_config():
    try:
        with open(CONFIG_FILE, "r") as f:
            config = json.load(f)
            return config["api_id"], config["api_hash"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError):
        return None, None

def salvar_config(api_id, api_hash):
    with open(CONFIG_FILE, "w") as f:
        json.dump({"api_id": api_id, "api_hash": api_hash}, f)
    Path(CONFIG_FILE).chmod(0o600)

async def main():
    api_id, api_hash = carregar_config()

    # Coletar credenciais se não existirem
    if not api_id or not api_hash:
        print("=== Configuração Inicial ===")
        api_id = input("Digite seu API ID do Telegram: ").strip()
        api_hash = getpass.getpass("Digite seu API Hash: ").strip()
        salvar_config(api_id, api_hash)
        print("\nConfiguração salva!\n")

    # Iniciar cliente
    client = TelegramClient(SESSION_FILE, api_id, api_hash)
    
    try:
        await client.start()
        print("\n=== Login realizado com sucesso! ===")

        # Listar grupos do usuário
        print("\nBuscando grupos...")
        grupos = []
        async for dialog in client.iter_dialogs():
            chat = dialog.entity
            if isinstance(chat, Channel) and chat.creator and chat.forum:
                grupos.append(chat)

        # Exibir grupos numerados
        print("\nGrupos disponíveis (com tópicos habilitados):")
        for i, grupo in enumerate(grupos, 1):
            print(f"{i}. {grupo.title}")
            
        # Seleção do grupo
        while True:
            try:
                escolha = int(input("\nDigite o número do grupo: "))
                grupo_escolhido = grupos[escolha-1]
                break
            except (ValueError, IndexError):
                print("Número inválido. Tente novamente.")
                
        # Verificar pasta input
        input_dir = Path("input")
        if not input_dir.exists():
            print("\nPasta 'input' não encontrada!")
            return
        
        # Listar cursos/tópicos a serem criados
        pastas_cursos = [d for d in input_dir.iterdir() if d.is_dir()]
        if not pastas_cursos:
            print("\nNenhum curso encontrado na pasta 'input'!")
            return
        
        print("\nCursos encontrados para criação:")
        for pasta in pastas_cursos:
            print(f"- {pasta.name}")
            
        # Confirmar com usuário
        confirmar = input("\nDeseja criar esses tópicos e enviar conteúdo? (s/N): ").lower()
        print(confirmar)
        if confirmar != 's':
            print("Operação cancelada.")
            return
        
        # Criar tópicos e processar conteúdo
        for pasta_curso in pastas_cursos:
            nome_topico = pasta_curso.name
            print(f"\nProcessando: {nome_topico}")
            
            # Verifica/Cria tópico
            topico_id = await criar_ou_obter_topico(client, grupo_escolhido, nome_topico)
            
            if not topico_id:
                print("❌ Não foi possível obter/criar o tópico. Pulando...")
                continue
            
            # Verifica conteúdo já enviado
            ja_enviados = await listar_conteudo_enviado(client, grupo_escolhido, topico_id)
            
            # Processa arquivos
            await processar_conteudo(client, grupo_escolhido, topico_id, pasta_curso, ja_enviados)

    finally:
        await client.disconnect()
        
        
async def criar_ou_obter_topico(client, grupo, nome_topico):
    try:
        # Verifica tópico existente
        topico_id = await verificar_topico_existente(client, grupo, nome_topico)
        if topico_id:
            print(f"Tópico existente encontrado: ID {topico_id}")
            return topico_id

        # Cria novo tópico
        result = await client(functions.channels.CreateForumTopicRequest(
            channel=grupo,
            title=nome_topico
        ))
        # print(f"o ID do topico novo é{result.topic.id} ou então {result.updates}")

        # Extrai o ID do tópico da resposta
        topic_id =  None
        for update in result.updates:
            # Método 1: Atualização direta do tópico
            if hasattr(update, 'topic') and isinstance(update.topic, types.ForumTopic):
                print("Pegou tópico do update")
                topic_id = update.topic.id
                break
                
            # Método 2: Mensagem de serviço de criação
            if isinstance(update, types.UpdateNewChannelMessage):
                msg = update.message
                if isinstance(msg.action, types.MessageActionTopicCreate):
                    print("Pegou tópico do update das mensagens")
                    topic_id = msg.id #return msg.id  # ID da mensagem raiz do tópico
                    break

        if not topic_id:
            raise ValueError("ID do tópico não encontrado na resposta")
        
        #Fechar o tópico(trancar ele não apaga o tópico)
        await client(functions.channels.EditForumTopicRequest(
            channel=grupo,
            topic_id=topic_id,
            closed=True  # ← Parâmetro crucial
        ))
        print("Tópico configurado como fechado com sucesso!")

        return topic_id
    
    except Exception as e:
        print(f"Erro na criação do tópico: {str(e)}")
        return None
    
async def verificar_topico_existente(client, grupo, nome_topico):
    try:
        # Converter para InputChannel corretamente
        input_channel = types.InputChannel(
            channel_id=grupo.id,
            access_hash=grupo.access_hash
        )
        
        all_topics = []
        offset_date = 0
        offset_id = 0
        offset_topic = 0
        total = 0
        
        while True:
            # Fazer a requisição com todos os offsets
            result = await client(functions.channels.GetForumTopicsRequest(
                channel=input_channel,
                offset_date=offset_date,
                offset_id=offset_id,
                offset_topic=offset_topic,
                limit=100  # Máximo permitido pela API
            ))
            
            # Capturar o total real na primeira iteração
            if not total:
                total = result.count
            
            # Processar tópicos válidos
            valid_topics = [t for t in result.topics if isinstance(t, types.ForumTopic)]
            
            # Verificar se encontrou o tópico
            for topic in result.topics:
                if topic.title == nome_topico:
                    return topic.id
            
            # Atualizar offsets com base no último tópico
            if valid_topics:
                last_topic = valid_topics[-1]
                
                # Obter a data da última mensagem do tópico
                last_message = next((m for m in result.messages 
                                   if m.id == last_topic.top_message), None)
                
                offset_date = last_message.date if last_message else 0
                offset_id = last_topic.top_message
                offset_topic = last_topic.id
            else:
                break  # Não há mais tópicos
            
            # Condição de saída quando atingir o total
            if len(all_topics) + len(valid_topics) >= total:
                break
                
            all_topics.extend(valid_topics)

        return None

    except Exception as e:
        print(f"Erro na verificação de tópicos: {str(e)}")
        return None

async def listar_conteudo_enviado(client, grupo, topico_id):
    try:
        mensagens = await client.get_messages(
            entity=grupo,
            limit=100,
            reply_to=topico_id  # ← Parâmetro correto para tópicos
        )
        return {msg.file.name if msg.file else None for msg in mensagens}
    except Exception as e:
        print(f"Erro ao verificar mensagens: {str(e)}")
        return set()


async def processar_conteudo(client, grupo, topico_id, pasta_curso, ja_enviados):
    arquivos = ordenar_arquivos(pasta_curso)
    
    for arquivo in arquivos:
        if arquivo.name in ja_enviados:
            print(f"⏩ Pulando arquivo já enviado: {arquivo.name}")
            continue
        
        if is_video(arquivo):
            await enviar_video(client, grupo, topico_id, arquivo)
        else:
            await enviar_arquivo(client, grupo, topico_id, arquivo)
        
        # Mover para output
        mover_para_output(arquivo, pasta_curso.name)

def ordenar_arquivos(pasta):
    arquivos = list(pasta.iterdir())
    return natsorted(arquivos, key=lambda x: (not is_video(x), x.name))

def is_video(arquivo):
    return arquivo.suffix.lower() in ['.mp4', '.mov', '.avi', '.mkv']

async def enviar_video(client, grupo, topico_id, caminho):
    legenda = caminho.stem
    print(f"Enviando vídeo: {legenda}")
    
    try:
        # Criar objeto InputForumTopic com o ID do tópico
        # forum_topic = types.InputForumTopic(
        #     channel=await client.get_input_channel(grupo.id),
        #     topic_id=topico_id
        # )
        
        # Criar objeto de progresso
        tamanho = caminho.stat().st_size
        progresso = ProgressoUpload(caminho, tamanho)
        
        await client.send_file(
            entity=grupo,
            file=str(caminho),
            caption=legenda,
            supports_streaming=True,
            # Usar reply_to com ID da mensagem do tópico
            reply_to=topico_id,  # ← Mudança crucial aqui
            progress_callback=progresso.atualizar
        )
        print("\n✅ Upload concluído!")
        
    except Exception as e:
        print(f"Erro ao enviar vídeo: {str(e)}")
        
async def enviar_arquivo(client, grupo, topico_id, caminho):
    legenda = caminho.stem  # Nome do arquivo sem extensão
    print(f"Enviando arquivo: {legenda}")
    
    try:
        # Criar objeto de progresso
        tamanho = caminho.stat().st_size
        progresso = ProgressoUpload(caminho, tamanho)
        
        await client.send_file(
            entity=grupo,
            file=str(caminho),
            caption=legenda,
            # Parâmetros diferentes do envio de vídeo:
            force_document=True,  # Forçar como documento
            supports_streaming=False,
            reply_to=topico_id,
            progress_callback=progresso.atualizar
        )
        print("\n✅ Upload concluído!")
        
    except Exception as e:
        print(f"\n❌ Erro ao enviar arquivo: {str(e)}")

def mover_para_output(arquivo, nome_curso):
    output_dir = Path("output") / nome_curso
    output_dir.mkdir(parents=True, exist_ok=True)
    
    destino = output_dir / arquivo.name
    try:
        arquivo.rename(destino)
    except Exception as e:
        print(f"Erro ao mover arquivo: {e}")

if __name__ == "__main__":
    asyncio.run(main())