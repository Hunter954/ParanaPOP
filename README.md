# News Flask (WordPress -> Flask)

Projeto Flask completo para hospedar no Railway, puxando notícias do WordPress via REST API e oferecendo admin para anúncios/métricas/configs.

## Rodar local
```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
flask --app wsgi.py init-db
flask --app wsgi.py create-admin admin@admin.com senha123
flask --app wsgi.py sync-wp
flask --app wsgi.py run --debug
```

## Railway
- Crie um Postgres no Railway
- Sete `DATABASE_URL`, `SECRET_KEY`, `WP_BASE_URL`
- Deploy apontando para este repo
- O comando de start já está no Procfile

## Admin
- /admin
- Gerencia slots de anúncios, embed AO VIVO, e vê métricas (pageviews)
