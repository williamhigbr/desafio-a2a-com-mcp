"""Agente A2A da Central de Salas: servidor A2A por fora, host MCP por dentro.

    uv run --project agente python agente/agente.py

Variáveis de ambiente (todas opcionais):
    AGENTE_PORT      porta HTTP do agente (padrão 7300)
    AGENTE_URL       URL pública do endpoint A2A no card (padrão http://localhost:7300/a2a)
    MCP_URL          endpoint do servidor MCP (padrão http://localhost:7301/mcp)
"""
import os
from contextlib import asynccontextmanager

import uvicorn
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import DefaultServerCallContextBuilder, create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, AgentProvider, AgentSkill
from starlette.applications import Starlette

from executor import FORMATO_PEDIDO, ExecutorDeReserva
from host_mcp import HostMCP

PORTA = int(os.environ.get("AGENTE_PORT", 7300))
URL_PUBLICA = os.environ.get("AGENTE_URL", f"http://localhost:{PORTA}/a2a")

# Card v1.0: a grafia (supportedInterfaces, protocolBinding, protocolVersion) é garantida pelos tipos protobuf.
card = AgentCard(
    name="Central de Salas",
    description="Reserva salas de reuniao da Hill Valley Tech.",
    version="1.0.0",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    supported_interfaces=[AgentInterface(url=URL_PUBLICA, protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"],
    default_output_modes=["text/plain"],
    skills=[AgentSkill(
        id="reservar-sala",
        name="Reservar sala",
        description="Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
        tags=["salas", "agenda"],
        input_modes=["text/plain"],
        output_modes=["text/plain"],
        examples=["reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 "
                  "fim=2026-11-03T15:00:00-03:00 responsavel=Marty"],
    )],
)
assert FORMATO_PEDIDO.startswith("reservar sala=")  # o exemplo do card segue o formato do parser


class AssumeV1QuandoAusente(DefaultServerCallContextBuilder):
    """O validador não envia A2A-Version, e pela spec header ausente significa 0.3, que o SDK
    recusa com -32009. Este agente só fala 1.0, que é o que o card anuncia, então header ausente
    é tratado como 1.0. Um header presente com outra versão continua sendo validado pelo SDK.
    """

    def build(self, request):
        ctx = super().build(request)
        ctx.state["headers"].setdefault("a2a-version", "1.0")  # Starlette entrega as chaves em minúsculas
        return ctx


host = HostMCP()


@asynccontextmanager
async def lifespan(_app):
    # Um Client MCP vivo durante toda a vida do processo: reuso de conexão, não sessão de protocolo.
    await host.abrir()
    try:
        yield
    finally:
        await host.fechar()


handler = DefaultRequestHandler(
    agent_executor=ExecutorDeReserva(host),
    task_store=InMemoryTaskStore(),
    agent_card=card,
)

app = Starlette(
    routes=create_agent_card_routes(card)
    + create_jsonrpc_routes(handler, "/a2a", context_builder=AssumeV1QuandoAusente()),
    lifespan=lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=PORTA)
