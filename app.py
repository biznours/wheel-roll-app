"""Streamlit app : Roll Analyzer Wheel pour CSP en danger.

Wrapper UI autour de roll_analyzer.py. Déployable sur Streamlit Cloud.

Lancement local :
    streamlit run app.py

Auth : mot de passe partagé via st.secrets["password"]
       - En local : .streamlit/secrets.toml
       - En prod  : configuré dans le dashboard Streamlit Cloud
"""

from __future__ import annotations

from datetime import datetime, date
from zoneinfo import ZoneInfo

import streamlit as st
import yfinance as yf

from roll_analyzer import (
    DTE_MIN, DTE_MAX, RATIO_ASSIGN, RATIO_ROLL,
    fetch_spot, fetch_expirations_window, prix_rachat_csp,
    generer_candidats_roll, calc_pru_net, evaluer_trajectoire_b,
    calculer_verdict, marche_us_ouvert,
)

_PARIS = ZoneInfo("Europe/Paris")


# ================================================================
# Page config
# ================================================================
st.set_page_config(
    page_title="Roll Analyzer Wheel",
    page_icon="🎯",
    layout="centered",
    initial_sidebar_state="collapsed",
)


# ================================================================
# Auth
# ================================================================
def check_password() -> bool:
    """Authentification simple par mot de passe partagé.

    Le mot de passe est lu depuis st.secrets["password"]. Pas de st.rerun()
    pour éviter une erreur DOM transitoire dans certaines versions de
    navigateur (NotFoundError: removeChild).
    """
    if st.session_state.get("authenticated"):
        return True

    try:
        expected = st.secrets["password"]
    except Exception:
        st.title("🎯 Roll Analyzer Wheel")
        st.error("⚠️ Configuration manquante : aucun mot de passe défini "
                 "côté serveur (st.secrets['password']).")
        return False

    st.title("🎯 Roll Analyzer Wheel")
    st.caption("Accès restreint — entre le mot de passe partagé.")

    with st.form("auth_form", clear_on_submit=True):
        pwd = st.text_input("Mot de passe", type="password")
        submitted = st.form_submit_button("Valider")

    if submitted and pwd == expected:
        st.session_state.authenticated = True
        return True
    if submitted:
        st.error("Mot de passe incorrect.")
    return False


if not check_password():
    st.stop()


# ================================================================
# Header
# ================================================================
st.title("🎯 Roll Analyzer Wheel")
st.caption(
    "Outil de décision pour CSP en danger : compare *roller* vs "
    "*accepter l'assignation + vendre un covered call*. Verdict basé sur le "
    "ratio des yields annualisés à horizon égal."
)

st.divider()


# ================================================================
# Form
# ================================================================
col1, col2 = st.columns(2)
with col1:
    ticker = st.text_input("Ticker", value="HAL", help="Ex : HAL, F, INTC, KMI...")
    strike_csp = st.number_input(
        "Strike CSP actuel ($)",
        min_value=0.01, value=30.00, step=0.5, format="%.2f",
    )
with col2:
    today = datetime.now(_PARIS).date()
    expiry_csp = st.date_input(
        "Expiration actuelle",
        value=today,
        min_value=today,
        format="YYYY-MM-DD",
    )
    premiums_cumul = st.number_input(
        "Cumul premiums encaissés ($)",
        min_value=0.0, value=0.0, step=0.05, format="%.2f",
        help="Somme de toutes les primes reçues sur cette position (vente initiale + rolls).",
    )

analyser = st.button("🔍 Analyser", use_container_width=True, type="primary")

st.divider()

if not analyser:
    st.info(
        "ℹ️ Remplis les 4 champs au-dessus et clique **Analyser**.  \n"
        "Le module compare :  \n"
        "- **Trajectoire A** : meilleur roll à crédit net positif (fenêtre 7-28 DTE)  \n"
        "- **Trajectoire B** : assignation + covered call au strike `max(PRU, spot)`  \n"
        "  \n"
        f"Verdict : ratio B/A > {RATIO_ASSIGN} → ASSIGNATION, < {RATIO_ROLL} → ROLL, sinon ÉQUIVALENT.  \n"
        "Règle prioritaire : si PRU > spot, force ROLL (philosophie wheel orthodoxe)."
    )
    st.stop()


# ================================================================
# Analyse
# ================================================================
ticker = ticker.strip().upper()
if not ticker:
    st.error("Ticker vide.")
    st.stop()
if expiry_csp <= today:
    st.error("L'expiration doit être strictement future.")
    st.stop()

inputs = {
    "ticker": ticker,
    "strike_csp": strike_csp,
    "expiry_csp": expiry_csp,
    "premiums_cumul": premiums_cumul,
}

with st.spinner(f"Analyse de {ticker}..."):
    try:
        ticker_obj = yf.Ticker(ticker)
        spot, last_ts = fetch_spot(ticker_obj)
        expiries = fetch_expirations_window(ticker_obj, today)
        prix_rachat = prix_rachat_csp(ticker_obj, strike_csp, expiry_csp)
        candidats, stats = generer_candidats_roll(
            ticker_obj, spot, strike_csp, expiry_csp, expiries, prix_rachat, today
        )
        pru_net = calc_pru_net(strike_csp, premiums_cumul)
        traj_b = None
        if candidats:
            traj_b = evaluer_trajectoire_b(ticker_obj, candidats[0], pru_net, spot, today)
        verdict_code, ratio = calculer_verdict(candidats, traj_b, spot, pru_net)
    except RuntimeError as e:
        st.error(f"❌ {e}")
        st.stop()
    except Exception as e:
        st.error(f"❌ Erreur inattendue : {e}")
        st.stop()


# ================================================================
# Affichage : position
# ================================================================
ouvert = marche_us_ouvert()
spot_label = "temps réel" if ouvert else f"clôture du {last_ts.strftime('%d/%m')}"

st.subheader(f"📋 Position : {ticker} ${strike_csp:.2f} exp {expiry_csp.isoformat()}")
c1, c2, c3 = st.columns(3)
c1.metric("Spot", f"${spot:.2f}", help=spot_label)
c2.metric("PRU net", f"${pru_net:.2f}",
           help=f"strike ${strike_csp:.2f} − cumul ${premiums_cumul:.2f}")
c3.metric("Rachat CSP", f"${prix_rachat:.2f}",
           help=f"Mid du put {strike_csp} @ {expiry_csp.isoformat()}")


# ================================================================
# Affichage : trajectoires
# ================================================================
tA, tB = st.columns(2)

with tA:
    st.markdown("### 📈 Trajectoire A — Roll")
    if candidats:
        top = candidats[0]
        st.markdown(
            f"**Strike** : ${top['strike']:.2f}  \n"
            f"**Expiration** : {top['expiry']} (+{top['dte_add']}j)  \n"
            f"**Crédit net** : ${top['credit_net']:+.2f}  \n"
            f"**|Δ|** : {abs(top['delta']):.3f}  \n"
            f"**Yield annualisé** : {top['yield_ann']*100:.2f}%"
        )
        with st.expander("Top 3 candidats roll"):
            rows = [{
                "Strike": f"${c['strike']:.2f}",
                "Expiry": c['expiry'],
                "DTE+": c['dte_add'],
                "Crédit": f"${c['credit_net']:+.2f}",
                "|Δ|": f"{abs(c['delta']):.3f}",
                "YA%": f"{c['yield_ann']*100:.2f}",
                "Score%": f"{c['score']*100:.2f}",
            } for c in candidats[:3]]
            st.dataframe(rows, use_container_width=True, hide_index=True)
    else:
        st.warning("Aucun candidat à crédit net positif.")
        if stats["testes"] > 0:
            st.caption(
                f"{stats['testes']} candidats testés, "
                f"{stats['rejet_credit']} rejetés pour crédit négatif vs "
                f"rachat ${prix_rachat:.2f}."
            )

with tB:
    st.markdown("### 📉 Trajectoire B — Assignation + CC")
    if traj_b and traj_b.get("viable"):
        st.markdown(
            f"**PRU après assignation** : ${traj_b['pru_net']:.2f}  \n"
            f"**Strike CC** : ${traj_b['strike_cc']:.2f}  \n"
            f"**Expiration CC** : {traj_b['expiry']} (+{traj_b['dte_cc']}j)  \n"
            f"**Prime CC** : ${traj_b['prime_cc']:.2f}  \n"
            f"**Dividendes** : ${traj_b['dividendes']:.2f}  \n"
            f"**Yield annualisé** : {traj_b['yield_b']*100:.2f}%"
        )
    elif traj_b:
        st.warning(f"Non viable : {traj_b.get('raison', 'raison inconnue')}")
    else:
        st.info("Non évaluée (aucun top roll de référence).")


# ================================================================
# Verdict
# ================================================================
st.divider()
st.markdown("### 🎯 Verdict")

VERDICT_DISPLAY = {
    "ROLL_SOUS_LEAU": ("🛡️ ROLL recommandé", "info",
                       "Position sous l'eau (PRU > spot). Règle prioritaire wheel : on attend le rebond."),
    "ASSIGNATION": ("✅ ASSIGNATION recommandée", "success",
                     f"Ratio yield_B / yield_A > {RATIO_ASSIGN} : la trajectoire B est nettement plus rentable."),
    "ROLL": ("🔄 ROLL recommandé", "info",
             f"Ratio yield_B / yield_A < {RATIO_ROLL} : la trajectoire A est plus rentable."),
    "EQUIVALENT": ("⚖️ ÉQUIVALENT — préférence personnelle", "info",
                    "Les deux trajectoires sont économiquement équivalentes. Choisis selon ta thèse sur le titre."),
    "ROLL_PAS_DE_CC": ("🔄 ROLL recommandé", "info",
                        "Aucun strike CC viable >= max(PRU, spot). Trajectoire B non calculée."),
    "ASSIGNATION_PAS_DE_ROLL": ("✅ ASSIGNATION recommandée (pas de roll viable)", "success",
                                  "Aucun roll à crédit positif possible, mais l'assignation reste rentable."),
    "BLOQUEE": ("🚨 Position BLOQUÉE", "error",
                  "Aucun roll à crédit positif et l'assignation cristalliserait une perte. Attendre rebond ou exit à perte."),
}

label, level, justif = VERDICT_DISPLAY.get(verdict_code, ("Verdict inconnu", "warning", ""))

if level == "success":
    st.success(f"**{label}**")
elif level == "error":
    st.error(f"**{label}**")
elif level == "warning":
    st.warning(f"**{label}**")
else:
    st.info(f"**{label}**")

if ratio is not None:
    st.caption(f"Ratio yield_B / yield_A = **{ratio:.2f}** "
                f"(seuils : assignation > {RATIO_ASSIGN} / roll < {RATIO_ROLL})")
st.write(justif)


# ================================================================
# Footer
# ================================================================
st.divider()
st.caption(
    "⚠️ Pas un conseil financier · DYOR · Outil de décision support uniquement. "
    "Vérifie toujours les prix bid/ask dans ton broker avant d'agir."
)
if not ouvert:
    st.caption("ℹ️ Marché US fermé : les prix d'options peuvent être obsolètes "
                "(données de clôture).")
