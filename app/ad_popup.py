import json
from datetime import datetime, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from wtforms import BooleanField, DateTimeLocalField, HiddenField, IntegerField, SelectField, StringField
from wtforms.validators import InputRequired, Length, NumberRange, Optional, ValidationError

from .models import SiteSetting
from .storage import normalize_media_url


SETTING_KEY = "popup_publicidade"
BRASILIA = ZoneInfo("America/Sao_Paulo")
DEFAULTS = {
    "enabled": False, "title": "", "image": "", "link": "", "seconds": 8,
    "starts_at": None, "ends_at": None, "scope": "all", "frequency": "session", "revision": "",
}


def popup_setting(*, lock=False):
    query = SiteSetting.query.filter_by(key=SETTING_KEY)
    return (query.with_for_update() if lock else query).first()


def popup_config(row):
    try:
        stored = json.loads(row.value or "{}") if row else {}
    except ValueError:
        stored = {}
    values = DEFAULTS | (stored if isinstance(stored, dict) else {})
    values["image"] = normalize_media_url(values["image"])
    return values


def read_date(value):
    if not isinstance(value, str):
        return None
    try:
        instant = datetime.fromisoformat(value)
    except ValueError:
        return None
    return instant if instant.tzinfo else None


def local_date(value):
    instant = read_date(value)
    return instant.astimezone(BRASILIA).replace(tzinfo=None) if instant else None


def campaign_status(config, now=None):
    now = now or datetime.now(timezone.utc)
    if not config["image"]:
        return "Sem imagem"
    if not config["enabled"]:
        return "Pausado"
    starts_at, ends_at = read_date(config["starts_at"]), read_date(config["ends_at"])
    if ends_at and now >= ends_at:
        return "Encerrado"
    if starts_at and now < starts_at:
        return "Agendado"
    return "No ar"


def visible_popup(endpoint):
    config = popup_config(popup_setting())
    if config["scope"] == "home" and endpoint != "site.home":
        return None
    return config if campaign_status(config) == "No ar" else None


def validate_link(_form, field):
    value = field.data.strip()
    try:
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or url.username is not None
                or any(c.isspace() or ord(c) < 32 for c in value) or "\\" in value):
            raise ValueError
        _ = url.port
    except ValueError as exc:
        raise ValidationError("Informe um link HTTP ou HTTPS válido.") from exc


class PopupAdForm(FlaskForm):
    revision = HiddenField()
    enabled = BooleanField("Campanha ativada")
    title = StringField("Nome da campanha", validators=[Optional(), Length(max=150)])
    image = FileField("Arquivo do banner", validators=[
        Optional(), FileAllowed(["jpg", "jpeg", "png", "webp", "gif"], "Envie JPG, PNG, WebP ou GIF."),
    ])
    remove_image = BooleanField("Remover a arte atual")
    link = StringField("Link do anúncio", validators=[Optional(), Length(max=800), validate_link])
    seconds = IntegerField("Fechar automaticamente após", validators=[InputRequired(), NumberRange(min=0, max=300)])
    starts_at = DateTimeLocalField("Início da campanha", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    ends_at = DateTimeLocalField("Fim da campanha", format="%Y-%m-%dT%H:%M", validators=[Optional()])
    scope = SelectField("Onde mostrar", choices=[("all", "Em todo o portal"), ("home", "Somente na página inicial")])
    frequency = SelectField("Frequência", choices=[
        ("session", "Uma vez por sessão de navegação"), ("page", "A cada página visitada"),
    ])

    def validate_ends_at(self, field):
        if field.data and self.starts_at.data and field.data <= self.starts_at.data:
            raise ValidationError("O fim da campanha deve ser depois do início.")

    @staticmethod
    def schedule(field):
        return field.data.replace(tzinfo=BRASILIA).astimezone(timezone.utc).isoformat() if field.data else None
