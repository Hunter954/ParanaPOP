from datetime import datetime, timedelta

from flask import Blueprint, render_template, redirect, url_for, request, flash
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy import func

from .models import db, User, AdSlot, SiteSetting, PageView
from .forms import LoginForm, AdSlotForm

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _require_admin():
    if not current_user.is_authenticated:
        return redirect(url_for("admin.login"))
    if not getattr(current_user, "is_admin", False):
        flash("Acesso negado.", "danger")
        return redirect(url_for("site.home"))
    return None


@admin_bp.get("/login")
def login():
    form = LoginForm()
    return render_template("admin/login.html", form=form)


@admin_bp.post("/login")
def login_post():
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower().strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            return redirect(url_for("admin.dashboard"))
        flash("Email ou senha inválidos.", "danger")
    return render_template("admin/login.html", form=form)


@admin_bp.get("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("site.home"))


@admin_bp.get("/")
@login_required
def dashboard():
    r = _require_admin()
    if r:
        return r

    pv_total = db.session.query(func.count(PageView.id)).scalar() or 0

    # Funciona em Postgres/SQLite/etc (sem func.interval que dá erro no Railway)
    since = datetime.utcnow() - timedelta(hours=24)
    pv_24h = db.session.query(func.count(PageView.id)).filter(PageView.created_at >= since).scalar() or 0

    slots = AdSlot.query.order_by(AdSlot.key.asc()).all()
    live_embed = SiteSetting.query.filter_by(key="live_embed_html").first()
    logo_url = SiteSetting.query.filter_by(key="logo_url").first()

    return render_template(
        "admin/dashboard.html",
        pv_total=pv_total,
        pv_24h=pv_24h,
        slots=slots,
        live_embed=(live_embed.value if live_embed else ""),
        logo_url=(logo_url.value if logo_url else ""),
    )


@admin_bp.get("/ads/new")
@login_required
def ads_new():
    r = _require_admin()
    if r:
        return r
    form = AdSlotForm()
    form.is_active.data = True
    return render_template("admin/ad_form.html", form=form, mode="new")


@admin_bp.post("/ads/new")
@login_required
def ads_new_post():
    r = _require_admin()
    if r:
        return r
    form = AdSlotForm()
    if form.validate_on_submit():
        if AdSlot.query.filter_by(key=form.key.data.strip()).first():
            flash("Já existe um slot com essa chave.", "danger")
            return render_template("admin/ad_form.html", form=form, mode="new")

        html = form.html.data or ""
        img = (form.image_url.data or "").strip()
        link = (form.link_url.data or "").strip() or "#"
        if img:
            html = f'<a href="{link}" target="_blank" rel="noopener"><img src="{img}" alt="" style="max-width:100%;height:auto;display:block;border-radius:6px;"></a>'

        slot = AdSlot(
            key=form.key.data.strip(),
            name=form.name.data.strip(),
            html=html,
            is_active=bool(form.is_active.data),
        )
        db.session.add(slot)
        db.session.commit()
        flash("Slot criado.", "success")
        return redirect(url_for("admin.dashboard"))

    return render_template("admin/ad_form.html", form=form, mode="new")


@admin_bp.get("/ads/<int:slot_id>/edit")
@login_required
def ads_edit(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    form = AdSlotForm(obj=slot)
    return render_template("admin/ad_form.html", form=form, mode="edit", slot=slot)


@admin_bp.post("/ads/<int:slot_id>/edit")
@login_required
def ads_edit_post(slot_id):
    r = _require_admin()
    if r:
        return r
    slot = AdSlot.query.get_or_404(slot_id)
    form = AdSlotForm()
    if form.validate_on_submit():
        slot.key = form.key.data.strip()
        slot.name = form.name.data.strip()
        html = form.html.data or ""
        img = (form.image_url.data or "").strip()
        link = (form.link_url.data or "").strip() or "#"
        if img:
            html = f'<a href="{link}" target="_blank" rel="noopener"><img src="{img}" alt="" style="max-width:100%;height:auto;display:block;border-radius:6px;"></a>'
        slot.html = html
        slot.is_active = bool(form.is_active.data)
        db.session.commit()
        flash("Slot atualizado.", "success")
        return redirect(url_for("admin.dashboard"))

    return render_template("admin/ad_form.html", form=form, mode="edit", slot=slot)


@admin_bp.post("/settings/live")
@login_required
def save_live():
    r = _require_admin()
    if r:
        return r

    html = request.form.get("live_embed_html", "")
    s = SiteSetting.query.filter_by(key="live_embed_html").first()

    if not s:
        s = SiteSetting(key="live_embed_html", value=html)
        db.session.add(s)
    else:
        s.value = html

    db.session.commit()
    flash("AO VIVO atualizado.", "success")
    return redirect(url_for("admin.dashboard"))


@admin_bp.post("/settings/logo")
@login_required
def save_logo():
    r = _require_admin()
    if r:
        return r

    logo_url = (request.form.get("logo_url", "") or "").strip()
    s = SiteSetting.query.filter_by(key="logo_url").first()
    if not s:
        s = SiteSetting(key="logo_url", value=logo_url)
        db.session.add(s)
    else:
        s.value = logo_url
    db.session.commit()
    flash("Logo atualizada.", "success")
    return redirect(url_for("admin.dashboard"))
