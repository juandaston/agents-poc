# Semantic layer (fallback bundle)

**Fuente de verdad:** `siigo-api/core/semantic/`

Esta copia permite ejecutar agents-poc sin montar el volumen de siigo-api.
Tras editar el catálogo en siigo-api, sincroniza:

```bash
cp ../siigo-api/core/semantic/catalog.yaml ../siigo-api/core/semantic/schema_context.md .
```

Docker Compose monta `../siigo-api/core/semantic` en `/app/semantic-live` (prioridad sobre `/app/semantic` embebido).
Si el mount no existe (p. ej. EC2 sin siigo-api al lado), la imagen usa `/app/semantic` automáticamente.
