"""Bordas das regras de domínio (Experimento 3). Funções puras: sem servidor, sem MCP."""
from datetime import datetime

import pytest

from dominio import ErroDeDominio, conflitos, livre, sobrepoe, validar

SALAS = {"sala-garagem": {"id": "sala-garagem", "nome": "Garagem", "capacidade": 12, "recursos": []}}
DIA = "2026-11-03T"


def hora(hhmm: str) -> str:
    return f"{DIA}{hhmm}:00-03:00"


def dt(hhmm: str) -> datetime:
    return datetime.fromisoformat(hora(hhmm))


# --- janela de uso (08:00 a 20:00) e duração (no máximo 2h) ---

@pytest.mark.parametrize("inicio, fim", [
    ("08:00", "10:00"),   # abre exatamente às 08:00, e dura exatamente 2h
    ("18:00", "20:00"),   # fecha exatamente às 20:00
    ("14:00", "16:00"),   # exatamente 2h no meio do dia
])
def test_bordas_que_passam(inicio, fim):
    i, f = validar("sala-garagem", hora(inicio), hora(fim), SALAS)
    assert (i, f) == (dt(inicio), dt(fim))


def test_fim_um_minuto_apos_20h_falha():
    with pytest.raises(ErroDeDominio, match="^Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00$"):
        validar("sala-garagem", hora("19:00"), hora("20:01"), SALAS)


def test_inicio_um_minuto_antes_das_8h_falha():
    with pytest.raises(ErroDeDominio, match="^Fora da janela de uso"):
        validar("sala-garagem", hora("07:59"), hora("09:00"), SALAS)


def test_duas_horas_e_um_minuto_falha():
    with pytest.raises(ErroDeDominio, match="^Duracao acima do limite: a politica permite no maximo 2 horas$"):
        validar("sala-garagem", hora("10:00"), hora("12:01"), SALAS)


# --- intervalo invertido ou vazio ---

def test_fim_igual_ao_inicio_falha():
    with pytest.raises(ErroDeDominio, match="^Intervalo invalido: fim deve ser posterior a inicio$"):
        validar("sala-garagem", hora("10:00"), hora("10:00"), SALAS)


def test_fim_menor_que_inicio_falha():
    with pytest.raises(ErroDeDominio, match="^Intervalo invalido: fim deve ser posterior a inicio$"):
        validar("sala-garagem", hora("10:00"), hora("09:00"), SALAS)


def test_sala_inexistente_falha():
    with pytest.raises(ErroDeDominio, match="^Sala inexistente: sala-x$"):
        validar("sala-x", hora("10:00"), hora("11:00"), SALAS)


def test_fuso_diferente_e_convertido_para_sao_paulo():
    # 11:00Z = 08:00 em -03:00: dentro da janela, mesmo que o "hour" recebido seja 11
    validar("sala-garagem", f"{DIA}11:00:00Z", f"{DIA}12:00:00Z", SALAS)
    # 23:30Z = 20:30 em -03:00: fora, mesmo que o "hour" recebido pareça qualquer coisa
    with pytest.raises(ErroDeDominio, match="^Fora da janela de uso"):
        validar("sala-garagem", f"{DIA}22:30:00Z", f"{DIA}23:30:00Z", SALAS)


# --- sobreposição: intervalos semiabertos [inicio, fim) ---

def test_reservas_encostadas_nao_conflitam():
    assert not sobrepoe(dt("14:00"), dt("15:00"), dt("15:00"), dt("16:00"))
    assert not sobrepoe(dt("15:00"), dt("16:00"), dt("14:00"), dt("15:00"))   # simétrico


def test_um_minuto_de_sobreposicao_conflita():
    assert sobrepoe(dt("14:00"), dt("15:01"), dt("15:00"), dt("16:00"))


def test_conflitos_e_livre_com_reserva_existente():
    reservas = [{"id": "res-0001", "sala": "sala-garagem", "inicio": hora("14:00"),
                 "fim": hora("15:00"), "responsavel": "Marty"}]
    assert livre("sala-garagem", dt("15:00"), dt("16:00"), reservas)            # encostada
    assert [r["id"] for r in conflitos("sala-garagem", dt("14:30"), dt("15:30"), reservas)] == ["res-0001"]
    assert livre("sala-fusca", dt("14:00"), dt("15:00"), reservas)              # outra sala
