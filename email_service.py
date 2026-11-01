"""Validacao do payload e envio SMTP com TLS."""

import json
import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import parseaddr

LOG = logging.getLogger(__name__)


def validar_payload(payload):
    if not isinstance(payload, dict):
        raise ValueError("O payload deve ser um objeto JSON.")
    for campo in ("id_solicitacao", "destinatario", "assunto", "corpo"):
        if not isinstance(payload.get(campo), str) or not payload[campo].strip():
            raise ValueError(f"Campo obrigatorio ausente ou invalido: {campo}.")
    destinatario = payload["destinatario"]
    if ("\r" in destinatario or "\n" in destinatario
            or parseaddr(destinatario)[1] != destinatario or "@" not in destinatario):
        raise ValueError("Informe um unico endereco de e-mail no destinatario.")
    if "\r" in payload["assunto"] or "\n" in payload["assunto"]:
        raise ValueError("O assunto nao pode ter quebras de linha.")
    return payload


def interpretar_payload(conteudo):
    return validar_payload(json.loads(conteudo))


class EnviadorEmail:
    def __init__(self, config, modo="simulado", senha=None):
        self.modo, self.senha = modo, senha
        self.smtp = dict(config["smtp"]) if config.has_section("smtp") else {}
        if modo == "smtp":
            for campo in ("host", "remetente"):
                if not self.smtp.get(campo, "").strip():
                    raise ValueError(f"Preencha smtp.{campo} em config.ini.")
            if self.smtp.get("seguranca") not in ("starttls", "ssl"):
                raise ValueError("smtp.seguranca deve ser starttls ou ssl.")
            if not 1 <= int(self.smtp.get("porta", "587")) <= 65535:
                raise ValueError("Porta SMTP invalida.")
            if float(self.smtp.get("timeout_segundos", "10")) <= 0:
                raise ValueError("O timeout SMTP precisa ser positivo.")

    def enviar(self, payload):
        if self.modo == "simulado":
            LOG.info("SIMULACAO concluida para a solicitacao %s; nenhum e-mail foi enviado.",
                     payload["id_solicitacao"])
            return
        mensagem = EmailMessage()
        mensagem["From"] = self.smtp["remetente"]
        mensagem["To"] = payload["destinatario"]
        mensagem["Subject"] = payload["assunto"]
        mensagem.set_content(payload["corpo"])
        argumentos = {"host": self.smtp["host"], "port": int(self.smtp["porta"]),
                      "timeout": float(self.smtp.get("timeout_segundos", "10"))}
        contexto = ssl.create_default_context()
        if self.smtp["seguranca"] == "ssl":
            conexao = smtplib.SMTP_SSL(**argumentos, context=contexto)
        else:
            conexao = smtplib.SMTP(**argumentos)
        with conexao as servidor:
            if self.smtp["seguranca"] == "starttls":
                servidor.starttls(context=contexto)
            if self.smtp.get("usuario"):
                servidor.login(self.smtp["usuario"], self.senha)
            recusados = servidor.send_message(mensagem)
            if recusados:
                raise RuntimeError("O servidor SMTP recusou o destinatario.")
        LOG.info("Servidor SMTP aceitou a solicitacao %s.", payload["id_solicitacao"])
