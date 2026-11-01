"""Testes locais: nao usam conta OCI nem enviam e-mails."""

import base64
import configparser
import hashlib
import json
import re
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

import oci
import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from consumidor import processar_mensagem
from email_service import EnviadorEmail, interpretar_payload
from fila_oci import ClienteFila, ErroFila


PAYLOAD = {"id_solicitacao": "teste-001", "destinatario": "aluno@example.com",
           "assunto": "Confirmação", "corpo": "Olá! Sua solicitação chegou."}


def mensagem(conteudo=None, entregas=1):
    return {"id": 123, "receipt": "receipt-atual", "deliveryCount": entregas,
            "content": json.dumps(PAYLOAD) if conteudo is None else conteudo}


class FluxoTests(unittest.TestCase):
    def test_sucesso_envia_antes_de_excluir(self):
        eventos = []
        cliente, enviador = Mock(), Mock()
        enviador.enviar.side_effect = lambda payload: eventos.append("envio")
        cliente.excluir.side_effect = lambda receipt: eventos.append("exclusao")
        self.assertTrue(processar_mensagem(cliente, mensagem(), enviador))
        self.assertEqual(eventos, ["envio", "exclusao"])
        cliente.excluir.assert_called_once_with("receipt-atual")
        cliente.atualizar_visibilidade.assert_not_called()

    def test_falha_temporaria_reentrega_sem_republicar(self):
        cliente, enviador = Mock(), Mock()
        self.assertFalse(processar_mensagem(cliente, mensagem(), enviador, falhar_primeira=True))
        cliente.excluir.assert_not_called()
        enviador.enviar.assert_not_called()
        cliente.atualizar_visibilidade.assert_called_once_with("receipt-atual", 5)
        cliente.publicar.assert_not_called()
        self.assertTrue(processar_mensagem(cliente, mensagem(entregas=2), enviador,
                                          falhar_primeira=True))
        enviador.enviar.assert_called_once_with(PAYLOAD)

    def test_json_invalido_nao_exclui(self):
        cliente, enviador = Mock(), Mock()
        self.assertFalse(processar_mensagem(cliente, mensagem("{invalido"), enviador))
        cliente.excluir.assert_not_called()
        enviador.enviar.assert_not_called()
        cliente.atualizar_visibilidade.assert_called_once()

    def test_falha_na_exclusao_nao_dispara_envio_novamente_no_mesmo_ciclo(self):
        cliente, enviador = Mock(), Mock()
        cliente.excluir.side_effect = ErroFila("timeout")
        self.assertFalse(processar_mensagem(cliente, mensagem(), enviador))
        enviador.enviar.assert_called_once()
        cliente.atualizar_visibilidade.assert_not_called()

    def test_atualizacao_falha_sem_excluir(self):
        cliente, enviador = Mock(), Mock()
        enviador.enviar.side_effect = RuntimeError("SMTP indisponivel")
        cliente.atualizar_visibilidade.side_effect = ErroFila("timeout")
        self.assertFalse(processar_mensagem(cliente, mensagem(), enviador))
        cliente.excluir.assert_not_called()

    def test_payload_exige_campos_e_rejeita_cabecalho_injetado(self):
        for payload in ({}, [], dict(PAYLOAD, assunto="Teste\nBcc: outro@example.com")):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                interpretar_payload(json.dumps(payload))

    @patch("email_service.smtplib.SMTP")
    def test_smtp_utiliza_tls_autentica_e_envia(self, smtp):
        config = configparser.ConfigParser()
        config.read_dict({"smtp": {"host": "smtp.example.com", "porta": "587",
                                  "seguranca": "starttls", "usuario": "aluno",
                                  "remetente": "origem@example.com"}})
        servidor = smtp.return_value.__enter__.return_value
        servidor.send_message.return_value = {}
        EnviadorEmail(config, "smtp", "senha-ficticia").enviar(PAYLOAD)
        servidor.starttls.assert_called_once()
        servidor.login.assert_called_once_with("aluno", "senha-ficticia")
        email = servidor.send_message.call_args.args[0]
        self.assertEqual(email["To"], PAYLOAD["destinatario"])
        self.assertIn("Olá!", email.get_content())


class TransporteAssinadoTests(unittest.TestCase):
    def test_requisicoes_usam_assinatura_valida_e_receipt_codificado(self):
        # A chave existe somente na memoria do teste. Nenhum segredo real e usado.
        chave = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = chave.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                  serialization.NoEncryption()).decode()
        sessao = requests.Session()
        sessao.auth = oci.signer.Signer("tenancy-teste", "usuario-teste", "fingerprint-teste",
                                       None, private_key_content=pem)
        capturadas = []

        def responder(request, **kwargs):
            capturadas.append((request, kwargs))
            parametros = dict(re.findall(r'(\w+)="([^"]*)"', request.headers["Authorization"]))
            linhas = []
            for cabecalho in parametros["headers"].split():
                valor = (f"{request.method.lower()} {request.path_url}" if cabecalho == "(request-target)"
                         else request.headers[cabecalho])
                linhas.append(f"{cabecalho}: {valor}")
            chave.public_key().verify(base64.b64decode(parametros["signature"]),
                                      "\n".join(linhas).encode(), padding.PKCS1v15(), hashes.SHA256())
            if request.body:
                corpo = request.body if isinstance(request.body, bytes) else request.body.encode()
                esperado = base64.b64encode(hashlib.sha256(corpo).digest()).decode()
                self.assertEqual(request.headers["x-content-sha256"], esperado)
            resposta = requests.Response()
            resposta.status_code = 200
            resposta._content = b'{"messages":[{"id":123}]}' if request.method == "POST" else b'{}'
            return resposta

        with patch.object(sessao, "send", side_effect=responder):
            cliente = ClienteFila("https://cell-1.queue.messaging.us-phoenix-1.oci.oraclecloud.com",
                                  "ocid1.queue.oc1.phx.teste", sessao)
            cliente.publicar(PAYLOAD)
            cliente.receber(30, 20)
            cliente.atualizar_visibilidade("abc+/=", 5)
            cliente.excluir("abc+/=")
        self.assertEqual([r.method for r, _ in capturadas], ["POST", "GET", "PUT", "DELETE"])
        envelope = json.loads(capturadas[0][0].body)
        self.assertEqual(json.loads(envelope["messages"][0]["content"]), PAYLOAD)
        consulta = parse_qs(urlsplit(capturadas[1][0].url).query)
        self.assertEqual(consulta["limit"], ["1"])
        self.assertEqual(consulta["visibilityInSeconds"], ["30"])
        self.assertGreater(capturadas[1][1]["timeout"][1], 20)
        self.assertTrue(capturadas[3][0].url.endswith("abc%2B%2F%3D"))
        self.assertEqual(json.loads(capturadas[2][0].body), {"visibilityInSeconds": 5})
        sessao.close()


if __name__ == "__main__":
    unittest.main()
