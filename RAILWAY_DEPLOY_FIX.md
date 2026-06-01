# Deploy Railway - correção mise/Python

Este projeto inclui `mise.toml` para desabilitar a verificação de GitHub artifact attestations do Python no Railway/Railpack.

Motivo: alguns builds com `runtime.txt` usando `python-3.11.9` podem falhar antes do `pip install` com:

`No GitHub artifact attestations found for python@3.11.9`

Correção aplicada:

```toml
[settings]
python.github_attestations = false
```

Alternativa pelo painel Railway:

```env
MISE_PYTHON_GITHUB_ATTESTATIONS=false
```
