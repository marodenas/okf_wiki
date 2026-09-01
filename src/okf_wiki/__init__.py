"""okf_wiki — soltar documentos → bundle OKF, con Claude como motor de síntesis."""

# Única fuente de la versión que el CLI firma en `generated.by` (ver `_producer()`).
# Debe coincidir con `project.version` de pyproject.toml; `test_pin_and_docs.py`
# falla si se desincronizan.
__version__ = "0.2.0"
