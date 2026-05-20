# Wheel Roll Analyzer — App web

Outil de décision pour CSP wheel en danger d'assignation : compare *roll* vs
*assignation + covered call* à horizon égal et propose un verdict.

🔒 Accès restreint par mot de passe (Streamlit Cloud).

## Lancement local

```bash
pip install -r requirements.txt
streamlit run app.py
```

Crée un fichier `.streamlit/secrets.toml` (modèle dans `.streamlit/secrets.toml.example`) :

```toml
password = "ton_mot_de_passe"
```

## Déploiement Streamlit Cloud

1. Connecte ce repo sur [share.streamlit.io](https://share.streamlit.io)
2. Main file : `app.py`
3. Settings → Secrets → ajoute :
   ```toml
   password = "TON_VRAI_PASSWORD"
   ```

## Logique

- **Trajectoire A — Roll** : meilleur candidat à crédit net positif (fenêtre 7-28 DTE), score = yield_annualisé × (1 − |delta|)
- **Trajectoire B — Assignation + CC** : assignation au strike, puis CC strike = max(PRU, spot)
- **Verdict** : ratio yield_B / yield_A
  - > 1.10 → ASSIGNATION
  - < 0.91 → ROLL
  - sinon → ÉQUIVALENT
- **Règle prioritaire** : si PRU > spot → force ROLL (philosophie wheel orthodoxe)

⚠️ Pas un conseil financier. DYOR.
