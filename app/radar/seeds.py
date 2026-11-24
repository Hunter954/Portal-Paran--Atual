import click

from ..models import db
from .migration import radar_cli
from .models import Source


INITIAL_SOURCES = (
    ("G1 Paraná", "https://g1.globo.com/rss/g1/pr/parana/", "Todo o Paraná"),
    ("G1 Oeste e Sudoeste", "https://g1.globo.com/rss/g1/pr/oeste-sudoeste/", "Oeste e Sudoeste"),
    ("G1 Norte e Noroeste", "https://g1.globo.com/rss/g1/pr/norte-noroeste/", "Norte e Noroeste"),
    ("G1 Campos Gerais e Sul", "https://g1.globo.com/rss/g1/pr/campos-gerais-sul/", "Campos Gerais e Sul"),
)


@radar_cli.command("seed")
def seed():
    """Adiciona feeds regionais habilitados para o primeiro monitoramento."""
    added = 0
    for name, url, region in INITIAL_SOURCES:
        if not Source.query.filter_by(url=url).first():
            db.session.add(Source(name=name, url=url, region=region, kind="rss", interval=15,
                                  priority=4, use_policy="Referência editorial e atribuição ao link original."))
            added += 1
    db.session.commit()
    click.echo(f"{added} fonte(s) adicionada(s).")

