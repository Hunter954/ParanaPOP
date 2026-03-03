from flask_wtf import FlaskForm
from wtforms import StringField, PasswordField, BooleanField, TextAreaField
from wtforms.validators import DataRequired, Email, Length

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email(), Length(max=190)])
    password = PasswordField("Senha", validators=[DataRequired(), Length(min=4, max=200)])

class AdSlotForm(FlaskForm):
    key = StringField("Chave (ex: lateral_1)", validators=[DataRequired(), Length(max=80)])
    name = StringField("Nome", validators=[DataRequired(), Length(max=190)])
    image_url = StringField("Imagem (URL)", validators=[Length(max=800)])
    link_url = StringField("Link (URL)", validators=[Length(max=800)])
    html = TextAreaField("HTML do anúncio")
    is_active = BooleanField("Ativo")
