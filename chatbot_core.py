# chatbot_core.py
"""
Chatbot RH & Santé - Version FIXÉE
- Retry prompt ultra-strict (interdit tout texte avant/après SQL)
- Extraction agressive du SQL depuis réponses LLM bavardes
- Détection et blocage des mots interdits même dans retry
- Pas de metadata dynamique dans retry (prompt trop gros)
"""

import os
import re
import time
import logging
import platform
from typing import Tuple, List, Optional, Dict
from dotenv import load_dotenv
from langsmith import traceable
from langsmith.wrappers import wrap_openai  # pas nécessaire ici (on utilise Groq)
from memory import (
    get_history,
    add_message
)
try:
    load_dotenv()
except:
    pass

try:
    import oracledb
except ImportError:
    raise ImportError("pip install oracledb")

from llm_client import llm_client, MODEL_PRIMARY, MODEL_FAST

from configuration_schema import SCHEMA_PROMPT
from rbac import (
    build_rbac_context,
    build_rbac_nl_context,
    valider_question_rbac,
    valider_rbac_sql,
    filtrer_resultats,
    classifier_intention_rbac,
    INTENT_SELF,
)
from sql_validator import valider_et_corriger

logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s - %(message)s', datefmt='%H:%M:%S')
logger = logging.getLogger(__name__)

# ─── Config ─────────────────────────────────────────────────────────────────
DB_USER = os.getenv("DB_USER", "pfe_chatbot")
DB_PASSWORD = os.getenv("DB_PASSWORD", "pfe2026")
DB_DSN = os.getenv("DB_DSN", "127.0.0.1:1521/XE")
ORACLE_LIB_DIR = os.getenv("ORACLE_LIB_DIR", r"C:\instantclient_23_0" if platform.system() == "Windows" else "/opt/oracle/instantclient")

# ─── Oracle ───────────────────────────────────────────────────────────────────
try:
    oracledb.init_oracle_client(lib_dir=ORACLE_LIB_DIR)
    logger.info("[OK] Oracle Client chargé")
except Exception as e:
    logger.warning(f"[WARN] Oracle init: {e}")

def get_connection():
    return oracledb.connect(user=DB_USER, password=DB_PASSWORD, dsn=DB_DSN)

try:
    c = get_connection(); c.close()
    logger.info("[OK] Connexion Oracle OK")
except Exception as e:
    logger.error(f"[ERR] Connexion Oracle: {e}"); raise

# ─── LLM (Groq + fallback automatique) ───────────────────────────────────────
client = llm_client
MODEL = MODEL_PRIMARY
MAX_TOKENS_PROMPT = 5000

# ─── Classification ───────────────────────────────────────────────────────────
SERVICE_CODES = {
    'IT': 'SERV_IT', 'INFORMATIQUE': 'SERV_IT', 'INFO': 'SERV_IT',
    'RH': 'SERV_RH', 'RESSOURCES HUMAINES': 'SERV_RH', 'RESSOURCE HUMAINE': 'SERV_RH',
    'COMPTABILIT': 'SERV_COMPT', 'COMPTA': 'SERV_COMPT',
    'DIRECTION': 'DIR_GEN', 'DIR_GEN': 'DIR_GEN', 'DIRECTION GENERALE': 'DIR_GEN'
}

GS_CODES = {'O+': 'OP', 'O-': 'OM', 'A+': 'AP', 'A-': 'AM', 'B+': 'BP', 'B-': 'BM', 'AB+': 'ABP', 'AB-': 'ABM'}

def classifier_question(q: str) -> Dict:
    qu = q.upper(); ql = q.lower()
    svc = None
    for k, v in SERVICE_CODES.items():
        if k in qu: svc = v; break
    gs = None
    for k, v in GS_CODES.items():
        if k in q.upper(): gs = v; break
    ym = re.search(r'\b(20\d{2})\b', q)
    year = int(ym.group(1)) if ym else None
    lm = re.search(r'\b(?:TOP\s*(\d+)|(\d+)\s*(?:PREMIERS?|MEILLEURS?))\b', qu)
    limit = int(lm.group(1) or lm.group(2)) if lm else None
    types = {
        'count': bool(re.search(r'\b(COMBien|NOMBRE|COUNT|COMBIEN|NB)\b', qu)),
        'max': bool(re.search(r'\b(PLUS (GRAND|ELEVE|HAUT)|MAXIMUM|LE PLUS)\b', qu)),
        'min': bool(re.search(r'\b(PLUS (PETIT|FAIBLE|BAS)|MINIMUM|LE MOINS)\b', qu)),
        'avg': bool(re.search(r'\b(MOYENNE|AVERAGE|MOY)\b', qu)),
        'top_n': bool(re.search(r'\b(TOP\s*\d+|PREMIERS?\s*\d+|MEILLEURS?\s*\d+)\b', qu)),
        'list': bool(re.search(r'\b(LISTE|AFFICHE|DONNE|QUELS?|QUELLES?)\b', qu)),
        'per_service': bool(re.search(r'\b(CHAQUE|PAR|POUR CHAQUE)\s+SERVICE\b', qu)),
        'time': bool(re.search(r'\b(20\d{2}|MOIS|ANNEE|DATE)\b', qu)),
    }
    return {'types': types, 'svc': svc, 'gs': gs, 'year': year, 'limit': limit, 'q': q}

# ─── Pré-validation ───────────────────────────────────────────────────────────
def pre_validate(q: str) -> Tuple[bool, Optional[str]]:
    ql = q.lower()

    # Bloquer uniquement les questions vraiment trop courtes
    if len(q.split()) < 2:
        return False, "Question trop vague. Ex: 'liste employés service RH'"

    # Bloquer uniquement les mots dangereux SQL
    dangerous = ['drop ', 'delete ', 'truncate ', 'update ', 'insert ']
    for d in dangerous:
        if d in ql:
            return False, "Opération non autorisée."

    return True, None
# ─── EXTRACTION AGRESSIVE DU SQL ─────────────────────────────────────────────
def extraire_sql_brut(texte: str) -> str:
    """
    Extrait le SQL brut d'une réponse LLM bavarde.
    Gère: markdown, explications, "Voici le SQL:", etc.
    """
    if not texte:
        return ""

    original = texte

    # 1. Supprimer les blocs markdown
    texte = re.sub(r'```sql\s*', '', texte, flags=re.IGNORECASE)
    texte = re.sub(r'```\s*', '', texte, flags=re.IGNORECASE)

    # 2. Supprimer les balises code inline
    texte = re.sub(r'`([^`]+)`', r'\1', texte)

    # 3. Chercher un pattern SELECT ... [;\n]
    # Prendre la plus longue chaîne commençant par SELECT et finissant par ; ou \n ou fin de string
    select_match = re.search(r'(SELECT\s+.+?)(?:;|\n\n|\.\s*$|$)', texte, re.IGNORECASE | re.DOTALL)
    if select_match:
        sql = select_match.group(1).strip()
        # Nettoyer
        sql = re.sub(r'\n+', ' ', sql)
        sql = " ".join(sql.split())
        sql = sql.rstrip(";").strip()
        # Vérifier qu'il ne contient pas de texte explicatif
        if len(sql.split()) > 3 and 'SELECT' in sql.upper():
            return sql

    # 4. Fallback: chercher tout ce qui commence par SELECT et va jusqu'à la fin
    lines = texte.split('\n')
    for line in lines:
        line = line.strip()
        if line.upper().startswith('SELECT'):
            # Prendre cette ligne et les suivantes jusqu'à la fin ou un point
            sql = line
            # Essayer de trouver la fin logique
            sql = re.sub(r'\s+Voici.*$', '', sql, flags=re.IGNORECASE)
            sql = re.sub(r'\s+Correction.*$', '', sql, flags=re.IGNORECASE)
            sql = " ".join(sql.split())
            sql = sql.rstrip(";").strip()
            if len(sql.split()) > 3:
                return sql

    # 5. Dernier fallback: nettoyer le texte entier
    texte = re.sub(r'[\n\r]+', ' ', texte)
    texte = re.sub(r'\s+', ' ', texte).strip()
    texte = re.sub(r'Voici\s+(la\s+)?(correction|requête|sql|query).*?:', '', texte, flags=re.IGNORECASE)
    texte = re.sub(r'La\s+requête\s+corrigée\s+est\s*:', '', texte, flags=re.IGNORECASE)
    texte = re.sub(r'Correction\s*:', '', texte, flags=re.IGNORECASE)
    texte = re.sub(r'SQL\s+corrigé\s*:', '', texte, flags=re.IGNORECASE)
    texte = texte.strip()

    # Si ça commence par SELECT, garder
    if texte.upper().startswith('SELECT'):
        texte = texte.rstrip(";").strip()
        return texte

    # Sinon retourner ce qu'on a, le validateur bloquera si c'est pas du SQL
    logger.warning(f"[EXTRACTION] Impossible d'extraire SQL proprement de: {original[:100]}...")
    return texte


def valider_logique_sql(sql, question):
    """
    Détecte les incohérences entre la question et le SQL généré.
    """
    question_upper = question.upper()
    sql_upper = sql.upper()
    problemes = []

    # "commence par X" → doit être LIKE 'X%' sans % au début
    if any(mot in question_upper for mot in ["COMMENCE PAR", "COMMENCE PAR LA LETTRE", "DÉBUTE PAR"]):
        # Chercher un LIKE avec % au début avant la lettre
        if re.search(r"LIKE\s+'%[A-Z]", sql_upper):
            problemes.append("commence_par")

    # "finit par X" → doit être LIKE '%X' sans % à la fin
    if any(mot in question_upper for mot in ["FINIT PAR", "SE TERMINE PAR", "TERMINE PAR"]):
        if re.search(r"LIKE\s+'[A-Z][^%]*'", sql_upper):
            problemes.append("finit_par")

    # "aucun / jamais / sans" → doit avoir NOT EXISTS ou NOT IN
    if any(mot in question_upper for mot in ["AUCUN", "JAMAIS", "SANS", "N'ONT PAS", "PAS DE"]):
        if "NOT EXISTS" not in sql_upper and "NOT IN" not in sql_upper:
            problemes.append("negation")

    # "plus que la moyenne" → doit avoir AVG dans sous-requête
    if "MOYENNE" in question_upper and "PLUS QUE" in question_upper:
        if "AVG" not in sql_upper:
            problemes.append("moyenne")

    # "le plus [X]" → doit avoir MAX ou ORDER BY ... ROWNUM
    if re.search(r"LE PLUS\s+\w+", question_upper):
        if "MAX" not in sql_upper and "ROWNUM" not in sql_upper:
            problemes.append("maximum")

    return problemes

def corriger_logique(sql, problemes, question, client):
    """Si des problèmes détectés → demander au LLM de corriger."""
    if not problemes:
        return sql

    descriptions = {
        "commence_par": "LIKE '%X%' incorrect pour 'commence par' → utiliser LIKE 'X%'",
        "finit_par":    "LIKE incorrect pour 'finit par' → utiliser LIKE '%X'",
        "negation":     "Négation sans NOT EXISTS/NOT IN → ajouter NOT EXISTS",
        "moyenne":      "Comparaison à la moyenne sans AVG → ajouter sous-requête AVG",
        "maximum":      "Maximum sans MAX ni ROWNUM → corriger la logique",
    }

    raisons = " | ".join(descriptions[p] for p in problemes)
    print(f"[VALIDATION LOGIQUE] Problèmes : {raisons}")

    prompt = f"""Corrige ce SQL Oracle — la logique est incorrecte.

Question originale : {question}
SQL incorrect : {sql}
Problèmes détectés : {raisons}

Retourne UNIQUEMENT le SQL corrigé, sur une seule ligne, sans backticks."""

    response = client.chat.completions.create(
        messages=[{"role": "user", "content": prompt}],
        model=MODEL_FAST,
        temperature=0.0,
    )
    sql_corrige = response.choices[0].message.content.strip()
    sql_corrige = sql_corrige.replace("```sql","").replace("```","").strip()
    return " ".join(sql_corrige.split()).rstrip(";").strip()

# ─── Corrections syntaxe ──────────────────────────────────────────────────────
def corriger_syntaxe(sql: str) -> str:
    orig = sql

    m = re.search(r'(.+?)\s+FETCH\s+FIRST\s+(\d+)\s+ROWS?\s+ONLY', sql, re.I | re.DOTALL)
    if m:
        sql = f"SELECT * FROM ({m.group(1).strip()}) WHERE ROWNUM <= {m.group(2)}"

    m = re.search(r'(.+?)\s+LIMIT\s+(\d+)\s*$', sql, re.I | re.DOTALL)
    if m:
        sql = f"SELECT * FROM ({m.group(1).strip()}) WHERE ROWNUM <= {m.group(2)}"

    sql = re.sub(r'\s+OFFSET\s+\d+\s+ROWS?\s*', ' ', sql, flags=re.I)
    sql = re.sub(r"(>=|<=|>|<|=)\s*'(\d{2}/\d{2}/\d{4})'", lambda m: f"{m.group(1)} TO_DATE('{m.group(2)}','DD/MM/YYYY')", sql)
    sql = re.sub(r"(>=|<=|>|<|=)\s*'(\d{4}-\d{2}-\d{2})'", lambda m: f"{m.group(1)} TO_DATE('{m.group(2)}','YYYY-MM-DD')", sql)
    sql = re.sub(r'ISNULL\s*\(([^)]+)\)', r'\1 IS NULL', sql, flags=re.I)
    sql = re.sub(r'IFNULL\s*\(', 'NVL(', sql, flags=re.I)
    sql = re.sub(r'COALESCE\s*\(([^,]+),\s*([^)]+)\)', r'NVL(\1, \2)', sql, flags=re.I)

    m = re.search(r'SELECT\s+TOP\s+(\d+)\s+(.+)', sql, re.I | re.DOTALL)
    if m:
        sql = f"SELECT * FROM (SELECT {m.group(2)}) WHERE ROWNUM <= {m.group(1)}"

    sql = " ".join(sql.split()).rstrip(";").strip()

    bad_patterns = ["FETCH FIRST", "LIMIT ", "LISTAGG", "NVL2", "PIVOT", "UNPIVOT", "REGEXP", "WITHIN GROUP", " TOP "]
    if any(p.upper() in sql.upper() for p in bad_patterns):
        logger.info("[SYNTAX] Pattern complexe, correction LLM...")
        sql = corriger_llm(sql, "syntaxique")

    if sql != orig:
        logger.info("[AUTO-CORR SYNTAXE] SQL corrigé")
    return sql

# ─── Corrections sémantique ─────────────────────────────────────────────────
def corriger_semantique(sql: str) -> str:
    orig = sql
    need_llm = False
    reason = ""

    # 0. Triple nesting flatten
    m = re.search(
        r'SELECT\s+\*\s+FROM\s+\(\s*SELECT\s+\*\s+FROM\s+\((.+?)\)\s*(ORDER\s+BY\s+([\w]+\.\w+(?:\s*(?:ASC|DESC))?))\s*\)\s*WHERE\s+ROWNUM\s*<=\s*(\d+)',
        sql, re.I | re.DOTALL
    )
    if m:
        inner = m.group(1).strip()
        order_full = m.group(3).strip()
        n = m.group(4)
        order_clean = re.sub(r'^\w+\.', '', order_full)
        sql = f"SELECT * FROM ({inner} ORDER BY {order_clean}) WHERE ROWNUM <= {n}"
        logger.info("[AUTO-CORR] Triple imbrication aplatie")

    # 1. ROWNUM=1 + ORDER BY same level
    if re.search(r'WHERE\s+ROWNUM\s*=\s*1', sql, re.I) and re.search(r'ORDER\s+BY', sql, re.I):
        mo = re.search(r'ORDER\s+BY\s+([\w.]+(?:\s*(?:ASC|DESC))?)', sql, re.I)
        oc = mo.group(0) if mo else "ORDER BY 1 DESC"
        inner = re.sub(r'WHERE\s+ROWNUM\s*=\s*1', '', sql, flags=re.I)
        inner = re.sub(r'ORDER\s+BY\s+[\w.]+(?:\s*(?:ASC|DESC))?', '', inner, flags=re.I).strip()
        sql = f"SELECT * FROM ({inner} {oc}) WHERE ROWNUM <= 1"
        logger.info("[AUTO-CORR] ROWNUM=1+ORDER BY corrigé")

    # 2. ROWNUM<=N + ORDER BY same level
    mr = re.search(r'WHERE\s+ROWNUM\s*<=\s*(\d+)', sql, re.I)
    if mr and re.search(r'ORDER\s+BY', sql, re.I):
        dr = 0
        for c in sql[:sql.upper().find('ROWNUM')]:
            dr += (c == '('); dr -= (c == ')')
        do = 0
        for c in sql[:sql.upper().find('ORDER BY')]:
            do += (c == '('); do -= (c == ')')
        if dr == 0 and do == 0:
            n = mr.group(1)
            mo = re.search(r'(ORDER\s+BY\s+[\w.]+(?:\s*(?:ASC|DESC))?)', sql, re.I)
            oc = mo.group(1) if mo else "ORDER BY 1 DESC"
            inner = re.sub(r'WHERE\s+ROWNUM\s*<=\s*\d+', '', sql, flags=re.I)
            inner = re.sub(r'ORDER\s+BY\s+[\w.]+(?:\s*(?:ASC|DESC))?', '', inner, flags=re.I).strip()
            inner = re.sub(r'\s+AND\s*$', '', inner, flags=re.I).strip()
            inner = re.sub(r'WHERE\s*$', '', inner, flags=re.I).strip()
            sql = f"SELECT * FROM ({inner} {oc}) WHERE ROWNUM <= {n}"
            logger.info("[AUTO-CORR] ROWNUM<=N+ORDER BY corrigé")

    # 3. Nested aggregation
    if re.search(r'AVG\s*\(\s*SUM\s*\(', sql, re.I) or re.search(r'SUM\s*\(\s*AVG\s*\(', sql, re.I):
        need_llm = True
        reason = "Agrégation imbriquée interdite"

    # 4. COUNT(DISTINCT *)
    sql = re.sub(r'COUNT\s*\(\s*DISTINCT\s*\*\s*\)', 'COUNT(*)', sql, flags=re.I)

    # 5. Division by zero
    sql = re.sub(r'(\w+)\s*/\s*(\w+)(?!\s*\()', lambda m: f"{m.group(1)}/NULLIF({m.group(2)},0)" if m.group(2).upper() not in ('0','NULLIF','100','365','12','1000') else m.group(0), sql)

    # 6. JOIN without ON
    if re.search(r'\bJOIN\b(?!\s*\()', sql, re.I):
        joins = len(re.findall(r'\bJOIN\b', sql, re.I))
        ons = len(re.findall(r'\bON\b', sql, re.I))
        if joins > ons:
            need_llm = True
            reason = f"{joins} JOIN mais {ons} ON"

    # 7. Trailing WHERE
    sql = re.sub(r'\s+WHERE\s*$', '', sql, flags=re.I).strip()

    # 8. Alias in outer ORDER BY
    ma = re.search(r'SELECT\s+\*\s+FROM\s+\((.+?)\)\s+WHERE\s+ROWNUM\s*<=\s*\d+\s+ORDER\s+BY\s+(\w+\.\w+)', sql, re.I | re.DOTALL)
    if ma:
        alias = ma.group(2).split('.')[0]
        col = ma.group(2).split('.')[1]
        if re.search(rf'\b{alias}\b', ma.group(1), re.I):
            sql = sql.replace(f"ORDER BY {ma.group(2)}", f"ORDER BY {col}")
            logger.info(f"[AUTO-CORR] Alias {alias} remplacé par {col}")

    if need_llm:
        logger.info(f"[SEMANTIQUE] Correction LLM: {reason}")
        sql = corriger_llm(sql, reason)

    if sql != orig:
        logger.info("[AUTO-CORR SEMANTIQUE] Appliquée")
    return sql

# ─── LLM corrector ────────────────────────────────────────────────────────────
@traceable(name="corriger_llm", run_type="llm")
def corriger_llm(sql: str, reason: str) -> str:
    prompt = f"""Expert Oracle 10g. Corrige ce SQL.
Problème: {reason}
Règles: Pas FETCH FIRST/LIMIT; dates TO_DATE/EXTRACT; ROWNUM+ORDER BY en sous-requête; pas AVG(SUM()); JOIN avec ON; 1 ligne; alias externes interdits dans ORDER BY; retourne UNIQUEMENT SQL sans backticks.
SQL: {sql}"""

    try:
        r = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL, temperature=0.0, max_tokens=800
        )
        texte = r.choices[0].message.content.strip()
        return extraire_sql_brut(texte)
    except Exception as e:
        logger.error(f"[ERR LLM corrector] {e}")
        return sql

# ─── Génération SQL ─────────────────────────────────────────────────────────
META_CACHE = None
META_TIME = 0
META_TTL = 300

def charger_meta() -> Dict:
    global META_CACHE, META_TIME
    if META_CACHE and (time.time() - META_TIME) < META_TTL:
        return META_CACHE
    conn = get_connection()
    meta = {}
    try:
        c = conn.cursor()
        c.execute("SELECT COD_SOC, COD_SERV, LIB_SERV FROM SERVICE ORDER BY COD_SOC, COD_SERV")
        meta["services"] = [{"cod_soc": r[0], "cod_serv": r[1], "lib_serv": r[2]} for r in c.fetchall()]
        c.execute("SELECT COD_CATEG, COD_CAT, COD_GRAD, LIB_GRAD FROM GRADE ORDER BY COD_CATEG, COD_CAT, COD_GRAD")
        meta["grades"] = [{"cod_categ": r[0], "cod_cat": r[1], "cod_grad": r[2], "lib_grad": r[3]} for r in c.fetchall()]
        c.execute("SELECT COD_SOC, LIB_SOC FROM SOCIETE ORDER BY COD_SOC")
        meta["societes"] = [{"cod_soc": r[0], "lib_soc": r[1]} for r in c.fetchall()]
        c.execute("SELECT GRP_SANG, LIB_GRP_SANG FROM GROUPE_SANGUIN ORDER BY GRP_SANG")
        meta["groupes_sanguins"] = [{"grp_sang": r[0], "lib_grp_sang": r[1]} for r in c.fetchall()]
    finally:
        conn.close()
    META_CACHE = meta; META_TIME = time.time()
    logger.info(f"[OK] Meta: {len(meta.get('services',[]))} serv, {len(meta.get('grades',[]))} grad")
    return meta


def build_meta_context(meta: Dict) -> str:
    """Build compact metadata context."""
    lines = ["\n=== DONNÉES ==="]
    if meta.get("services"):
        lines.append(f"Services: {len(meta['services'])} total")
        for s in meta["services"][:5]:
            lines.append(f"  {s['cod_serv']}={s['lib_serv']}")
    if meta.get("grades"):
        lines.append(f"Grades: {len(meta['grades'])} total")
        for g in meta["grades"][:3]:
            lines.append(f"  ({g['cod_categ']},{g['cod_cat']},{g['cod_grad']})={g['lib_grad']}")
    lines.append("Utilise ces codes exacts.")
    return "\n".join(lines)


# ─── Résolution de contexte / Mémoire ───────────────────────────────────────
@traceable(name="reformuler_question", run_type="llm")
def reformuler_question_avec_contexte(question: str, history: List[Dict[str, str]]) -> str:
    """
    Réécrit la question de l'utilisateur si elle dépend du contexte des messages précédents.
    """
    if not history:
        return question

    # Ne garder que les 4-6 derniers messages pour limiter le coût en tokens
    recent_history = history[-6:]
    
    prompt_context = "Voici l'historique récent de la conversation avec l'utilisateur :\n"
    for msg in recent_history:
        role = "Utilisateur" if msg.get("role") == "user" else "Assistant"
        prompt_context += f"{role}: {msg.get('content')}\n"

    prompt = f"""{prompt_context}
Question actuelle de l'utilisateur : "{question}"

Tâche :
Si la question actuelle fait référence à un élément mentionné précédemment (ex: "son", "ses", "cet employé", "ce service", "et pour 2024 ?"), réécris la question actuelle pour qu'elle soit autonome, claire et complète.
Si la question actuelle est déjà autonome et explicite, retourne-la exactement telle quelle sans modification.

Règles :
- Réponds UNIQUEMENT avec la question réécrite (ou originale). Aucun commentaire, pas de guillemets.
"""

    try:
        r = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_FAST,
            temperature=0.0,
            max_tokens=150
        )
        question_reformulee = r.choices[0].message.content.strip()
        logger.info(f"[MEMOIRE] Original: '{question}' --> Reformulée: '{question_reformulee}'")
        return question_reformulee
    except Exception as e:
        logger.error(f"[ERR Reformulation Mémoire] {e}")
        return question
    
def detect_language(text: str) -> str:
    """Détecte la langue de la question : arabe ('ar'), anglais ('en') ou français ('fr', défaut)."""
    if any('\u0600' <= c <= '\u06FF' for c in text):
        return 'ar'

    mots_en = {'the', 'is', 'are', 'how', 'many', 'what', 'which', 'employees',
               'show', 'list', 'who', 'salary', 'department', 'total', 'average'}
    mots_fr = {'le', 'la', 'les', 'des', 'est', 'sont', 'combien', 'quel', 'quels',
               'quelle', 'employés', 'liste', 'qui', 'salaire', 'service', 'total', 'moyenne'}

    tokens = set(re.findall(r"[a-zA-Z']+", text.lower()))
    score_en = len(tokens & mots_en)
    score_fr = len(tokens & mots_fr)

    if score_en > 0 and score_en > score_fr:
        return 'en'
    return 'fr'


LANG_SQL_INSTRUCTIONS = {
    'fr': "",
    'ar': (
        "\n\nNOTE LANGUE: la question de l'utilisateur est posée en ARABE. "
        "Comprends l'intention en arabe mais génère le SQL EXCLUSIVEMENT avec les noms "
        "de tables/colonnes du schéma fournis (ils restent en français/anglais technique). "
        "Ne traduis jamais les noms de colonnes."
    ),
    'en': (
        "\n\nLANGUAGE NOTE: the user's question is in ENGLISH. "
        "Understand the intent in English but generate the SQL using EXCLUSIVELY the "
        "table/column names from the provided schema (they remain in French). "
        "Never translate column names."
    ),
}

# Exemple pour generer_sql :

_PERSONAL_INFO_PATTERNS = [
    r"\binformations?\s+personnelles?\b",
    r"\bmes infos\b",
    r"\bmon profil\b",
    r"\bfiche\s+(employé|employe|personnelle?)\b",
]


def est_requete_fiche_personnelle(question: str) -> bool:
    return any(re.search(p, question, re.IGNORECASE) for p in _PERSONAL_INFO_PATTERNS)


def sql_fiche_personnelle(mat_pers: str) -> str:
    """SQL court et complet pour les infos personnelles — évite les requêtes tronquées du LLM."""
    mat = re.sub(r"[^0-9A-Za-z]", "", str(mat_pers).strip())
    return (
        f"SELECT P.MAT_PERS, P.NOM_PERS, P.PREN_PERS, P.SEXE, P.DAT_NAIS, "
        f"P.DAT_ENT, P.DAT_EMB, P.DAT_DEPART, P.ETAT_ACT, P.GRP_SANG, "
        f"GS.LIB_GRP_SANG, G.LIB_GRAD, S.LIB_SERV, P.HANDICAP, P.NBRE_ENF "
        f"FROM PERSONNEL P "
        f"LEFT JOIN GRADE G ON P.COD_CATEG=G.COD_CATEG AND P.COD_CAT=G.COD_CAT AND P.COD_GRAD=G.COD_GRAD "
        f"LEFT JOIN SERVICE S ON P.COD_SOC=S.COD_SOC AND P.COD_SERV=S.COD_SERV "
        f"LEFT JOIN GROUPE_SANGUIN GS ON P.GRP_SANG=GS.GRP_SANG "
        f"WHERE P.MAT_PERS = '{mat}'"
    )


def est_sql_incomplet(sql: str) -> bool:
    """Détecte un SQL tronqué (JOIN sans ON, etc.) avant exécution Oracle."""
    up = sql.strip().rstrip(";").upper()
    if "FROM" not in up:
        return True
    if re.search(r"\bJOIN\s+\w+\s*$", up):
        return True
    joins = len(re.findall(r"\bJOIN\b", up))
    ons = len(re.findall(r"\bON\b", up))
    return joins > ons


@traceable(name="generer_sql", run_type="llm")
def generer_sql(question: str, history: List[Dict[str, str]] = None,
                 user_context: dict = None, lang: str = 'fr') -> str:
    # 1. Reformuler la question en exploitant la mémoire si présente
    if history:
        question_effective = reformuler_question_avec_contexte(question, history)
    else:
        question_effective = question

    if user_context and (
        est_requete_fiche_personnelle(question_effective)
        or (
            classifier_intention_rbac(question_effective) == INTENT_SELF
            and re.search(r"\binformations?\b", question_effective, re.IGNORECASE)
        )
    ):
        mat = user_context.get("mat_pers")
        if mat:
            logger.info(f"[SQL] Fiche personnelle template pour matricule {mat}")
            return sql_fiche_personnelle(mat)

    cl = classifier_question(question_effective)

    ctx = ""
    if cl['svc']: ctx += f"\nService: {cl['svc']}"
    if cl['gs']: ctx += f"\nGS: {cl['gs']}"
    if cl['year']: ctx += f"\nAnnée: {cl['year']}"
    if cl['limit']: ctx += f"\nLimite: {cl['limit']}"
    if any(v for v in cl['types'].values()):
        ctx += f"\nTypes: {[k for k,v in cl['types'].items() if v]}"

    lang_instr = LANG_SQL_INSTRUCTIONS.get(lang, "")
    rbac_ctx = build_rbac_context(user_context, question_effective)

    try:
        meta = charger_meta()
        meta_ctx = build_meta_context(meta)
        prompt = SCHEMA_PROMPT + meta_ctx + ctx + lang_instr + rbac_ctx
    except:
        prompt = SCHEMA_PROMPT + ctx + lang_instr + rbac_ctx

    if len(prompt) // 4 > MAX_TOKENS_PROMPT:
        logger.warning(f"[WARN] Prompt trop gros ({len(prompt)//4} tokens), fallback statique")
        prompt = SCHEMA_PROMPT + ctx + lang_instr + rbac_ctx

    logger.info(f"[DEBUG] ~{len(prompt)//4} tokens")

    try:
        r = client.chat.completions.create(
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": f"SQL pour: {question_effective}"}
            ],
            model=MODEL, temperature=0.0, max_tokens=800
        )
        texte = r.choices[0].message.content.strip()
        sql = extraire_sql_brut(texte)

        # Maintenir un seul SELECT de niveau 0
        up = sql.upper()
        d = 0; found = False
        for i in range(len(up)):
            if up[i] == '(': d += 1
            elif up[i] == ')': d -= 1
            elif up[i:i+6] == 'SELECT' and d == 0:
                if found:
                    sql = sql[:i].strip()
                    break
                found = True

        sql = sql.rstrip(";").strip()
        sql = corriger_syntaxe(sql)
        sql = corriger_semantique(sql)
        sql, _ = valider_et_corriger(sql, llm_corrector_fn=corriger_llm)
        
        # Validation logique question ↔ SQL
        problemes = valider_logique_sql(sql, question_effective)
        if problemes:
            sql = corriger_logique(sql, problemes, question_effective, client)
        
        return sql
    except Exception as e:
        logger.error(f"[ERR Generation] {e}")
        raise
# ─── Génération de la réponse textuelle / Synthèse ───────────────────────────
@traceable(name="generer_reponse_textuelle", run_type="llm")
def generer_reponse_textuelle(question: str, cols: List[str], rows: List[Tuple]) -> str:
    """Génère un court paragraphe d'explication convivial basé sur le résultat de la requête SQL."""
    if not rows:
        return "Aucune donnée n'a été trouvée pour répondre à votre demande."
    
    # On transmet uniquement les 10 premières lignes pour ne pas engorger le prompt
    sample_data = [dict(zip(cols, row)) for row in rows[:10]]
    
    prompt = f"""Tu es un assistant RH & Santé professionnel, courtois et concis.
L'utilisateur a posé la question suivante : "{question}"

Voici un extrait des résultats obtenus en base de données :
{sample_data}

Instructions :
1. Rédige une ou deux phrases d'introduction synthétiques et professionnelles qui présentent ou résument le résultat.
2. Si des employés sont concernés, mentionne leurs matricules et noms/prénoms le cas échéant.
3. Sois direct, utile, sans répétition inutile. Ne génère PAS de tableau Markdown (il sera affiché séparément)."""

    try:
        r = client.chat.completions.create(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_FAST,
            temperature=0.3,
            max_tokens=250
        )
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[ERR Synthèse textuelle] {e}")
        return "Voici les résultats correspondant à votre recherche :"



# ─── Retry execution ──────────────────────────────────────────────────────────
# ULTRA-STRICT retry prompt - interdit TOUT texte autour du SQL
PROMPT_RETRY = """Tu es un expert Oracle 10g. Corrige ce SQL qui a une erreur.

RÈGLE ABSOLUE: Retourne UNIQUEMENT le SQL, pas une seule lettre avant ou après. Pas de "Voici", pas de "Correction", pas d'explication. JUSTE le SQL sur une ligne.

RÈGLES ORACLE 10g:
- Si la requête concerne des employés, inclus OBLIGATOIREMENT le MATRICULE / MAT_PERS dans les colonnes sélectionnées.
- Pas FETCH FIRST/LIMIT → ROWNUM <= N dans sous-requête
- Pas dates string → TO_DATE() ou EXTRACT()
- Pas ROWNUM=1 avec ORDER BY → sous-requête: SELECT * FROM (SELECT ... ORDER BY col DESC) WHERE ROWNUM <= 1
- Pas AVG(SUM()) → sous-requête séparée
- Pas fonctions agrégation imbriquées
- JOIN doit avoir ON
- SQL sur UNE SEULE LIGNE
- Pas point-virgule final
- Alias table (P,B,S,G) valides UNIQUEMENT dans leur requête. Dans sous-requête externe, ORDER BY utilise nom colonne direct sans alias.
- Pour "plus jeune": MIN(dat_nais) ou MAX(dat_nais) selon logique. Pour Oracle: dat_nais la plus récente = plus jeune.
- Pour "chaque service" / "par service": GROUP BY obligatoire sur S.LIB_SERV ou P.COD_SERV
- HAVING avec moyenne: HAVING SUM(B.TOT_NET) > (SELECT AVG(TOT_NET) FROM BULT_SOIN)
- ORA-00979: toutes les colonnes du SELECT qui ne sont pas dans une fonction d'agrégation doivent être dans GROUP BY.
- ORA-00904: vérifier que la colonne existe réellement dans le schéma fourni. Ne jamais inventer de colonne.
SQL ERRONÉ:
{sql}

ERREUR ORACLE:
{erreur}

SQL CORRIGÉ (UNIQUEMENT SQL, RIEN D'AUTRE):"""

MOTS_INTERDITS = ["DROP", "DELETE", "UPDATE", "INSERT", "TRUNCATE", "ALTER", "CREATE", "GRANT", "REVOKE"]

@traceable(name="executer_oracle", run_type="tool")
def executer(sql: str, max_try: int = 3) -> Tuple[List[str], List[Tuple], str]:
    cur_sql = sql
    derniere_erreur = None

    for t in range(max_try):
        try:
            # Vérification sécurité AVANT exécution
            for mot in MOTS_INTERDITS:
                if mot in cur_sql.upper():
                    raise ValueError(f"Requête refusée: '{mot}'")

            # Vérification que c'est bien du SQL
            if not cur_sql.upper().startswith('SELECT'):
                logger.warning(f"[SECURITÉ] Pas un SELECT: {cur_sql[:80]}...")
                raise ValueError("Requête invalide: ne commence pas par SELECT")

            if est_sql_incomplet(cur_sql):
                raise oracledb.DatabaseError(
                    "SQL incomplet (JOIN sans ON ou requête tronquée)"
                )

            conn = get_connection()
            c = conn.cursor()
            c.execute(cur_sql)
            cols = [col[0] for col in c.description]
            rows = c.fetchall()
            conn.close()
            return cols, rows, cur_sql

        except ValueError as ve:
            raise ve

        except oracledb.DatabaseError as e:
            derniere_erreur = e
            try:
                conn.close()
            except:
                pass

            logger.warning(f"[RETRY {t+1}/{max_try}] {str(e)[:120]}")
            print(f"\nSQL EN ERREUR :\n{cur_sql}\n")

            if t >= max_try - 1:
                raise Exception(
                    f"Échec après {max_try} tentatives. "
                    f"Dernière erreur: {str(derniere_erreur)}"
                )

            try:
                r = client.chat.completions.create(
                    messages=[{"role": "user", "content": PROMPT_RETRY.format(
                        sql=cur_sql, erreur=str(e)
                    )}],
                    model=MODEL, temperature=0.0, max_tokens=800
                )
                texte = r.choices[0].message.content.strip()
                cur_sql = extraire_sql_brut(texte)

                if not cur_sql.upper().startswith('SELECT'):
                    logger.error(
                        f"[RETRY] LLM n'a pas retourné de SQL valide: "
                        f"{cur_sql[:100]}..."
                    )
                    raise derniere_erreur

                logger.info(f"[RETRY] SQL: {cur_sql[:80]}...")

            except ValueError:
                raise
            except Exception as e2:
                logger.error(f"[ERR Retry] {e2}")
                raise derniere_erreur

    # Sécurité : ne devrait jamais arriver
    raise Exception("Échec inattendu : executer() terminée sans résultat")


PROMPT_REPONSE_FR = """Tu es un assistant RH. Génère une réponse naturelle en français pour accompagner ce résultat SQL.

Question posée : {question}
Nombre de lignes : {nb_lignes}
Colonnes : {colonnes}
Données (5 premières lignes) : {apercu}

RÈGLES :
- 1 à 3 phrases maximum, naturelles et professionnelles
- Si 0 résultat : explique qu'aucune donnée n'a été trouvée
- Si 1 résultat : présente directement la réponse
- Si plusieurs résultats : donne un résumé chiffré
- Ne répète pas toutes les données, le tableau est déjà affiché
- Pas de markdown, pas de bullet points
- Réponds UNIQUEMENT en français

Exemples :
- "J'ai trouvé 15 employés actifs dans le service Informatique."
- "Aucun employé ne correspond à cette recherche."
- "Le service RH totalise 45 230 dinars de remboursements en 2024, soit le montant le plus élevé."
"""

PROMPT_REPONSE_AR = """أنت مساعد للموارد البشرية. اكتب رداً طبيعياً باللغة العربية الفصحى المبسطة لمرافقة نتيجة الاستعلام التالية.

السؤال المطروح: {question}
عدد الصفوف: {nb_lignes}
الأعمدة: {colonnes}
عينة من البيانات (أول 5 صفوف): {apercu}

القواعد:
- جملة إلى ثلاث جمل كحد أقصى، بأسلوب طبيعي واحترافي
- إذا كانت النتيجة صفراً: وضّح أنه لم يتم العثور على أي بيانات
- إذا كانت النتيجة واحدة: قدّم الجواب مباشرة
- إذا كانت النتائج متعددة: قدّم ملخصاً رقمياً
- لا تكرر كل البيانات، الجدول معروض بالفعل
- بدون markdown وبدون نقاط تعداد
- أجب باللغة العربية فقط

أمثلة:
- "وجدت 15 موظفاً نشطاً في قسم المعلوماتية."
- "لا يوجد أي موظف يطابق هذا البحث."
"""

PROMPT_REPONSE_EN = """You are an HR assistant. Generate a natural response in English to accompany this SQL result.

Question asked: {question}
Number of rows: {nb_lignes}
Columns: {colonnes}
Data (first 5 rows): {apercu}

RULES:
- 1 to 3 sentences maximum, natural and professional
- If 0 results: explain that no data was found
- If 1 result: present the answer directly
- If several results: give a numeric summary
- Do not repeat all the data, the table is already displayed
- No markdown, no bullet points
- Answer in English only

Examples:
- "I found 15 active employees in the IT department."
- "No employee matches this search."
"""

PROMPT_REPONSE_TEMPLATES = {
    'fr': PROMPT_REPONSE_FR,
    'ar': PROMPT_REPONSE_AR,
    'en': PROMPT_REPONSE_EN,
}

def generer_reponse_nl(question, colonnes, lignes, client, lang: str = 'fr',
                       user_context: dict = None, history: List[Dict[str, str]] = None):
    """Génère une phrase de résumé en langage naturel, dans la langue détectée de la question."""
    try:
        template = PROMPT_REPONSE_TEMPLATES.get(lang, PROMPT_REPONSE_FR)
        rbac_nl = build_rbac_nl_context(user_context, question)

        history_ctx = ""
        if history:
            recent = history[-6:]
            history_ctx = "Contexte de la conversation récente :\n"
            for msg in recent:
                role_label = "Utilisateur" if msg.get("role") == "user" else "Assistant"
                history_ctx += f"{role_label}: {msg.get('content', '')}\n"
            history_ctx += "\n"

        apercu = [list(r) for r in lignes[:5]]
        response = client.chat.completions.create(
            messages=[{"role": "user", "content": history_ctx + rbac_nl + template.format(
                question=question,
                nb_lignes=len(lignes),
                colonnes=list(colonnes),
                apercu=apercu
            )}],
            model=MODEL,
            temperature=0.3,
            max_tokens=200
        )
        return response.choices[0].message.content.strip()
    except:
        return None
# ─── Format results ───────────────────────────────────────────────────────────
def format_result(cols: List[str], rows: List[Tuple]) -> str:
    if not rows:
        return "Aucune donnée trouvée."
    widths = [len(str(c)) for c in cols]
    for row in rows:
        for i, v in enumerate(row):
            widths[i] = max(widths[i], len(str(v) if v is not None else "NULL"))
    sep = "+-" + "-+-".join(["-" * w for w in widths]) + "-+"
    header = "| " + " | ".join([str(c).ljust(widths[i]) for i, c in enumerate(cols)]) + " |"
    lines = [sep, header, sep]
    for row in rows:
        lines.append("| " + " | ".join([(str(v) if v is not None else "NULL").ljust(widths[i]) for i, v in enumerate(row)]) + " |")
    lines.append(sep)
    return "\n".join(lines)

# ─── Cache ────────────────────────────────────────────────────────────────────
class QueryCache:
    def __init__(self, max_size=50):
        self.cache = {}; self.max_size = max_size; self.hits = {}
    def get(self, q):
        k = q.lower().strip()
        if k in self.cache:
            self.hits[k] = self.hits.get(k, 0) + 1
            return self.cache[k]
        return None
    def set(self, q, result):
        k = q.lower().strip()
        if len(self.cache) >= self.max_size:
            lru = min(self.hits, key=self.hits.get)
            del self.cache[lru]; del self.hits[lru]
        self.cache[k] = result; self.hits[k] = 1

cache = QueryCache(50)

# ─── Audit / Journalisation ───────────────────────────────────────────────────
def log_audit(mat_pers: Optional[str], action: str, detail: str):
    """
    Enregistre une entrée dans AUDIT_LOG (traçabilité des requêtes chatbot).
    Ne doit JAMAIS faire échouer le traitement principal : toute erreur est loggée puis ignorée.
    """
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO AUDIT_LOG (MAT_PERS, ACTION, DETAIL, DATE_LOG)
               VALUES (:1, :2, :3, CURRENT_TIMESTAMP)""",
            (mat_pers or "ANONYME", action, (detail or "")[:1000])
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning(f"[AUDIT] Échec journalisation: {e}")

# ─── Main processing ─────────────────────────────────────────────────────────
@traceable(name="process", run_type="chain")
def process(q: str, history: List[Dict[str, str]] = None, user_context: dict = None) -> Dict:
    start = time.time()
    lang = detect_language(q)
    mat_pers = (user_context or {}).get("mat_pers")
    res = {
        'ok': False,
        'sql_gen': '',
        'sql_final': '',
        'cols': [],
        'rows': [],
        'n': 0,
        'err': None,
        'reponse_nl': None,
        'lang': lang,
        'ms': 0
    }
    try:
        ok, err = pre_validate(q)
        if not ok:
            res['err'] = err
            res['ms'] = (time.time()-start)*1000
            log_audit(mat_pers, "QUERY_REJECTED", f"Q: {q[:200]} | lang={lang} | motif: {err}")
            return res

        question_effective = q
        if history:
            question_effective = reformuler_question_avec_contexte(q, history)

        rbac_q_ok, rbac_q_err = valider_question_rbac(question_effective, user_context)
        if not rbac_q_ok:
            res['err'] = f"[RBAC] {rbac_q_err}"
            res['ms'] = (time.time()-start)*1000
            log_audit(mat_pers, "QUERY_RBAC_BLOCKED", f"Q: {q[:200]} | lang={lang} | {rbac_q_err}")
            return res

        cache_key = q
        if user_context:
            cache_key = f"{user_context.get('role','')}:{user_context.get('mat_pers','')}:{q}"

        cached = cache.get(cache_key)
        if cached and not history: # Bypass cache si conversation avec historique
            cols, rows, sql = cached
            reponse_nl = generer_reponse_nl(question_effective, cols, rows, client, lang=lang,
                                            user_context=user_context, history=history)

            res.update({
                'ok': True,
                'sql_final': sql,
                'cols': cols,
                'rows': [list(r) for r in rows],
                'n': len(rows),
                'reponse_nl': reponse_nl,
                'ms': (time.time()-start)*1000
            })
            log_audit(mat_pers, "QUERY_CACHE",
                      f"Q: {q[:200]} | lang={lang} | SQL: {sql[:500]} | Rows: {len(rows)}")
            return res

        logger.info("[1/3] Génération SQL (avec mémoire)...")
        sql_gen = generer_sql(question_effective, history=None, user_context=user_context, lang=lang)
        res['sql_gen'] = sql_gen
        logger.info(f"[2/3] SQL: {sql_gen[:80]}...")

        rbac_ok, rbac_err = valider_rbac_sql(sql_gen, user_context, question_effective)
        if not rbac_ok:
            res['err'] = f"[RBAC] {rbac_err}"
            res['ms'] = (time.time()-start)*1000
            log_audit(mat_pers, "QUERY_RBAC_BLOCKED", f"Q: {q[:200]} | SQL: {sql_gen[:300]}")
            return res

        logger.info("[3/3] Exécution Oracle...")
        cols, rows, sql_final = executer(sql_gen)

        rbac_sql_ok, rbac_sql_err = valider_rbac_sql(sql_final, user_context, question_effective)
        if not rbac_sql_ok:
            res['err'] = f"[RBAC] {rbac_sql_err}"
            res['ms'] = (time.time()-start)*1000
            log_audit(mat_pers, "QUERY_RBAC_BLOCKED", f"Q: {q[:200]} | SQL: {sql_final[:300]}")
            return res

        cols, rows, filter_err = filtrer_resultats(cols, rows, user_context, question_effective)
        if filter_err:
            res['err'] = f"[RBAC] {filter_err}"
            res['ms'] = (time.time()-start)*1000
            log_audit(mat_pers, "QUERY_RBAC_BLOCKED", f"Q: {q[:200]} | filtre résultats")
            return res

        if not history:
            cache.set(cache_key, (cols, rows, sql_final))

        res.update({
            'ok': True,
            'sql_final': sql_final,
            'cols': cols,
            'rows': [list(r) for r in rows],
            'n': len(rows),
            'ms': (time.time()-start)*1000
        })
        reponse_nl = generer_reponse_nl(question_effective, cols, rows, client, lang=lang,
                                        user_context=user_context, history=history)
        res['reponse_nl'] = reponse_nl

        log_audit(mat_pers, "QUERY",
                  f"Q: {q[:200]} | lang={lang} | SQL: {sql_final[:500]} | Rows: {len(rows)}")

    except ValueError as ve:
        res['err'] = f"[SECURITÉ] {ve}"
        log_audit(mat_pers, "QUERY_BLOCKED", f"Q: {q[:200]} | lang={lang} | {ve}")
    except oracledb.DatabaseError as de:
        res['err'] = f"[ÉCHEC] {de}"
        log_audit(mat_pers, "QUERY_ERROR", f"Q: {q[:200]} | lang={lang} | {de}")
    except Exception as e:
        res['err'] = f"[ERREUR] {e}"
        log_audit(mat_pers, "QUERY_ERROR", f"Q: {q[:200]} | lang={lang} | {e}")
    res['ms'] = (time.time()-start)*1000
    return res
# ─── Interactive loop ─────────────────────────────────────────────────────────
import uuid
def main():
    session_id = str(uuid.uuid4())
    print("\n" + "="*60)
    print("   CHATBOT RH & SANTÉ - Version FIXÉE")
    print("="*60)
    print(f"DSN: {DB_DSN} | Modèle: {MODEL}")
    print("Commandes: quitter | cache | debug")
    print("="*60 + "\n")
    while True:
        try:
            q = input("Votre question : ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nAu revoir."); break
        if q.lower() == "quitter":
            print("Au revoir."); break
        if q.lower() == "cache":
            print(f"\n[Cache] {len(cache.cache)} requêtes")
            for qk, n in sorted(cache.hits.items(), key=lambda x: -x[1])[:5]:
                print(f"  {qk[:45]}... ({n} accès)")
            print(); continue
        if q.lower() == "debug":
            lvl = logging.DEBUG if logger.level == logging.INFO else logging.INFO
            logger.setLevel(lvl)
            print(f"[DEBUG] {'activé' if lvl == logging.DEBUG else 'désactivé'}"); continue
        if not q:
            continue
        r = process(session_id, q)
        if r['ok']:
            print(f"\n{'='*60}")
            print(f"SQL généré : {r['sql_gen']}")
            if r['sql_final'] != r['sql_gen']:
                print(f"SQL final  : {r['sql_final']}")
            print(f"{'='*60}")
            print(f"\nRésultat ({r['n']} ligne(s)):")
            print(format_result(r['cols'], [tuple(row) for row in r['rows']]))
            print(f"\nTemps: {r['ms']:.1f} ms")
        else:
            print(f"\n[ERREUR] {r['err']}")
        print("-" * 60 + "\n")

if __name__ == "__main__":
    main()