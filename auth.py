"""
Blueprint d'authentification pour Opsiom -- inscription / connexion / deconnexion.

Compte 100% local a cette app (stocke dans la base definie par DATABASE_URL,
cf. models.py) : ce n'est pas Octix, juste un login simple protegeant l'acces
au chat et au quota gratuit.

Routes exposees (endpoints "auth.login", "auth.register", "auth.logout") :
  GET/POST /login     -> connexion (pseudo ou e-mail + mot de passe)
  GET/POST /register  -> creation de compte
  GET      /logout    -> deconnexion
"""
import re

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from models import User, db

auth_bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("login.html")

    identifier = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""

    if not identifier or not password:
        flash("Renseigne ton pseudo (ou e-mail) et ton mot de passe.", "error")
        return render_template("login.html", username=identifier), 400

    user = User.query.filter(
        (User.username == identifier) | (User.email == identifier.lower())
    ).first()

    if user is None or not user.check_password(password):
        flash("Identifiants incorrects.", "error")
        return render_template("login.html", username=identifier), 401

    login_user(user, remember=True)
    next_url = request.args.get("next")
    # Evite les redirections externes (open redirect) : on n'accepte que les
    # chemins internes qui commencent par "/".
    if not next_url or not next_url.startswith("/"):
        next_url = url_for("index")
    return redirect(next_url)


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("index"))

    if request.method == "GET":
        return render_template("register.html")

    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip().lower()
    password = request.form.get("password") or ""
    confirm = request.form.get("confirm") or ""

    form_values = {"username": username, "email": email}

    if not (3 <= len(username) <= 32):
        flash("Le pseudo doit faire entre 3 et 32 caracteres.", "error")
        return render_template("register.html", **form_values), 400

    if not EMAIL_RE.match(email):
        flash("Adresse e-mail invalide.", "error")
        return render_template("register.html", **form_values), 400

    if len(password) < 8:
        flash("Le mot de passe doit faire au moins 8 caracteres.", "error")
        return render_template("register.html", **form_values), 400

    if password != confirm:
        flash("Les deux mots de passe ne correspondent pas.", "error")
        return render_template("register.html", **form_values), 400

    if User.query.filter_by(username=username).first():
        flash("Ce pseudo est deja pris.", "error")
        return render_template("register.html", **form_values), 409

    if User.query.filter_by(email=email).first():
        flash("Un compte existe deja avec cet e-mail.", "error")
        return render_template("register.html", **form_values), 409

    user = User(username=username, email=email)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()

    login_user(user, remember=True)
    return redirect(url_for("index"))


@auth_bp.get("/logout")
@login_required
def logout():
    logout_user()
    flash("Tu as ete deconnecte.", "info")
    return redirect(url_for("auth.login"))
