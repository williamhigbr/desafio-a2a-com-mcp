"""Host MCP do agente (Fase 6): cliente MCP puro, sem A2A ainda.

Roda sozinho e faz o ciclo inteiro contra o servidor MCP: descobre as tools,
lê a política, chama reservar_sala e, se vier input_required, pergunta a escolha
no terminal e faz o retry com id novo, inputResponses e o requestState ecoado.

    uv run --project agente python agente/host_mcp.py \
        sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty
"""
import asyncio
import os
import secrets
import sys

from mcp import Client
from mcp.types import ElicitResult, Implementation, InputRequiredResult, PaginatedRequestParams

MCP_URL = os.environ.get("MCP_URL", "http://localhost:7301/mcp")


def novo_traceparent(trace_id: str | None = None) -> str:
    """W3C trace context: trace-id fixo por fluxo, span-id novo a cada salto."""
    return f"00-{trace_id or secrets.token_hex(16)}-{secrets.token_hex(8)}-01"


async def _elicitation_sobe_para_o_a2a(ctx, params):
    # Existe só para o SDK DECLARAR a capability de elicitation. Quem responde é o
    # usuário (aqui, o terminal; na Fase 8, o cliente A2A via pausa da Task).
    # Se isto for chamado, o SDK tentou responder sozinho: a ponte vazou.
    raise RuntimeError("elicitation deve subir para o usuario, nao ser respondida pelo SDK")


def criar_cliente() -> Client:
    return Client(
        MCP_URL,
        mode="2026-07-28",  # versão fixada: sem probe, sem sessão
        elicitation_callback=_elicitation_sobe_para_o_a2a,
        client_info=Implementation(name="agente-central-de-salas", version="1.0.0"),
    )


async def reservar(args: dict[str, str]) -> None:
    traceparent = novo_traceparent()
    meta = {"traceparent": traceparent}
    print(f"traceparent: {traceparent}", file=sys.stderr)

    async with criar_cliente() as c:
        # Descoberta em runtime, não lista fixa.
        tools = await c.session.list_tools(params=PaginatedRequestParams(_meta=meta))
        nomes = {t.name for t in tools.tools}
        print(f"tools descobertas: {sorted(nomes)}", file=sys.stderr)
        if "reservar_sala" not in nomes:
            sys.exit("o servidor nao oferece reservar_sala")

        # Resource é escolha da aplicação: o host lê a política e extrai a versão.
        pol = await c.session.read_resource("politica://uso", meta=meta)
        versao = pol.contents[0].text.splitlines()[0].split(":", 1)[1].strip()
        print(f"politica: {versao}", file=sys.stderr)

        # allow_input_required=True: o host enxerga o input_required cru, em vez de o SDK fechar o ciclo.
        r = await c.session.call_tool("reservar_sala", args, meta=meta, allow_input_required=True)

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
            # Retry: request novo (o SDK gera id novo), mesma chave, requestState ecoado sem tocar.
            r = await c.session.call_tool(
                "reservar_sala", args, meta=meta,
                input_responses={chave: resposta},
                request_state=r.request_state, allow_input_required=True,
            )

        if r.is_error:
            print("erro: " + " ".join(getattr(b, "text", "") for b in r.content))
        else:
            print(r.structured_content)


def parse_args(argv: list[str]) -> dict[str, str]:
    args = dict(a.split("=", 1) for a in argv)
    faltando = {"sala", "inicio", "fim", "responsavel"} - args.keys()
    if faltando:
        sys.exit(f"faltam argumentos: {sorted(faltando)}\n{__doc__}")
    return args


if __name__ == "__main__":
    asyncio.run(reservar(parse_args(sys.argv[1:])))
