# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
pip install -r requirements.txt
streamlit run app.py                      # web UI (default workflow)
python roll_analyzer.py                   # interactive CLI prompt
python roll_analyzer.py --ticker HAL --strike 30 --expiry 2025-06-20 --cumul 70 --contrats 1
```

No test suite, linter, or formatter is configured. Verification is manual: run `streamlit run app.py` and exercise the form, or run the CLI with a known ticker.

Local secrets go in `.streamlit/secrets.toml` (gitignored — copy `.streamlit/secrets.toml.example`). In production they live in the Streamlit Cloud dashboard. Required keys: `password`, `smtp_user`, `smtp_password`, `smtp_to`, `counter_visit`, `counter_usage`, `admin_url_token`.

## Architecture

Two-file Python app — a Streamlit UI wrapper sitting on a self-contained analytics module.

**`roll_analyzer.py`** owns all business logic and is independently runnable (CLI + interactive modes via `main()`). Pure functions take a `yfinance.Ticker` and return dicts; no Streamlit imports. This separation is deliberate — `app.py` imports the pure functions and renders results, the module itself stays UI-agnostic.

**`app.py`** is the Streamlit wrapper: auth gate, form rendering, calls into `roll_analyzer`, displays results, plus session-scoped concerns (visit counter via counterapi.dev, feedback form via Gmail SMTP, admin bypass via `?admin=TOKEN` query param).

### Decision pipeline (the core algorithm)

For a cash-secured put (CSP) at risk of assignment, compare two economically-equivalent-horizon trajectories:

1. **Trajectory A — Roll** (`generer_candidats_roll`): scan puts in expirations within `[DTE_MIN, DTE_MAX]` days (default 7–28), at strikes in `[spot * SPOT_FLOOR_RATIO, strike_csp]`. Compute `credit_net = mid_new_put - prix_rachat_csp - 2 * frais_par_share`. Reject negatives. Score = `yield_annualisé × (1 - |delta|)` (Black-Scholes delta via `calc_delta_put`). Top-scored candidate wins.

2. **Trajectory B — Assignation + Covered Call** (`evaluer_trajectoire_b`): at the *same expiry* as the top A candidate, pick the first call strike ≥ `max(PRU_net, spot)` (wheel "golden rule" — never sell CC below cost basis). Add expected dividends in the window. Yield = `(prime_nette + div) / PRU_net × 365/dte`.

3. **Verdict** (`calculer_verdict`): seven discrete codes driven by `ratio = yield_B / yield_A` against thresholds `RATIO_ASSIGN` (1.10) and `RATIO_ROLL` (0.91), plus three short-circuits: `PRU > spot` → forces `ROLL_SOUS_LEAU` (wheel orthodoxy: wait for rebound rather than crystallize a loss); A non-viable + B non-viable → `BLOQUEE`; A viable + no CC strike found → `ROLL_PAS_DE_CC`. Both `app.py` and `roll_analyzer.py` map each code to its own user-facing label/justification block — keep them in sync if you add a code.

### Tunable constants (top of `roll_analyzer.py`)

`TAUX_SANS_RISQUE`, `DTE_MIN`/`DTE_MAX`, `SPOT_FLOOR_RATIO`, `RATIO_ASSIGN`/`RATIO_ROLL`, `FRAIS_PAR_LEG_DEFAUT`. Changing thresholds shifts verdict distribution — `app.py` re-imports `RATIO_ASSIGN`/`RATIO_ROLL` for its info panel, so both layers stay coherent automatically.

### Conventions

- **French UI strings throughout** (verdict labels, captions, error messages). Keep new user-facing text in French; comments and identifiers are a mix.
- All `yfinance` calls funnel through helpers that raise `RuntimeError` with French messages on missing data — `app.py` catches these specifically and shows them via `st.error`. Generic `Exception` is a separate branch ("Erreur inattendue").
- Prices: always use `get_mid(bid, ask, last)` — handles NaN, zero, and missing fields uniformly. Never read `lastPrice` directly.
- Fees are stored as `frais_par_leg` ($/contract/leg) and converted to per-share inside the analyzer (`fps = frais_par_leg / 100`). The UI computes `frais_par_leg = frais_roll_total / (2 * nb_contrats)` before passing in.
- Admin mode (`?admin=TOKEN` matching `admin_url_token` secret) skips counter increments and surfaces visit/usage stats in the footer.

## Langue
- Toujours répondre et commenter le code en français.

## Style de travail attendu
- Explications directes, pas à pas (Git, VS Code, terminal inclus).
- Avant tout refactor important ou changement de logique structurante : exposer le plan et demander validation.
- Modifications ciblées et minimales par défaut ; ne pas réécrire des fichiers entiers sans raison.

## Règles métier (stratégie Wheel)
- Stratégie : Wheel (Sell Put → Assignment → Covered Call).
- Pas de margin, jamais.
- Logique de frais : credit roll déduit 2 legs (2 × fps), CC déduit 1 leg (fps). Préserver cette asymétrie.

## Contexte d'exécution (marché)
- Produit prévu pour fonctionner en MARCHÉ OUVERT, univers S&P 500 et Nasdaq 100.
- Des tests seront faits MARCHÉ FERMÉ par nécessité :
  - Ne pas conclure à un bug si les données temps réel sont absentes/figées hors cotation : c'est attendu.
  - Le code doit rester testable hors marché sans planter.

## Secrets (Streamlit)
- L'app utilise st.secrets : password, smtp_user, smtp_password, smtp_to, counter_visit, counter_usage, admin_url_token.
- En local : .streamlit/secrets.toml (gitignored, voir secrets.toml.example). En prod : dashboard Streamlit Cloud.
- Ne JAMAIS commiter de secrets réels ni les afficher en clair.

## Garde-fous anti-hallucination
- Ne jamais inventer de tickers, chiffres, prix ou données. Si une donnée manque, le dire.
- Se baser sur le code et les fichiers réels, pas sur des suppositions.
- En cas de doute sur l'intention, poser une question avant d'agir.

## Périmètre du dépôt
- Ce dépôt est l'ANALYZER officiel (wheel-roll-app) : seul projet à modifier ici.
- Le scanner est un dépôt SÉPARÉ (wheel-scanner, dans le même dossier parent). NE PAS le modifier depuis ici.