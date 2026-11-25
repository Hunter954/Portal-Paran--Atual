from datetime import UTC, datetime

from ..models import db

MANUAL_REGION = "Busca livre"


def now():
    return datetime.now(UTC).replace(tzinfo=None)


class Source(db.Model):
    __tablename__ = "radar_sources"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    kind = db.Column(db.String(20), nullable=False)
    region = db.Column(db.String(100), nullable=False, default="")
    category = db.Column(db.String(100), nullable=False, default="")
    use_policy = db.Column(db.String(300), nullable=False, default="")
    priority = db.Column(db.Integer, nullable=False, default=3)
    interval = db.Column(db.Integer, nullable=False, default=15)
    active = db.Column(db.Boolean, nullable=False, default=True)
    archived = db.Column(db.Boolean, nullable=False, default=False)
    next_run = db.Column(db.DateTime, nullable=False, default=now, index=True)
    last_run = db.Column(db.DateTime)
    last_success = db.Column(db.DateTime)
    failures = db.Column(db.Integer, nullable=False, default=0)
    response_ms = db.Column(db.Integer)
    cache_url = db.Column(db.String(1000))
    etag = db.Column(db.String(500))
    modified = db.Column(db.String(100))


class Run(db.Model):
    __tablename__ = "radar_source_runs"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.ForeignKey("radar_sources.id"), nullable=False, index=True)
    started = db.Column(db.DateTime, nullable=False, default=now)
    finished = db.Column(db.DateTime)
    kind = db.Column(db.String(12), nullable=False)
    state = db.Column(db.String(12), nullable=False, default="pending", index=True)
    message = db.Column(db.String(400))
    accepted = db.Column(db.Integer, nullable=False, default=0)
    excluded = db.Column(db.Integer, nullable=False, default=0)
    received = db.Column(db.Integer, nullable=False, default=0)
    duration_ms = db.Column(db.Integer)


class Story(db.Model):
    __tablename__ = "radar_stories"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500), nullable=False)
    location = db.Column(db.String(150), nullable=False)
    region = db.Column(db.String(100), nullable=False)
    category = db.Column(db.String(100), nullable=False)
    score = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(20), nullable=False, default="new", index=True)
    previous_state = db.Column(db.String(20))
    created = db.Column(db.DateTime, nullable=False, default=now, index=True)
    updated = db.Column(db.DateTime, nullable=False, default=now)
    post_id = db.Column(db.ForeignKey("post.id", ondelete="SET NULL"), unique=True)
    scheduled = db.Column(db.DateTime, index=True)
    approved_draft_id = db.Column(db.Integer)
    articles = db.relationship("Article", back_populates="story", order_by="Article.id")


class Article(db.Model):
    __tablename__ = "radar_articles"

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.ForeignKey("radar_sources.id"), nullable=False, index=True)
    story_id = db.Column(db.ForeignKey("radar_stories.id"), nullable=False, index=True)
    url = db.Column(db.String(1000), unique=True, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    summary = db.Column(db.Text, nullable=False, default="")
    evidence = db.Column(db.String(500), nullable=False)
    digest = db.Column(db.String(64), nullable=False, index=True)
    published_at = db.Column(db.DateTime)
    discovered = db.Column(db.DateTime, nullable=False, default=now)
    story = db.relationship("Story", back_populates="articles")
    source = db.relationship("Source")


class Draft(db.Model):
    __tablename__ = "radar_drafts"
    __table_args__ = (db.UniqueConstraint("story_id", "version"),)

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.ForeignKey("radar_stories.id"), nullable=False, index=True)
    version = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(500), nullable=False)
    excerpt = db.Column(db.Text, nullable=False)
    body = db.Column(db.Text, nullable=False)
    facts = db.Column(db.JSON, nullable=False, default=list)
    pending = db.Column(db.JSON, nullable=False, default=list)
    created = db.Column(db.DateTime, nullable=False, default=now)
    author_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"))
    model = db.Column(db.String(100))


class DraftMedia(db.Model):
    __tablename__ = "radar_draft_media"

    draft_id = db.Column(db.ForeignKey("radar_drafts.id", ondelete="CASCADE"), primary_key=True)
    path = db.Column(db.String(800), nullable=False)
    credit = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(500), nullable=False)
    url = db.Column(db.String(1000), nullable=False)

    license = db.Column(db.String(300))


class Job(db.Model):
    __tablename__ = "radar_jobs"

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.ForeignKey("radar_stories.id"), nullable=False, index=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"))
    state = db.Column(db.String(12), nullable=False, default="pending", index=True)
    created = db.Column(db.DateTime, nullable=False, default=now)
    started = db.Column(db.DateTime)
    finished = db.Column(db.DateTime)
    error = db.Column(db.String(300))


class Audit(db.Model):
    __tablename__ = "radar_audit"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"))
    story_id = db.Column(db.ForeignKey("radar_stories.id"), index=True)
    source_id = db.Column(db.ForeignKey("radar_sources.id"), index=True)
    action = db.Column(db.String(35), nullable=False)
    details = db.Column(db.String(300))
    created = db.Column(db.DateTime, nullable=False, default=now, index=True)

 
class WhatsAppPreference(db.Model):
    __tablename__ = "radar_whatsapp_preferences"

    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    phone = db.Column(db.String(16), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=False)
    minimum = db.Column(db.Integer, nullable=False, default=75)
    subscribed = db.Column(db.DateTime, nullable=False, default=now)


class Alert(db.Model):
    __tablename__ = "radar_alerts"
    __table_args__ = (db.UniqueConstraint("story_id", "user_id"),)

    id = db.Column(db.Integer, primary_key=True)
    story_id = db.Column(db.ForeignKey("radar_stories.id"), nullable=False)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    state = db.Column(db.String(12), nullable=False)
    message = db.Column(db.String(160))
    created = db.Column(db.DateTime, nullable=False, default=now)
    finished = db.Column(db.DateTime)



class GroupDelivery(db.Model):
    __tablename__ = "radar_group_deliveries"
    __table_args__ = (db.UniqueConstraint("group_id", "kind", "subject_id"),)

    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.String(40), nullable=False, index=True)
    kind = db.Column(db.String(10), nullable=False)
    subject_id = db.Column(db.Integer, nullable=False)
    state = db.Column(db.String(12), nullable=False)
    message = db.Column(db.String(160))
    created = db.Column(db.DateTime, nullable=False, default=now)
    finished = db.Column(db.DateTime)

class InboundMessage(db.Model):
    __tablename__ = "radar_whatsapp_events"

    id = db.Column(db.Integer, primary_key=True)
    event_key = db.Column(db.String(90), unique=True, nullable=False)
    user_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    created = db.Column(db.DateTime, nullable=False, default=now)


class ChatSession(db.Model):
    __tablename__ = "radar_chat_sessions"

    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    scope = db.Column(db.String(40), primary_key=True)
    mode = db.Column(db.String(12), nullable=False, default="idle")
    page_limit = db.Column(db.Integer, nullable=False, default=10)
    remaining = db.Column(db.Integer, nullable=False, default=0)
    before_id = db.Column(db.Integer)
    urgent = db.Column(db.Boolean, nullable=False, default=False)
    updated = db.Column(db.DateTime, nullable=False, default=now)


class ChatCard(db.Model):
    __tablename__ = "radar_chat_cards"

    message_id = db.Column(db.String(40), primary_key=True)
    story_id = db.Column(db.ForeignKey("radar_stories.id", ondelete="CASCADE"), nullable=False)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="SET NULL"))
    scope = db.Column(db.String(40), nullable=False)
    version = db.Column(db.Integer)
    created = db.Column(db.DateTime, nullable=False, default=now, index=True)


class ChatDraftReply(db.Model):
    __tablename__ = "radar_chat_draft_replies"
    __table_args__ = (db.UniqueConstraint("job_id", "actor_id", "scope"),)

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.ForeignKey("radar_jobs.id", ondelete="CASCADE"), nullable=False)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    scope = db.Column(db.String(40), nullable=False)
    state = db.Column(db.String(12), nullable=False, default="pending", index=True)
    created = db.Column(db.DateTime, nullable=False, default=now)


class SiteChat(db.Model):
    __tablename__ = "radar_site_chat"

    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    scope = db.Column(db.String(40), primary_key=True)
    step = db.Column(db.String(24), nullable=False)
    data = db.Column(db.JSON, nullable=False, default=dict)
    updated = db.Column(db.DateTime, nullable=False, default=now)


class SiteCard(db.Model):
    __tablename__ = "radar_site_cards"

    message_id = db.Column(db.String(40), primary_key=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    scope = db.Column(db.String(40), nullable=False)
    kind = db.Column(db.String(12), nullable=False)
    data = db.Column(db.JSON, nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=now)


class CoverSearch(db.Model):
    __tablename__ = "radar_cover_searches"

    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    scope = db.Column(db.String(40), nullable=False)
    term = db.Column(db.String(220), nullable=False)
    location = db.Column(db.String(100), nullable=False, default="")
    target = db.Column(db.JSON, nullable=False)
    created = db.Column(db.DateTime, nullable=False, default=now)
    state = db.Column(db.String(12), nullable=False, index=True, default="pending")


class ManualSearch(db.Model):
    __tablename__ = "radar_manual_searches"
    id = db.Column(db.Integer, primary_key=True)
    actor_id = db.Column(db.ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)
    scope = db.Column(db.String(40), nullable=False, default="")
    term = db.Column(db.String(220), nullable=False)
    days = db.Column(db.Integer, nullable=False, default=7)
    state = db.Column(db.String(12), nullable=False, default="pending", index=True)
    results = db.Column(db.JSON, nullable=False, default=list)
    error = db.Column(db.String(300))
    created = db.Column(db.DateTime, nullable=False, default=now)
    finished = db.Column(db.DateTime)


def audit(action, *, actor=None, story=None, source=None, details=None):
    db.session.add(Audit(action=action, actor_id=actor, story_id=story, source_id=source, details=details))

