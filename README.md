# Opsiom — interface front-end (Render)

Interface de chat indépendante, qui appelle l'API `/api/chat` du Space
Hugging Face `JuloEco/Opsiom` en coulisses. Aucune dépendance à Gradio :
tout le rendu (thème violet/rose, sidebar, bulles de conversation) est du
HTML/CSS/JS que tu contrôles entièrement.

## Pourquoi Render (et pas Vercel / PythonAnywhere)

- **PythonAnywhere (gratuit)** : les connexions sortantes sont limitées à
  une liste blanche de domaines. `hf.space` n'y figure pas par défaut —
  il faudrait payer ou demander un ajout manuel. Écarté pour ce cas d'usage.
- **Vercel** : pensé pour du serverless à exécution courte (10s de timeout
  par défaut). Un cold start ZeroGPU peut largement dépasser ça → requêtes
  qui échouent aléatoirement.
- **Render** : process Flask persistant, pas de restriction réseau, timeout
  configurable — le bon choix pour attendre une réponse potentiellement
  lente sans se faire couper par la plateforme elle-même.

## Déploiement

1. Pousse ce dossier sur un repo GitHub.
2. Sur [render.com](https://render.com) → **New > Web Service** → connecte
   le repo (ou utilise `render.yaml` inclus pour un déploiement "Blueprint"
   en un clic).
3. Si tu ne passes pas par `render.yaml`, configure manuellement :
   - **Build Command** : `pip install -r requirements.txt`
   - **Start Command** : `gunicorn app:app --timeout 120`

   ⚠️ Le `--timeout 120` n'est pas cosmétique : par défaut, gunicorn tue un
   worker qui met plus de 30s à répondre. Comme un cold start ZeroGPU peut
   dépasser ça, un timeout gunicorn trop court fera planter la requête
   *avant* même que notre propre gestion d'erreur (dans `app.py`,
   `REQUEST_TIMEOUT`) ait eu la main.

4. Variables d'environnement (**Environment** dans le dashboard Render) :
   - `OPSIOM_API_URL` = `https://juloeco-opsiom.hf.space/api`
   - `OPSIOM_API_KEY` = (si tu as ajouté une auth Bearer côté Space — voir
     plus bas)
   - `OPSIOM_TIMEOUT` = `90` (optionnel, défaut déjà à 90s)

## Sécuriser `/api/chat` côté Space (recommandé avant mise en ligne publique)

Sans ça, n'importe qui trouvant l'URL du Space peut taper sur `/api/chat`
et consommer ton quota ZeroGPU (5 min/jour en gratuit). Dans `app.py` du
Space (`_build_flask_api`), ajoute avant chaque route sensible :

```python
import os

OPSIOM_SHARED_SECRET = os.environ.get("OPSIOM_SHARED_SECRET", "")

def _check_auth():
    if not OPSIOM_SHARED_SECRET:
        return True  # pas de secret configuré = pas de vérification (dev only)
    return request.headers.get("Authorization") == f"Bearer {OPSIOM_SHARED_SECRET}"

@flask_app.post("/chat")
def chat_endpoint():
    if not _check_auth():
        return jsonify({"error": "Non autorisé."}), 401
    ...
```

Puis ajoute `OPSIOM_SHARED_SECRET` dans les **Secrets** du Space (même
valeur que `OPSIOM_API_KEY` côté Render).

## Développement local

```bash
pip install -r requirements.txt
export OPSIOM_API_URL=https://juloeco-opsiom.hf.space/api
python app.py
```

Puis ouvre `http://localhost:5000`.

## Logs

Toutes les erreurs (timeout, connexion, HTTP, JSON invalide, imprévue) sont
loggées côté serveur avec `logging` — visibles dans l'onglet **Logs** de
Render en production, dans la console en local.
