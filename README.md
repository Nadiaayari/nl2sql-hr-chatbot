# NL2SQL HR Chatbot

Chatbot RH intelligent permettant d'interroger une base de données Oracle en langage naturel, avec authentification JWT et contrôle d'accès basé sur les rôles (RBAC).

## Démo

<!-- Remplace ce lien par celui généré automatiquement quand tu glisses ta vidéo dans l'éditeur GitHub -->
https://github.com/user-attachments/assets/TON-LIEN-VIDEO-ICI

## Fonctionnalités

- Traduction de questions en langage naturel vers des requêtes SQL (NL2SQL)
- Authentification sécurisée via JWT
- Contrôle d'accès multi-niveaux (RBAC) : classification d'intention, détection d'injection de prompt, validation SQL, filtrage des résultats
- Support multilingue (français, anglais, arabe)
- Intégration LLM via Groq (LLaMA) avec fallback multi-fournisseurs

## Stack technique

- **Backend** : FastAPI, Python
- **Base de données** : Oracle 10g XE
- **LLM** : LLaMA via Groq, avec fallback OpenRouter / Google Gemini
- **Frontend** : Vite, JavaScript
- **Sécurité** : JWT, RBAC multi-couches

## Installation

```bash
git clone https://github.com/Nadiaayari/nl2sql-hr-chatbot.git
cd nl2sql-hr-chatbot
pip install -r requirements.txt
cp env.example .env   # puis renseigne tes propres clés API
```

### Frontend

```bash
cd frontend
npm install
npm run dev
```

### Backend

```bash
uvicorn api:app --reload
```

## Configuration

Copie `env.example` vers `.env` et renseigne tes propres clés (Groq, base de données, etc.). Ne commite jamais ton fichier `.env`.

## Auteur

Nadia Ayari — Étudiante en Data Science Engineering, ESPRIT
