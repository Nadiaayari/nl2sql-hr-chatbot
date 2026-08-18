"""
Contrôle d'accès par rôle (RBAC) pour le chatbot RH & Santé.

Rôles :
  ADMIN / RH_COMPLET → accès total
  DIRECTION / MANAGER → ses propres infos + statistiques globales + infos générales
  EMPLOYE             → ses propres données uniquement
"""

import re
from typing import Dict, List, Optional, Tuple

ROLE_FULL_ACCESS = frozenset({"ADMIN", "RH_COMPLET"})
ROLE_HYBRID = frozenset({"MANAGER", "DIRECTION"})
ROLE_SELF_ONLY = frozenset({"EMPLOYE"})

# Colonnes identifiant un employé individuellement
PII_COLUMNS = frozenset({
    "NOM_PERS", "PREN_PERS", "MAT_PERS", "DOCTEUR",
    "DAT_NAISS", "DAT_NAI", "GRP_SANG", "HANDICAP", "NBRE_ENF",
    "TOT_NET", "TOT_HONOR", "MNT_REMBOURSE", "NUM_SOINS",
})

# Intentions de requête
INTENT_SELF = "self"
INTENT_AGGREGATE = "aggregate"
INTENT_GENERAL = "general"
INTENT_OTHER = "other_individual"
INTENT_UNKNOWN = "unknown"

_SELF_PATTERNS = [
    r"\b(mes|mon|ma|moi|mes propres|ma propre)\b",
    r"\b(my|mine|my own)\b",
    r"\bmes (bulletins?|remboursements?|soins?|informations?|données?)\b",
    r"\bmon (grade|service|matricule|bulletin|remboursement)\b",
]

_AGGREGATE_PATTERNS = [
    r"\b(combien|total|totaux|moyenne|statistique|statistiques|effectif|effectifs)\b",
    r"\b(répartition|repartition|comparaison globale)\b",
    r"\bpar service\b",
    r"\btop\s+\d+\b",
    r"\b(le|la) plus\b.{0,40}\b(service|remboursement|bulletin|employé|employe)\b",
    r"\b(count|sum|average|statistics|total|how many)\b",
    r"\bemployés?\s+(actifs?|inactifs?)\s+(au total|par service|dans l['']entreprise)\b",
    r"\bdépensé|depense|dépense\b.{0,30}\b(remboursement|soin)\b",
]

_GENERAL_PATTERNS = [
    r"\b(liste des|quels sont les|quelles sont les)\s+(services?|grades?|catégories?|groupes sanguins?)\b",
    r"\binformations?\s+générales?\b",
    r"\bstructure (de l['']entreprise|des services)\b",
    r"\bquels services\b",
    r"\bquels grades\b",
    r"\bgroupes sanguins\b",
]

_OTHER_INDIVIDUAL_PATTERNS = [
    r"\b(nom|prénom|prenom) (de |du |d[''])(?!moi|mes)\w+",
    r"\b(bulletin|remboursement)s?\s+(de |du |d[''])(?!moi|mes|mon|ma)\w+",
    r"\b(liste|donne|affiche|montre).{0,30}(noms?|prénoms?|prenoms?|matricules?)\b",
    r"\bdonnées? (de |du |d[''])(l[''])?employ",
    r"\binfos? (de |du |d[''])(l[''])?employ",
    r"\bqui est\b",
    r"\bemployé\s+[A-ZÀ-Ü]",
    r"\b(tous les|toutes les|liste des|chaque|autres?)\s+employ",
    r"\bemployés?\s+(de la|de l'|du)\s+(société|entreprise|boîte|boite)\b",
    r"\bautres?\s+services?\b",
    r"\bmatricule\s+['\"]?\w+",
]

_EMPLOYE_FORBIDDEN = [
    r"\b(tous les|toutes les|liste des|chaque|autres?)\s+employ",
    r"\bemployés?\s+(de la|de l'|du)\s+(société|entreprise|boîte|boite)",
    r"\bqui est\b",
    r"\bnom (de |du |d[''])?(l[''])?employ",
    r"\bprénom (de |du |d[''])?(l[''])?employ",
    r"\bprenom (de |du |d[''])?(l[''])?employ",
    r"\bcombien d['']employés?\s+(au total|dans l['']entreprise|dans la société|par service)",
    r"\btotal.{0,20}(remboursement|bulletin|soin)\b",
    r"\bstatistique",
    r"\bpar service\b",
    r"\b(liste des|quels sont les)\s+(services?|grades?)\b",
]


def _role(user_context: Optional[dict]) -> str:
    if not user_context:
        return "EMPLOYE"
    return (user_context.get("role") or "EMPLOYE").upper()


def has_full_access(role: str) -> bool:
    return role in ROLE_FULL_ACCESS


def classifier_intention_rbac(question: str) -> str:
    """Classifie l'intention : self, aggregate, general, other_individual, unknown."""
    q = question.strip()
    if any(re.search(p, q, re.IGNORECASE) for p in _SELF_PATTERNS):
        return INTENT_SELF
    if any(re.search(p, q, re.IGNORECASE) for p in _OTHER_INDIVIDUAL_PATTERNS):
        return INTENT_OTHER
    if any(re.search(p, q, re.IGNORECASE) for p in _AGGREGATE_PATTERNS):
        return INTENT_AGGREGATE
    if any(re.search(p, q, re.IGNORECASE) for p in _GENERAL_PATTERNS):
        return INTENT_GENERAL
    return INTENT_UNKNOWN


def valider_question_rbac(
    question: str, user_context: Optional[dict]
) -> Tuple[bool, Optional[str]]:
    """Bloque les questions hors périmètre avant génération SQL."""
    if not user_context:
        return True, None

    role = _role(user_context)
    mat_pers = user_context.get("mat_pers", "")
    q = question.strip()
    intent = classifier_intention_rbac(q)

    if role in ROLE_FULL_ACCESS:
        return True, None

    if role in ROLE_SELF_ONLY:
        if intent in (INTENT_AGGREGATE, INTENT_GENERAL):
            return False, (
                "Accès refusé : en tant qu'employé, vous ne pouvez consulter "
                "que vos propres informations personnelles."
            )
        if intent == INTENT_OTHER:
            return False, (
                "Accès refusé : en tant qu'employé, vous ne pouvez consulter "
                "que vos propres informations personnelles."
            )
        for pat in _EMPLOYE_FORBIDDEN:
            if re.search(pat, q, re.IGNORECASE):
                return False, (
                    "Accès refusé : en tant qu'employé, vous ne pouvez consulter "
                    "que vos propres informations personnelles."
                )
        for m in re.findall(r"\b(?:EMP|MAT)?\d{3,}\b", q, re.IGNORECASE):
            if m.upper().replace("EMP", "") != mat_pers.upper().replace("EMP", "") and m != mat_pers:
                return False, (
                    f"Accès refusé : vous ne pouvez consulter que vos propres données "
                    f"(matricule {mat_pers})."
                )

    elif role in ROLE_HYBRID:
        if intent == INTENT_OTHER:
            return False, (
                "Accès refusé : vous ne pouvez consulter que vos propres informations, "
                "les statistiques globales et les informations générales. "
                "Les données individuelles des autres employés ne sont pas accessibles."
            )
        for m in re.findall(r"\b(?:EMP|MAT)?\d{3,}\b", q, re.IGNORECASE):
            normalized = m.upper().replace("EMP", "")
            own = mat_pers.upper().replace("EMP", "")
            if normalized != own and m != mat_pers and intent != INTENT_AGGREGATE:
                return False, (
                    f"Accès refusé : vous ne pouvez consulter que vos propres données "
                    f"(matricule {mat_pers}), les statistiques globales et les informations générales."
                )

    return True, None


def build_rbac_context(user_context: Optional[dict], question: str = "") -> str:
    """Instructions RBAC injectées dans le prompt SQL selon le rôle connecté."""
    if not user_context:
        return ""

    role = _role(user_context)
    mat_pers = user_context.get("mat_pers", "")
    intent = classifier_intention_rbac(question) if question else INTENT_UNKNOWN

    if role in ROLE_FULL_ACCESS:
        return (
            f"\n\n=== ACCÈS COMPLET ({role}) ===\n"
            f"L'utilisateur a un accès total à toutes les données employés.\n"
            f"Aucune restriction de filtrage requise.\n"
        )

    if role in ROLE_SELF_ONLY:
        return (
            f"\n\n=== RESTRICTION D'ACCÈS — EMPLOYÉ (OBLIGATOIRE) ===\n"
            f"Matricule connecté : {mat_pers}\n"
            f"RÈGLES STRICTES :\n"
            f"- TOUTE requête DOIT contenir WHERE P.MAT_PERS = '{mat_pers}' "
            f"(ou B.MAT_PERS = '{mat_pers}').\n"
            f"- INTERDIT : données d'autres employés, listes globales, statistiques, comparaisons.\n"
            f"- AUTORISÉ UNIQUEMENT : ses propres bulletins, remboursements, grade, service, infos personnelles.\n"
        )

    if role in ROLE_HYBRID:
        base = (
            f"\n\n=== RESTRICTION D'ACCÈS — {role} (OBLIGATOIRE) ===\n"
            f"Matricule connecté : {mat_pers}\n"
            f"L'utilisateur peut consulter UNIQUEMENT trois types de données :\n"
            f"  1. SES PROPRES INFORMATIONS (matricule {mat_pers})\n"
            f"  2. STATISTIQUES GLOBALES (COUNT, SUM, AVG, GROUP BY — sans identité individuelle)\n"
            f"  3. INFORMATIONS GÉNÉRALES (services, grades, groupes sanguins — tables de référence)\n"
            f"INTERDIT : données individuelles d'autres employés (noms, prénoms, bulletins d'autrui).\n"
        )
        if intent == INTENT_SELF:
            base += (
                f"\n→ Requête personnelle détectée : filtrer WHERE P.MAT_PERS = '{mat_pers}'.\n"
            )
        elif intent == INTENT_AGGREGATE:
            base += (
                "\n→ Statistiques globales détectées : utiliser COUNT/SUM/AVG avec GROUP BY. "
                "Pas de NOM_PERS, PREN_PERS, MAT_PERS individuel dans le SELECT.\n"
            )
        elif intent == INTENT_GENERAL:
            base += (
                "\n→ Informations générales détectées : interroger SERVICE, GRADE, GROUPE_SANGUIN. "
                "Pas de données personnelles individuelles.\n"
            )
        return base

    return ""


def build_rbac_nl_context(user_context: Optional[dict], question: str = "") -> str:
    """Instructions RBAC pour la réponse en langage naturel."""
    if not user_context:
        return ""

    role = _role(user_context)
    mat_pers = user_context.get("mat_pers", "")

    if role in ROLE_FULL_ACCESS:
        return ""

    if role in ROLE_SELF_ONLY:
        return (
            f"\nIMPORTANT : l'utilisateur est un EMPLOYÉ (matricule {mat_pers}). "
            f"Ne mentionne QUE ses propres données. Ne cite jamais d'autres employés, "
            f"ni de statistiques globales.\n"
        )

    if role in ROLE_HYBRID:
        intent = classifier_intention_rbac(question) if question else INTENT_UNKNOWN
        if intent == INTENT_SELF:
            return (
                f"\nIMPORTANT : l'utilisateur est {role} (matricule {mat_pers}). "
                f"Il consulte SES PROPRES informations. Ne mentionne que ses données personnelles.\n"
            )
        if intent in (INTENT_AGGREGATE, INTENT_GENERAL):
            return (
                f"\nIMPORTANT : l'utilisateur est {role}. "
                f"Il consulte des statistiques globales ou informations générales. "
                f"Présente des totaux, moyennes, effectifs ou listes de référence. "
                f"Ne cite JAMAIS de noms, prénoms ou matricules individuels d'autres employés.\n"
            )
        return (
            f"\nIMPORTANT : l'utilisateur est {role} (matricule {mat_pers}). "
            f"Ne présente que : ses propres infos, des statistiques agrégées, ou des infos générales. "
            f"Ne cite jamais de données individuelles d'autres employés.\n"
        )

    return ""


def _sql_has_individual_pii(sql: str) -> bool:
    """True si le SQL retourne des données individuelles identifiables."""
    sql_upper = sql.upper()

    if "GROUP BY" in sql_upper:
        select_part = sql_upper.split("FROM")[0] if "FROM" in sql_upper else sql_upper
        agg_funcs = ("COUNT(", "SUM(", "AVG(", "MIN(", "MAX(")
        if any(f in select_part for f in agg_funcs):
            pii_in_select = any(c in select_part for c in PII_COLUMNS)
            if not pii_in_select:
                return False

    for col in ("NOM_PERS", "PREN_PERS", "DOCTEUR"):
        if re.search(rf"\b{col}\b", sql_upper):
            if "GROUP BY" not in sql_upper:
                return True

    if re.search(r"\bMAT_PERS\b", sql_upper):
        if "COUNT(" not in sql_upper and "GROUP BY" not in sql_upper:
            return True

    if re.search(r"\bNUM_SOINS\b", sql_upper):
        if not any(f in sql_upper for f in ("COUNT(", "SUM(", "AVG(", "GROUP BY")):
            return True

    return False


def _sql_is_aggregate(sql: str) -> bool:
    sql_upper = sql.upper()
    return any(f in sql_upper for f in ("COUNT(", "SUM(", "AVG(", "GROUP BY"))


def _sql_is_general_reference(sql: str) -> bool:
    sql_upper = sql.upper()
    ref_tables = ("SERVICE", "GRADE", "GROUPE_SANGUIN", "CATEGORIE")
    return any(t in sql_upper for t in ref_tables) and "PERSONNEL" not in sql_upper


def _sql_has_mat_filter(sql: str, mat_pers: str) -> bool:
    return bool(re.search(
        rf"MAT_PERS\s*=\s*['\"]?{re.escape(mat_pers)}['\"]?",
        sql, re.IGNORECASE
    ))


def valider_rbac_sql(
    sql: str, user_context: Optional[dict], question: str = ""
) -> Tuple[bool, Optional[str]]:
    """Vérification post-génération : bloque l'exécution si le SQL ne respecte pas le RBAC."""
    if not user_context:
        return True, None

    role = _role(user_context)
    mat_pers = user_context.get("mat_pers", "")
    intent = classifier_intention_rbac(question) if question else INTENT_UNKNOWN

    if role in ROLE_FULL_ACCESS:
        return True, None

    if role in ROLE_SELF_ONLY and mat_pers:
        if not _sql_has_mat_filter(sql, mat_pers):
            return False, (
                f"Accès refusé : en tant qu'employé, vous ne pouvez consulter "
                f"que vos propres données (MAT_PERS = {mat_pers})."
            )

    elif role in ROLE_HYBRID:
        if intent == INTENT_SELF or (intent == INTENT_UNKNOWN and _sql_has_mat_filter(sql, mat_pers)):
            if not _sql_has_mat_filter(sql, mat_pers):
                return False, (
                    f"Accès refusé : pour vos informations personnelles, "
                    f"seules vos propres données sont accessibles (MAT_PERS = {mat_pers})."
                )
        elif intent == INTENT_AGGREGATE or (intent == INTENT_UNKNOWN and _sql_is_aggregate(sql)):
            if _sql_has_individual_pii(sql):
                return False, (
                    "Accès refusé : les statistiques globales ne doivent pas contenir "
                    "de données individuelles identifiables. Utilisez COUNT, SUM, AVG avec GROUP BY."
                )
        elif intent == INTENT_GENERAL or (intent == INTENT_UNKNOWN and _sql_is_general_reference(sql)):
            if _sql_has_individual_pii(sql):
                return False, (
                    "Accès refusé : les informations générales ne doivent pas "
                    "contenir de données personnelles individuelles."
                )
        elif intent == INTENT_OTHER:
            return False, (
                "Accès refusé : vous ne pouvez consulter que vos propres informations, "
                "les statistiques globales et les informations générales."
            )
        elif intent == INTENT_UNKNOWN:
            if _sql_has_individual_pii(sql) and not _sql_has_mat_filter(sql, mat_pers):
                return False, (
                    "Accès refusé : vous ne pouvez consulter que vos propres informations, "
                    "les statistiques globales et les informations générales."
                )

    return True, None


def filtrer_resultats(
    cols: List[str],
    rows: List,
    user_context: Optional[dict],
    question: str = "",
) -> Tuple[List[str], List, Optional[str]]:
    """
    Filtre les résultats en sortie de base (filet de sécurité post-exécution).
    Retourne (cols, rows, erreur_optionnelle).
    """
    if not user_context or not rows:
        return cols, rows, None

    role = _role(user_context)
    mat_pers = user_context.get("mat_pers", "")
    intent = classifier_intention_rbac(question) if question else INTENT_UNKNOWN

    if role in ROLE_FULL_ACCESS:
        return cols, rows, None

    cols_upper = [c.upper() for c in cols]

    if role in ROLE_HYBRID and intent in (INTENT_AGGREGATE, INTENT_GENERAL):
        pii_present = [c for c in cols_upper if c in PII_COLUMNS]
        if pii_present and intent == INTENT_AGGREGATE:
            return cols, [], (
                "Accès refusé : ces résultats contiennent des données individuelles. "
                "Reformulez en statistiques agrégées "
                "(ex: « combien d'employés par service », « total remboursements en 2024 »)."
            )

    if role in ROLE_SELF_ONLY and mat_pers and "MAT_PERS" in cols_upper:
        idx = cols_upper.index("MAT_PERS")
        filtered = [r for r in rows if str(r[idx]).strip() == mat_pers]
        if not filtered and rows:
            return cols, [], (
                f"Accès refusé : ces données ne vous appartiennent pas "
                f"(matricule {mat_pers})."
            )
        return cols, filtered, None

    if role in ROLE_HYBRID and intent == INTENT_SELF and mat_pers and "MAT_PERS" in cols_upper:
        idx = cols_upper.index("MAT_PERS")
        filtered = [r for r in rows if str(r[idx]).strip() == mat_pers]
        if not filtered and rows:
            return cols, [], (
                f"Accès refusé : ces données ne vous appartiennent pas "
                f"(matricule {mat_pers})."
            )
        return cols, filtered, None

    if role in ROLE_HYBRID and intent in (INTENT_AGGREGATE, INTENT_GENERAL, INTENT_UNKNOWN):
        pii_present = [c for c in cols_upper if c in ("NOM_PERS", "PREN_PERS", "DOCTEUR")]
        if pii_present and "MAT_PERS" in cols_upper:
            return cols, [], (
                "Accès refusé : ces résultats contiennent des données individuelles d'autres employés. "
                "Consultez vos propres informations, des statistiques globales ou des infos générales."
            )

    return cols, rows, None
