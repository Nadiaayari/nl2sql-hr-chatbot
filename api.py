# api.py

from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator, model_validator
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta
import logging
from jose import JWTError, jwt
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm

from chatbot_core import process, get_connection, detect_language
from llm_client import llm_client, MODEL_PRIMARY, MODEL_FAST, OPENROUTER_API_KEY, GEMINI_API_KEY
from db_auth import auth_db, VALID_ROLES

logger = logging.getLogger(__name__)

# ── CONFIG ──
SECRET_KEY = "chatbot_rh_sante_2026_secret_key"   # CHANGEZ EN PROD
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 480

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")

ALLOWED_SERVICES = {"SERV_RH", "DIR_GEN", "DIR_FILIAL", "SERV_IT", "SERV_COMPT"}

ROLE_PERMISSIONS = {
    "ADMIN":      "Accès total — toutes les données de tous les employés",
    "RH_COMPLET": "Accès total — toutes les données de tous les employés",
    "DIRECTION":  "Ses propres infos + statistiques globales + informations générales",
    "MANAGER":    "Ses propres infos + statistiques globales + informations générales",
    "EMPLOYE":    "Ses propres données uniquement",
}

ADMIN_ROLES = {"ADMIN", "RH_COMPLET"}

# ── APP ──
app = FastAPI(title="Chatbot RH & Santé API", version="2.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── AUTH ──
def create_access_token(data: dict):
    to_encode = data.copy()
    expire = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, SECRET_KEY, algorithm=ALGORITHM)


def get_current_user(token: str = Depends(oauth2_scheme)):
    creds_exc = HTTPException(status_code=401, detail="Token invalide ou expiré")
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        mat_pers = payload.get("sub")
        if mat_pers is None:
            raise creds_exc
    except JWTError:
        raise creds_exc

    user = auth_db.get_user_by_mat(mat_pers)
    if user is None:
        raise HTTPException(status_code=401, detail="Compte désactivé ou non validé")
    if user["cod_serv"] not in ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail="Service non habilité")
    return user


def require_role(*roles: str):
    def checker(current_user: dict = Depends(get_current_user)):
        if current_user["role"] not in roles:
            raise HTTPException(
                status_code=403,
                detail=f"Accès réservé aux rôles : {', '.join(roles)}"
            )
        return current_user
    return checker


require_admin = require_role(*ADMIN_ROLES)

# ── MODELS ──
class RegisterRequest(BaseModel):
    mat_pers: str
    password: str
    password_confirm: str

    @field_validator("mat_pers")
    @classmethod
    def mat_not_empty(cls, v):
        if not v.strip():
            raise ValueError("Matricule requis")
        return v.strip()

    @model_validator(mode="after")
    def passwords_match(self):
        if self.password != self.password_confirm:
            raise ValueError("Les mots de passe ne correspondent pas")
        return self


class ApproveRequest(BaseModel):
    role: str

    @field_validator("role")
    @classmethod
    def role_valid(cls, v):
        r = v.strip().upper()
        if r not in VALID_ROLES:
            raise ValueError(f"Rôle invalide. Acceptés : {', '.join(sorted(VALID_ROLES))}")
        return r


class QuestionRequest(BaseModel):
    question: str
    history: Optional[List[Dict[str, str]]] = []


class QueryResponse(BaseModel):
    question: str
    lang: str
    sql: str
    colonnes: list[str]
    lignes: list[list]
    nb_lignes: int
    reponse_nl: Optional[str] = None
    erreur: Optional[str] = None


# ── ROUTES PUBLIQUES ──
@app.post("/register")
def register(req: RegisterRequest):
    """Inscription : matricule + mot de passe → compte PENDING."""
    ok, msg = auth_db.register(req.mat_pers, req.password)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}


@app.post("/login")
def login(form_data: OAuth2PasswordRequestForm = Depends()):
    user, err = auth_db.authenticate(form_data.username, form_data.password)

    if err == "pending":
        raise HTTPException(
            status_code=403,
            detail="Compte en attente de validation par un administrateur."
        )
    if err == "rejected":
        raise HTTPException(
            status_code=403,
            detail="Votre demande d'inscription a été rejetée. Contactez les RH."
        )
    if err == "no_role":
        raise HTTPException(
            status_code=403,
            detail="Compte actif mais rôle non assigné. Contactez un administrateur."
        )
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Identifiant ou mot de passe incorrect."
        )

    if user["cod_serv"] not in ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail="Service non habilité")

    token = create_access_token({
        "sub": user["mat_pers"],
        "username": user["username"],
        "cod_serv": user["cod_serv"],
        "role": user["role"],
    })
    return {"access_token": token, "token_type": "bearer"}


@app.get("/")
def root():
    return {"message": "Chatbot RH & Santé API", "status": "running"}


@app.get("/health")
def health():
    return {
        "status": "ok",
        "llm": {
            "active_provider": llm_client.active_provider,
            "active_model": llm_client.active_model,
            "primary_model": MODEL_PRIMARY,
            "fast_model": MODEL_FAST,
            "fallback_openrouter": bool(OPENROUTER_API_KEY),
            "fallback_gemini": bool(GEMINI_API_KEY),
        },
    }


# ── ROUTES AUTHENTIFIÉES ──
@app.get("/me")
def me(current_user: dict = Depends(get_current_user)):
    return {
        "mat_pers": current_user["mat_pers"],
        "nom": current_user["nom"],
        "prenom": current_user["prenom"],
        "service": current_user["cod_serv"],
        "role": current_user["role"],
        "permissions": ROLE_PERMISSIONS.get(current_user["role"], "Inconnu"),
        "is_admin": current_user["role"] in ADMIN_ROLES,
    }


@app.get("/history")
def get_history(current_user: dict = Depends(get_current_user)):
    conn = get_connection()
    cur = conn.cursor()
    cur.execute(
        """SELECT QUESTION, REPONSE, NB_LIGNES, DATE_CONV FROM CONVERSATIONS
           WHERE MAT_PERS = :1 ORDER BY DATE_CONV DESC FETCH FIRST 20 ROWS ONLY""",
        (current_user["mat_pers"],)
    )
    rows = [
        {"question": r[0], "reponse": r[1], "nb_lignes": r[2], "date": r[3]}
        for r in cur.fetchall()
    ]
    conn.close()
    return rows


def save_conversation(mat_pers: str, question: str, reponse: str, nb_lignes: int):
    """Persiste un échange dans CONVERSATIONS (best-effort)."""
    try:
        conn = get_connection()
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO CONVERSATIONS (MAT_PERS, QUESTION, REPONSE, NB_LIGNES, DATE_CONV)
               VALUES (:1, :2, :3, :4, CURRENT_TIMESTAMP)""",
            (mat_pers, question[:500], (reponse or "")[:2000], nb_lignes)
        )
        conn.commit()
        cur.close()
        conn.close()
    except Exception as e:
        logger.warning(f"[CONVERSATIONS] Échec sauvegarde: {e}")


@app.post("/query", response_model=QueryResponse)
def query(req: QuestionRequest, current_user: dict = Depends(get_current_user)):
    """Tout utilisateur authentifié peut interroger le chatbot (RBAC dans chatbot_core)."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question vide")

    user_context = {
        "mat_pers": current_user["mat_pers"],
        "cod_serv": current_user["cod_serv"],
        "role": current_user["role"],
    }

    try:
        resultat = process(question, history=req.history or [], user_context=user_context)
        reponse_nl = resultat.get("reponse_nl")
        if not resultat.get("err"):
            save_conversation(
                current_user["mat_pers"], question, reponse_nl or "", resultat.get("n", 0)
            )
        return QueryResponse(
            question=question,
            lang=resultat.get("lang", "fr"),
            sql=resultat.get("sql_final", ""),
            colonnes=resultat.get("cols", []),
            lignes=resultat.get("rows", []),
            nb_lignes=resultat.get("n", 0),
            reponse_nl=reponse_nl,
            erreur=resultat.get("err"),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── ROUTES ADMIN ──
@app.get("/admin/pending-users")
def admin_pending_users(current_user: dict = Depends(require_admin)):
    return auth_db.get_pending_users()


@app.patch("/admin/users/{mat_pers}/approve")
def admin_approve_user(
    mat_pers: str,
    req: ApproveRequest,
    current_user: dict = Depends(require_admin),
):
    ok, msg = auth_db.approve_user(mat_pers, req.role)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}


@app.patch("/admin/users/{mat_pers}/reject")
def admin_reject_user(
    mat_pers: str,
    current_user: dict = Depends(require_admin),
):
    ok, msg = auth_db.reject_user(mat_pers)
    if not ok:
        raise HTTPException(status_code=400, detail=msg)
    return {"message": msg}


@app.get("/cache")
def get_cache():
    return {"taille": 0}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api:app", host="0.0.0.0", port=8000, reload=True)
