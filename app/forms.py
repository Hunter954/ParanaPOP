from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField
from wtforms import (
    StringField,
    PasswordField,
    BooleanField,
    TextAreaField,
    SelectMultipleField,
)
from wtforms.validators import DataRequired, Email, Length, Optional


class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=190)])
    password = PasswordField("Senha", validators=[DataRequired(), Length(min=4, max=200)])


class AdSlotForm(FlaskForm):
    key = StringField("Chave (ex: lateral_1)", validators=[DataRequired(), Length(max=80)])
    name = StringField("Nome", validators=[DataRequired(), Length(max=190)])
    image_url = StringField("Imagem (URL)", validators=[Length(max=800)])
    link_url = StringField("Link (URL)", validators=[Length(max=800)])
    image_file = FileField("Imagem (arquivo)", validators=[Optional(), FileAllowed(["jpg", "jpeg", "png", "webp", "gif", "svg"], "Envie uma imagem válida.")])
    html = TextAreaField("HTML do anúncio")
    is_active = BooleanField("Ativo")


class CategoryForm(FlaskForm):
    name = StringField("Nome", validators=[DataRequired(), Length(max=190)])
    slug = StringField("Slug", validators=[Optional(), Length(max=190)])


class PostAdminForm(FlaskForm):
    title = StringField("Título", validators=[DataRequired(), Length(max=500)])
    excerpt = TextAreaField("Resumo", validators=[Optional()])
    content_html = TextAreaField("Conteúdo", validators=[Optional()])
    featured_image = StringField("Imagem destacada (URL)", validators=[Optional(), Length(max=800)])
    featured_image_file = FileField("Imagem destacada (arquivo)", validators=[Optional(), FileAllowed(["jpg", "jpeg", "png", "webp", "gif", "svg"], "Envie uma imagem válida.")])
    categories = SelectMultipleField("Categorias", coerce=int, validators=[Optional()])
