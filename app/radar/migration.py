import click

from ..models import db
from .models import Alert, Article, Audit, ChatCard, ChatDraftReply, ChatSession, Draft, GroupDelivery, InboundMessage, Job, Run, Source, Story, WhatsAppPreference


TABLES = [model.__table__ for model in (Source, Run, Story, Article, Draft, Job, Audit, WhatsAppPreference, Alert, InboundMessage, GroupDelivery,
                                             ChatSession, ChatCard, ChatDraftReply)]


def upgrade():
    db.metadata.create_all(db.engine, tables=TABLES)


@click.group("radar")
def radar_cli():
    """Gerencia a persistência e o worker do Radar."""


@radar_cli.command("migrate")
def migrate():
    """Instala as tabelas do Radar."""
    upgrade()
    click.echo("Tabelas do Radar disponíveis.")


@radar_cli.command("downgrade")
@click.confirmation_option(prompt="Excluir as tabelas e os dados do Radar?")
def downgrade():
    """Remove as tabelas do Radar após desativar o worker."""
    db.metadata.drop_all(db.engine, tables=TABLES)
    click.echo("Tabelas do Radar removidas.")

