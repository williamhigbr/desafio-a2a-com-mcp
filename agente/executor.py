"""AgentExecutor do agente: traduz A2A (Task) para MCP (tools/call) e de volta.

O SDK A2A guarda a Task (InMemoryTaskStore). Este módulo guarda só o que é privado da
ponte (Pausa), fora da Task, para que o requestState nunca chegue ao cliente A2A.
"""
import json
import re
import secrets
import sys
from dataclasses import dataclass

from a2a.helpers import get_message_text, new_task_from_user_message, new_text_part
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import TaskState
from mcp.types import InputRequiredResult

from host_mcp import HostMCP, texto_de

# Formatos fixos do enunciado. É protocolo do agente (como ler o pedido), não domínio.
PEDIDO = re.compile(r"^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$")
ESCOLHA = re.compile(r"^escolha=(\S+)$")
FORMATO_PEDIDO = "reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>"
TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")

# Campos da reserva que vão para o artifact (o mesmo JSON do bloco de contratos do enunciado).
CAMPOS_RESERVA = ("reserva", "sala", "inicio", "fim", "responsavel")


@dataclass
class Pausa:
    """Estado privado da ponte. NUNCA entra na Task: vive num dict à parte, chaveado pelo task_id."""
    request_state: str
    chave: str
    opcoes: list[str]
    argumentos: dict
    trace_id: str
    politica: str


def parse_pedido(texto: str) -> dict | None:
    m = PEDIDO.match(texto.strip())
    if not m:
        return None
    sala, inicio, fim, responsavel = m.groups()
    return {"sala": sala, "inicio": inicio, "fim": fim, "responsavel": responsavel.strip()}


def parse_escolha(texto: str) -> str | None:
    m = ESCOLHA.match(texto.strip())
    return m.group(1) if m else None


def extrair_trace_id(traceparent: str | None) -> str | None:
    """trace-id do header W3C traceparent; None se ausente, malformado ou todo zero."""
    m = TRACEPARENT.match((traceparent or "").strip().lower())
    if not m or set(m.group(1)) == {"0"}:
        return None
    return m.group(1)


def linha_alternativas(opcoes: list[str]) -> str:
    """Exatamente esta linha, sem prefixo nem saudação (verificações 28 e 36)."""
    return "alternativas: " + ", ".join(opcoes)


def log(task_id: str, estado: str, detalhe: str = "") -> None:
    """Transições no stderr: evidência de SUBMITTED e WORKING, que a resposta bloqueante não mostra."""
    print(json.dumps({"task": task_id, "estado": estado, "detalhe": detalhe or None}),
          file=sys.stderr, flush=True)


class ExecutorDeReserva(AgentExecutor):
    def __init__(self, host: HostMCP):
        self.host = host
        self.pausas: dict[str, Pausa] = {}

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        headers = (context.call_context.state.get("headers") or {}) if context.call_context else {}
        traceparent = headers.get("traceparent")
        task = context.current_task
        if task is None:  # SendMessage sem taskId: Task nova, nasce SUBMITTED
            task = new_task_from_user_message(context.message)
            await event_queue.enqueue_event(task)
            log(task.id, "TASK_STATE_SUBMITTED")
        up = TaskUpdater(event_queue, task.id, task.context_id)
        texto = get_message_text(context.message)
        try:
            if task.id in self.pausas:
                await self.continuar(up, task.id, texto, traceparent)
            else:
                await self.iniciar(up, task.id, texto, traceparent)
        except Exception as e:  # sem isto, o framework marca a Task como erro genérico
            self.pausas.pop(task.id, None)
            await self.falhar(up, task.id, f"Erro interno do agente: {e}")

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        raise NotImplementedError("CancelTask esta fora de escopo")

    async def iniciar(self, up: TaskUpdater, task_id: str, texto: str, traceparent: str | None) -> None:
        log(task_id, "TASK_STATE_WORKING")
        await up.start_work()
        args = parse_pedido(texto)
        if args is None:
            await self.falhar(up, task_id, f"Pedido em formato invalido. Use: {FORMATO_PEDIDO}")
            return
        trace_id = extrair_trace_id(traceparent) or secrets.token_hex(16)
        politica = await self.host.descobrir(trace_id)  # tools/list + resources/read
        r = await self.host.reservar(args, trace_id)
        await self.tratar(up, task_id, r, args, trace_id, politica)

    async def continuar(self, up: TaskUpdater, task_id: str, texto: str, traceparent: str | None) -> None:
        # Fase 8: retomada da Task pausada (escolha=<id> / escolha=recusar, retry com requestState).
        raise NotImplementedError("retomada da pausa ainda nao implementada (Fase 8)")

    async def tratar(self, up: TaskUpdater, task_id: str, r, args: dict, trace_id: str, politica: str) -> None:
        if isinstance(r, InputRequiredResult):
            # Fase 8: aqui o input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
            await self.falhar(up, task_id, "Sala ocupada: pausa da Task ainda nao implementada (Fase 8)")
            return
        self.pausas.pop(task_id, None)
        if r.is_error:
            await self.falhar(up, task_id, texto_de(r))  # mensagem exata da tool
            return
        sc = r.structured_content or {}
        if sc.get("reservado"):
            reserva = {**{k: sc.get(k) for k in CAMPOS_RESERVA}, "politica": politica}
            await up.add_artifact([new_text_part(json.dumps(reserva, ensure_ascii=False))], name="reserva")
            log(task_id, "TASK_STATE_COMPLETED", reserva["reserva"])
            await up.complete(up.new_agent_message(
                [new_text_part(f"Reserva {reserva['reserva']} confirmada na {reserva['sala']}.")]))
        else:
            log(task_id, "TASK_STATE_CANCELED", sc.get("motivo") or "")
            await up.cancel(up.new_agent_message([new_text_part("Reserva recusada.")]))

    async def falhar(self, up: TaskUpdater, task_id: str, mensagem: str) -> None:
        """FAILED com a mensagem em status.message E no history.

        O SDK só move status.message para o history quando chega o status seguinte. Publicar
        antes um WORKING com o mesmo texto faz a mensagem entrar no history; o FAILED em seguida
        a deixa também em status.message (verificação 35).
        """
        await up.update_status(TaskState.TASK_STATE_WORKING, message=up.new_agent_message([new_text_part(mensagem)]))
        log(task_id, "TASK_STATE_FAILED", mensagem)
        await up.failed(up.new_agent_message([new_text_part(mensagem)]))
