# Werkruimte-laag (in aanbouw)

Hier komt de laag die van de engine een bruikbare assistent maakt:

- **Werkruimtes** per project, elk met eigen documenten en gesprekken
- **Documenten erin** (tekst, markdown, PDF, Word, webpagina's, GitHub-repo's) met
  **bronvermelding** in het antwoord
- **Vectordatabase** op SQLite met cosine-zoeken; embeddings via de eigen engine
  (standaard `nomic-embed-text-v1.5`)
- **Model-routing**: regels die per vraag het beste model kiezen
- **Embed-widget** voor je eigen websites en een **REST-API** met sleutels per
  werkruimte

Zelfde uitgangspunten als de engine: pure Python, alleen de standaardbibliotheek,
en eigen code.
