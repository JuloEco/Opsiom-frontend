"""
Modèles de données — comptes Opsiom et quota gratuit.

Un compte a droit à un nombre de messages gratuits par jour
(FREE_DAILY_QUOTA, défini dans app.py). Le compteur se remet à zéro tout
seul au changement de date, pas besoin de tâche planifiée.
"""
from datetime import date, datetime

from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    # --- Quota gratuit ---------------------------------------------------
    messages_used_today = db.Column(db.Integer, default=0, nullable=False)
    quota_date = db.Column(db.Date, default=date.today, nullable=False)

    # --- Mot de passe ------------------------------------------------------
    def set_password(self, raw_password):
        self.password_hash = generate_password_hash(raw_password)

    def check_password(self, raw_password):
        return check_password_hash(self.password_hash, raw_password)

    # --- Quota ---------------------------------------------------------
    def _reset_quota_if_needed(self):
        """Remet le compteur à zéro si on a changé de jour depuis le dernier appel."""
        if self.quota_date != date.today():
            self.quota_date = date.today()
            self.messages_used_today = 0

    def quota_status(self, daily_limit):
        self._reset_quota_if_needed()
        return {
            "used": self.messages_used_today,
            "limit": daily_limit,
            "remaining": max(0, daily_limit - self.messages_used_today),
        }

    def can_send_message(self, daily_limit):
        self._reset_quota_if_needed()
        return self.messages_used_today < daily_limit

    def register_message_sent(self):
        """À appeler une fois la réponse obtenue avec succès (pas avant),
        pour ne jamais décompter un message qui a échoué côté serveur."""
        self._reset_quota_if_needed()
        self.messages_used_today += 1
        db.session.commit()

    def __repr__(self):
        return f"<User {self.username}>"
