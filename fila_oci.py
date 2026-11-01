"""Configuracao e chamadas HTTP assinadas para a API REST da OCI Queue."""

import configparser
import getpass
import json
import os
from pathlib import Path
from urllib.parse import quote, urlsplit

import oci
import requests

BASE = Path(__file__).resolve().parent


class ErroFila(RuntimeError):
    """Erro de comunicacao com a fila, sem registrar credenciais ou receipts."""

    def __init__(self, mensagem, status=None):
        super().__init__(mensagem)
        self.status = status


def carregar_configuracao():
    caminho = BASE / "config.ini"
    config = configparser.ConfigParser(interpolation=None)
    if not config.read(caminho, encoding="utf-8-sig"):
        raise ValueError("Copie config.ini.example para config.ini e preencha o OCID.")
    ocid = config.get("fila", "ocid").strip()
    if not ocid.startswith("ocid1.queue.") or ocid.endswith("-dlq"):
        raise ValueError("Informe o OCID da fila principal, sem -dlq, em config.ini.")
    endpoint = urlsplit(config.get("fila", "endpoint").strip())
    if (endpoint.scheme != "https" or not endpoint.hostname
            or endpoint.username or endpoint.password
            or endpoint.path not in ("", "/") or endpoint.query or endpoint.fragment):
        raise ValueError("Informe somente o endpoint HTTPS de mensagens da sua fila.")
    limites = {"visibilidade_segundos": (1, 43200),
               "espera_segundos": (0, 30), "nova_tentativa_segundos": (1, 43200)}
    for nome, (minimo, maximo) in limites.items():
        valor = config.getint("consumidor", nome)
        if not minimo <= valor <= maximo:
            raise ValueError(f"{nome} deve estar entre {minimo} e {maximo}.")
    return config


def criar_cliente(config):
    caminho = Path(os.path.expandvars(config.get("oci", "arquivo"))).expanduser()
    credenciais = oci.config.from_file(str(caminho), config.get("oci", "perfil"))
    oci.config.validate_config(credenciais)
    senha_chave = credenciais.get("pass_phrase") or None
    if "ENCRYPTED" in Path(credenciais["key_file"]).expanduser().read_text():
        senha_chave = senha_chave or getpass.getpass("Senha da chave privada: ")
    assinador = oci.signer.Signer(
        tenancy=credenciais["tenancy"],
        user=credenciais["user"],
        fingerprint=credenciais["fingerprint"],
        private_key_file_location=credenciais["key_file"],
        pass_phrase=senha_chave,
    )
    sessao = requests.Session()
    sessao.auth = assinador
    sessao.headers.update({"Accept": "application/json"})
    return ClienteFila(config.get("fila", "endpoint"), config.get("fila", "ocid"), sessao)


class ClienteFila:
    def __init__(self, endpoint, ocid, sessao):
        self.url = f"{endpoint.strip().rstrip('/')}/20210201/queues/{quote(ocid.strip(), safe='')}/messages"
        self.sessao = sessao

    def requisitar(self, metodo, sufixo="", espera=0, **argumentos):
        # O timeout HTTP precisa ser maior que a espera do long polling.
        try:
            resposta = self.sessao.request(
                metodo, self.url + sufixo, timeout=(5, max(15, espera + 10)),
                allow_redirects=False, **argumentos,
            )
        except requests.RequestException as erro:
            raise ErroFila(f"Falha de rede ({type(erro).__name__}).") from erro
        try:
            if not 200 <= resposta.status_code < 300:
                try:
                    codigo = resposta.json().get("code", "ErroOCI")
                except (ValueError, AttributeError):
                    codigo = "RespostaInesperada"
                request_id = resposta.headers.get("opc-request-id", "indisponivel")
                raise ErroFila(
                    f"OCI HTTP {resposta.status_code}: {codigo}; request-id={request_id}",
                    status=resposta.status_code,
                )
            return resposta.json() if resposta.content else {}
        except ValueError as erro:
            raise ErroFila("A OCI retornou uma resposta JSON invalida.") from erro
        finally:
            resposta.close()

    def publicar(self, payload):
        # content e uma STRING JSON; o envelope externo pertence a API da OCI.
        corpo = {"messages": [{"content": json.dumps(payload, ensure_ascii=False)}]}
        resultado = self.requisitar("POST", json=corpo)
        if not resultado.get("messages"):
            raise ErroFila("A publicacao nao retornou a confirmacao da mensagem.")
        return resultado["messages"][0]

    def receber(self, visibilidade=30, espera=20):
        resultado = self.requisitar("GET", espera=espera, params={
            "limit": 1, "visibilityInSeconds": visibilidade, "timeoutInSeconds": espera,
        })
        return resultado.get("messages", [])

    def atualizar_visibilidade(self, receipt, segundos):
        return self.requisitar("PUT", "/" + quote(receipt, safe=""),
                               json={"visibilityInSeconds": segundos})

    def excluir(self, receipt):
        self.requisitar("DELETE", "/" + quote(receipt, safe=""))

    def fechar(self):
        self.sessao.close()
