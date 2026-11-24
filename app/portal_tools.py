from datetime import date, datetime, time, timedelta, timezone
import hashlib
from io import BytesIO
import os
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from flask import Blueprint, abort, current_app, flash, redirect, render_template, request, send_file, url_for
from flask_login import current_user, login_required
from flask_wtf.csrf import generate_csrf, validate_csrf
import requests
from wtforms.validators import ValidationError

from .models import Post, SiteSetting, db
from .timezone_utils import format_datetime_br


bp = Blueprint("portal_tools", __name__, url_prefix="/admin")
LOCAL_TZ = ZoneInfo("America/Sao_Paulo")
SECTIONS = {"sidebar_1": "Lateral quadrada", "sidebar_2": "Lateral vertical"}
HOME_DATE_KEY = "pa_home_calendar_date"


@bp.before_request
@login_required
def require_admin():
    if not current_user.is_active or not current_user.is_admin:
        abort(403)
    if request.method == "POST":
        try:
            validate_csrf(request.form.get("csrf_token"))
        except ValidationError:
            abort(400)


def screen(template, section, **values):
    return render_template(f"admin/{template}.html", section=section,
                           admin_media_root=current_app.config["MEDIA_ROOT"], portal_csrf=generate_csrf,
                           **values)


def home_date():
    item = SiteSetting.query.filter_by(key=HOME_DATE_KEY).first()
    try:
        return date.fromisoformat(item.value) if item and item.value else None
    except ValueError:
        return None


def day_bounds(day):
    return tuple(datetime.combine(ref, time(), LOCAL_TZ).astimezone(timezone.utc).replace(tzinfo=None)
                 for ref in (day, day + timedelta(days=1)))


@bp.route("/calendario-home", methods=["GET", "POST"])
def calendar():
    today = datetime.now(LOCAL_TZ).date()
    if request.method == "POST":
        selected = None
        try:
            if request.form.get("enabled") == "1":
                selected = date.fromisoformat(request.form.get("day", ""))
                if selected > today:
                    raise ValueError
            if selected:
                first, last = day_bounds(selected)
                if not Post.query.filter(Post.published_at >= first, Post.published_at < last).first():
                    flash("Escolha uma data com matérias publicadas.", "warning")
                    return redirect(url_for("portal_tools.calendar"))
        except (ValueError, OverflowError):
            flash("Informe uma data até hoje.", "warning")
            return redirect(url_for("portal_tools.calendar"))
        item = SiteSetting.query.filter_by(key=HOME_DATE_KEY).first()
        if item is None:
            item = SiteSetting(key=HOME_DATE_KEY)
            db.session.add(item)
        item.value = selected.isoformat() if selected else ""
        db.session.commit()
        flash("Data da página inicial atualizada.", "success")
        return redirect(url_for("portal_tools.calendar"))
    day = home_date()
    return screen("home_calendar", "home_calendar", day=day, today=today)


def request_captures(path, *, report=False):
    base, secret = os.getenv("PARANAATUAL_AD_REPORTS_URL"), os.getenv("PARANAATUAL_AD_REPORTS_TOKEN")
    if not base or not secret:
        raise ValueError("O serviço de capturas ainda está sem configuração no painel.")
    with requests.Session() as session:
        session.trust_env = False
        with session.get(base.rstrip("/") + path, headers={"Authorization": "Bearer " + secret},
                         timeout=(4, 65 if report else 8), allow_redirects=False, stream=report) as response:
            if response.status_code == 404 and report:
                raise ValueError("Sem capturas para a seção e o período selecionados.")
            if response.status_code != 200:
                raise ValueError("Serviço de capturas indisponível.")
            if not report:
                return response.json()
            output = BytesIO()
            checksum = hashlib.sha256()
            for piece in response.iter_content(65536):
                if output.tell() + len(piece) > 70_000_000:
                    raise ValueError("Escolha um período menor para baixar o relatório.")
                output.write(piece)
                checksum.update(piece)
            output.seek(0)
            if output.read(4) != b"PK\x03\x04" or checksum.hexdigest() != response.headers.get("X-Report-SHA256"):
                raise ValueError("O arquivo retornou com falha na verificação.")
            output.seek(0)
            return output


@bp.get("/prints")
def prints():
    data, error, latest = None, "", ""
    try:
        data = request_captures("/v1/status")
        if data.get("latest"):
            latest = format_datetime_br(datetime.fromisoformat(data["latest"]))
    except (requests.RequestException, ValueError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else "Serviço de capturas indisponível."
    return screen("prints", "prints", captures=data, latest=latest, error=error, sections=SECTIONS,
                  today=datetime.now(LOCAL_TZ).date())


@bp.get("/prints/download")
def prints_download():
    try:
        section = request.args.get("section", "")
        try:
            start = date.fromisoformat(request.args.get("start", ""))
            end = date.fromisoformat(request.args.get("end", ""))
        except ValueError as exc:
            raise ValueError("Informe datas válidas para baixar o relatório.") from exc
        if (section not in SECTIONS or start > end or end > datetime.now(LOCAL_TZ).date()
                or (end - start).days > 365):
            raise ValueError("Escolha uma seção e um período de até 366 dias, sem datas futuras.")
        params = urlencode({"section": section, "start": start, "end": end})
        result = request_captures("/v1/report.docx?" + params, report=True)
        return send_file(result, mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         as_attachment=True, download_name=f"ParanaAtual_{section}_{start}_{end}.docx", max_age=0)
    except (requests.RequestException, ValueError) as exc:
        flash(str(exc) if isinstance(exc, ValueError) else "Serviço de capturas indisponível.", "warning")
        return redirect(url_for("portal_tools.prints"))



@bp.get("/estudio")
def media_studio():
    return screen("media_studio", "media_studio")


@bp.get("/whatsapp")
def whatsapp_panel():
    return redirect(url_for("radar.whatsapp"))
