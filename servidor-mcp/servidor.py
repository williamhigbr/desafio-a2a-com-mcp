# servidor-mcp/servidor.py (esqueleto) [verificado: construtor, middleware, run]
import json, os, sys
from pathlib import Path
from pydantic import BaseModel
from mcp.server.mcpserver import MCPServer, RequestStateSecurity
from mcp.server.mcpserver.exceptions import ToolError

from dominio import ErroDeDominio, alternativas, conflitos, validar

DADOS = Path(__file__).resolve().parent.parent / "dados"

# Estado de aplicação (não de protocolo): carregado uma vez, reservas novas entram em memória.
SALAS: dict[str, dict] = {s["id"]: s for s in json.loads((DADOS / "salas.json").read_text(encoding="utf-8"))}
RESERVAS: list[dict] = json.loads((DADOS / "reservas.json").read_text(encoding="utf-8"))
# Primeira linha da política: "versao: 2026-11-01"
POLITICA_VERSAO = (DADOS / "politica-de-uso.md").read_text(encoding="utf-8").splitlines()[0].split(":", 1)[1].strip()

async def log_requests(ctx, call_next):
    # ctx.meta é o _meta do request; o traceparent chega aqui, não em header HTTP
    meta = ctx.meta or {}
    print(json.dumps({"method": ctx.method, "id": ctx.request_id,
                      "traceparent": meta.get("traceparent")}), file=sys.stderr, flush=True)
    return await call_next(ctx)

def _segredo() -> str:
    s = os.environ.get("REQUEST_STATE_SECRET", "")
    try:
        ok = len(bytes.fromhex(s)) >= 32
    except ValueError:
        ok = len(s.encode()) >= 32
    if not ok:
        sys.exit("REQUEST_STATE_SECRET ausente ou com menos de 32 bytes")
    return s

mcp = MCPServer(
    "central-de-salas", version="1.0.0",
    request_state_security=RequestStateSecurity(keys=[_segredo()], ttl=15 * 60),  # 15 min: dentro de 5-30
    middleware=[log_requests],
)

class SalaOut(BaseModel):
    id: str; nome: str; capacidade: int; recursos: list[str]

class ListaDeSalas(BaseModel):
    salas: list[SalaOut]

@mcp.tool()
def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=[SalaOut(**s) for s in SALAS.values()])

class ConflitoOut(BaseModel):
    id: str; inicio: str; fim: str; responsavel: str

class Disponibilidade(BaseModel):
    sala: str; livre: bool; conflitos: list[ConflitoOut]

@mcp.tool()
def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    try:
        i, f = validar(sala, inicio, fim, SALAS)       # regra de domínio
    except ErroDeDominio as e:
        raise ToolError(str(e))                        # tradução para o MCP: isError: true
    em_conflito = conflitos(sala, i, f, RESERVAS)
    return Disponibilidade(
        sala=sala, livre=not em_conflito,
        conflitos=[ConflitoOut(id=r["id"], inicio=r["inicio"], fim=r["fim"], responsavel=r["responsavel"])
                   for r in em_conflito],
    )

class ReservaOut(BaseModel):
    # Todos opcionais: o mesmo modelo serve à reserva criada e à recusa (reservado=False + motivo).
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None

def criar_reserva(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    """Grava em memória com o próximo id sequencial (res-0003, res-0004, ...)."""
    proximo = max(int(r["id"].removeprefix("res-")) for r in RESERVAS) + 1 if RESERVAS else 1
    reserva = {"id": f"res-{proximo:04d}", "sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel}
    RESERVAS.append(reserva)
    return ReservaOut(reserva=reserva["id"], sala=sala, inicio=inicio, fim=fim,
                      responsavel=responsavel, politica=POLITICA_VERSAO)

@mcp.tool()
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    try:
        i, f = validar(sala, inicio, fim, SALAS)
    except ErroDeDominio as e:
        raise ToolError(str(e))
    if not conflitos(sala, i, f, RESERVAS):
        return criar_reserva(sala, inicio, fim, responsavel)
    if not alternativas(sala, i, f, SALAS, RESERVAS):
        raise ToolError("Sem alternativas disponiveis no intervalo")
    # TODO Fase 5: aqui entra o MRTR (input_required com elicitation das alternativas).
    raise ToolError("Conflito com alternativas: MRTR ainda nao implementado")

# Resource: confira no SDK a assinatura exata de @mcp.resource (uri, mime_type)
@mcp.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")

if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=int(os.environ.get("MCP_PORT", 7301)),
            stateless_http=True, json_response=True)