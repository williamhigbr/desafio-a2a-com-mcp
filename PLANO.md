# Plano de execução: A Ponte (A2A por fora, MCP por dentro)

Este plano serve a dois objetivos ao mesmo tempo, e nenhum dos dois é opcional:

1. Entregar o desafio passando nas 36 verificações do validador e no Fluxo do avaliador (passos 1 a 13 do `README.md`).
2. Sair dele sabendo explicar, com evidência de log na mão, por que cada peça funciona.

Por isso cada fase tem três blocos:

- Construir: o que implementar, com exemplos.
- Experimento: algo para quebrar, observar ou medir. Não pule: é aqui que o protocolo deixa de ser teoria.
- Checkpoint: perguntas que você deve saber responder antes de avançar. Anote as respostas num caderno seu (fora do repo ou num arquivo ignorado pelo git). Elas viram, quase prontas, a seção "Decisões técnicas" do README.

> Nota sobre os exemplos de código: os trechos marcados como [verificado] foram testados contra o `mcp==2.2.0` num venv descartável. Os demais são esboços: confira assinaturas no código do SDK (`.venv/lib/python3.*/site-packages/mcp/`) antes de confiar neles. Ler o SDK faz parte do exercício.

---

## 0. Mapa mental antes de qualquer código

```
 cliente A2A (validador)                 AGENTE (porta 7300)                         SERVIDOR MCP (porta 7301)
 ───────────────────────        ─────────────────────────────────────────      ─────────────────────────────
  GET /.well-known/agent-card ─▶ card v1.0
  SendMessage "reservar ..."  ─▶ Task SUBMITTED → WORKING
   header traceparent            │  host MCP (cliente do SDK)
                                 ├─ tools/list ─────────────────────────────▶  descoberta
                                 ├─ resources/read politica://uso ──────────▶  "versao: 2026-11-01"
                                 └─ tools/call reservar_sala (id=A) ────────▶  conflito?
                                                                               ├─ não: complete + structuredContent
                                    ◀──────── input_required + requestState ◀┘  sim: elicitation form + estado selado
                              ◀─ Task INPUT_REQUIRED       ╔═══ A PONTE ═══╗
                                 "alternativas: x, y"      ║ guarda requestState + chave + enum POR TASK
  SendMessage taskId,         ─▶ Task WORKING              ║ nunca devolve requestState ao cliente A2A
   "escolha=x"                   └─ tools/call (id=B≠A) ────╚═══════════════╝─▶  verifica selo, reconstrói, reserva
                                    inputResponses + requestState ecoado
                              ◀─ Task COMPLETED + artifact "reserva"
```

Os dois protocolos não têm sessão. Cada um guarda estado num objeto com nome:

| Camada | Onde vive o estado | Quem guarda | Quem pode ler |
|---|---|---|---|
| A2A | Task (`id`, `status.state`, `history`, `artifacts`) | o agente (servidor A2A) | o cliente A2A, via `GetTask` |
| MCP | `requestState` (MRTR) | o cliente MCP (o agente), sem abrir | só o servidor MCP, que o selou |

A ponte é o ponto em que um `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` do A2A, e o `requestState` fica preso à Task até a resposta chegar.

Checkpoint 0
- O agente faz três papéis ao mesmo tempo. Quais são? (Dica: servidor A2A, host MCP e... quem cria o cliente MCP?)
- Por que o `requestState` não pode ficar guardado no servidor MCP? E por que ele pode (e deve) ficar guardado no agente?

---

## 1. Decisões de stack (já pesquisadas)

Recomendação: Python com `uv`. Os exemplos em `exemplos/wire/` foram capturados de uma implementação em Python. A chave `__main__:escolha_de_sala`, o prefixo `v1.` do `requestState` e os `title` gerados pelo pydantic no `inputSchema` entregam isso. Seguir a mesma stack reduz a distância entre o que você produz e o que o validador espera.

O que foi confirmado no `mcp==2.2.0`, e por que isso importa:

| Necessidade do desafio | Como o SDK Python resolve | Status |
|---|---|---|
| Rejeitar `_meta` sem `protocolVersion`/`clientCapabilities` com `-32602` + HTTP 400 | O próprio transporte faz, antes de chegar na sua tool | [verificado] |
| Header `Mcp-Method` divergente do corpo → `-32020` | O transporte faz | [verificado] |
| MRTR (`input_required`) | `Resolve(fn)` + resolver que devolve `Elicit(msg, Modelo)` | [verificado] |
| Chave do `inputRequests` | `"<modulo>:<nome_do_resolver>"`, ex. `__main__:escolha_de_sala` | [verificado] |
| `requestState` com integridade | `RequestStateSecurity(keys=[segredo], ttl=...)`: AES-256-GCM + HKDF, amarrado a método, tool, digest dos argumentos e expiração | [verificado] |
| Estado adulterado/expirado → `-32602` | `RequestStateBoundary`, mensagem `Invalid or expired requestState` | lido no código |
| Cliente sem elicitation form → `-32021` + `requiredCapabilities` | `_require_capability` em `mcp/server/mcpserver/resolve.py` | lido no código |
| Erro de execução com prefixo | `ToolError("msg")` → `isError: true`, texto `Error executing tool <nome>: msg` | [verificado] |
| Agente enxergar o `input_required` cru | `client.session.call_tool(..., allow_input_required=True)` | [verificado] |
| Retry com id novo | cada `call_tool` do SDK emite id novo | [verificado] |

Duas pegadinhas do lado do cliente, observadas no teste:

1. O cliente do SDK só declara a capability de elicitation se você passar um `elicitation_callback`. E declara `{"elicitation": {"form": {}, "url": {}}}`, não só `form`. Veja `mcp/client/session.py`, em `_build_capabilities`. Decida o que fazer na Fase 6.
2. O cliente pode emitir um `tools/list` implícito, sem o seu `traceparent` (ele revalida o `outputSchema` em `validate_tool_result`). Você vai caçar isso no log na Fase 9.

Sobre o A2A: o README não obriga nem desaconselha SDK, só exige "A2A v1.0, binding JSON-RPC 2.0 sobre HTTP" e versões travadas. Este plano usa o `a2a-sdk[http-server]==1.1.5`, que é o que se usa no dia a dia. Confirmado num agente de teste, com o MCP simulado:

| Necessidade do desafio | Como o `a2a-sdk` resolve | Status |
|---|---|---|
| Card v1.0 (`supportedInterfaces`, `protocolBinding`, `protocolVersion`) | `AgentCard`/`AgentInterface` (protobuf) + `create_agent_card_routes` | [verificado] |
| `SendMessage`/`GetTask` em `/a2a` | `create_jsonrpc_routes(handler, "/a2a")` + `DefaultRequestHandler` | [verificado] |
| Resposta no formato `result.task` | o handler serializa assim (`GetTask` devolve a Task direto em `result`; o validador aceita os dois) | [verificado] |
| `SendMessage` bloqueia até o estado terminal ou interrompido | o handler espera `COMPLETED`, `FAILED`, `CANCELED` ou `INPUT_REQUIRED` | [verificado] |
| Continuação chama `execute()` de novo com a Task | `context.current_task` vem preenchido | [verificado] |
| Task terminal recusa mensagem nova | `-32602` "Task ... is in terminal state" | [verificado] |
| Header `traceparent` da chamada A2A | `context.call_context.state["headers"]["traceparent"]` | [verificado] |
| Mensagem de falha visível | `updater.failed(msg)` põe o texto em `status.message` | [verificado] |

Três fricções do `a2a-sdk` que o teste revelou. Todas viram exercício nas Fases 7 e 9:

1. O validador não envia o header `A2A-Version`. A spec manda interpretar header ausente como `0.3`, e o SDK faz exatamente isso: responde `-32009 VersionNotSupported` a todas as chamadas. O SDK está certo segundo a spec, e o validador não pode ser alterado. A saída limpa é um `ServerCallContextBuilder` que assume `1.0` quando o header falta, uma decisão que precisa ser documentada no README.
2. A mensagem de status corrente fica em `status.message` e só vai para o `history` quando chega o próximo status. Numa Task FAILED, a mensagem da tool fica em `status.message` e não entra no `history`. A verificação 35 lê `status.message` e passa. Mas o critério do README e o passo 10 do avaliador falam em "visível no histórico". Decida como garantir as duas coisas.
3. O SDK instrumenta OpenTelemetry. No teste, o stderr mostrou `ValueError: Token ... was created in a different Context` ao fazer `detach` de contexto entre tarefas assíncronas. É ruído do SDK, mas pesa na sua escolha de propagar o `traceparent` via contexto OTel (Fase 9).

Checkpoint 1
- Abra `mcp/server/request_state.py` e responda: o que exatamente é selado (liste os campos `v`, `iat`, `exp`, `m`, `t`, `a`, `s`, `aud`)? Qual desses campos faz o validador 18 (argumentos adulterados) passar?
- Qual é a diferença entre assinar (HMAC) e cifrar com AEAD? O enunciado exige qual dos dois?

---

## 2. Fase 0: preparação do ambiente

Construir

1. Fork no GitHub e clone do fork. Trabalhe na `main` do fork, como pede o enunciado.
2. O Python da máquina é 3.9, e o validador exige 3.10 ou superior. Use o `uv`, que já está instalado, para ter um 3.12 isolado:
   ```bash
   uv python install 3.12
   ```
3. Estrutura sugerida (sem ORM, sem banco, sem camada de serviço):
   ```
   servidor-mcp/
     pyproject.toml        # mcp==2.2.0 (versões com ==), requires-python >=3.10
     servidor.py           # MCPServer, tools, resource, middleware de log
     dominio.py            # carregar JSON, validar política, conflito, alternativas
   agente/
     pyproject.toml        # mcp==2.2.0, a2a-sdk[http-server]==1.1.5, starlette==..., uvicorn==... (travados)
     agente.py             # card, DefaultRequestHandler, rotas, context builder, uvicorn
     executor.py           # AgentExecutor: a ponte (pausa e retomada)
     host_mcp.py           # cliente MCP: descoberta, política, call/retry
   .env.example            # REQUEST_STATE_SECRET= (vazio!)
   ```
4. Crie os projetos e trave as versões:
   ```bash
   uv init --no-workspace --python 3.12 servidor-mcp
   uv add --project servidor-mcp "mcp==2.2.0"
   uv init --no-workspace --python 3.12 agente
   uv add --project agente "mcp==2.2.0" "a2a-sdk[http-server]==1.1.5"
   uv tree --project agente | grep -E "starlette|uvicorn"   # descubra as versões resolvidas
   uv add --project agente "starlette==<versão>" "uvicorn==<versão>"   # importados direto: trave também
   ```
   Faça commit dos `uv.lock`: eles travam as dependências transitivas.
5. Gere o segredo. Nunca faça commit dele (o `.gitignore` já ignora `.env`):
   ```bash
   echo "REQUEST_STATE_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env
   set -a; source .env; set +a
   ```

Checkpoint 0.1
- `token_hex(32)` gera 64 caracteres. Quantos bytes de aleatoriedade isso tem? O SDK aceita uma string de 32 caracteres, mas uma string hex de 32 caracteres tem só 16 bytes de aleatoriedade. Seu servidor deve validar isso na subida (por exemplo, com `len(bytes.fromhex(s)) >= 32`) e se recusar a subir sem a variável.

---

## 3. Fase 1: ler o contrato antes de escrever código

Construir: nada. Leia `exemplos/wire/01` a `11` e `validador/validar.py` inteiros.

Experimento: monte você mesmo a tabela abaixo, sem consultar a coluna "Fase". Depois compare.

| # | O que o validador cobra | Fase |
|---|---|---|
| 01-02 | `tools/list` com as 3 tools, `inputSchema.type == "object"` | 2 |
| 03 | `listar_salas`: `json.loads(texto) == structuredContent` | 2 |
| 04-05 | `_meta` sem campos obrigatórios → `-32602` + HTTP 400 | 2 (SDK) |
| 06 | tool inexistente recusada | 2 (SDK) |
| 07-08 | resource `politica://uso`; URI inexistente → `-32602` | 2 |
| 09-12 | mensagens exatas: sala, janela, duração, intervalo | 3 |
| 13-14 | conflito → `input_required`, 1 entrada, form, `enum == ["sala-fusca","sala-mirante"]` | 5 |
| 15 | sem elicitation → `-32021` + `requiredCapabilities` + HTTP 400 | 5 |
| 16 | retry aceita e reserva a alternativa | 5 |
| 17 | `requestState` com os 6 últimos caracteres trocados → `-32602` | 5 |
| 18 | retry com outros argumentos não toma efeito | 5 |
| 19 | `decline` → `complete`, sem `isError`, `reservado: false` | 5 |
| 20 | conflito sem alternativa → "Sem alternativas disponiveis no intervalo" | 5 |
| 21-23 | card, `supportedInterfaces[].protocolBinding == "JSONRPC"`, `protocolVersion` 1.0, skill | 7 |
| 24-26 | sala livre → COMPLETED, artifact `reserva` com `politica`, `GetTask` | 7 |
| 27-28 | sala ocupada → INPUT_REQUIRED, `alternativas: sala-fusca, sala-mirante` | 8 |
| 29 | escolha fora do enum → continua INPUT_REQUIRED | 8 |
| 30 | escolha válida → COMPLETED na sala escolhida | 8 |
| 31 | `SendMessage` em Task terminal → erro JSON-RPC | 7 |
| 32 | `escolha=recusar` → CANCELED | 8 |
| 33 | duas Tasks pausadas ao mesmo tempo, cada uma com sua reserva | 8 |
| 34 | `requestState` nunca aparece em resposta A2A | 8 |
| 35 | sala inexistente → FAILED com a mensagem em `status.message` | 7 |
| 36 | mesmo pedido duas vezes → mesma pausa, byte a byte | 8 |

Checkpoint 1.1 (as respostas estão no validador)
- Por que o validador precisa de processos recém-iniciados? Simule de cabeça: qual reserva a verificação 16 cria, e por que isso muda as alternativas da verificação 33?
- Na verificação 35, o validador lê `status.message`, não `history`. O que isso implica para a sua Task FAILED?
- Calcule na mão as alternativas de `sala-garagem` das 14h às 15h: capacidade 12 ou mais, livres, ordenadas por (capacidade, id). O resultado precisa ser `sala-fusca, sala-mirante`.

---

## 4. Fase 2: servidor MCP mínimo (`listar_salas`, resource e log)

Conceitos: stateless com `_meta` por request, tools contra resources, stderr no lugar de logging, Streamable HTTP.

Construir

```python
# servidor-mcp/servidor.py (esqueleto) [verificado: construtor, middleware, run]
import json, os, sys
from pathlib import Path
from pydantic import BaseModel
from mcp.server.mcpserver import MCPServer, RequestStateSecurity

DADOS = Path(__file__).resolve().parent.parent / "dados"

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
    ...

# Resource: confira no SDK a assinatura exata de @mcp.resource (uri, mime_type)
@mcp.resource("politica://uso", mime_type="text/markdown")
def politica_de_uso() -> str:
    return (DADOS / "politica-de-uso.md").read_text(encoding="utf-8")

if __name__ == "__main__":
    mcp.run("streamable-http", host="127.0.0.1", port=int(os.environ.get("MCP_PORT", 7301)),
            stateless_http=True, json_response=True)
```

Detalhes que valem nota:
- Retornar um modelo pydantic faz o SDK gerar o `outputSchema`, o `structuredContent` e o bloco de texto com o mesmo JSON. Compare com `01-tools-list.json`.
- O `middleware` do `MCPServer` só vê requests que passaram pela validação do transporte. Um request rejeitado por falta de `_meta` não chega nele. Decida se isso atende "cada request recebido é registrado". Se não atender, acrescente um log no nível ASGI/uvicorn e justifique no README.

Experimento 2.1: fale MCP "na unha" com `curl` (os headers são os de `exemplos/wire`):

```bash
H=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H 'MCP-Protocol-Version: 2026-07-28')
META='"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28","io.modelcontextprotocol/clientCapabilities":{"elicitation":{"form":{}}},"traceparent":"00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"}'

# a) caminho feliz
curl -s -i localhost:7301/mcp "${H[@]}" -H 'Mcp-Method: tools/list' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":1,\"method\":\"tools/list\",\"params\":{$META}}"

# b) sem clientCapabilities: espere HTTP 400 e -32602
curl -s -i localhost:7301/mcp "${H[@]}" -H 'Mcp-Method: tools/list' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{"_meta":{"io.modelcontextprotocol/protocolVersion":"2026-07-28"}}}'

# c) header mentindo sobre o método: espere -32020
curl -s -i localhost:7301/mcp "${H[@]}" -H 'Mcp-Method: tools/call' \
  -d "{\"jsonrpc\":\"2.0\",\"id\":3,\"method\":\"tools/list\",\"params\":{$META}}"
```

Observe o stderr do servidor nos três casos. Em qual deles a sua linha de log aparece?

Experimento 2.2: MCP Inspector (`npx @modelcontextprotocol/inspector`). Veja se a versão disponível fala a revisão `2026-07-28`. Se falar, confira o `_meta` e o `resultType`. Se não falar, anote isso: é um bom exemplo de ecossistema atrás da spec, e o `curl` resolve.

Checkpoint 2
- Por que não existe `initialize` nesse fluxo? O que substitui a negociação de versão e de capabilities?
- `politica://uso` é resource e `reservar_sala` é tool. Quem decide usar cada um (modelo, aplicação ou usuário)?

---

## 5. Fase 3: regras da política e erros de execução

Construir (em `dominio.py`, funções puras e testáveis):

```python
from datetime import datetime, timedelta, timezone
SP = timezone(timedelta(hours=-3))

class ErroDeDominio(Exception): ...

def validar(sala: str, inicio: str, fim: str, salas: dict) -> tuple[datetime, datetime]:
    if sala not in salas:
        raise ErroDeDominio(f"Sala inexistente: {sala}")
    i, f = datetime.fromisoformat(inicio), datetime.fromisoformat(fim)
    if f <= i:
        raise ErroDeDominio("Intervalo invalido: fim deve ser posterior a inicio")
    i_sp, f_sp = i.astimezone(SP), f.astimezone(SP)
    abre = i_sp.replace(hour=8, minute=0, second=0, microsecond=0)
    fecha = i_sp.replace(hour=20, minute=0, second=0, microsecond=0)
    if i_sp < abre or f_sp > fecha:
        raise ErroDeDominio("Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00")
    if f - i > timedelta(hours=2):
        raise ErroDeDominio("Duracao acima do limite: a politica permite no maximo 2 horas")
    return i, f

def sobrepoe(a_i, a_f, b_i, b_f) -> bool:
    return a_i < b_f and b_i < a_f          # intervalos semiabertos [inicio, fim)

def alternativas(sala, i, f, salas, reservas) -> list[str]:
    cap = salas[sala]["capacidade"]
    livres = [s for s in salas.values()
              if s["id"] != sala and s["capacidade"] >= cap and livre(s["id"], i, f, reservas)]
    return [s["id"] for s in sorted(livres, key=lambda s: (s["capacidade"], s["id"]))][:3]
```

Nas tools, converta `ErroDeDominio` em `ToolError(str(e))`. O SDK devolve `isError: true` com `Error executing tool <nome>: <msg>`, e o validador aceita o prefixo.

Decida e documente a ordem das validações. O validador testa cada erro isolado, mas "10h→9h" também poderia violar outras regras se a ordem fosse outra.

Experimento 3: escreva testes unitários do domínio (pytest, travado no `pyproject`) para as bordas: 08:00 a 10:00 passa, 18:00 a 20:00 passa, 19:00 a 20:01 falha, exatamente 2h passa, 2h01 falha, `fim == inicio` falha, e reservas encostadas (14 a 15 e 15 a 16) não conflitam.

Checkpoint 3
- Por que "sala inexistente" é `isError` (erro de execução) e "tool inexistente" é erro de protocolo? Quem é o público de cada erro: o modelo ou o desenvolvedor do cliente?

---

## 6. Fase 4: `reservar_sala` no caminho feliz

Construir: a reserva cria `res-0003`, `res-0004` e assim por diante, em memória. `structuredContent` usa o `ReservaOut` de `01-tools-list.json` (todos os campos opcionais, `reservado` e `motivo`), e `politica` é a versão lida da primeira linha do arquivo. `consultar_disponibilidade` reusa `validar` e devolve `{sala, livre, conflitos: [...]}`.

Checkpoint 4
- A reserva criada precisa aparecer em `consultar_disponibilidade` logo depois. Onde fica esse estado? Por que isso não fere "nada de sessão"? (Pense em estado de aplicação contra estado de protocolo.)

---

## 7. Fase 5: MRTR, o coração do lado servidor

Conceitos: MRTR como resposta à falta de sessão, elicitation form, negociação de capability por request, handle não é autenticação.

Experimento 5.0: sinta a primeira fricção antes de resolvê-la. Numa tool de teste, chame `await ctx.elicit(...)` direto e invoque via `curl`. Leia o erro: ele diz que o transporte stateless não tem canal de volta. Guarde a mensagem para citar no README. Depois apague essa tool.

Construir: o padrão Resolve/Elicit [verificado, gera exatamente o formato de `03-...input-required.json`]:

```python
from typing import Annotated, Literal
from pydantic import BaseModel, Field, create_model
from mcp.server.mcpserver import Resolve, Elicit, ElicitationResult, AcceptedElicitation
from mcp.server.mcpserver.exceptions import ToolError

def escolha_de_sala(sala: str, inicio: str, fim: str):
    """Resolver: roda ANTES do corpo da tool, em TODA rodada (inclusive no retry)."""
    try:
        i, f = validar(sala, inicio, fim, SALAS)
    except ErroDeDominio as e:
        raise ToolError(str(e))
    if livre(sala, i, f, RESERVAS):
        return None                                   # nada a perguntar
    alts = alternativas(sala, i, f, SALAS, RESERVAS)
    if not alts:
        raise ToolError("Sem alternativas disponiveis no intervalo")
    Escolha = create_model("Escolha", sala=(Literal[tuple(alts)],
                           Field(title="Sala", description="Sala alternativa escolhida")))
    return Elicit("A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa.", Escolha)

@mcp.tool()
def reservar_sala(sala: str, inicio: str, fim: str, responsavel: str,
                  escolha: Annotated[ElicitationResult[BaseModel], Resolve(escolha_de_sala)]) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if isinstance(escolha, AcceptedElicitation):
        destino = sala if escolha.data is None else escolha.data.sala
        return criar_reserva(destino, inicio, fim, responsavel)
    return ReservaOut(reservado=False, motivo="recusado")   # decline ou cancel
```

Por que esse desenho atende cada requisito:

- `resultType: input_required` com uma entrada: o resolver pediu `Elicit` e o SDK agrupa a pergunta num `InputRequiredResult`. Não existe callback.
- `requestedSchema` plano com `enum`: `Literal[("sala-fusca","sala-mirante")]` vira `enum`. Com uma só alternativa, veja se sai `const` ou `enum` (os dois são aceitos).
- `-32021`: o SDK verifica a capability do request corrente no momento em que precisaria perguntar. Se não há conflito, um cliente sem elicitation reserva normalmente. Isso é o certo: a capability só é exigida quando é usada.
- Integridade e expiração: `RequestStateSecurity(keys=[segredo], ttl=900)`.
- Restart: a chave vem do ambiente, não de `os.urandom`. Esse é o padrão `ephemeral()` que o SDK usa se você não passar nada, e quebraria o passo 12 do avaliador.
- Argumentos adulterados (verificação 18): o envelope sela o digest dos `arguments`. Argumentos divergentes fazem o estado ser rejeitado com `-32602`, que é um dos dois caminhos aceitos.

Experimento 5.1 (passo 11 do avaliador). Precisa de `jq` (`brew install jq`):

```bash
H=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' \
   -H 'MCP-Protocol-Version: 2026-07-28' -H 'Mcp-Method: tools/call' -H 'Mcp-Name: reservar_sala')
jq '.request.body' exemplos/wire/03-tools-call-conflito-input-required.json > /tmp/conflito.json
curl -s localhost:7301/mcp "${H[@]}" -d @/tmp/conflito.json | tee /tmp/r.json | jq .
RS=$(jq -r .result.requestState /tmp/r.json); KEY=$(jq -r '.result.inputRequests|keys[0]' /tmp/r.json)

# adultera 1 caractere no meio
RUIM="${RS:0:40}$( [ "${RS:40:1}" = A ] && echo B || echo A )${RS:41}"
jq --arg rs "$RUIM" --arg k "$KEY" '.request.body | .id=99 | .params.requestState=$rs
   | .params.inputResponses={($k):{"action":"accept","content":{"sala":"sala-fusca"}}}' \
   exemplos/wire/04-tools-call-retry.json > /tmp/retry-ruim.json
curl -s localhost:7301/mcp "${H[@]}" -d @/tmp/retry-ruim.json | jq .      # espere -32602
```

Experimento 5.2 (passo 12): monte `/tmp/retry-bom.json` com `$RS` intacto, reinicie o servidor MCP (Ctrl+C e suba de novo com o mesmo `.env`) e envie. A reserva deve ser concluída. Depois repita reiniciando com outro segredo e explique o erro.

Experimento 5.3: expiração. Suba temporariamente com `ttl=5`, espere 6 segundos, faça o retry e observe `-32602` e o motivo real no log (`requestState rejected on tools/call: expired`). Volte para 900.

Experimento 5.4: tente decodificar o `requestState` com base64. Você consegue ler o conteúdo? Explique por que o enunciado diz "o conteúdo pode ser legível, o que não pode é ser adulterável", e por que o AES-GCM do SDK vai além disso.

Experimento 5.5 (passo 13): reenvie `/tmp/conflito.json` com `clientCapabilities: {}` e confirme `-32021`, HTTP 400 e `data.requiredCapabilities`. Depois envie `{"elicitation": {"url": {}}}` e explique o resultado (dica: form mode, não elicitation em geral).

Experimento 5.6: faça o retry reaproveitando o mesmo `id` do request inicial. O que o seu servidor faz? A spec exige id novo. Por que um servidor stateless nem teria como detectar a repetição?

Checkpoint 5
- O resolver roda de novo no retry. Por que isso é seguro? O que acontece se, entre o `input_required` e o retry, outra pessoa reservar a `sala-fusca`? (Leia `_request_digest` em `resolve.py`: a pergunta muda e o SDK pergunta de novo em vez de consumir a resposta velha.)
- Onde está o "estado" entre as duas rodadas? Liste o que está no token e o que é recalculado.
- "Handle não é autenticação": o que impede o cliente A de usar o `requestState` do cliente B? Nesse desafio sem autenticação, nada. Onde o SDK amarraria isso (`bind_principal`)?

---

## 8. Fase 6: o agente como cliente MCP puro (sem A2A ainda)

Conceitos: o host cria clientes, descoberta em runtime, resource como escolha da aplicação, trace context no `_meta`.

Construir um script `agente/host_mcp.py` executável sozinho, que faz o ciclo inteiro e responde a elicitation pelo `input()` do terminal:

```python
# [verificado: Client(mode, elicitation_callback, client_info), list_tools(params=...),
#  call_tool(meta, input_responses, request_state, allow_input_required)]
from mcp import Client
from mcp.types import (Implementation, PaginatedRequestParams, ElicitResult,
                       InputRequiredResult)

async def _elicitation_sobe_para_o_a2a(ctx, params):
    # Existe só para o SDK DECLARAR a capability. Quem responde é o cliente A2A,
    # via pausa da Task. Se isto for chamado, a ponte vazou.
    raise RuntimeError("elicitation deve virar TASK_STATE_INPUT_REQUIRED")

cliente = Client(MCP_URL, mode="2026-07-28",          # versão fixada: sem probe, sem sessão
                 elicitation_callback=_elicitation_sobe_para_o_a2a,
                 client_info=Implementation(name="agente-central-de-salas", version="1.0.0"))

async with cliente as c:
    meta = {"traceparent": traceparent}
    tools = await c.session.list_tools(params=PaginatedRequestParams(_meta=meta))
    assert "reservar_sala" in {t.name for t in tools.tools}          # descoberta, não lista fixa
    pol = await c.session.read_resource("politica://uso", meta=meta)  # confira o tipo do parâmetro uri
    versao = pol.contents[0].text.splitlines()[0].split(":", 1)[1].strip()

    r = await c.session.call_tool("reservar_sala", args, meta=meta, allow_input_required=True)
    if isinstance(r, InputRequiredResult):
        chave, pedido = next(iter(r.input_requests.items()))
        # ... pergunte no terminal, depois:
        r = await c.session.call_tool("reservar_sala", args, meta=meta,
                input_responses={chave: ElicitResult(action="accept", content={"sala": escolhida})},
                request_state=r.request_state, allow_input_required=True)
```

Experimento 6.1: rode o script com o stderr do servidor visível e confira:
- `tools/list` antes do primeiro `tools/call`;
- ids diferentes no par `tools/call` inicial/retry;
- `traceparent` presente em todos os requests;
- `clientCapabilities` como `{"elicitation":{"form":{},"url":{}}}`.

Decisão 6.2, sobre a capability: o enunciado pede a forma `{"elicitation": {"form": {}}}`. O SDK também anuncia `url`, uma capability que o seu agente não implementa. Você tem duas saídas legítimas:
(a) documentar no README como limitação do SDK, citando o trecho de `mcp/client/session.py` (`_build_capabilities`), como o enunciado autoriza;
(b) encontrar um ponto de extensão limpo para declarar só `form`.
Não reescreva o protocolo na mão para contornar. Anote a escolha e o porquê.

Experimento 6.3: remova o `elicitation_callback` e rode de novo. Qual erro volta? Isso é a segunda fricção vista do lado do cliente.

Experimento 6.4: troque `allow_input_required=True` por `False` e restaure um callback que responde sozinho. O ciclo fecha sem nunca perguntar a ninguém. É exatamente o erro que o enunciado descreve ("a Task nunca pausa").

Checkpoint 6
- Por que `mode="2026-07-28"` é mais fiel ao "nada de sessão" do que o modo `auto`?
- Manter o objeto `Client` vivo entre Tasks fere o "nada de sessão"? Por que não?

---

## 9. Fase 7: o agente como servidor A2A

Conceitos: Agent Card e well-known URI, Task com identidade, estado e produto, opacidade.

Construir com o `a2a-sdk` [verificado: todos os trechos abaixo rodaram no agente de teste]

1. Card, com a grafia da v1.0 garantida pelos tipos protobuf. `url` configurável por variável de ambiente, padrão `http://localhost:7300/a2a`:

```python
from a2a.types import (AgentCard, AgentInterface, AgentCapabilities, AgentSkill, AgentProvider)

card = AgentCard(
    name="Central de Salas", description="Reserva salas de reuniao da Hill Valley Tech.", version="1.0.0",
    provider=AgentProvider(organization="Hill Valley Tech", url="https://hillvalley.example"),
    supported_interfaces=[AgentInterface(url=URL_PUBLICA, protocol_binding="JSONRPC", protocol_version="1.0")],
    capabilities=AgentCapabilities(streaming=False, push_notifications=False, extended_agent_card=False),
    default_input_modes=["text/plain"], default_output_modes=["text/plain"],
    skills=[AgentSkill(id="reservar-sala", name="Reservar sala", description="...", tags=["salas", "agenda"],
                       input_modes=["text/plain"], output_modes=["text/plain"], examples=["reservar sala=..."])],
)
```

2. Montagem do app. O `ServerCallContextBuilder` resolve a fricção 1 (header `A2A-Version` ausente):

```python
from starlette.applications import Starlette
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes, create_agent_card_routes, DefaultServerCallContextBuilder
from a2a.server.tasks import InMemoryTaskStore

class AssumeV1QuandoAusente(DefaultServerCallContextBuilder):
    """O validador não envia A2A-Version; pela spec, ausente = 0.3. Este agente só fala 1.0,
    que é o que o card anuncia. Decisão explícita, documentada no README."""
    def build(self, request):
        ctx = super().build(request)
        ctx.state["headers"].setdefault("a2a-version", "1.0")
        return ctx

handler = DefaultRequestHandler(agent_executor=ExecutorDeReserva(host), task_store=InMemoryTaskStore(), agent_card=card)
app = Starlette(routes=create_agent_card_routes(card)
                + create_jsonrpc_routes(handler, "/a2a", context_builder=AssumeV1QuandoAusente()),
                lifespan=...)  # abre/fecha o Client MCP (Fase 6)
```

3. O `AgentExecutor`. O SDK guarda a Task (`InMemoryTaskStore`), e você guarda só o que é privado da ponte:

```python
from dataclasses import dataclass
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.helpers import new_task_from_user_message, new_text_part, get_message_text

@dataclass
class Pausa:                 # NUNCA entra na Task: vive num dict à parte, chaveado pelo task_id
    request_state: str; chave: str; opcoes: list[str]
    argumentos: dict; trace_id: str; politica: str

class ExecutorDeReserva(AgentExecutor):
    def __init__(self, host):
        self.host = host
        self.pausas: dict[str, Pausa] = {}

    async def execute(self, context: RequestContext, event_queue):
        tp = context.call_context.state.get("headers", {}).get("traceparent")
        task = context.current_task
        if task is None:                                    # SendMessage sem taskId: Task nova
            task = new_task_from_user_message(context.message)   # nasce SUBMITTED
            await event_queue.enqueue_event(task)
        up = TaskUpdater(event_queue, task.id, task.context_id)
        texto = get_message_text(context.message)
        if task.id in self.pausas:
            await self.continuar(up, task.id, texto, tp)    # Fase 8
        else:
            await self.iniciar(up, task.id, texto, tp)      # Fase 8

    async def cancel(self, context, event_queue):
        raise NotImplementedError   # CancelTask está fora de escopo
```

Guardar `Pausa` fora da Task torna a verificação 34 (vazamento) um problema de arquitetura resolvido, e não de disciplina: a Task é serializada pelo SDK e você não controla o que entra nela além do que publica.

4. Estados: `new_task_from_user_message` cria em `SUBMITTED`. `up.start_work()` passa para `WORKING` antes de chamar o MCP. Depois vem `up.complete()`, `up.failed()`, `up.cancel()` ou `up.requires_input()`. O `TaskUpdater` recusa qualquer atualização depois de um estado terminal (`RuntimeError`), e o handler recusa `SendMessage` para Task terminal com `-32602`.
5. Registre as transições no stderr do agente (um `print` antes de cada chamada ao `up`). É a sua evidência de que a Task passou por `SUBMITTED` e `WORKING`, porque a resposta bloqueante só mostra o estado final.
6. `isError: true` → `up.failed(up.new_agent_message([new_text_part(texto_da_tool)]))`. Fricção 2: o texto fica em `status.message` (a verificação 35 passa), mas não no `history`. Para atender "visível no histórico", uma saída é publicar antes um `up.update_status(TaskState.TASK_STATE_WORKING, message=msg)` com o mesmo texto: ao chegar o status seguinte, o SDK move essa mensagem para o `history`. Teste e confira com `GetTask`.
7. Artifact `reserva`: `await up.add_artifact([new_text_part(json.dumps({...}))], name="reserva")`, com `reserva`, `sala`, `inicio`, `fim`, `responsavel` e `politica`. O campo `politica` vem da leitura do resource feita pelo agente.
8. Parser do pedido (é protocolo, não domínio):
   `^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$` e `^escolha=(\S+)$`. Formato inválido → `FAILED` com mensagem clara.

Experimento 7.0, a fricção 1 ao vivo: suba o app sem o `context_builder` e mande o corpo de `exemplos/wire/08` por `curl`. Leia o `-32009`, depois leia `a2a/utils/version_validator.py` e explique por que o SDK está certo. Em seguida mande o mesmo `curl` com `-H 'A2A-Version: 1.0'` e veja funcionar. Só então coloque o builder. Pergunta: qual é o risco de assumir `1.0` quando o header falta, e por que ele é aceitável para um agente que só anuncia `1.0` no card?

Experimento 7.1 (passo 3 do avaliador): `curl -s localhost:7300/.well-known/agent-card.json | jq .` e compare, campo a campo, com o `07`.

Experimento 7.2: `SendMessage` com sala livre, depois `GetTask`, depois um novo `SendMessage` referenciando a mesma Task. Confira o `-32602` "in terminal state" e confirme no stderr que o `execute()` nem foi chamado. Quem recusou foi o handler do SDK, antes do seu código.

Experimento 7.3: leia `a2a/server/request_handlers/default_request_handler_v2.py` e encontre o ponto em que o `SendMessage` bloqueante decide responder (procure `INTERRUPTED_TASK_STATES`). É esse trecho que faz a resposta sair exatamente quando a Task pausa.

Checkpoint 7
- Opacidade: olhando só o card e as respostas, o cliente tem como saber que não há LLM? E que existe um servidor MCP atrás? Por que isso é uma propriedade desejável?
- Qual a diferença entre `INPUT_REQUIRED` (interrompido) e `COMPLETED` (terminal) do ponto de vista de quem pode mandar a próxima mensagem?
- O SDK esconde o wire. Sem olhar o código dele, desenhe a máquina de estados que ele implementa: quais transições são permitidas, quem recusa o quê (handler contra `TaskUpdater`) e com qual erro. Depois confirme lendo `active_task.py` e `task_updater.py`.

---

## 10. Fase 8: a ponte

Construir: os dois métodos do executor. É o trecho que deve ser citado no README como "Onde a ponte acontece".

```python
    async def iniciar(self, up, task_id, texto, tp):
        await up.start_work()
        args = parse_pedido(texto)                       # ou up.failed(...) se inválido
        trace_id = extrair_trace_id(tp) or novo_trace_id()
        politica = await self.host.descobrir(trace_id)   # tools/list + resources/read
        r = await self.host.reservar(args, trace_id)
        await self.tratar(up, task_id, r, args, trace_id, politica)

    async def continuar(self, up, task_id, texto, tp):
        p = self.pausas[task_id]
        escolha = parse_escolha(texto)
        if escolha == "recusar":
            resposta = ElicitResult(action="decline")
        elif escolha in p.opcoes:                        # valida contra o enum do SERVIDOR
            resposta = ElicitResult(action="accept", content={"sala": escolha})
        else:                                            # fora do enum: continua pausada, sem chamar MCP
            await up.requires_input(up.new_agent_message([new_text_part(linha_alternativas(p.opcoes))]))
            return
        await up.start_work()
        trace_id = extrair_trace_id(tp) or p.trace_id
        r = await self.host.reservar(p.argumentos, trace_id,
                                     input_responses={p.chave: resposta},
                                     request_state=p.request_state)      # ◀── volta ao servidor, sem tocar
        await self.tratar(up, task_id, r, p.argumentos, trace_id, p.politica)

    async def tratar(self, up, task_id, r, args, trace_id, politica):
        if isinstance(r, InputRequiredResult):                           # ◀── MCP input_required
            chave, pedido = next(iter(r.input_requests.items()))
            prop = pedido.params.requested_schema["properties"]["sala"]
            opcoes = prop.get("enum") or [prop["const"]]                 # protocolo, não domínio
            self.pausas[task_id] = Pausa(r.request_state, chave, opcoes, args, trace_id, politica)
            await up.requires_input(up.new_agent_message([new_text_part(linha_alternativas(opcoes))]))
            return                                                       # ──▶ A2A INPUT_REQUIRED
        self.pausas.pop(task_id, None)
        if r.is_error:
            await up.failed(up.new_agent_message([new_text_part(texto_de(r))])); return
        sc = r.structured_content or {}
        if sc.get("reservado"):
            await up.add_artifact([new_text_part(json.dumps({**campos_da_reserva(sc), "politica": politica}))], name="reserva")
            await up.complete(up.new_agent_message([new_text_part(f"Reserva {sc['reserva']} confirmada na {sc['sala']}.")]))
        else:
            await up.cancel(up.new_agent_message([new_text_part("Reserva recusada.")]))

def linha_alternativas(opcoes):  # exatamente isto, sem prefixo nem saudação (verificações 28 e 36)
    return "alternativas: " + ", ".join(opcoes)
```

Observe o contrato do `AgentExecutor` (docstring em `a2a/server/agent_execution/agent_executor.py`): para `INPUT_REQUIRED`, o executor publica o status e retorna. O framework chama `execute()` de novo quando chega a próxima mensagem com o `taskId`. É o mesmo desenho do MRTR do outro lado: ninguém fica bloqueado esperando o usuário. Os dois protocolos resolveram a ausência de sessão do mesmo jeito.

Tratamento que evita bugs sutis:
- O retry pode voltar `input_required` de novo (a pergunta mudou porque outra reserva ocupou a alternativa). `tratar()` lida com isso de forma genérica, sem regra de domínio no agente.
- `traceparent` da continuação: use o header da nova chamada se vier; se não vier, use o trace-id guardado na `Pausa`. O span-id pode ser novo (`secrets.token_hex(8)`), o trace-id não.
- Determinismo (verificação 36): o texto da pausa não pode conter ids aleatórios, horário ou contadores. Os `messageId` e `timestamp` gerados pelo SDK não entram no texto comparado.
- Concorrência: o SDK garante uma execução por vez por request, mas `self.pausas` é compartilhado entre Tasks. Como a chave é o `task_id`, não há disputa entre Tasks diferentes.

Experimento 8.1 (passos 7 a 9 do avaliador):

```bash
A=(-H 'Content-Type: application/json' -H 'traceparent: 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01')
jq '.request.body' exemplos/wire/08-a2a-send-message.json > /tmp/send.json
TASK=$(curl -s localhost:7300/a2a "${A[@]}" -d @/tmp/send.json | tee /dev/stderr | jq -r .result.task.id)
jq --arg t "$TASK" '.request.body | .params.message.taskId=$t' exemplos/wire/10-a2a-send-message-continuacao.json > /tmp/cont.json
curl -s localhost:7300/a2a "${A[@]}" -d @/tmp/cont.json | jq .result.task.status
curl -s localhost:7300/a2a "${A[@]}" -d "{\"jsonrpc\":\"2.0\",\"id\":9,\"method\":\"GetTask\",\"params\":{\"id\":\"$TASK\"}}" | jq .
```

Experimento 8.2: abra duas Tasks em conflito, continue a segunda antes da primeira e confira no stderr do MCP que cada retry levou o seu `requestState`. Depois introduza de propósito um bug (um `self.ultima_pausa` único no lugar de `self.pausas[task_id]`) e veja a verificação 33 falhar. Desfaça.

Experimento 8.3: `grep` em todas as respostas A2A por `v1.` e pelos primeiros 40 caracteres do `requestState` que aparecem no log do MCP. Nada deve aparecer. Depois, como contraprova, coloque temporariamente o `requestState` no `metadata` de um `update_status` e veja a verificação 34 falhar. É assim que um vazamento acontece com SDK: por um campo que "parecia interno".

Checkpoint 8
- Aponte, com arquivo e linha, o ponto em que `input_required` vira `INPUT_REQUIRED` e o ponto em que o `requestState` volta para o servidor. Esse é o parágrafo do README.
- Se o agente reiniciar com uma Task pausada, o que se perde? E se o servidor MCP reiniciar? Por que a assimetria?

---

## 10b. Ferramenta de exploração (opcional): UI em Streamlit

Quando usar: depois que a Fase 8 funciona pelo `curl`. Antes disso, o `curl` e o stderr ensinam mais, porque obrigam você a ler cada campo. Depois, a UI ajuda a explorar casos de borda, a manter várias Tasks à vista e a demonstrar o fluxo.

Regras para ela não virar problema:

- Interface gráfica está fora do escopo do desafio: não é proibida, mas não é avaliada. Fica em `ferramentas/ui/`, com `pyproject.toml` próprio. Streamlit nunca entra nas dependências de `agente/` ou `servidor-mcp/`, e a seção "Como rodar" do README não depende dela.
- A UI é mais um cliente A2A, como qualquer outro agente da empresa. A aba A2A fala só com `/.well-known/agent-card.json` e `/a2a`. Se ela precisar de algo que o protocolo não entrega, o problema está no agente, não na UI.
- A aba "MCP direto" é outro cliente, independente, para estudar o servidor. Ela não substitui o agente, e nada da aba A2A passa por ela.
- O wire fica sempre visível: cada ação registra o request e o response exatos na aba Wire. Uma UI que só mostra resultados bonitos esconde justamente o que você está estudando.
- As chamadas usam `httpx` puro, e não o cliente do `a2a-sdk`, para que o que você vê seja byte a byte o que o validador envia.

Montagem:

```bash
uv init --no-workspace --python 3.12 ferramentas/ui
uv add --project ferramentas/ui "streamlit==<versão>" "httpx==<versão>"   # trave; testado com 1.64.0 e 0.28.1
uv run --project ferramentas/ui streamlit run ferramentas/ui/app.py
```

`ferramentas/ui/app.py` [verificado: renderiza sem exceção via `streamlit.testing.AppTest`, e o ciclo pausa → fora do enum → escolha → Task terminal recusada foi exercitado contra um agente simulado]:

```python
"""Explorador da Ponte: harness de teste, NÃO faz parte da entrega.

Aba A2A: fala só com o agente (card, SendMessage, GetTask). Nunca vê o requestState.
Aba MCP direto: outro cliente, independente do agente, para estudar o servidor MCP.
Aba Wire: todo request/response exatamente como foi para a rede.
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


def a2a(metodo: str, params: dict) -> dict:
    corpo = {"jsonrpc": "2.0", "id": secrets.token_hex(6), "method": metodo, "params": params}
    headers = {"Content-Type": "application/json", "traceparent": traceparent()}
    resposta = httpx.post(f"{AGENTE}/a2a", json=corpo, headers=headers, timeout=30).json()
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
        st.session_state["ultimo_erro"] = resposta["error"]
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
    resposta = {"http": r.status_code, **r.json()}
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
```

Experimentos com a UI, cada um ligado a um critério:

| Ação na UI | O que observar | Critério ou verificação |
|---|---|---|
| Duas Tasks em conflito; responda a segunda antes da primeira | cada uma termina com a sua reserva; no stderr do MCP, cada retry com o seu `requestState` | 33 |
| "fora do enum" numa Task pausada | continua `INPUT_REQUIRED`, mesma linha `alternativas:`, e nenhum `tools/call` no stderr do MCP | 29 |
| "recusar" | `TASK_STATE_CANCELED`; no MCP, retry com `action: decline` | 32 |
| "SendMessage nesta Task" depois de COMPLETED | erro JSON-RPC na tela e nenhuma transição no stderr do agente | 31 |
| Mesmo pedido de sala ocupada duas vezes | textos de pausa idênticos | 36 |
| Faixa verde na aba Wire | nada parecido com `v1.<token>` nas respostas do agente | 34 |
| Trace-id da barra lateral | `grep` no `mcp.log` encontra todos os requests do agente daquela sessão | trace-id propagado |
| MCP direto: conflito, reiniciar o servidor MCP, retry | a reserva conclui, porque o estado estava no `session_state` da UI e não no servidor | passo 12 do avaliador |
| MCP direto: marcar "trocar 1 caractere" | `-32602` | 17 |
| MCP direto: desmarcar "declarar elicitation form" e reservar em conflito | `-32021`, `http: 400`, `requiredCapabilities` | 15 |
| MCP direto: reserva de sala livre com a capability desmarcada (edite os argumentos) | conclui normalmente: a capability só é exigida quando a elicitation é usada | reflexão da Fase 5 |

Checkpoint 10b
- O `session_state` do Streamlit guarda o `requestState` entre um clique e outro, exatamente como o agente guarda na `Pausa`. Em que sentido a UI, na aba MCP, está fazendo o papel de host MCP? E por que, na aba A2A, ela não pode nem saber que esse estado existe?
- A UI interpreta a linha `alternativas: ...` para montar os botões. Isso é um contrato frágil? O que o A2A ofereceria para tornar essa pergunta estruturada (pense em `DataPart`) e por que o enunciado preferiu texto fixo?

---

## 11. Fase 9: observabilidade, a caça ao `traceparent`

Critérios: o log do MCP mostra `tools/list` antes do primeiro `tools/call`, o mesmo trace-id do validador aparece, e o id do retry é diferente do inicial.

Experimento 9.1: rode o validador salvando a saída e o stderr do MCP em arquivo (o `.gitignore` já ignora `*.log`):

```bash
# terminal 1
uv run --project servidor-mcp python servidor-mcp/servidor.py 2> mcp.log
# terminal 2
uv run --project agente python agente/agente.py
# terminal 3
uv run --python 3.12 python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301 | tee validador.log
TID=$(head -1 validador.log | awk '{print $NF}')
grep "$TID" mcp.log                              # todos os requests do agente nas Tasks com header
grep -n '"method": "tools/' mcp.log | head       # ordem list -> call
```

Experimento 9.2, a caça: procure no `mcp.log` requests do agente sem `traceparent` dentro de uma Task que recebeu o header. No teste de fumaça deste plano apareceu um `tools/list` implícito, emitido pelo próprio cliente do SDK para revalidar o `outputSchema` (`validate_tool_result` em `mcp/client/session.py`). Investigue:
- Ele acontece no seu fluxo? Com que frequência?
- Correção A: o SDK injeta W3C trace context no `_meta` a partir do contexto OpenTelemetry corrente (`inject_trace_context` em `mcp/shared/jsonrpc_dispatcher.py`). Com `opentelemetry.context.attach(extract({"traceparent": tp}))` durante o trabalho da Task, os requests implícitos também herdam o trace-id. Teste e confirme no log. Cuidado com contextvars e tarefas concorrentes: faça `detach` no `finally`. Atenção: o `a2a-sdk` também instrumenta OTel e, no teste, já emitiu `ValueError: Token ... was created in a different Context` no stderr. Se você também fizer `attach`, confirme que os seus tokens não entram nessa confusão: o `attach` e o `detach` precisam acontecer na mesma tarefa asyncio, dentro do `execute()`.
- Correção B: passar `meta` explícito em tudo e evitar o gatilho do request implícito.
- Escolha, prove com `grep` e documente.

Lembrete: spans e exportação de OpenTelemetry estão fora de escopo. Usar `opentelemetry-api` só como propagador de contexto é outra coisa (e ele já vem como dependência do `mcp`).

Checkpoint 9
- No W3C Trace Context, o que é cada campo de `00-<trace-id>-<span-id>-<flags>`? Por que o span-id pode mudar entre agente e servidor, e o trace-id não?

---

## 12. Fase 10: validação completa e ensaio do avaliador

1. Mate tudo, suba do zero e rode o validador. Repita até `resumo: 36 passaram` e exit code 0 (`echo $?`).
2. Percorra os 13 passos do "Fluxo do avaliador" com os scripts das Fases 5 e 8. Salve as saídas: elas são a sua evidência.
3. Determinismo: rode o validador três vezes, reiniciando os processos entre as execuções. As 36 devem passar sempre.
4. Integridade dos arquivos do starter:
   ```bash
   git fetch upstream   # git remote add upstream https://github.com/devfullcycle/desafio-a2a-com-mcp
   git diff --stat upstream/main -- dados/ validador/ exemplos/    # tem de sair vazio
   ```
5. Varredura de segredo antes do push: `git grep -nE '[0-9a-f]{64}'` não pode achar o seu segredo. O `.env` não pode estar no índice.
6. Sem LLM: nenhuma dependência de `openai`, `anthropic` ou afins em `uv tree`.

---

## 13. Fase 11: README e entrega do zero

Substitua o `README.md` pelas quatro seções exigidas:

- Como rodar: comandos exatos a partir de um clone limpo (`uv sync`, geração do segredo sem o valor, subida dos dois processos, validador com `uv run --python 3.12`).
- Onde a ponte acontece: arquivo, função e linha da transição `input_required` → `TASK_STATE_INPUT_REQUIRED`, e do retry com `request_state` e `input_responses`.
- Decisões técnicas: AES-256-GCM via `RequestStateSecurity` (integridade e confidencialidade), TTL de 15 min, chave em `REQUEST_STATE_SECRET` (mínimo de 32 bytes, validado na subida), store de Tasks (`InMemoryTaskStore` do `a2a-sdk`) com a `Pausa` guardada à parte no executor, o `ServerCallContextBuilder` que assume `A2A-Version: 1.0` e por quê, a decisão sobre `form` + `url` na capability e a solução do `traceparent` implícito.
- Saída do validador: a saída completa da última execução, em bloco de código.

Ensaio final (passo 10 da ordem sugerida do enunciado): `git clone` do seu fork em `/tmp/ensaio`, siga só o README, rode o validador e apague `/tmp/ensaio`. Se algum comando precisou de ajuste, o README está errado.

---

## 14. Armadilhas (sintoma → causa → correção)

| Sintoma | Causa provável | Correção |
|---|---|---|
| Erro "sem canal de volta" ao pedir escolha | `ctx.elicit()` no transporte stateless | `Resolve` + `Elicit` (MRTR) |
| 15 falha: veio `input_required` para cliente sem capability | checagem própria mal posicionada ou ausente | deixar o SDK checar no resolver |
| 16 passa, mas o passo 12 do avaliador falha | segredo gerado em runtime (`ephemeral`) | chave de `REQUEST_STATE_SECRET` |
| 17 responde 500 | exceção própria ao decodificar o estado | deixar a `RequestStateBoundary` rejeitar |
| 21-23 passam, mas 24 em diante voltam `-32009` | validador sem header `A2A-Version`; o SDK assume 0.3 | `ServerCallContextBuilder` que assume `1.0` (Fase 7) |
| 27 falha: a Task vai direto para COMPLETED | cliente MCP com callback respondendo sozinho | `allow_input_required=True` |
| 28 falha por um espaço | `"alternativas:"` sem espaço ou com `", "` errado | string exata |
| 33 falha | pausa única, não por Task | `self.pausas[task_id]` |
| 34 falha | `requestState` em `metadata`, artifact ou mensagem | `Pausa` fora da Task, que é o SDK quem serializa |
| 35 falha | texto da tool só no `history` ou ausente | `up.failed(msg)` põe em `status.message` |
| Avaliador não acha a mensagem de FAILED no `history` | o SDK só move o status para o `history` no status seguinte | status intermediário com a mesma mensagem (Fase 7, item 6) |
| Exceção no `execute()` e Task em estado inesperado | exceção não tratada: o framework marca a Task como erro | capture e publique `up.failed(...)` você mesmo |
| 36 falha | id ou horário no texto da pausa | texto determinístico |
| trace-id ausente em algum request | request implícito do SDK | Fase 9 |
| a segunda execução do validador falha | processos não reiniciados | sempre subir do zero |

---

## 15. Perguntas de domínio (para fechar o aprendizado)

Se você consegue responder a todas sem consultar nada, o desafio cumpriu o papel dele:

1. Por que MRTR é "o servidor termina a resposta pedindo informação" e não "o servidor pergunta"? Que problema de infraestrutura (balanceador, várias réplicas, restart) isso resolve?
2. O que aconteceria se o agente abrisse o `requestState` para mostrar ao usuário "o que ele contém"? Que acoplamento isso cria?
3. Por que a capability é declarada por request e não uma vez por conexão? O que o servidor ganha com isso num mundo sem sessão?
4. A Task e o `requestState` são os dois "estados nomeados". Em que sentido um é o espelho do outro, e em que sentido são diferentes (quem guarda, quem lê, quanto tempo vivem)?
5. Se amanhã a regra de alternativas mudar (por exemplo, exigir `camera`), quantas linhas do agente mudam? Se a resposta não for zero, o agente está implementando domínio.
6. Onde, na sua entrega, você autenticaria o principal para impedir o replay do `requestState` de outro usuário, se autenticação estivesse no escopo?
