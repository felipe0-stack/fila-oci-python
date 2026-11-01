"""Recebe mensagens, processa e exclui somente depois do sucesso."""

import argparse
import getpass
import logging
import os
import time

from email_service import EnviadorEmail, interpretar_payload
from fila_oci import ErroFila, carregar_configuracao, criar_cliente

LOG = logging.getLogger(__name__)


def processar_mensagem(cliente, mensagem, enviador, atraso=5,
                       falhar_primeira=False, falhar_sempre=False):
    receipt = mensagem["receipt"]
    entregas = mensagem.get("deliveryCount", 1)
    LOG.info("RECEBIDA mensagem_oci=%s entrega=%s", mensagem.get("id"), entregas)
    try:
        payload = interpretar_payload(mensagem["content"])
        if falhar_sempre or (falhar_primeira and entregas == 1):
            raise RuntimeError("Falha controlada para demonstrar uma nova tentativa.")
        enviador.enviar(payload)
    except Exception as erro:
        LOG.warning("PROCESSAMENTO FALHOU: %s", erro)
        try:
            # Nao republicamos a mensagem: ela permanece na fila da OCI.
            cliente.atualizar_visibilidade(receipt, atraso)
            LOG.info("NOVA TENTATIVA podera ocorrer em %s segundos.", atraso)
        except ErroFila as erro_fila:
            LOG.error("Nao foi possivel atualizar a visibilidade: %s", erro_fila)
        return False
    # A confirmacao e separada do envio: falhar ao excluir nao desfaz o e-mail.
    try:
        cliente.excluir(receipt)
        LOG.info("EXCLUIDA mensagem_oci=%s", mensagem.get("id"))
        return True
    except ErroFila as erro:
        LOG.error("Processamento concluido, mas exclusao nao confirmada: %s", erro)
        LOG.error("A mensagem pode reaparecer e causar processamento repetido.")
        return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--modo", choices=("simulado", "smtp"), default="simulado")
    parser.add_argument("--uma-vez", action="store_true", help="Executa somente uma consulta.")
    falhas = parser.add_mutually_exclusive_group()
    falhas.add_argument("--falhar-primeira", action="store_true")
    falhas.add_argument("--falhar-sempre", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cliente = None
    try:
        config = carregar_configuracao()
        senha = None
        if args.modo == "smtp" and config.get("smtp", "usuario", fallback=""):
            senha = os.environ.get("SMTP_PASSWORD") or getpass.getpass("Senha SMTP: ")
        enviador = EnviadorEmail(config, args.modo, senha)
        cliente = criar_cliente(config)
        LOG.info("Consumidor iniciado; modo=%s. Encerre com Ctrl+C.", args.modo)
        if args.modo == "simulado":
            LOG.info("Mensagens processadas em SIMULACAO tambem serao excluidas da fila.")
        while True:
            try:
                mensagens = cliente.receber(config.getint("consumidor", "visibilidade_segundos"),
                                             config.getint("consumidor", "espera_segundos"))
            except ErroFila as erro:
                LOG.error("Falha ao consultar a fila: %s", erro)
                # Configuracao/permissao incorreta exige correcao, nao repeticao sem fim.
                if args.uma_vez or (erro.status and erro.status not in (408, 429) and erro.status < 500):
                    return 1
                time.sleep(5)
                continue
            sucesso = True
            for mensagem in mensagens:
                sucesso = processar_mensagem(
                    cliente, mensagem, enviador,
                    config.getint("consumidor", "nova_tentativa_segundos"),
                    args.falhar_primeira, args.falhar_sempre,
                ) and sucesso
            if not mensagens:
                LOG.info("Nenhuma mensagem disponivel nesta consulta.")
            if args.uma_vez:
                return 0 if sucesso else 1
            # Pequena pausa tambem protege contra respostas vazias antecipadas.
            if not mensagens:
                time.sleep(1)
    except KeyboardInterrupt:
        LOG.info("Consumidor encerrado.")
        return 0
    except Exception as erro:
        LOG.error("Nao foi possivel continuar: %s", erro)
        return 1
    finally:
        if cliente:
            cliente.fechar()


if __name__ == "__main__":
    raise SystemExit(main())
