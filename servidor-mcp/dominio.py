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

def conflitos(sala: str, i: datetime, f: datetime, reservas: list[dict]) -> list[dict]:
    """Reservas da sala que se sobrepõem ao intervalo [i, f)."""
    return [r for r in reservas
            if r["sala"] == sala and sobrepoe(i, f, datetime.fromisoformat(r["inicio"]),
                                              datetime.fromisoformat(r["fim"]))]

def livre(sala: str, i: datetime, f: datetime, reservas: list[dict]) -> bool:
    return not conflitos(sala, i, f, reservas)

def alternativas(sala, i, f, salas, reservas) -> list[str]:
    cap = salas[sala]["capacidade"]
    livres = [s for s in salas.values()
              if s["id"] != sala and s["capacidade"] >= cap and livre(s["id"], i, f, reservas)]
    return [s["id"] for s in sorted(livres, key=lambda s: (s["capacidade"], s["id"]))][:3]