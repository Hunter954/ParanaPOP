"""Recovery is enabled only after a private Baileys token is configured."""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta

import requests
from flask import current_app, flash, redirect, render_template, session, url_for
from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField
from wtforms.validators import DataRequired, Email, Length, EqualTo

from .models import User, db


class RequestForm(FlaskForm):
    email = StringField("E-mail", validators=[DataRequired(), Email()])


class VerifyForm(FlaskForm):
    code = StringField("Código", validators=[DataRequired(), Length(min=6, max=6)])
    password = PasswordField("Nova senha", validators=[DataRequired(), Length(min=8, max=200)])
    confirm = PasswordField("Confirme a senha", validators=[DataRequired(), EqualTo("password")])


def _hash(user, code):
    secret = current_app.secret_key.encode()
    return hmac.new(secret, f"{user.id}:{code}".encode(), hashlib.sha256).hexdigest()


def register_recovery_routes(blueprint, portal):
    @blueprint.route("/recuperar-senha", methods=["GET", "POST"])
    def recovery_request():
        form = RequestForm()
        if form.validate_on_submit():
            email = form.email.data.strip().lower()
            user = User.query.filter_by(email=email, is_active=True).with_for_update().first()
            token = os.getenv("WHATSAPP_RECOVERY_TOKEN", "")
            url = os.getenv("WHATSAPP_RECOVERY_URL", "").rstrip("/")
            now = datetime.utcnow()
            if user and user.whatsapp and token and url and (
                user.reset_requested is None or user.reset_requested < now - timedelta(seconds=90)
            ):
                user.reset_requested = now
                db.session.commit()
                code = f"{secrets.randbelow(1000000):06d}"
                try:
                    response = requests.post(
                        f"{url}/send-recovery-code",
                        json={"portal": portal, "phone": user.whatsapp, "code": code},
                        headers={"Authorization": f"Bearer {token}"}, timeout=12,
                    )
                    if response.status_code == 202:
                        user.reset_hash = _hash(user, code)
                        user.reset_expires = now + timedelta(minutes=5)
                        user.reset_requested = now
                        user.reset_attempts = 0
                        db.session.commit()
                except requests.RequestException:
                    current_app.logger.warning("Não foi possível solicitar a entrega do código")
            session["recovery_email"] = email
            flash("Se a conta tiver WhatsApp cadastrado, o código será enviado.", "info")
            return redirect(url_for("admin.recovery_verify"))
        return render_template("admin/recovery.html", form=form, step="request")

    @blueprint.route("/recuperar-senha/codigo", methods=["GET", "POST"])
    def recovery_verify():
        form = VerifyForm()
        email = session.get("recovery_email")
        if not email:
            return redirect(url_for("admin.recovery_request"))
        if form.validate_on_submit():
            user = User.query.filter_by(email=email, is_active=True).with_for_update().first()
            now = datetime.utcnow()
            valid = bool(user and user.reset_hash and user.reset_expires and user.reset_expires > now
                         and user.reset_attempts < 5 and re.fullmatch(r"[0-9]{6}", form.code.data))
            if valid:
                valid = hmac.compare_digest(user.reset_hash, _hash(user, form.code.data))
            if user and user.reset_hash:
                user.reset_attempts += 1
                if valid:
                    user.set_password(form.password.data)
                    user.reset_hash = None
                    user.reset_expires = None
                    user.reset_attempts = 0
                elif user.reset_attempts >= 5:
                    user.reset_hash = None
                db.session.commit()
            if valid:
                session.pop("recovery_email", None)
                flash("Senha atualizada. Entre com a nova senha.", "success")
                return redirect(url_for("admin.login"))
            flash("Código inválido ou expirado.", "danger")
        return render_template("admin/recovery.html", form=form, step="verify")
