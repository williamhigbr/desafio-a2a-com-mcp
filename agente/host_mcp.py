"""Host MCP do agente: cria o cliente MCP, descobre as tools, lê a política e chama reservar_sala.

Usado pelo executor A2A (Fase 7/8) e também executável sozinho, como cliente MCP puro
que responde a elicitation pelo terminal (Fase 6):

    uv run --project agente python agente/host_mcp.py \
        sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty
"""
import asyncio
import os
import secrets
import sys
from contextlib import AsyncExitStack

from mcp import Client
from mcp.types import (CallToolResult, ElicitResult, Implementation, InputRequiredResult,
                       PaginatedRequestParams)

MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")


def novo_traceparent(trace_id: str | None = None) -> str:
    """W3C trace context: trace-id fixo por fluxo, span-id novo a cada salto."""
    return f"00-{trace_id or secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


async def _elicitation_sobe_para_o_a2a(ctx, params):
    # Existe só para o SDK DECLARAR a capability de elicitation (Decisão 6.2: o SDK declara
    # form + url). Quem responde é o usuário, via pausa da Task. Se isto for chamado, o SDK
    # tentou responder sozinho: a ponte vazou.
    raise RuntimeError("elicitation deve subir para o usuario, nao ser respondida pelo SDK")


class HostMCP:
    """Mantém um Client MCP vivo (reuso de conexão, não sessão de protocolo).

    Cada request leva no _meta a versão, as capabilities e o traceparent: nada é inferido
    de chamadas anteriores.
    """

    def __init__(self, url: str = MCP_URL):
        self.url = url
        self._pilha: AsyncExitStack | None = None
        self._cliente = None

    async def abrir(self) -> None:
        self._pilha = AsyncExitStack()
        self._cliente = await self._pilha.enter_async_context(Client(
            self.url,
            mode="2026-07-28",  # versão fixada: sem probe, sem sessão
            elicitation_callback=_elicitation_sobe_para_o_a2a,
            client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
        ))

    async def fechar(self) -> None:
        if self._pilha:
            await self._pilha.aclose()
        self._pilha = self._cliente = None

    async def __aenter__(self) -> "HostMCP":
        await self.abrir()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.fechar()

    @property
    def _sessao(self):
        if self._cliente is None:
            raise RuntimeError("HostMCP nao foi aberto")
        return self._cliente.session

    async def descobrir(self, trace_id: str) -> str:
        """tools/list (descoberta em runtime) + resources/read da política. Devolve a versão."""
        meta = {"traceparent": novo_traceparent(trace_id)}
        tools = await self._sessao.list_tools(params=PaginatedRequestParams(_meta=meta))
        nomes = {t.name for t in tools.tools}
        print(f"[host] tools descobertas: {sorted(nomes)}", file=sys.stderr, flush=True)
        if "reservar_sala" not in nomes:
            raise RuntimeError("o servidor MCP nao oferece reservar_sala")
        # Resource é escolha da aplicação: o host lê a política e extrai a versão da 1a linha.
        pol = await self._sessao.read_resource("politica://uso", meta={"traceparent": novo_traceparent(trace_id)})
        return pol.contents[0].text.splitlines()[0].split(":", 1)[1].strip()

    async def reservar(self, args: dict, trace_id: str, *, input_responses: dict | None = None,
                       request_state: str | None = None) -> CallToolResult | InputRequiredResult:
        """tools/call reservar_sala. Cada chamada é um request novo, com id novo gerado pelo SDK.

        allow_input_required=True: o host enxerga o input_required cru, em vez de o SDK fechar o
        ciclo sozinho. No retry, request_state vai ecoado sem modificação (é opaco).
        """
        return await self._sessao.call_tool(
            "reservar_sala", args, meta={"traceparent": novo_traceparent(trace_id)},
            input_responses=input_responses, request_state=request_state, allow_input_required=True,
        )


def texto_de(resultado: CallToolResult) -> str:
    return " ".join(getattr(b, "text", "") for b in resultado.content).strip()


# --- CLI da Fase 6: cliente MCP puro, elicitation respondida no terminal ---

async def _cli(args: dict[str, str]) -> None:
    trace_id = secrets.token_hex(16)
    print(f"trace-id: {trace_id}", file=sys.stderr)
    async with HostMCP() as host:
        print(f"politica: {await host.descobrir(trace_id)}", file=sys.stderr)
        r = await host.reservar(args, trace_id)
        while isinstance(r, InputRequiredResult):
            chave, pedido = next(iter(r.input_requests.items()))
            prop = pedido.params.requested_schema["properties"]["sala"]
            opcoes = prop.get("enum") or [prop["const"]]
            print(pedido.params.message)
            print("alternativas: " + ", ".join(opcoes))
            escolhida = input("escolha (id da sala ou 'recusar'): ").strip()
            if escolhida == "recusar":
                resposta = ElicitResult(action="decline")
            elif escolhida in opcoes:
                resposta = ElicitResult(action="accept", content={"sala": escolhida})
            else:
                print("escolha fora das alternativas")
                continue  # pergunta de novo sem chamar o servidor
            r = await host.reservar(args, trace_id, input_responses={chave: resposta},
                                    request_state=r.request_state)
        print("erro: " + texto_de(r) if r.is_error else r.structured_content)


def _parse_args(argv: list[str]) -> dict[str, str]:
    args = dict(a.split("=", 1) for a in argv)
    faltando = {"sala", "inicio", "fim", "responsavel"} - args.keys()
    if faltando:
        sys.exit(f"faltam argumentos: {sorted(faltando)}\n{__doc__}")
    return args


if __name__ == "__main__":
    asyncio.run(_cli(_parse_args(sys.argv[1:])))
