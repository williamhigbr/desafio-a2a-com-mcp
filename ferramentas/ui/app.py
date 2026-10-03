"""Explorador da Ponte: harness de teste, NÃO faz parte da entrega.

Aba A2A: fala só com o agente (card, SendMessage, GetTask). Nunca vê o requestState.
Aba MCP direto: outro cliente, independente do agente, para estudar o servidor MCP.
Aba Wire: todo request/response exatamente como foi para a rede.

    uv run --project ferramentas/ui streamlit run ferramentas/ui/app.py
"""
import re
import secrets

import httpx
import streamlit as st

st.set_page_config(page_title="A Ponte - explorador", layout="wide")
AGENTE = st.sidebar.text_input("Agente", "http://localhost:7300")
MCP = st.sidebar.text_input("Servidor MCP", "http://localhost:7301/mcp")

# Streamlit reexecuta o script a cada clique: tudo que precisa sobreviver vai para session_state.
ss = st.session_state
ss.setdefault("trace_id", secrets.token_hex(16))
ss.setdefault("tarefas", {})     # task_id -> última Task vista
ss.setdefault("wire", [])        # [(origem, request, response)]
ss.setdefault("mrtr", None)      # último input_required visto na aba MCP
st.sidebar.code(ss.trace_id, language=None)
st.sidebar.caption("trace-id desta sessão: procure no stderr do servidor MCP")


def traceparent() -> str:
    return f"00-{ss.trace_id}-{secrets.token_hex(8)}-01"   # trace-id fixo, span-id novo


def _json(r: httpx.Response) -> dict:
    try:
        return r.json()
    except ValueError:
        return {"_bruto": r.text}


def a2a(metodo: str, params: dict) -> dict:
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": params}
    headers = {"Content-Type": "application/json", "traceparent": traceparent()}
    resposta = _json(httpx.post(f"{AGENTE}/a2a", json=corpo, headers=headers, timeout=30))
    ss.wire.append(("A2A", {"headers": headers, "body": corpo}, resposta))
    return resposta


def task_de(resposta: dict) -> dict:
    resultado = resposta.get("result") or {}
    return resultado.get("task") or resultado


def enviar(texto: str, task_id: str | None = None) -> None:
    msg = {"messageId": f"msg-{secrets.token_hex(6)}", "role": "ROLE_USER", "parts": [{"text": texto}]}
    if task_id:
        msg["taskId"] = task_id
    resposta = a2a("SendMessage", {"message": msg})
    if "error" in resposta:
        ss["ultimo_erro"] = resposta["error"]
        return
    t = task_de(resposta)
    ss.tarefas[t["id"]] = t


def mcp(metodo: str, params: dict, nome: str | None = None, elicitation: bool = True) -> dict:
    meta = {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}}} if elicitation else {},
            "traceparent": traceparent()}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": "2026-07-28", "Mcp-Method": metodo}
    if nome:
        headers["Mcp-Name"] = nome
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": {**params, "_meta": meta}}
    r = httpx.post(MCP, json=corpo, headers=headers, timeout=30)
    resposta = {"http": r.status_code, **_json(r)}
    ss.wire.append(("MCP", {"headers": headers, "body": corpo}, resposta))
    return resposta


aba_a2a, aba_mcp, aba_wire = st.tabs(["A2A (via agente)", "MCP direto", "Wire"])

with aba_a2a:
    if st.button("GET agent-card"):
        st.json(httpx.get(f"{AGENTE}/.well-known/agent-card.json", timeout=10).json())
    with st.form("pedido"):
        c = st.columns(4)
        sala = c[0].text_input("sala", "sala-garagem")
        inicio = c[1].text_input("inicio", "2026-11-03T14:00:00-03:00")
        fim = c[2].text_input("fim", "2026-11-03T15:00:00-03:00")
        resp = c[3].text_input("responsavel", "Marty")
        if st.form_submit_button("SendMessage (Task nova)"):
            enviar(f"reservar sala={sala} inicio={inicio} fim={fim} responsavel={resp}")
    if erro := ss.pop("ultimo_erro", None):
        st.error(f"erro JSON-RPC: {erro}")

    for tid, t in reversed(list(ss.tarefas.items())):
        estado = t["status"]["state"]
        texto = " ".join(p.get("text", "") for p in (t["status"].get("message") or {}).get("parts", []))
        with st.expander(f"{estado}  |  {tid}", expanded=estado == "TASK_STATE_INPUT_REQUIRED"):
            st.write(texto)
            if estado == "TASK_STATE_INPUT_REQUIRED" and texto.startswith("alternativas: "):
                opcoes = texto.removeprefix("alternativas: ").split(", ")
                cols = st.columns(len(opcoes) + 2)
                for i, o in enumerate(opcoes):
                    if cols[i].button(o, key=f"{tid}-{o}"):
                        enviar(f"escolha={o}", tid); st.rerun()
                if cols[-2].button("recusar", key=f"{tid}-recusar"):
                    enviar("escolha=recusar", tid); st.rerun()
                if cols[-1].button("fora do enum", key=f"{tid}-fora"):
                    enviar("escolha=sala-aquario", tid); st.rerun()
            b = st.columns(2)
            if b[0].button("GetTask", key=f"{tid}-get"):
                ss.tarefas[tid] = task_de(a2a("GetTask", {"id": tid})); st.rerun()
            if b[1].button("SendMessage nesta Task", key=f"{tid}-extra"):
                enviar("escolha=sala-mirante", tid); st.rerun()   # em Task terminal: deve dar erro
            st.json(t, expanded=False)

with aba_mcp:
    st.caption("Cliente MCP independente. Não passa pelo agente.")
    elic = st.checkbox("declarar elicitation form", value=True)
    c = st.columns(3)
    if c[0].button("tools/list"):
        st.json(mcp("tools/list", {}, elicitation=elic))
    if c[1].button("resources/read politica://uso"):
        st.json(mcp("resources/read", {"uri": "politica://uso"}, "politica://uso", elicitation=elic))
    if c[2].button("reservar_sala em conflito (garagem 14h-15h)"):
        args = {"sala": "sala-garagem", "inicio": "2026-11-03T14:00:00-03:00",
                "fim": "2026-11-03T15:00:00-03:00", "responsavel": "Marty"}
        r = mcp("tools/call", {"name": "reservar_sala", "arguments": args}, "reservar_sala", elicitation=elic)
        if (r.get("result") or {}).get("resultType") == "input_required":
            ss.mrtr = {"args": args, "chave": next(iter(r["result"]["inputRequests"])),
                       "estado": r["result"]["requestState"]}
        st.json(r)
    if ss.mrtr:
        st.subheader("Retry (o cliente guarda o estado; o servidor pode até reiniciar)")
        adulterar = st.checkbox("trocar 1 caractere do requestState")
        escolha = st.text_input("sala escolhida (ou vazio para decline)", "sala-fusca")
        if st.button("tools/call retry (id novo)"):
            estado = ss.mrtr["estado"]
            if adulterar:
                estado = estado[:40] + ("B" if estado[40] == "A" else "A") + estado[41:]
            resposta = {"action": "accept", "content": {"sala": escolha}} if escolha else {"action": "decline"}
            st.json(mcp("tools/call", {"name": "reservar_sala", "arguments": ss.mrtr["args"],
                                       "inputResponses": {ss.mrtr["chave"]: resposta},
                                       "requestState": estado}, "reservar_sala"))

with aba_wire:
    # O token do agente nunca passa pela UI, então procuramos pelo FORMATO do requestState do SDK
    # ("v1." + base64url longo) em tudo que o agente devolveu.
    vazou = any(re.search(r"v1\.[A-Za-z0-9_-]{40,}", str(resp)) for origem, _, resp in ss.wire if origem == "A2A")
    (st.error if vazou else st.success)(f"algo com cara de requestState em respostas A2A: {'SIM' if vazou else 'não'}")
    for origem, req, resp in reversed(ss.wire):
        with st.expander(f"{origem}  {req['body']['method']}  id={req['body']['id']}"):
            st.json({"request": req, "response": resp})
