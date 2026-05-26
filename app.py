"""Streamlit app : Roll Analyzer Wheel pour CSP en danger.

Wrapper UI autour de roll_analyzer.py. Déployable sur Streamlit Cloud.

Lancement local :
    streamlit run app.py

Secrets attendus (dans .streamlit/secrets.toml en local, dashboard en prod) :
    password        = "..."    # auth principale (utilisateurs)
    smtp_user       = "..."    # expéditeur Gmail
    smtp_password   = "..."    # App password Gmail
    smtp_to         = "..."    # destinataire feedback (admin)
    counter_visit   = "..."    # namespace counterapi visites
    counter_usage   = "..."    # namespace counterapi clics Analyser
    admin_url_token = "..."    # token URL pour exclure l'admin (?admin=TOKEN)
"""

from __future__ import annotations

import smtplib
from datetime import datetime, date
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email import encoders
from zoneinfo import ZoneInfo

import requests
import streamlit as st
import yfinance as yf

from roll_analyzer import (
    DTE_MIN, DTE_MAX, RATIO_ASSIGN, RATIO_ROLL, FRAIS_PAR_LEG_DEFAUT,
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

st.markdown('<meta name="google" content="notranslate">', unsafe_allow_html=True)


# ================================================================
# Auth
# ================================================================
def check_password() -> bool:
    """Authentification simple par mot de passe partagé.

    Utilise st.empty() pour pouvoir effacer le form du DOM après validation.
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

    placeholder = st.empty()
    submitted = False
    pwd = ""
    with placeholder.container():
        st.title("🎯 Roll Analyzer Wheel")
        st.caption("Accès restreint — entre le mot de passe partagé.")
        with st.form("auth_form", clear_on_submit=True):
            pwd = st.text_input("Mot de passe", type="password")
            submitted = st.form_submit_button("Valider")
        if submitted and pwd != expected:
            st.error("Mot de passe incorrect.")

    if submitted and pwd == expected:
        st.session_state.authenticated = True
        placeholder.empty()
        return True
    return False


if not check_password():
    st.stop()


# ================================================================
# Compteur de visites (incrémenté 1x par session)
# ================================================================
def _est_admin() -> bool:
    """Vrai si l'utilisateur courant est admin (skip compteurs + voit stats).

    Détection via query param ?admin=TOKEN qui matche st.secrets['admin_url_token'].
    L'admin doit bookmarker l'URL avec le token pour conserver ses droits.
    """
    if st.session_state.get("admin_unlocked"):
        return True
    try:
        token = st.secrets.get("admin_url_token")
        if token and st.query_params.get("admin") == token:
            st.session_state.admin_unlocked = True
            return True
    except Exception:
        pass
    return False


def _incrementer_compteur(key_name: str, dedupe_session: bool = False) -> None:
    """Incrémente un compteur counterapi.dev (silencieux si échec).

    Args:
        key_name: clé du secret à utiliser ('counter_visit' ou 'counter_usage').
        dedupe_session: si True, n'incrémente qu'une fois par session.
    """
    if dedupe_session and st.session_state.get(f"counted_{key_name}"):
        return
    try:
        ns = st.secrets.get(key_name)
        if not ns:
            return
        # counterapi.dev v1 : /{namespace}/{counter}/up incrémente
        requests.get(f"https://api.counterapi.dev/v1/{ns}/counter/up", timeout=3)
        if dedupe_session:
            st.session_state[f"counted_{key_name}"] = True
    except Exception:
        pass


def _lire_compteur(key_name: str) -> int | None:
    """Lit la valeur courante d'un compteur. Retourne None si indisponible."""
    try:
        ns = st.secrets.get(key_name)
        if not ns:
            return None
        # counterapi.dev v1 : /{namespace}/{counter} retourne {"count": N, ...}
        r = requests.get(f"https://api.counterapi.dev/v1/{ns}/counter", timeout=3)
        r.raise_for_status()
        return int(r.json().get("count", 0))
    except Exception:
        return None


# Visites : 1 fois par session, sauf si admin
if not _est_admin():
    _incrementer_compteur("counter_visit", dedupe_session=True)


# ================================================================
# Header
# ================================================================
st.title("🎯 Roll Analyzer Wheel")
st.caption(
    "Outil de décision pour CSP en danger : compare *roller* vs "
    "*accepter l'assignation + vendre un covered call*. Verdict basé sur le "
    "ratio des yields annualisés à horizon égal."
)
st.caption(
    "⚠️ Désactive la traduction automatique de ton navigateur sur cette page "
    "(clic droit → *Afficher la page d'origine*), sinon les termes financiers "
    "seront mal traduits (Strike → Grève, Ticker → Télescripteur…)."
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
        help="Strike du put que tu as déjà vendu (ta position actuelle).",
    )
with col2:
    today = datetime.now(_PARIS).date()
    expiry_csp = st.date_input(
        "Expiration actuelle",
        value=today,
        min_value=today,
        format="DD/MM/YYYY",
    )
    premiums_cumul_total = st.number_input(
        "Cumul primes encaissées (montant total $)",
        min_value=0.0, value=0.0, step=1.0, format="%.2f",
        help="Montant total reçu en dollars, tous contrats confondus. "
             "Ex : prime 0.35$/action × 2 contrats × 100 actions = entre 70$.",
    )

col3, col4, col5 = st.columns(3)
with col3:
    nb_contrats = st.number_input(
        "Nombre de contrats",
        min_value=1, value=1, step=1,
        help="Nombre de contrats CSP ouverts sur cette position.",
    )
with col4:
    frais_passes = st.number_input(
        "Frais déjà payés ($)",
        min_value=0.0, value=0.0, step=1.0, format="%.2f",
        help="Total des frais broker déjà payés sur cette position "
             "(ouverture + rolls précédents). Déduit du cumul de primes.",
    )
with col5:
    frais_roll = st.number_input(
        "Frais du roll ($)",
        min_value=0.0, value=0.0, step=0.50, format="%.2f",
        help="Frais affichés par ton broker pour le roll "
             "(rachat + vente combinés, tous contrats). "
             "Visible dans l'outil Roll Order chez IBKR/MEXEM.",
    )

analyser = st.button("🔍 Analyser", use_container_width=True, type="primary")

st.divider()

if not analyser:
    st.info(
        "ℹ️ Remplis les champs au-dessus et clique **Analyser**.  \n"
        "Le module compare :  \n"
        "- **Trajectoire A** : meilleur roll à crédit net positif (fenêtre 7-28 DTE)  \n"
        "- **Trajectoire B** : assignation + covered call au strike `max(PRU, spot)`  \n"
        "  \n"
        f"Verdict : ratio B/A > {RATIO_ASSIGN} → ASSIGNATION, < {RATIO_ROLL} → ROLL, sinon ÉQUIVALENT.  \n"
        "Règle prioritaire : si PRU > spot, force ROLL (philosophie wheel orthodoxe)."
    )

else:
    # ================================================================
    # Analyse
    # ================================================================
    ticker = ticker.strip().upper()
    premiums_cumul = (premiums_cumul_total - frais_passes) / (nb_contrats * 100.0)
    frais_par_leg = frais_roll / (2 * max(nb_contrats, 1))

    if not ticker:
        st.error("Ticker vide.")
        st.stop()
    if expiry_csp <= today:
        st.error("L'expiration doit être strictement future.")
        st.stop()

    with st.spinner(f"Analyse de {ticker}..."):
        try:
            ticker_obj = yf.Ticker(ticker)
            spot, last_ts = fetch_spot(ticker_obj)
            expiries = fetch_expirations_window(ticker_obj, today)
            prix_rachat = prix_rachat_csp(ticker_obj, strike_csp, expiry_csp)
            candidats, stats = generer_candidats_roll(
                ticker_obj, spot, strike_csp, expiry_csp, expiries, prix_rachat, today,
                frais_par_leg=frais_par_leg,
            )
            pru_net = calc_pru_net(strike_csp, premiums_cumul)
            traj_b = None
            if candidats:
                traj_b = evaluer_trajectoire_b(
                    ticker_obj, candidats[0], pru_net, spot, today,
                    frais_par_leg=frais_par_leg,
                )
            verdict_code, ratio = calculer_verdict(candidats, traj_b, spot, pru_net)
        except RuntimeError as e:
            st.error(f"❌ {e}")
            st.stop()
        except Exception as e:
            st.error(f"❌ Erreur inattendue : {e}")
            st.stop()

    # Incrémente le compteur d'utilisations (analyse réussie), sauf si admin
    if not _est_admin():
        _incrementer_compteur("counter_usage", dedupe_session=False)

    ouvert = marche_us_ouvert()
    spot_label = "temps réel" if ouvert else f"clôture du {last_ts.strftime('%d-%m-%Y')}"

    expiry_fr = expiry_csp.strftime("%d-%m-%Y")
    st.subheader(f"📋 Position : {ticker} ${strike_csp:.2f} exp {expiry_fr}")
    c1, c2, c3 = st.columns(3)
    c1.metric("Spot", f"${spot:.2f}", help=spot_label)
    c2.metric("PRU net", f"${pru_net:.2f}",
               help=f"strike ${strike_csp:.2f} − (primes {premiums_cumul_total:.2f}$ "
                    f"− frais passés {frais_passes:.2f}$) / {nb_contrats * 100}")
    c3.metric("Rachat CSP", f"${prix_rachat:.2f}",
               help=f"Mid du put {strike_csp} @ {expiry_fr}")
    frais_cc = frais_roll / 2
    frais_parts = []
    if frais_passes > 0:
        frais_parts.append(f"Frais passés : {frais_passes:.2f}$ (déduits des primes)")
    if frais_roll > 0:
        frais_parts.append(
            f"Frais roll : {frais_roll:.2f}$ (déduits du crédit) · "
            f"Frais CC estimés : {frais_cc:.2f}$ (½ du roll)"
        )
    if frais_parts:
        st.caption("📊 " + " · ".join(frais_parts))

    # Trajectoires
    tA, tB = st.columns(2)
    with tA:
        st.markdown("### 📈 Trajectoire A — Roll")
        if candidats:
            top = candidats[0]
            exp_fr = datetime.strptime(top['expiry'], "%Y-%m-%d").strftime("%d-%m-%Y")
            credit_total = top['credit_net'] * nb_contrats * 100
            st.markdown(
                f"**Strike** : ${top['strike']:.2f}  \n"
                f"**Expiration** : {exp_fr} (+{top['dte_add']}j)  \n"
                f"**Crédit net** : ${top['credit_net']:+.2f}/action "
                f"(**${credit_total:+,.0f}** total)  \n"
                f"**|Δ|** : {abs(top['delta']):.3f}  \n"
                f"**Yield annualisé** : {top['yield_ann']*100:.2f}%"
            )
            if frais_roll > 0:
                st.caption("ℹ️ Crédit net = prime roll − rachat CSP − frais roll")
            else:
                st.caption("ℹ️ Crédit net = prime roll − rachat CSP")
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
            exp_b_fr = datetime.strptime(traj_b['expiry'], "%Y-%m-%d").strftime("%d-%m-%Y")
            prime_total = traj_b['prime_cc'] * nb_contrats * 100
            st.markdown(
                f"**PRU après assignation** : ${traj_b['pru_net']:.2f}  \n"
                f"**Strike CC** : ${traj_b['strike_cc']:.2f}  \n"
                f"**Expiration CC** : {exp_b_fr} (+{traj_b['dte_cc']}j)  \n"
                f"**Prime CC** : ${traj_b['prime_cc']:.2f}/action "
                f"(**${prime_total:,.0f}** total)  \n"
                f"**Dividendes** : ${traj_b['dividendes']:.2f}  \n"
                f"**Yield annualisé** : {traj_b['yield_b']*100:.2f}%"
            )
        elif traj_b:
            st.warning(f"Non viable : {traj_b.get('raison', 'raison inconnue')}")
        else:
            st.info("Non évaluée (aucun top roll de référence).")

    # Top 3 candidats roll — full width pour éviter le scroll horizontal
    if candidats:
        with st.expander("Top 3 candidats roll (trajectoire A)"):
            rows = [{
                "Strike": f"${c['strike']:.2f}",
                "Expiry": datetime.strptime(c['expiry'], "%Y-%m-%d").strftime("%d-%m-%Y"),
                "DTE+": c['dte_add'],
                "Crédit/act": f"${c['credit_net']:+.2f}",
                "Crédit total": f"${c['credit_net'] * nb_contrats * 100:+,.0f}",
                "|Δ|": f"{abs(c['delta']):.3f}",
                "Annualisé %": f"{c['yield_ann']*100:.2f}",
                "Score %": f"{c['score']*100:.2f}",
            } for c in candidats[:3]]
            st.dataframe(rows, use_container_width=True, hide_index=True)

    # Verdict
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

    if not ouvert:
        st.caption("ℹ️ Marché US fermé : les prix d'options peuvent être obsolètes "
                    "(données de clôture).")


# ================================================================
# Feedback (commentaire + screenshot → mail à l'admin)
# ================================================================
st.divider()
with st.expander("💬 Laisser un retour"):
    st.caption("Ton message m'est envoyé directement, jamais visible par les autres utilisateurs.")
    fb_text = st.text_area("Commentaire", placeholder="Bug, suggestion, retour d'expérience...",
                            key="fb_text")
    fb_image = st.file_uploader("Screenshot (optionnel)", type=["png", "jpg", "jpeg"],
                                  key="fb_image")
    if st.button("Envoyer", key="fb_submit"):
        if not fb_text.strip():
            st.warning("Tape un message avant d'envoyer.")
        else:
            try:
                smtp_user = st.secrets["smtp_user"]
                smtp_password = st.secrets["smtp_password"]
                smtp_to = st.secrets["smtp_to"]
            except Exception:
                st.error("Configuration mail manquante côté serveur.")
                st.stop()
            try:
                msg = MIMEMultipart()
                msg["From"] = smtp_user
                msg["To"] = smtp_to
                msg["Subject"] = (
                    f"[Roll Analyzer] Feedback — {datetime.now(_PARIS).strftime('%d-%m-%Y %H:%M')}"
                )
                msg.attach(MIMEText(fb_text, "plain", "utf-8"))
                if fb_image is not None:
                    img_data = fb_image.read()
                    img = MIMEImage(img_data, name=fb_image.name)
                    img.add_header("Content-Disposition", "attachment", filename=fb_image.name)
                    msg.attach(img)
                with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
                    server.login(smtp_user, smtp_password.replace(" ", ""))
                    server.sendmail(smtp_user, [smtp_to], msg.as_string())
                st.success("✅ Merci ! Message envoyé.")
            except Exception as e:
                st.error(f"Échec d'envoi : {e}")


# ================================================================
# Footer + logo compteur admin
# ================================================================
st.divider()
st.caption(
    "⚠️ Pas un conseil financier · DYOR · Outil de décision support uniquement. "
    "Vérifie toujours les prix bid/ask dans ton broker avant d'agir."
)

# Stats admin (visible uniquement avec query param ?admin=TOKEN dans l'URL)
if _est_admin():
    visites = _lire_compteur("counter_visit")
    usages = _lire_compteur("counter_usage")
    parts = []
    parts.append(f"👁️ Visites : **{visites}**" if visites is not None else "👁️ Visites : n/a")
    parts.append(f"🔍 Analyses : **{usages}**" if usages is not None else "🔍 Analyses : n/a")
    st.caption("📊 " + " · ".join(parts) + "  (mode admin — non compté)")
