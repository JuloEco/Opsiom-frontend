"""
plans.py — Définition centrale des forfaits Opsiom et des missions qui les
débloquent.

TOUT est ici pour rester facile à modifier : quotas, modèles autorisés,
missions et seuils. Rien de tout ça n'est dispersé ailleurs dans le code —
app.py et progression.py ne font que LIRE ce module, jamais le contraire.

Rappel important (voir progression.py) : LearnCode, Classroom et OmniaMind
ont chacun leur PROPRE base de données (ce ne sont pas des tables partagées
malgré ce qu'on pensait au départ) ; seule l'authentification est commune,
via Octix. Les missions ci-dessous sont donc calculées en lisant, en lecture
seule, les données déjà existantes dans chacune de ces bases.
"""

import os

# ---------------------------------------------------------------------------
# 0. Modèles & administrateurs
# ---------------------------------------------------------------------------
# Noms affichés dans le sélecteur. Les ids (nano / small / large) sont ceux du
# serveur d'inférence et ne changent pas : seul l'affichage est renommé, ici,
# à un seul endroit. Sert aussi à lister les modèles proposables dans un code
# de promo (voir promo.py).
MODEL_DISPLAY = {
    "nano": {"label": "Opsiom Micro", "params": "25M"},
    "small": {"label": "Opsiom Nano", "params": "45M"},
    "large": {"label": "Opsiom Large", "params": "200M"},
}

# Comptes administrateurs : accès illimité aux modèles et aux tokens, et accès
# à l'espace /admin (création de codes de promo). Comparaison EXACTE avec le
# pseudo Octix (sensible à la casse : "jules" et "Jules" seraient deux comptes
# différents). Surchargeable sans redéployer le code via la variable
# d'environnement ADMIN_USERNAMES (pseudos séparés par des virgules).
ADMIN_USERNAMES = {
    name.strip()
    for name in os.environ.get("ADMIN_USERNAMES", "Jules").split(",")
    if name.strip()
}

# Valeur "restant" renvoyée pour un compte sans limite (le front affiche ∞).
UNLIMITED_REMAINING = 10**9

# ---------------------------------------------------------------------------
# 1. Forfaits — quotas & fonctionnalités
# ---------------------------------------------------------------------------
# Ordre important : du plus bas au plus haut, utilisé pour savoir si un
# forfait "couvre" un autre (ex: Pro donne aussi accès à ce que Plus donne).
PLAN_ORDER = ["free", "plus", "pro"]

PLANS = {
    "free": {
        "label": "Opsiom Free",
        "emoji": "🟢",
        "daily_tokens": 2000,
        "models": ["nano", "small"],
        "context_messages": 6,       # nb de messages d'historique gardés en contexte
        "description": "Forfait de base, obtenu automatiquement.",
    },
    "plus": {
        "label": "Opsiom Plus",
        "emoji": "🔵",
        "daily_tokens": 10_000,
        "models": ["nano", "small", "large"],
        "context_messages": 14,
        "description": "Débloqué en accomplissant des missions dans l'écosystème.",
    },
    "pro": {
        "label": "Opsiom Pro",
        "emoji": "🟣",
        "daily_tokens": 50_000,
        "models": ["nano", "small", "large"],
        "context_messages": 30,
        "priority_inference": True,
        "description": "Forfait avancé, nécessite une progression plus importante.",
    },
}

DEFAULT_PLAN = "free"


def plan_rank(plan_id: str) -> int:
    """Position du forfait dans PLAN_ORDER (0 = free). Inconnu -> 0."""
    try:
        return PLAN_ORDER.index(plan_id)
    except ValueError:
        return 0


def plan_config(plan_id: str) -> dict:
    return PLANS.get(plan_id, PLANS[DEFAULT_PLAN])


# ---------------------------------------------------------------------------
# 2. Missions — une mission = une fonction qui lit un dict de stats brutes
#    (voir progression.py: gather_raw_stats) et renvoie un int (progression
#    actuelle). "target" est le seuil à atteindre.
# ---------------------------------------------------------------------------
# Clés attendues dans raw_stats (produites par progression.py) :
#   learncode.lessons_completed   -> nb de cours/leçons avec une note enregistrée
#   learncode.lessons_passed      -> nb de cours réussis (note >= 50/100)
#   classroom.activities_graded   -> nb de devoirs Classroom notés
#   classroom.activities_passed   -> nb de devoirs Classroom notés >= 10/20
#   omniamind.challenges_passed   -> nb de sessions quiz/match/swipe >= 70%
#   omniamind.study_sessions      -> nb total de sessions d'étude terminées
#   opsiom.active_days            -> nb de jours différents où le compte a discuté avec Opsiom
#   opsiom.conversations          -> nb de conversations Opsiom envoyées (compteur cumulé)

MISSIONS = {
    "plus": [
        {
            "key": "learncode_3_lecons",
            "label": "Terminer 3 leçons LearnCode",
            "stat": "learncode.lessons_completed",
            "target": 3,
        },
        {
            "key": "classroom_1_activite",
            "label": "Réussir une activité Classroom",
            "stat": "classroom.activities_passed",
            "target": 1,
        },
        {
            "key": "omniamind_1_defi",
            "label": "Réussir un défi Omnia Mind",
            "stat": "omniamind.challenges_passed",
            "target": 1,
        },
        {
            "key": "opsiom_3_jours",
            "label": "Utiliser Opsiom 3 jours différents",
            "stat": "opsiom.active_days",
            "target": 3,
        },
    ],
    "pro": [
        {
            "key": "learncode_10_lecons",
            "label": "Terminer 10 leçons LearnCode",
            "stat": "learncode.lessons_completed",
            "target": 10,
        },
        {
            "key": "classroom_5_activites",
            "label": "Réussir 5 activités Classroom",
            "stat": "classroom.activities_passed",
            "target": 5,
        },
        {
            "key": "omniamind_5_defis",
            "label": "Réussir 5 défis Omnia Mind",
            "stat": "omniamind.challenges_passed",
            "target": 5,
        },
        {
            "key": "opsiom_10_jours",
            "label": "Utiliser Opsiom 10 jours différents",
            "stat": "opsiom.active_days",
            "target": 10,
        },
        {
            "key": "opsiom_20_conversations",
            "label": "Envoyer 20 conversations à Opsiom",
            "stat": "opsiom.conversations",
            "target": 20,
        },
    ],
}


def _get_stat(raw_stats: dict, dotted_key: str) -> int:
    ns, _, key = dotted_key.partition(".")
    return int((raw_stats.get(ns) or {}).get(key, 0) or 0)


def evaluate_missions(plan_id: str, raw_stats: dict) -> list[dict]:
    """Renvoie la liste des missions du forfait `plan_id` avec leur
    progression actuelle, prête à afficher."""
    out = []
    for mission in MISSIONS.get(plan_id, []):
        current = _get_stat(raw_stats, mission["stat"])
        out.append({
            **mission,
            "current": min(current, mission["target"]),
            "done": current >= mission["target"],
        })
    return out


def compute_unlocked_plan(raw_stats: dict, lab: bool = False) -> str:
    """Détermine le forfait le plus haut dont TOUTES les missions sont
    remplies. Ne redescend jamais en dessous de 'free'. `lab` n'influe pas
    sur le forfait (Lab est un statut séparé, voir plus bas)."""
    unlocked = "free"
    for plan_id in PLAN_ORDER[1:]:
        missions = evaluate_missions(plan_id, raw_stats)
        if missions and all(m["done"] for m in missions):
            unlocked = plan_id
        else:
            break  # les forfaits sont cumulatifs/progressifs : on s'arrête au premier non atteint
    return unlocked


def next_locked_plan(current_plan: str) -> str | None:
    """Prochain forfait à débloquer après `current_plan`, ou None si déjà au max."""
    idx = plan_rank(current_plan)
    if idx + 1 < len(PLAN_ORDER):
        return PLAN_ORDER[idx + 1]
    return None


# ---------------------------------------------------------------------------
# 3. Opsiom Lab — statut séparé, indépendant des forfaits classiques.
#    Pour l'instant : pas d'avantage concret tant qu'aucun modèle
#    expérimental n'existe, seule l'architecture (colonne User.lab) est
#    préparée. Attribution manuelle uniquement pour le moment (voir
#    scripts d'admin à venir) — aucun critère automatique n'est calculé ici.
# ---------------------------------------------------------------------------
LAB_LABEL = "Opsiom Lab"
LAB_EMOJI = "🧪"
LAB_DESCRIPTION = (
    "Statut bêta-testeur, séparé des forfaits. Donnera accès aux modèles "
    "expérimentaux dès qu'ils existeront."
)
