"""Publica uma solicitacao de envio de e-mail na fila."""

import argparse
import logging
import uuid

from email_service import validar_payload
from fila_oci import carregar_configuracao, criar_cliente


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destinatario", default="aluno@example.com")
    parser.add_argument("--assunto", default="Teste da fila OCI")
    parser.add_argument("--corpo", default="Minha primeira mensagem enviada pelo Python.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cliente = None
    try:
        payload = validar_payload({"id_solicitacao": str(uuid.uuid4()),
                                   "destinatario": args.destinatario,
                                   "assunto": args.assunto, "corpo": args.corpo})
        cliente = criar_cliente(carregar_configuracao())
        confirmacao = cliente.publicar(payload)
        logging.info("PUBLICADA solicitacao=%s mensagem_oci=%s",
                     payload["id_solicitacao"], confirmacao.get("id"))
        return 0
    except Exception as erro:
        logging.error("Publicacao nao confirmada: %s", erro)
        logging.error("Se houve timeout, verifique a fila antes de repetir: ela pode ter recebido a mensagem.")
        return 1
    finally:
        if cliente:
            cliente.fechar()


if __name__ == "__main__":
    raise SystemExit(main())
