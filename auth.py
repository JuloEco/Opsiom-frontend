"""
Authentification Opsiom -- deleguee a Octix.

Meme methode que les autres apps de l'ecosysteme (LearnCode/Omnia) :
une session Flask "brute" (pas de Flask-Login, pas de mot de passe stocke
localement). Opsiom ne cree JAMAIS de compte lui-meme : la creation de
compte se fait uniquement sur le portail Octix (Octix_NEW_ACCOUNT_V2,
/inscription).

Ce module :
  1. Verifie les identifiants aupres de l'API Octix (POST {OCTIX_URL}/login),
     exactement comme octix_login() dans LearnCode.
  2. Stocke le pseudo et le token recus dans session["user"] /
     session["octix_token"].
  3. Fournit login_required, un decorateur qui bloque l'acces si
     session["user"] est absent (redirection HTML, ou JSON pour les routes
     appelees en fetch() par le JS : /status, /models, /chat).
  4. Maintient une "ombre" locale (table users, cf. models.py) qui sert
     uniquement a suivre le quota de messages gratuits par compte.

Variables d'environnement :
  OCTIX_URL         - URL de l'API Octix (le meme backend que celui utilise
                       par le portail Octix et par LearnCode).
  OCTIX_PORTAL_URL  - URL publique du portail Octix (Octix_NEW_ACCOUNT_V2),
                       utilisee pour le lien "Creer un compte" (/inscription).
"""
import os
from functools import wraps

import requests
from flask import Blueprint, flash, jsonify, redirect, render_template, request, session, url_for

from models import User, db

auth_bp = Blueprint("auth", __name__)

OCTIX_URL = os.environ.get("OCTIX_URL", "http://localhost:5050")
OCTIX_PORTAL_URL = os.environ.get("OCTIX_PORTAL_URL", "http://localhost:5051")

# Routes appelees en fetch() par le JS : une redirection HTML les ferait
# echouer silencieusement (JSON attendu), donc elles recoivent du JSON 401
# plutot qu'une redirection classique quand la session est absente/expiree.
_JSON_ROUTES = ("/status", "/models", "/chat")


def octix_login(username, password):
    """Verifie les identifiants aupres d'Octix. Retourne (ok, token_ou_message)."""
    try:
        r = requests.post(f"{OCTIX_URL}/login", json={"username": username, "password": password}, timeout=5)
        if r.status_code == 200:
            return True, r.json()["token"]
        return False, "Pseudo ou mot de passe incorrect."
    except requests.exceptions.RequestException:
        return False, "Le service Octix est injoignable. Réessaie plus tard."


def login_required(view_func):
    """Bloque l'acces si personne n'est connecte (session["user"] absent)."""
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            if request.path in _JSON_ROUTES:
                return jsonify({"error": "Session expirée, reconnecte-toi.", "auth_required": True}), 401
            return redirect(url_for("auth.login", next=request.path))
        return view_func(*args, **kwargs)
    return wrapped


def current_username():
    """Pseudo Octix de la personne connectee, ou None."""
    return session.get("user")


def current_user_record():
    """Renvoie (en la creant si besoin) l'ombre locale de quota associee au
    compte actuellement en session. None si personne n'est connecte."""
    username = session.get("user")
    if not username:
        return None
    user = User.query.filter_by(username=username).first()
    if user is None:
        user = User(username=username)
        db.session.add(user)
        db.session.commit()
    return user


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if "user" in session:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html", octix_portal_url=OCTIX_PORTAL_URL)

    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not username or not password:
        flash("Entre ton pseudo Octix et ton mot de passe.", "error")
        return render_template("login.html", username=username, octix_portal_url=OCTIX_PORTAL_URL), 400

    ok, result = octix_login(username, password)
    if not ok:
        flash(result, "error")
        return render_template("login.html", username=username, octix_portal_url=OCTIX_PORTAL_URL), 401

    session["user"] = username
    session["octix_token"] = result
    current_user_record()  # crée la ligne de quota locale si c'est une première connexion

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
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
