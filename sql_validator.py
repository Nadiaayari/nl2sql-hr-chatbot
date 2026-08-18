# sql_validator.py
"""
Validateur SQL avancé utilisant sqlglot pour l'analyse syntaxique et sémantique.
Fournit des corrections automatiques pour Oracle 10g.
"""

import re
from typing import Tuple, Optional, Callable

try:
    import sqlglot
    from sqlglot import exp
    SQLGLOT_AVAILABLE = True
except ImportError:
    SQLGLOT_AVAILABLE = False
    print("[WARNING] sqlglot non installé. Le validateur AST sera désactivé.")
    print("[INFO] Installez avec: pip install sqlglot")


def valider_et_corriger(sql: str, llm_corrector_fn: Optional[Callable] = None) -> Tuple[str, Optional[str]]:
    """
    Valide et corrige le SQL en utilisant sqlglot AST parser.

    Args:
        sql: Requête SQL à valider
        llm_corrector_fn: Fonction optionnelle pour correction LLM en cas d'erreur

    Returns:
        Tuple (sql_corrige, error_message ou None)
    """
    if not SQLGLOT_AVAILABLE:
        return sql, None

    if not sql or not sql.strip():
        return sql, "SQL vide"

    try:
        # Essayer de parser avec Oracle dialect
        parsed = sqlglot.parse_one(sql, read='oracle', dialect='oracle')

        # Vérifications sémantiques supplémentaires
        sql_corrige = _post_parse_corrections(sql, parsed)

        return sql_corrige, None

    except Exception as e:
        error_msg = str(e)

        # Tentative de correction automatique pour erreurs communes
        sql_corrige = _auto_fix_common_errors(sql, error_msg)

        # Si toujours erreur et LLM disponible
        if sql_corrige == sql and llm_corrector_fn:
            try:
                sql_corrige = llm_corrector_fn(
                    sql, 
                    f"Erreur de syntaxe SQL Oracle: {error_msg}"
                )
                # Re-valider après correction LLM
                try:
                    sqlglot.parse_one(sql_corrige, read='oracle', dialect='oracle')
                    return sql_corrige, None
                except:
                    pass
            except Exception:
                pass

        return sql_corrige, error_msg


def _post_parse_corrections(original_sql: str, parsed) -> str:
    """
    Corrections post-parsing basées sur l'AST.
    """
    sql = original_sql

    # Vérifier les fonctions non supportées Oracle 10g
    unsupported_funcs = ['LISTAGG', 'PIVOT', 'UNPIVOT', 'REGEXP_SUBSTR', 
                        'REGEXP_REPLACE', 'NVL2', 'COALESCE']

    for func in unsupported_funcs:
        if func.upper() in sql.upper():
            if func == 'COALESCE':
                # COALESCE → NVL chainé (simplifié)
                sql = re.sub(
                    r'COALESCE\s*\(([^,]+),\s*([^)]+)\)',
                    r'NVL(, )',
                    sql,
                    flags=re.IGNORECASE
                )

    return sql


def _auto_fix_common_errors(sql: str, error_msg: str) -> str:
    """
    Corrections automatiques pour erreurs sqlglot communes.
    """
    original = sql

    # 1. Erreur de parenthèses mal fermées
    if "Unexpected token" in error_msg or "Expecting" in error_msg:
        # Compter les parenthèses
        open_count = sql.count('(')
        close_count = sql.count(')')
        if open_count > close_count:
            sql += ')' * (open_count - close_count)
        elif close_count > open_count:
            # Trop de fermantes, difficile à corriger automatiquement
            pass

    # 2. Alias de table manquant dans ORDER BY
    if "ORDER BY" in sql.upper() and re.search(r'ORDER\s+BY\s+\w+\.\w+', sql, re.IGNORECASE):
        # Vérifier si l'alias est défini dans la requête externe
        pass  # Géré par corriger_semantique

    # 3. Double point-virgule
    sql = sql.rstrip(';').strip()

    # 4. Espaces multiples
    sql = ' '.join(sql.split())

    return sql if sql != original else original


def extraire_tables(sql: str) -> list:
    """
    Extrait la liste des tables utilisées dans le SQL.
    """
    if not SQLGLOT_AVAILABLE:
        # Fallback regex
        tables = re.findall(r'FROM\s+(\w+)', sql, re.IGNORECASE)
        tables += re.findall(r'JOIN\s+(\w+)', sql, re.IGNORECASE)
        return list(set(tables))

    try:
        parsed = sqlglot.parse_one(sql, read='oracle')
        tables = []
        for table in parsed.find_all(exp.Table):
            tables.append(table.name)
        return tables
    except:
        return []


def extraire_colonnes_select(sql: str) -> list:
    """
    Extrait les colonnes du SELECT.
    """
    if not SQLGLOT_AVAILABLE:
        return []

    try:
        parsed = sqlglot.parse_one(sql, read='oracle')
        columns = []
        for expr in parsed.find(exp.Select).expressions:
            columns.append(str(expr))
        return columns
    except:
        return []


def verifier_jointures(sql: str, schema_tables: dict) -> Tuple[bool, list]:
    """
    Vérifie que toutes les tables ont les conditions de jointure nécessaires.
    """
    warnings = []
    tables = extraire_tables(sql)

    # Vérifier les jointures manquantes pour les tables connues
    if 'PERSONNEL' in tables and 'BULT_SOIN' in tables:
        if not (re.search(r'P\.COD_SOC\s*=\s*B\.COD_SOC', sql) and 
                re.search(r'P\.MAT_PERS\s*=\s*B\.MAT_PERS', sql)):
            warnings.append("Jointure PERSONNEL-BULT_SOIN incomplète (COD_SOC + MAT_PERS)")

    if 'PERSONNEL' in tables and 'SERVICE' in tables:
        if not (re.search(r'P\.COD_SOC\s*=\s*S\.COD_SOC', sql) and 
                re.search(r'P\.COD_SERV\s*=\s*S\.COD_SERV', sql)):
            warnings.append("Jointure PERSONNEL-SERVICE incomplète (COD_SOC + COD_SERV)")

    return len(warnings) == 0, warnings
