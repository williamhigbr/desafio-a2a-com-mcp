# A Ponte: agente A2A com MCP por dentro

Entrega do desafio "A Ponte" (MBA Engenharia de Software com IA, curso de MCP e A2A). São dois processos Python separados:

- `servidor-mcp/` é o servidor MCP da central de salas, em Streamable HTTP na porta `7301`, endpoint `/mcp`. Usa `mcp==2.2.0`, revisão `2026-07-28`.
- `agente/` é o agente: servidor A2A v1.0 (JSON-RPC) na porta `7300`, endpoint `/a2a`, card em `/.well-known/agent-card.json`. Por dentro, é host MCP do servidor acima. Usa `a2a-sdk[http-server]==1.1.5` e `mcp==2.2.0`.

O agente não usa LLM. Ele lê o pedido em formato fixo e decide por regra. Todas as regras de sala (política, conflito e alternativas) ficam no servidor MCP.

## Como rodar

Pré-requisitos: [uv](https://docs.astral.sh/uv/). O `uv` baixa o Python 3.12 que os projetos pedem, então não é preciso instalar Python à parte. As versões estão travadas nos `pyproject.toml` e nos `uv.lock`.

```bash
git clone https://github.com/williamhigbr/desafio-a2a-com-mcp.git
cd desafio-a2a-com-mcp
uv sync --project servidor-mcp
uv sync --project agente
```

Gere a chave de integridade do `requestState`. São 32 bytes aleatórios, em 64 caracteres hex. Ela fica num `.env` que o git ignora. Nunca faça commit desse valor.

```bash
echo "REQUEST_STATE_SECRET=$(python3 -c 'import secrets; print(secrets.token_hex(32))')" > .env
```

Suba cada processo num terminal, a partir da raiz do repositório.

Terminal 1, servidor MCP. O stderr aparece na tela e também é gravado em `mcp.log`:

```bash
set -a; source .env; set +a
uv run --project servidor-mcp python servidor-mcp/servidor.py 2> >(tee mcp.log >&2)
```

Terminal 2, agente:

```bash
cd agente && uv run python agente.py
```

Terminal 3, validador:

```bash
python3 validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
# se o python3 da máquina for anterior ao 3.10:
uv run --no-project --python 3.12 python validador/validar.py --agente http://localhost:7300 --mcp http://localhost:7301
```

Rode o validador sempre com os dois processos recém-iniciados. As reservas ficam em memória, e uma execução muda o resultado da seguinte.

O servidor MCP se recusa a subir sem `REQUEST_STATE_SECRET` ou com menos de 32 bytes. As portas e URLs podem ser trocadas por variável de ambiente, mas os padrões são os do enunciado:

| Variável | Padrão |
|---|---|
| `MCP_PORT` | `7301` |
| `AGENTE_PORT` | `7300` |
| `AGENTE_URL`, a URL anunciada no card | `http://localhost:7300/a2a` |
| `MCP_URL`, o servidor MCP que o agente usa | `http://localhost:7301/mcp` |

Os testes do domínio rodam com `uv run --directory servidor-mcp pytest -v`.

## Onde a ponte acontece

A ponte está em `agente/executor.py`, na classe `ExecutorDeReserva`.

O `input_required` do MCP vira `TASK_STATE_INPUT_REQUIRED` em `tratar()`, a partir da linha 149. O host chama `tools/call` com `allow_input_required=True` (`agente/host_mcp.py`, `HostMCP.reservar()`), e por isso recebe o `InputRequiredResult` cru, sem o SDK responder a elicitation sozinho. O ramo `if isinstance(r, InputRequiredResult)`, na linha 150, faz três coisas:

1. Lê as opções do `enum` da elicitation com `opcoes_da_elicitation()`. É leitura de protocolo: quem calculou as alternativas foi o servidor.
2. Guarda o `requestState`, a chave do `inputRequests`, as opções, os argumentos, o trace-id e a versão da política numa `Pausa` ligada ao `task_id` (`self.pausas[task_id] = Pausa(...)`, linha 159).
3. Publica `up.requires_input(...)` com a linha `alternativas: <ids>` (linha 161) e retorna, sem ficar bloqueado esperando.

O `requestState` volta ao servidor em `continuar()`, a partir da linha 130. Quando chega o `SendMessage` com o mesmo `taskId`, o SDK A2A chama `execute()` de novo. O executor encontra a `Pausa` e traduz a resposta: `escolha=<id>` vira `accept` e `escolha=recusar` vira `decline`. Uma escolha fora do `enum` repete a pausa sem chamar o MCP. Em seguida, faz um `tools/call` novo com `input_responses={p.chave: resposta}` e `request_state=p.request_state`, ecoado sem abrir nem modificar (linhas 144 a 146). O id do JSON-RPC é novo porque cada `call_tool` é um request novo e o SDK numera cada um com um contador (`_allocate_id()` em `mcp/shared/jsonrpc_dispatcher.py`). O resultado volta para `tratar()`. Se o servidor pedir de novo, a Task pausa outra vez; senão, termina em `COMPLETED`, `CANCELED` ou `FAILED`.

Do lado do servidor, o MRTR está em `servidor-mcp/servidor.py`. O resolver `escolha_de_sala()` (linha 105) devolve `Elicit(...)` com um `Literal` das alternativas, e a tool `reservar_sala()` (linha 127) recebe o `ElicitationResult` via `Resolve`. O SDK transforma o `Elicit` em `input_required` com `requestState` selado. Não existe callback nem canal de volta.

## Decisões técnicas

### `requestState`

- **Proteção.** Usei o `RequestStateSecurity(keys=[segredo], ttl=15 * 60)` do próprio SDK (`servidor-mcp/servidor.py`, linha 51). O token é selado com AES-256-GCM, que dá integridade e confidencialidade. Ele fica amarrado a método, tool, digest dos argumentos, audiência (o nome do servidor) e expiração. Qualquer caractere trocado invalida a tag do GCM, e a `RequestStateBoundary` responde `-32602 Invalid or expired requestState`. O motivo real (`seal`, `expired`, `request binding`, `unknown key`) vai só para o stderr.
- **Validade.** O token vale 15 minutos, dentro da faixa de 5 a 30 pedida pelo enunciado.
- **Chave.** A chave vem de `REQUEST_STATE_SECRET`. Na subida, `_segredo()` exige pelo menos 32 bytes decodificados: uma string hex de 64 caracteres passa, uma de 32 não. A checagem do SDK mede o tamanho da string, não a entropia, e por isso a validação própria. Como a chave é estável, o retry funciona depois de reiniciar o servidor. O padrão do SDK sem chave seria efêmero e quebraria isso.
- **Argumentos adulterados no retry.** O envelope sela o digest dos `arguments`. Se o retry chega com argumentos diferentes, o estado é rejeitado com `-32602`, que é um dos dois caminhos que o enunciado aceita.
- **Reinício e corrida.** O resolver roda de novo no retry e recalcula conflito e alternativas. O SDK só aceita a resposta se a pergunta atual for idêntica à que foi feita, porque o digest da pergunta também viaja no token. Se as alternativas mudarem entre a pausa e o retry, por exemplo depois de um reinício que apagou reservas em memória, o servidor pergunta de novo em vez de consumir uma resposta velha. O log mostra "the question changed since it was asked".

### Estado das Tasks

- **Tasks.** As Tasks ficam no `InMemoryTaskStore` do `a2a-sdk`, que é dono de `id`, `contextId`, `status`, `history` e `artifacts`.
- **Pausa.** O estado privado da ponte fica fora da Task, num `dict[str, Pausa]` do executor chaveado por `task_id`. Como a Task é serializada pelo SDK, esse desenho impede que o `requestState` vaze no card, no artifact ou em mensagens (verificação 34). Também garante que duas Tasks pausadas nunca troquem de estado (verificação 33).
- **Reinício do agente.** Um reinício perde as Tasks e as pausas, e isso é aceito para persistência em memória. Um reinício do servidor MCP não perde nada do fluxo pendente, porque o estado viaja no token.

### A2A

- **Header `A2A-Version` ausente.** O validador não envia esse header. Pela spec, header ausente significa `0.3`, e o SDK recusa com `-32009 VersionNotSupported`. O `AssumeV1QuandoAusente` (`agente/agente.py`, linha 50) preenche `1.0` só quando o header falta, porque o agente só fala `1.0`, que é o que o card anuncia. Um header presente com outra versão continua sendo validado pelo SDK.
- **Mensagem de `FAILED` no histórico.** O SDK só move o `status.message` para o `history` quando chega o próximo status. Por isso `falhar()` publica um `WORKING` com o texto da tool antes do `FAILED`. A mensagem exata, por exemplo `Error executing tool reservar_sala: Sala inexistente: sala-inexistente`, fica no `history` e também em `status.message`.
- **Transições no stderr.** Cada transição é registrada no stderr do agente (`{"task": ..., "estado": ...}`), como evidência de `SUBMITTED` e `WORKING`, que a resposta bloqueante não mostra.

### MCP: capability, trace e log

- **Capability de elicitation: limitação do SDK.** O enunciado pede `{"elicitation": {"form": {}}}`. O cliente do `mcp==2.2.0` declara `form` e `url` sempre que há `elicitation_callback`, sem parâmetro para escolher (`mcp/client/session.py`, `_build_capabilities`):

  ```python
  elicitation = (
      types.ElicitationCapability(form=types.FormElicitationCapability(), url=types.UrlElicitationCapability())
      if self._elicitation_callback is not _default_elicitation_callback
  ```

  Não sobrescrevi o método privado nem montei o `_meta` à mão, porque isso seria reescrever o protocolo. O agente declara form mode, que é o que o servidor exige (`_require_capability` aceita quando `form` está presente). O agente só trata form: um `input_required` que não seja form com `properties.sala`, como um pedido em URL mode, leva a Task a `FAILED` com `Pedido de entrada nao suportado pelo agente` (`opcoes_da_elicitation()`). O callback de elicitation existe só para o SDK declarar a capability, e lança erro se for chamado, porque quem responde é o cliente A2A.
- **`traceparent`.** O trace-id do header A2A vai no `_meta.traceparent` de todo request MCP daquela Task, com um span-id novo por request. Se o header não vier, o agente gera um trace-id e o mantém em toda a Task, inclusive no retry. Não usei propagação via contexto OpenTelemetry. O `a2a-sdk` já emite um ruído inofensivo de OTel no stderr (`ValueError: Token ... was created in a different Context`), que não afeta o fluxo.
- **`tools/list` sem `traceparent` no log.** Durante cada `tools/call`, o próprio servidor chama internamente o handler de `tools/list` para validar os headers `Mcp-Param` (`mcp/server/_streamable_http_modern.py`, `_tool_input_schema`). Essa chamada passa pelo middleware de log com o mesmo id do `tools/call` e sem `traceparent`, mas não veio da rede. O middleware a marca com o campo `"interno"`. Os requests de rede do agente sempre levam `traceparent`. As linhas de rede sem `traceparent` são do validador falando direto com o MCP: têm id hex e capability só com `form`.

### Domínio

- **Ordem das validações.** A ordem é sala inexistente, intervalo invertido ou vazio, janela de uso e duração. Assim, `10:00→09:00` é "Intervalo invalido".
- **Horários.** São convertidos para `-03:00` antes de comparar com a janela de 08:00 às 20:00.
- **Intervalos.** São semiabertos `[inicio, fim)`: reservas encostadas não conflitam.
- **Testes.** Estão em `servidor-mcp/tests/test_dominio.py`.

### Evidências e ferramentas

- **`evidencias/`.** Tem três execuções do validador, cada uma com os processos recém-iniciados (`validador-run{1,2,3}.log`, todas com 36/36). Tem também o roteiro dos passos 3 e 7 a 13 do Fluxo do avaliador (`fluxo-avaliador.txt`) e o stderr dos dois processos em cada execução.
- **`ferramentas/ui/`.** É uma UI em Streamlit para explorar os dois protocolos. Não faz parte da entrega e tem dependências próprias.

## Saída do validador

```
trace-id desta execucao: e4a4c8de37b6534a325262838aefcb22
procure esse valor no stderr do servidor MCP para conferir a propagacao do traceparent.

PASS 01 tools/list traz as tres tools
PASS 02 toda tool tem inputSchema de objeto
PASS 03 listar_salas devolve structuredContent e o mesmo JSON em texto
PASS 04 _meta sem protocolVersion devolve -32602 e HTTP 400
PASS 05 _meta sem clientCapabilities devolve -32602 e HTTP 400
PASS 06 tool inexistente e recusada, por -32602 ou por isError
PASS 07 resources/read de politica://uso devolve a politica
PASS 08 resources/read de URI inexistente devolve -32602
PASS 09 sala inexistente devolve isError com a mensagem exata
PASS 10 fora da janela devolve isError com a mensagem exata
PASS 11 duracao acima de 2h devolve isError com a mensagem exata
PASS 12 intervalo invertido devolve isError com a mensagem exata
PASS 13 conflito devolve input_required com inputRequests e requestState
PASS 14 a elicitation e form mode e oferece as alternativas na ordem certa
PASS 15 conflito sem a capability elicitation devolve -32021 e HTTP 400
PASS 16 retry com inputResponses e requestState conclui a reserva
PASS 17 requestState adulterado e rejeitado com -32602
PASS 18 argumentos adulterados no retry nao tomam efeito
PASS 19 recusa conclui sem reservar e sem isError
PASS 20 conflito sem alternativa possivel devolve isError com a mensagem exata

PASS 21 agent card responde 200 no well-known com JSON
PASS 22 o card declara a interface JSON-RPC com url e versao 1.0
PASS 23 o card declara a skill reservar-sala
PASS 24 SendMessage com sala livre conclui a Task
PASS 25 o artifact chama reserva e traz a versao da politica
PASS 26 GetTask devolve id, contextId e estado corrente
PASS 27 SendMessage com sala ocupada pausa a Task
PASS 28 a Task pausada lista as alternativas na ordem certa
PASS 29 escolha fora do enum mantem a Task pausada
PASS 30 a continuacao conclui a Task na sala escolhida
PASS 31 SendMessage em Task terminal e recusado
PASS 32 a recusa termina a Task em CANCELED
PASS 33 duas Tasks pausadas ao mesmo tempo concluem cada uma com a sua reserva
PASS 34 nenhuma resposta A2A carrega o requestState
PASS 35 sala inexistente termina a Task em FAILED com a mensagem da tool
PASS 36 o agente e deterministico: o mesmo pedido produz a mesma pausa

resumo: 36 passaram, 0 falharam, de 36 verificacoes
```
