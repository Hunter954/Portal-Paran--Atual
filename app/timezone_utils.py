from datetime import timezone
from zoneinfo import ZoneInfo

BRASILIA_TZ = ZoneInfo("America/Sao_Paulo")

def to_brasilia(value):
    """Converte datetime salvo em UTC para o horário oficial de Brasília."""
    if not value:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(BRASILIA_TZ)

def format_datetime_br(value, pattern="%d/%m/%Y - %Hh%M"):
    converted = to_brasilia(value)
    return converted.strftime(pattern) if converted else ""

def iso_brasilia(value):
    converted = to_brasilia(value)
    return converted.isoformat() if converted else ""
