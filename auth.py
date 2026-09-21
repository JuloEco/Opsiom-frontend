"""
Authentification Opsiom -- deleguee a Octix.

Opsiom ne cree JAMAIS de compte lui-meme : la creation de compte se fait
uniquement sur le portail Octix (Octix_NEW_ACCOUNT_V2, /inscription).
Ce module se contente de :
  1. Verifier les identifiants aupres de l'API Octix (POST {OCTIX_URL}/login),
     exactement comme le fait compte_routes.py du portail Octix.
  2. Recuperer le profil (GET {OCTIX_URL}/account/me) pour connaitre l'e-mail.
  3. Maintenir une "ombre" locale (table users, cf. models.py) qui sert
     uniquement a suivre le quota de messages gratuits par compte -- aucun
     mot de passe n'y est jamais stocke.

Variables d'environnement :
  OCTIX_URL         - URL de l'API Octix (le meme backend que celui utilise
                       par le portail Octix), ex: https://octix-api.exemple.com
  OCTIX_PORTAL_URL  - URL publique du portail Octix (Octix_NEW_ACCOUNT_V2),
                       utilisee pour rediriger "Creer un compte" et
                       "Mot de passe oublie" vers /inscription et
                       /mot-de-passe-oublie.

Routes exposees (endpoints "auth.login", "auth.register", "auth.logout") :
  GET/POST /login     -> connexion via l'API Octix
  GET      /register  -> redirige vers {OCTIX_PORTAL_URL}/inscription
  GET      /logout    -> deconnexion locale (ne touche pas a la session Octix)
"""
import os

import requests
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from models import User, db

auth_bp = Blueprint("auth", __name__)

OCTIX_URL = os.environ.get("OCTIX_URL", "http://localhost:5050").rstrip("/")
OCTIX_PORTAL_URL = os.environ.get("OCTIX_PORTAL_URL", "http://localhost:5051").rstrip("/")


def _json_or_empty(response):
    try:
        return response.json() if response.content else {}
    except ValueError:
        return {}


def _octix_login(username, password):
    """POST {OCTIX_URL}/login -- identique a compte_routes.py du portail Octix.
    Renvoie (True, {"token": ..., "username": ...}) ou (False, "message d'erreur")."""
    try:
        response = requests.post(
            f"{OCTIX_URL}/login",
            json={"username": username, "password": password},
            timeout=8,
        )
        data = _json_or_empty(response)
        if response.status_code == 200:
            return True, data
        return False, data.get("error", "Pseudo ou mot de passe incorrect.")
    except requests.exceptions.Timeout:
        return False, "Octix met trop de temps à répondre. Réessaie dans un instant."
    except requests.exceptions.RequestException:
        return False, "Octix est injoignable pour le moment. Réessaie dans un instant."


def _octix_profile(token):
    """GET {OCTIX_URL}/account/me avec le token recu au login -- pour recuperer
    l'e-mail (facultatif, purement informatif cote Opsiom)."""
    try:
        response = requests.get(
            f"{OCTIX_URL}/account/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if response.status_code == 200:
            return _json_or_empty(response)
    except requests.exceptions.RequestException:
        pass
    return {}


def _sync_local_user(username, email=None):
    """Cree ou met a jour l'ombre locale (quota) associee a ce compte Octix.
    Ne stocke jamais de mot de passe : l'authentification reste entierement
    du ressort d'Octix."""
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username, email=email or f"{username}@octix.local")
        db.session.add(user)
    elif email and user.email != email:
        user.email = email
    db.session.commit()
    return user


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html")

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("Entre ton pseudo Octix et ton mot de passe.", "error")
        return render_template("login.html", username=username), 400

    ok, result = _octix_login(username, password)
    if not ok:
        flash(result, "error")
        return render_template("login.html", username=username), 401

    token = result.get("token")
    octix_username = result.get("username", username)
    profile = _octix_profile(token) if token else {}

    user = _sync_local_user(octix_username, profile.get("email"))
    login_user(user, remember=True)

    next_url = request.args.get("next")
    # Evite les redirections externes (open redirect) : uniquement des
    # chemins internes qui commencent par "/".
    if not next_url or not next_url.startswith("/"):
        next_url = url_for("index")
    return redirect(next_url)


@auth_bp.get("/register")
def register():
    """Opsiom ne cree pas de compte : on renvoie vers le portail Octix,
    seul point de creation de compte de tout l'ecosysteme."""
    return redirect(f"{OCTIX_PORTAL_URL}/inscription")


@auth_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Tu as été déconnecté.", "info")
    return redirect(url_for("auth.login"))
