# db_auth.py
import os
import re
import oracledb
from passlib.context import CryptContext
from typing import Optional, Dict, Any, List, Tuple

pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")

ORACLE_DSN   = os.getenv("DB_DSN", "127.0.0.1:1521/XE")
ORACLE_USER  = os.getenv("DB_USER", "pfe_chatbot")
ORACLE_PASS  = os.getenv("DB_PASSWORD", "pfe2026")
ORACLE_LIB   = os.getenv("ORACLE_LIB_DIR", r"C:\instantclient_23_0")

VALID_ROLES = {"EMPLOYE", "MANAGER", "DIRECTION", "RH_COMPLET", "ADMIN"}


class OracleAuth:
    def __init__(self):
        oracledb.init_oracle_client(lib_dir=ORACLE_LIB)
        c = self._connect()
        c.close()

    def _connect(self):
        return oracledb.connect(user=ORACLE_USER, password=ORACLE_PASS, dsn=ORACLE_DSN)

    @staticmethod
    def _make_username(prenom: str, nom: str) -> str:
        pren = re.sub(r"[^a-z0-9]", "", (prenom or "").lower())
        nom_ = re.sub(r"[^a-z0-9]", "", (nom or "").lower())
        return f"{pren}.{nom_}" if pren and nom_ else f"user.{nom_ or pren or 'unknown'}"

    def register(self, mat_pers: str, password: str) -> Tuple[bool, str]:
        """
        Inscription étape 1 : matricule + mot de passe.
        Retourne (success, message).
        """
        mat_pers = mat_pers.strip()
        if len(password) < 6:
            return False, "Le mot de passe doit contenir au moins 6 caractères."

        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()

            cur.execute("""
                SELECT MAT_PERS, TRIM(NOM_PERS), TRIM(PREN_PERS), COD_SERV
                FROM PERSONNEL
                WHERE MAT_PERS = :1 AND ETAT_ACT = 'A'
            """, (mat_pers,))
            emp = cur.fetchone()
            if not emp:
                return False, "Matricule introuvable ou employé inactif."

            _, nom, prenom, cod_serv = emp

            cur.execute("""
                SELECT STATUT FROM APP_USERS WHERE MAT_PERS = :1
            """, (mat_pers,))
            existing = cur.fetchone()
            if existing:
                statut = (existing[0] or "").strip()
                if statut == "ACTIF":
                    return False, "Un compte actif existe déjà pour ce matricule."
                if statut == "PENDING":
                    return False, "Une demande d'inscription est déjà en attente de validation."
                if statut == "REJETE":
                    cur.execute("DELETE FROM APP_USERS WHERE MAT_PERS = :1", (mat_pers,))

            username = self._make_username(prenom, nom)
            cur.execute("SELECT COUNT(*) FROM APP_USERS WHERE USERNAME = :1", (username,))
            if cur.fetchone()[0] > 0:
                username = f"{username}.{mat_pers[-3:]}"

            pass_hash = pwd_context.hash(password)
            cod_serv = str(cod_serv).strip() if cod_serv else None

            cur.execute("""
                INSERT INTO APP_USERS
                    (MAT_PERS, USERNAME, PASS_HASH, COD_SERV, ROLE, ACTIVE, STATUT)
                VALUES (:1, :2, :3, :4, NULL, '0', 'PENDING')
            """, (mat_pers, username, pass_hash, cod_serv))
            conn.commit()
            return True, (
                f"Inscription enregistrée pour {prenom} {nom}. "
                f"Identifiant : {username}. "
                "Un administrateur validera votre rôle sous peu."
            )
        except Exception as e:
            print(f"[OracleAuth] register: {e}")
            return False, f"Erreur lors de l'inscription : {e}"
        finally:
            if conn:
                conn.close()

    def authenticate(self, username: str, password: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        Authentification.
        Retourne (user_dict, error_code).
        error_code : 'pending' | 'rejected' | 'no_role' | None
        user_dict est None si identifiants incorrects.
        """
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT MAT_PERS, USERNAME, PASS_HASH, COD_SERV, ROLE, STATUT, ACTIVE
                FROM APP_USERS
                WHERE USERNAME = :1
            """, (username.lower().strip(),))

            row = cur.fetchone()
            if not row:
                return None, None

            mat_pers, db_user, pass_hash, cod_serv, role, statut, active = row
            pass_hash = pass_hash.strip() if pass_hash else None
            statut = (statut or "ACTIF").strip()

            if not pwd_context.verify(password, pass_hash):
                return None, None

            if statut == "PENDING":
                return None, "pending"
            if statut == "REJETE":
                return None, "rejected"
            if statut != "ACTIF" or active != "1":
                return None, "inactive"

            role_clean = role.strip() if role else None
            if not role_clean:
                return None, "no_role"

            return {
                "mat_pers": str(mat_pers).strip(),
                "username": db_user,
                "cod_serv": cod_serv.strip() if cod_serv else None,
                "role": role_clean,
                "statut": statut,
            }, None
        except Exception as e:
            print(f"[OracleAuth] authenticate: {e}")
            return None, None
        finally:
            if conn:
                conn.close()

    def get_user_by_mat(self, mat_pers: str) -> Optional[Dict[str, Any]]:
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT u.MAT_PERS, u.USERNAME, u.COD_SERV, u.ROLE, u.STATUT,
                       p.NOM_PERS, p.PREN_PERS
                FROM APP_USERS u
                JOIN PERSONNEL p ON p.MAT_PERS = u.MAT_PERS
                WHERE u.MAT_PERS = :1 AND u.STATUT = 'ACTIF' AND u.ACTIVE = '1'
            """, (mat_pers,))
            row = cur.fetchone()
            if not row:
                return None
            return {
                "mat_pers": str(row[0]).strip(),
                "username": row[1],
                "cod_serv": row[2].strip() if row[2] else None,
                "role": row[3].strip() if row[3] else None,
                "statut": row[4].strip() if row[4] else "ACTIF",
                "nom": row[5].strip() if row[5] else None,
                "prenom": row[6].strip() if row[6] else None,
            }
        finally:
            if conn:
                conn.close()

    def get_pending_users(self) -> List[Dict[str, Any]]:
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT u.MAT_PERS, u.USERNAME, u.COD_SERV,
                       p.NOM_PERS, p.PREN_PERS, u.STATUT
                FROM APP_USERS u
                JOIN PERSONNEL p ON p.MAT_PERS = u.MAT_PERS
                WHERE u.STATUT = 'PENDING'
                ORDER BY u.MAT_PERS
            """)
            return [
                {
                    "mat_pers": str(r[0]).strip(),
                    "username": r[1],
                    "cod_serv": r[2].strip() if r[2] else None,
                    "nom": r[3].strip() if r[3] else None,
                    "prenom": r[4].strip() if r[4] else None,
                    "statut": r[5].strip() if r[5] else "PENDING",
                }
                for r in cur.fetchall()
            ]
        finally:
            if conn:
                conn.close()

    def approve_user(self, mat_pers: str, role: str) -> Tuple[bool, str]:
        role = role.strip().upper()
        if role not in VALID_ROLES:
            return False, f"Rôle invalide. Valeurs acceptées : {', '.join(sorted(VALID_ROLES))}"

        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT STATUT FROM APP_USERS WHERE MAT_PERS = :1
            """, (mat_pers,))
            row = cur.fetchone()
            if not row:
                return False, "Utilisateur introuvable."
            if (row[0] or "").strip() != "PENDING":
                return False, "Cet utilisateur n'est pas en attente de validation."

            cur.execute("""
                UPDATE APP_USERS
                SET ROLE = :1, STATUT = 'ACTIF', ACTIVE = '1'
                WHERE MAT_PERS = :2
            """, (role, mat_pers))
            conn.commit()
            return True, f"Compte {mat_pers} approuvé avec le rôle {role}."
        except Exception as e:
            print(f"[OracleAuth] approve_user: {e}")
            return False, str(e)
        finally:
            if conn:
                conn.close()

    def reject_user(self, mat_pers: str) -> Tuple[bool, str]:
        conn = None
        try:
            conn = self._connect()
            cur = conn.cursor()
            cur.execute("""
                SELECT STATUT FROM APP_USERS WHERE MAT_PERS = :1
            """, (mat_pers,))
            row = cur.fetchone()
            if not row:
                return False, "Utilisateur introuvable."
            if (row[0] or "").strip() != "PENDING":
                return False, "Cet utilisateur n'est pas en attente de validation."

            cur.execute("""
                UPDATE APP_USERS
                SET STATUT = 'REJETE', ACTIVE = '0', ROLE = NULL
                WHERE MAT_PERS = :1
            """, (mat_pers,))
            conn.commit()
            return True, f"Demande de {mat_pers} rejetée."
        except Exception as e:
            print(f"[OracleAuth] reject_user: {e}")
            return False, str(e)
        finally:
            if conn:
                conn.close()


auth_db = OracleAuth()
