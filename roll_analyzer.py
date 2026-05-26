# ================================================================
#  ROLL ANALYZER - v1
#  Decision support CSP : ROLL vs ASSIGNATION + Covered Call
#  Compare deux trajectoires economiques a horizon egal.
# ================================================================

import argparse
import sys
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import yfinance as yf
from scipy.stats import norm


# ================================================================
# CONSTANTES
# ================================================================
TAUX_SANS_RISQUE = 0.045
DTE_MIN = 7
DTE_MAX = 28
SPOT_FLOOR_RATIO = 0.85   # strike candidat roll min = spot * 0.85

# Seuils de verdict (ratio yield_B / yield_A)
RATIO_ASSIGN = 1.10
RATIO_ROLL = 0.91

FRAIS_PAR_LEG_DEFAUT = 1.25  # $/contrat/leg (MEXEM/IBKR)

_PARIS = ZoneInfo("Europe/Paris")
_NY = ZoneInfo("America/New_York")


# ================================================================
# INPUTS CLI (non interactif)
# ================================================================
def parse_args_cli() -> dict | None:
    """Parse les args CLI. Retourne dict d'inputs ou None si aucun arg fourni
    (auquel cas main() bascule sur le mode interactif).
    """
    parser = argparse.ArgumentParser(
        description="Roll analyzer pour CSP wheel — compare ROLL vs ASSIGNATION+CC.",
    )
    parser.add_argument("--ticker", type=str, help="Ticker (ex: HAL)")
    parser.add_argument("--strike", type=float, help="Strike CSP actuel ($)")
    parser.add_argument("--expiry", type=str, help="Expiration actuelle (YYYY-MM-DD)")
    parser.add_argument("--cumul", type=float, default=None,
                        help="Cumul total premiums encaissees ($), defaut 0")
    parser.add_argument("--contrats", type=int, default=1,
                        help="Nombre de contrats, defaut 1")
    parser.add_argument("--frais", type=float, default=FRAIS_PAR_LEG_DEFAUT,
                        help=f"Commission par contrat par leg ($), defaut {FRAIS_PAR_LEG_DEFAUT}")
    args = parser.parse_args()

    if args.ticker is None and args.strike is None and args.expiry is None and args.cumul is None:
        return None

    missing = []
    if args.ticker is None: missing.append("--ticker")
    if args.strike is None: missing.append("--strike")
    if args.expiry is None: missing.append("--expiry")
    if missing:
        parser.error(f"arguments manquants : {', '.join(missing)}")

    try:
        expiry = datetime.strptime(args.expiry, "%Y-%m-%d").date()
    except ValueError:
        parser.error(f"--expiry doit etre YYYY-MM-DD (recu : {args.expiry})")

    today = datetime.now(_PARIS).date()
    if expiry <= today:
        parser.error(f"--expiry doit etre future (recu : {args.expiry}, today {today})")
    if args.strike <= 0:
        parser.error(f"--strike doit etre > 0 (recu : {args.strike})")
    cumul_total = args.cumul if args.cumul is not None else 0.0
    if cumul_total < 0:
        parser.error(f"--cumul doit etre >= 0 (recu : {cumul_total})")
    if args.contrats < 1:
        parser.error(f"--contrats doit etre >= 1 (recu : {args.contrats})")
    if args.frais < 0:
        parser.error(f"--frais doit etre >= 0 (recu : {args.frais})")

    return {
        "ticker": args.ticker.strip().upper(),
        "strike_csp": args.strike,
        "expiry_csp": expiry,
        "premiums_cumul": cumul_total / (args.contrats * 100),
        "premiums_cumul_total": cumul_total,
        "nb_contrats": args.contrats,
        "frais_par_leg": args.frais,
    }


# ================================================================
# INPUTS INTERACTIFS
# ================================================================
def parse_inputs() -> dict:
    """Saisie interactive des parametres de la position CSP.

    Retourne un dict : ticker, strike_csp, expiry_csp (date), premiums_cumul.
    """
    print("=" * 64)
    print(" ROLL ANALYZER - decision support CSP")
    print("=" * 64)

    ticker = input("Ticker (ex: HAL) : ").strip().upper()
    if not ticker:
        raise ValueError("Ticker vide.")

    strike_csp = _saisir_float("Strike CSP actuel ($) : ", min_val=0.01)
    expiry_csp = _saisir_date("Expiration actuelle (YYYY-MM-DD) : ")
    nb_contrats = int(_saisir_float("Nombre de contrats : ", min_val=1.0))
    cumul_total = _saisir_float("Cumul primes encaissees (total $) : ", min_val=0.0)
    frais = _saisir_float(
        f"Commission par contrat/leg ($, defaut {FRAIS_PAR_LEG_DEFAUT}) : ",
        min_val=0.0,
    )

    return {
        "ticker": ticker,
        "strike_csp": strike_csp,
        "expiry_csp": expiry_csp,
        "premiums_cumul": cumul_total / (nb_contrats * 100),
        "premiums_cumul_total": cumul_total,
        "nb_contrats": nb_contrats,
        "frais_par_leg": frais,
    }


def _saisir_float(prompt: str, min_val: float = 0.0) -> float:
    while True:
        raw = input(prompt).strip().replace(",", ".")
        try:
            val = float(raw)
        except ValueError:
            print("  -> nombre invalide, reessaie.")
            continue
        if val < min_val:
            print(f"  -> valeur >= {min_val} attendue, reessaie.")
            continue
        return val


def _saisir_date(prompt: str) -> date:
    today = datetime.now(_PARIS).date()
    while True:
        raw = input(prompt).strip()
        try:
            d = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            print("  -> format YYYY-MM-DD attendu, reessaie.")
            continue
        if d <= today:
            print("  -> date d'expiration doit etre future, reessaie.")
            continue
        return d


# ================================================================
# HELPERS MARCHE
# ================================================================
def get_mid(bid, ask, last) -> float | None:
    """Mid robuste : (bid+ask)/2 si bid>0 et ask>0, sinon last si dispo, sinon None."""
    try:
        bid = float(bid) if bid is not None else 0.0
        ask = float(ask) if ask is not None else 0.0
        last = float(last) if last is not None else 0.0
    except (TypeError, ValueError):
        return None
    if np.isnan(bid):
        bid = 0.0
    if np.isnan(ask):
        ask = 0.0
    if np.isnan(last):
        last = 0.0
    if bid > 0 and ask > 0:
        return (bid + ask) / 2
    if last > 0:
        return last
    return None


# ================================================================
# BLACK-SCHOLES
# ================================================================
def calc_delta_put(spot: float, strike: float, dte: int, iv: float,
                   r: float = TAUX_SANS_RISQUE) -> float | None:
    """Delta d'un put via Black-Scholes (valeur signee, dans [-1, 0])."""
    if dte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    T = dte / 365.0
    try:
        d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
        return round(float(norm.cdf(d1) - 1), 4)
    except Exception:
        return None


def calc_delta_call(spot: float, strike: float, dte: int, iv: float,
                    r: float = TAUX_SANS_RISQUE) -> float | None:
    """Delta d'un call via Black-Scholes (dans [0, 1])."""
    if dte <= 0 or iv <= 0 or spot <= 0 or strike <= 0:
        return None
    T = dte / 365.0
    try:
        d1 = (np.log(spot / strike) + (r + 0.5 * iv ** 2) * T) / (iv * np.sqrt(T))
        return round(float(norm.cdf(d1)), 4)
    except Exception:
        return None


# ================================================================
# FETCH MARCHE
# ================================================================
def marche_us_ouvert() -> bool:
    """True si NYSE est dans sa fenetre regular trading (lun-ven 9h30-16h NY).

    Approximation : ignore les holidays NYSE. Suffisant pour le libelle
    d'affichage "temps reel" vs "cloture du JJ/MM".
    """
    now_ny = datetime.now(_NY)
    if now_ny.weekday() >= 5:
        return False
    open_ny = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    close_ny = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    return open_ny <= now_ny <= close_ny


def fetch_spot(ticker_obj) -> tuple[float, pd.Timestamp]:
    """Spot + timestamp de la derniere bougie. Leve RuntimeError si pas de donnee."""
    try:
        hist = ticker_obj.history(period="1d")
    except Exception as e:
        raise RuntimeError(f"Erreur fetch historique : {e}")
    if hist is None or hist.empty:
        raise RuntimeError(
            f"Aucune donnee de prix pour '{ticker_obj.ticker}' "
            f"(ticker invalide ou delistee)."
        )
    spot = float(hist["Close"].iloc[-1])
    if spot <= 0 or np.isnan(spot):
        raise RuntimeError(f"Spot invalide ({spot}) pour '{ticker_obj.ticker}'.")
    return spot, hist.index[-1]


def fetch_expirations_window(ticker_obj, today: date,
                              dte_min: int = DTE_MIN,
                              dte_max: int = DTE_MAX) -> list[tuple[str, int]]:
    """Liste (expiry_str, dte) dans la fenetre. Leve RuntimeError si chaine vide."""
    try:
        expiries = ticker_obj.options
    except Exception as e:
        raise RuntimeError(f"Erreur chargement liste d'expirations : {e}")
    if not expiries:
        raise RuntimeError(
            f"Aucune chaine d'options disponible pour '{ticker_obj.ticker}'."
        )
    out = []
    for exp_str in expiries:
        try:
            d = datetime.strptime(exp_str, "%Y-%m-%d").date()
        except ValueError:
            continue
        dte = (d - today).days
        if dte_min <= dte <= dte_max:
            out.append((exp_str, dte))
    return out


def prix_rachat_csp(ticker_obj, strike_csp: float, expiry_csp: date) -> float:
    """Mid du put strike_csp @ expiry_csp (cout pour racheter le CSP actuel).

    Leve RuntimeError si l'expiry n'existe plus dans la chaine, si le strike
    manque, ou si pas de prix valide.
    """
    expiry_str = expiry_csp.isoformat()
    try:
        all_exp = ticker_obj.options
    except Exception as e:
        raise RuntimeError(f"Erreur chargement liste d'expirations : {e}")
    if expiry_str not in all_exp:
        raise RuntimeError(
            f"Expiry CSP {expiry_str} introuvable dans la chaine options. "
            f"Verifier la date saisie ou contrat deja expire."
        )
    try:
        chain = ticker_obj.option_chain(expiry_str)
    except Exception as e:
        raise RuntimeError(f"Erreur chargement chaine {expiry_str} : {e}")
    puts = chain.puts
    row = puts[np.isclose(puts["strike"], strike_csp)]
    if row.empty:
        dispo = sorted(puts["strike"].tolist())
        raise RuntimeError(
            f"Strike {strike_csp} introuvable dans les puts {expiry_str}. "
            f"Strikes proches : {[s for s in dispo if abs(s - strike_csp) <= 5]}"
        )
    r = row.iloc[0]
    mid = get_mid(r.get("bid"), r.get("ask"), r.get("lastPrice"))
    if mid is None:
        raise RuntimeError(
            f"Pas de prix valide pour put {strike_csp} @ {expiry_str} "
            f"(bid/ask/last tous nuls)."
        )
    return mid


# ================================================================
# TRAJECTOIRE A : ROLL
# ================================================================
def generer_candidats_roll(ticker_obj, spot: float, strike_csp: float,
                            expiry_csp: date, expiries: list[tuple[str, int]],
                            prix_rachat: float, today: date,
                            frais_par_leg: float = FRAIS_PAR_LEG_DEFAUT,
                            ) -> tuple[list[dict], dict]:
    """Genere les candidats roll valides + stats de rejet.

    Pour chaque expiry strictement posterieure a expiry_csp, scanne les puts
    dans [spot * SPOT_FLOOR_RATIO, strike_csp]. Filtres : credit_net >= 0,
    IV > 0, prix dispo. Retourne (candidats tries par score, stats).
    """
    dte_csp = (expiry_csp - today).days
    strike_floor = spot * SPOT_FLOOR_RATIO
    fps = frais_par_leg / 100.0  # fee per share
    candidats = []
    stats = {"testes": 0, "rejet_credit": 0, "rejet_iv": 0, "rejet_prix": 0}

    for exp_str, dte_cand in expiries:
        dte_add = dte_cand - dte_csp
        if dte_add <= 0:
            continue
        try:
            chain = ticker_obj.option_chain(exp_str)
        except Exception:
            continue
        puts = chain.puts

        for _, row in puts.iterrows():
            try:
                strike = float(row["strike"])
            except (TypeError, ValueError):
                continue
            if not (strike_floor <= strike <= strike_csp):
                continue
            stats["testes"] += 1
            mid = get_mid(row.get("bid"), row.get("ask"), row.get("lastPrice"))
            if mid is None:
                stats["rejet_prix"] += 1
                continue
            credit_net = mid - prix_rachat - 2 * fps
            if credit_net < 0:
                stats["rejet_credit"] += 1
                continue
            iv = row.get("impliedVolatility")
            if iv is None or pd.isna(iv) or iv <= 0:
                stats["rejet_iv"] += 1
                continue
            delta = calc_delta_put(spot, strike, dte_cand, float(iv))
            if delta is None:
                stats["rejet_iv"] += 1
                continue
            yield_ann = (credit_net / strike) * (365 / dte_add)
            score = yield_ann * (1 - abs(delta))
            candidats.append({
                "strike": strike,
                "expiry": exp_str,
                "dte_cand": dte_cand,
                "dte_add": dte_add,
                "mid": mid,
                "credit_net": credit_net,
                "delta": delta,
                "iv": float(iv),
                "yield_ann": yield_ann,
                "score": score,
            })

    candidats.sort(key=lambda c: c["score"], reverse=True)
    return candidats, stats


def afficher_top_rolls(candidats: list[dict], stats: dict,
                        prix_rachat: float, n: int = 3) -> None:
    """Affiche le top N candidats + ligne de stats de rejet."""
    print(f"  Top {n} candidats roll :")
    if candidats:
        print(f"    {'#':<2} {'Strike':>7}  {'Expiry':<10}  {'DTE+':>4}  "
              f"{'Credit':>7}  {'|Delta|':>7}  {'YA%':>7}  {'Score%':>7}")
        print(f"    {'-'*2} {'-'*7}  {'-'*10}  {'-'*4}  "
              f"{'-'*7}  {'-'*7}  {'-'*7}  {'-'*7}")
        for i, c in enumerate(candidats[:n], 1):
            print(f"    {i:<2} {c['strike']:>7.2f}  {c['expiry']:<10}  "
                  f"{c['dte_add']:>4}  ${c['credit_net']:>+5.2f}  "
                  f"{abs(c['delta']):>7.3f}  {c['yield_ann']*100:>7.2f}  "
                  f"{c['score']*100:>7.2f}")
    else:
        print("    (aucun candidat valide)")

    elimines = stats["rejet_credit"] + stats["rejet_iv"] + stats["rejet_prix"]
    if stats["testes"] > 0 and elimines > 0:
        raisons = []
        if stats["rejet_credit"]:
            raisons.append(
                f"{stats['rejet_credit']} credit_net < 0 vs rachat ${prix_rachat:.2f}"
            )
        if stats["rejet_iv"]:
            raisons.append(f"{stats['rejet_iv']} IV manquante/invalide")
        if stats["rejet_prix"]:
            raisons.append(f"{stats['rejet_prix']} prix indispo")
        print(f"  Note : {stats['testes']} candidats testes, {elimines} elimines "
              f"({'; '.join(raisons)}).")


# ================================================================
# TRAJECTOIRE B : ASSIGNATION + COVERED CALL
# ================================================================
def calc_pru_net(strike_csp: float, premiums_cumul: float) -> float:
    """Prix de revient net apres assignation = strike - premiums encaissees."""
    return strike_csp - premiums_cumul


def choisir_strike_cc(calls_df, pru_net: float, spot: float) -> float | None:
    """Premier strike call >= max(PRU_net, spot). None si aucun ne satisfait.

    Regle d'or wheel : strike CC jamais sous le PRU (pas de vente a perte).
    Et jamais sous spot non plus pour eviter un CC deep ITM (call-away immediat,
    perte d'upside).
    """
    cible = max(pru_net, spot)
    try:
        strikes = sorted(float(s) for s in calls_df["strike"].tolist())
    except Exception:
        return None
    for s in strikes:
        if s >= cible:
            return s
    return None


def fetch_dividendes_fenetre(ticker_obj, today: date, date_fin: date) -> float:
    """Montant du prochain dividende attendu dans [today, date_fin], 0 sinon.

    Lit info.exDividendDate et info.lastDividendValue ; fallback sur
    ticker.dividends pour le montant si lastDividendValue absent.
    """
    try:
        info = ticker_obj.info
    except Exception:
        return 0.0
    ex_raw = info.get("exDividendDate")
    if ex_raw is None:
        return 0.0
    try:
        if isinstance(ex_raw, (int, float)):
            ex_date = datetime.fromtimestamp(float(ex_raw)).date()
        elif isinstance(ex_raw, str):
            ex_date = datetime.strptime(ex_raw[:10], "%Y-%m-%d").date()
        else:
            return 0.0
    except (ValueError, OSError, OverflowError):
        return 0.0
    if not (today <= ex_date <= date_fin):
        return 0.0
    montant = info.get("lastDividendValue")
    if montant is None or (isinstance(montant, float) and (np.isnan(montant) or montant <= 0)):
        try:
            divs = ticker_obj.dividends
            if divs is not None and not divs.empty:
                montant = float(divs.iloc[-1])
            else:
                montant = 0.0
        except Exception:
            montant = 0.0
    try:
        return float(montant) if montant else 0.0
    except (TypeError, ValueError):
        return 0.0


def evaluer_trajectoire_b(ticker_obj, top_roll: dict, pru_net: float,
                           spot: float, today: date,
                           frais_par_leg: float = FRAIS_PAR_LEG_DEFAUT,
                           ) -> dict:
    """Evalue assignation + CC a meme expiry que le top roll.

    Retourne dict avec viable (bool) et details. Si non viable : champ 'raison'.
    """
    expiry_str = top_roll["expiry"]
    dte_cc = top_roll["dte_cand"]
    cible = max(pru_net, spot)

    try:
        chain = ticker_obj.option_chain(expiry_str)
    except Exception as e:
        return {"viable": False, "raison": f"Erreur chaine CC {expiry_str} : {e}",
                "pru_net": pru_net, "spot": spot, "cible_strike": cible,
                "expiry": expiry_str, "dte_cc": dte_cc}

    calls = chain.calls
    strike_cc = choisir_strike_cc(calls, pru_net, spot)
    if strike_cc is None:
        return {"viable": False, "pru_net": pru_net, "spot": spot,
                "cible_strike": cible, "expiry": expiry_str, "dte_cc": dte_cc,
                "raison": (f"Aucun strike call >= ${cible:.2f} "
                           f"(max PRU/spot) dans la chaine {expiry_str}")}

    row = calls[np.isclose(calls["strike"], strike_cc)].iloc[0]
    mid = get_mid(row.get("bid"), row.get("ask"), row.get("lastPrice"))
    if mid is None:
        return {"viable": False, "pru_net": pru_net, "spot": spot,
                "cible_strike": cible, "expiry": expiry_str, "dte_cc": dte_cc,
                "strike_cc": strike_cc,
                "raison": f"Pas de prix valide pour call {strike_cc} @ {expiry_str}"}

    iv = row.get("impliedVolatility")
    iv_val = float(iv) if iv is not None and not pd.isna(iv) and iv > 0 else None
    delta_cc = calc_delta_call(spot, strike_cc, dte_cc, iv_val) if iv_val else None

    expiry_date = datetime.strptime(expiry_str, "%Y-%m-%d").date()
    div = fetch_dividendes_fenetre(ticker_obj, today, expiry_date)

    fps = frais_par_leg / 100.0
    prime_nette = mid - fps
    yield_b = ((prime_nette + div) / pru_net) * (365 / dte_cc)

    return {
        "viable": True,
        "pru_net": pru_net,
        "spot": spot,
        "cible_strike": cible,
        "expiry": expiry_str,
        "dte_cc": dte_cc,
        "strike_cc": strike_cc,
        "prime_cc": mid,
        "prime_cc_nette": prime_nette,
        "delta_cc": delta_cc,
        "iv_cc": iv_val,
        "dividendes": div,
        "yield_b": yield_b,
        "frais_cc": fps,
    }


def afficher_trajectoire_b(traj_b: dict, strike_csp: float, premiums: float) -> None:
    """Affiche le bloc trajectoire B."""
    print(f"  PRU_net      : ${traj_b['pru_net']:.2f}  "
          f"(strike ${strike_csp:.2f} - premiums ${premiums:.2f})")
    print(f"  Spot         : ${traj_b['spot']:.2f}")
    print(f"  Horizon CC   : {traj_b['expiry']} (+{traj_b['dte_cc']}j)")
    print(f"  Cible strike : ${traj_b['cible_strike']:.2f}  "
          f"(max PRU/spot, regle d'or wheel)")

    if not traj_b["viable"]:
        print(f"  [INFO] {traj_b['raison']}")
        print(f"         Trajectoire B non viable.")
        return

    print(f"  Strike CC    : ${traj_b['strike_cc']:.2f}")
    print(f"  Prime CC     : ${traj_b['prime_cc']:.2f}")
    if traj_b["delta_cc"] is not None:
        print(f"  |Delta| CC   : {abs(traj_b['delta_cc']):.3f}")
    else:
        print(f"  Delta CC     : N/A (IV manquante)")
    if traj_b["iv_cc"] is not None:
        print(f"  IV CC        : {traj_b['iv_cc']*100:.1f}%")
    print(f"  Dividendes   : ${traj_b['dividendes']:.2f}  "
          f"(attendus dans [today, expiry CC])")
    print(f"  Yield B (an.): {traj_b['yield_b']*100:.2f}%")

    if traj_b["spot"] < traj_b["pru_net"]:
        print()
        print("  [AVERTISSEMENT] Spot < PRU_net : position bloquee.")
        print("                  Envisager attente rebond ou exit a perte si")
        print("                  fondamentaux degrades.")


# ================================================================
# VERDICT
# ================================================================
def calculer_verdict(candidats: list[dict], traj_b: dict | None,
                     spot: float, pru_net: float) -> tuple[str, float | None]:
    """Determine le code verdict + ratio si applicable.

    Codes possibles :
      - ROLL_SOUS_LEAU         : A viable, PRU > spot (force ROLL)
      - ASSIGNATION            : A viable, B viable, ratio > 1.10
      - ROLL                   : A viable, B viable, ratio < 0.91
      - EQUIVALENT             : A viable, B viable, 0.91 <= ratio <= 1.10
      - ROLL_PAS_DE_CC         : A viable, B non viable (cas 4)
      - ASSIGNATION_PAS_DE_ROLL: A non viable, B viable, PRU <= spot (cas 3a)
      - BLOQUEE                : A non viable + (PRU > spot OU B non viable) (cas 3b)
    """
    a_viable = bool(candidats)
    b_viable = traj_b is not None and traj_b.get("viable", False)
    sous_leau = pru_net > spot

    if a_viable:
        if sous_leau:
            return ("ROLL_SOUS_LEAU", None)
        if not b_viable:
            return ("ROLL_PAS_DE_CC", None)
        yield_a = candidats[0]["yield_ann"]
        yield_b = traj_b["yield_b"]
        if yield_a <= 0:
            return ("ASSIGNATION", float("inf"))
        ratio = yield_b / yield_a
        if ratio > RATIO_ASSIGN:
            return ("ASSIGNATION", ratio)
        if ratio < RATIO_ROLL:
            return ("ROLL", ratio)
        return ("EQUIVALENT", ratio)

    # A non viable
    if sous_leau or not b_viable:
        return ("BLOQUEE", None)
    return ("ASSIGNATION_PAS_DE_ROLL", None)


def afficher_rapport_final(inputs: dict, spot: float, last_ts: pd.Timestamp,
                            marche_ouvert: bool, candidats: list[dict],
                            traj_b: dict | None, pru_net: float,
                            verdict_code: str, ratio: float | None) -> None:
    """Rapport final encadre : en-tete + bloc A + bloc B + verdict + action."""
    print()
    print("=" * 64)
    print(" ROLL ANALYZER - VERDICT")
    print("=" * 64)
    print()
    print(f" Position : {inputs['ticker']} ${inputs['strike_csp']:.2f} "
          f"exp {inputs['expiry_csp'].isoformat()}")
    if marche_ouvert:
        print(f" Spot     : ${spot:.2f} (temps reel)")
    else:
        print(f" Spot     : ${spot:.2f} (cloture du {last_ts.strftime('%d/%m')})")
    print(f" PRU net  : ${pru_net:.2f} "
          f"(strike ${inputs['strike_csp']:.2f} - cumul ${inputs['premiums_cumul']:.2f})")
    print()

    # Bloc A
    print("-" * 64)
    print(" TRAJECTOIRE A - Meilleur roll")
    print("-" * 64)
    if candidats:
        top = candidats[0]
        print(f"   Strike     : ${top['strike']:.2f}")
        print(f"   Expiration : {top['expiry']} (+{top['dte_add']}j additionnels)")
        print(f"   Credit net : +${top['credit_net']:.2f}")
        print(f"   |Delta|    : {abs(top['delta']):.3f}")
        print(f"   Yield annualise : {top['yield_ann']*100:.2f}%")
    else:
        print("   Aucun candidat roll a credit net positif.")
    print()

    # Bloc B
    print("-" * 64)
    print(" TRAJECTOIRE B - Assignation + Covered Call")
    print("-" * 64)
    if traj_b and traj_b.get("viable"):
        print(f"   PRU net apres assignation : ${traj_b['pru_net']:.2f}")
        print(f"   Strike CC suggere         : ${traj_b['strike_cc']:.2f}")
        print(f"   Expiration CC             : {traj_b['expiry']} "
              f"(meme que roll, horizon egal)")
        print(f"   Prime CC                  : ${traj_b['prime_cc']:.2f}")
        print(f"   Dividendes attendus       : ${traj_b['dividendes']:.2f}")
        print(f"   Yield annualise           : {traj_b['yield_b']*100:.2f}%")
    elif traj_b:
        print(f"   Non viable : {traj_b.get('raison', 'raison inconnue')}")
    else:
        print("   Non evaluee (aucun top roll de reference).")
    print()

    # Verdict + action
    _afficher_bloc_verdict(verdict_code, ratio, candidats, traj_b,
                            spot, pru_net, inputs)


def _afficher_bloc_verdict(code: str, ratio: float | None,
                            candidats: list[dict], traj_b: dict | None,
                            spot: float, pru_net: float, inputs: dict) -> None:
    """Bloc verdict encadre avec justification + action selon le code."""
    print("=" * 64)
    top = candidats[0] if candidats else None
    ya = top["yield_ann"] * 100 if top else None
    yb = traj_b["yield_b"] * 100 if traj_b and traj_b.get("viable") else None

    if code == "ROLL_SOUS_LEAU":
        print(" VERDICT : ROLL RECOMMANDE (position sous l'eau)")
        print()
        if yb is not None:
            print(f" Yield_A = {ya:.2f}% | Yield_B = {yb:.2f}% (non retenu)")
        else:
            print(f" Yield_A = {ya:.2f}% | Yield_B = N/A")
        print(" Ratio non applicable (regle prioritaire : PRU > spot).")
        print()
        print(f" Justification : titre sous le PRU (${pru_net:.2f} > spot ${spot:.2f}).")
        print(" Strategie wheel sur entreprises de qualite : on attend le rebond")
        print(" plutot que de plafonner l'upside via CC au PRU. La trajectoire B")
        print(" est mathematiquement attractive mais cristalliserait une position")
        print(" a breakeven en sacrifiant le potentiel de rebond.")
        print()
        print(" Action suggeree :")
        print(f"   - Rouler vers strike ${top['strike']:.2f} exp {top['expiry']} "
              f"(credit +${top['credit_net']:.2f}, +{top['dte_add']}j)")
        print("   - Surveiller : si position deja roulee 2 fois sur ce titre,")
        print("     reevaluer les fondamentaux. Une chute persistante peut")
        print("     signaler une degradation au-dela du filtre amont du scanner.")
        print("   - En dernier recours si rebond trop lent : exit a perte")
        print("     plutot que rolls infinis.")

    elif code == "ASSIGNATION":
        print(" VERDICT : ASSIGNATION RECOMMANDEE")
        print(f" Ratio yield_B / yield_A = {ratio:.2f} "
              f"(seuil {RATIO_ASSIGN:.2f} nettement depasse)")
        print()
        print(" Action suggeree :")
        print(f"   1. Laisser le CSP {inputs['ticker']} ${inputs['strike_csp']:.2f} "
              f"expirer ITM (assignation au strike)")
        print(f"   2. Vendre immediatement CC {inputs['ticker']} "
              f"${traj_b['strike_cc']:.2f} exp {traj_b['expiry']}")
        print(f"   3. Encaisser prime ${traj_b['prime_cc']:.2f} + "
              f"dividendes eventuels")

    elif code == "ROLL":
        print(" VERDICT : ROLL RECOMMANDE")
        print(f" Ratio yield_B / yield_A = {ratio:.2f} "
              f"(sous seuil {RATIO_ROLL:.2f})")
        print()
        print(" Action suggeree :")
        print(f"   - Roller le CSP : racheter put ${inputs['strike_csp']:.2f} "
              f"@ {inputs['expiry_csp'].isoformat()} et vendre put "
              f"${top['strike']:.2f} @ {top['expiry']}")
        print(f"   - Credit net : +${top['credit_net']:.2f} sur +{top['dte_add']}j")

    elif code == "EQUIVALENT":
        print(" VERDICT : EQUIVALENT - preference personnelle")
        print()
        print(f" Yield_A = {ya:.2f}% | Yield_B = {yb:.2f}% | Ratio = {ratio:.2f}")
        print(" Ecart sous le seuil de significativite (10%).")
        print()
        print(" Justification : les deux trajectoires sont economiquement")
        print(" equivalentes a horizon egal. La decision depend de ta these")
        print(" sur le titre :")
        print()
        print("   - Si tu veux GARDER le cash (these neutre ou prudente sur l'action)")
        print(f"     -> ROLL : strike ${top['strike']:.2f} exp {top['expiry']}, "
              f"credit +${top['credit_net']:.2f}")
        print()
        print("   - Si tu veux ACQUERIR le titre (these bullish, conviction long terme)")
        print(f"     -> ASSIGNATION : strike CC ${traj_b['strike_cc']:.2f} "
              f"exp {traj_b['expiry']}, prime +${traj_b['prime_cc']:.2f}")
        print()
        print(" Tie-breaker technique optionnel :")
        delta_abs = abs(top["delta"])
        print(f"   - |Delta| top roll actuel = {delta_abs:.3f}")
        print("   - |Delta| > 0.50 -> tendance assignation (le put est")
        print("     probablement assigne de toute facon, autant choisir le moment)")
        print("   - |Delta| < 0.30 -> tendance roll (assignation peu")
        print("     probable, on garde de la flexibilite)")

    elif code == "ASSIGNATION_PAS_DE_ROLL":
        print(" VERDICT : ASSIGNATION RECOMMANDEE (pas de roll viable)")
        print()
        print(" Trajectoire A : aucun candidat a credit net positif.")
        print(f" Trajectoire B : Yield {yb:.2f}%")
        print()
        print(" Justification : la chaine options ne permet aucun roll a credit")
        print(" positif dans la fenetre 7-28 DTE. La trajectoire A n'existe pas")
        print(f" economiquement. Comme PRU (${pru_net:.2f}) <= spot (${spot:.2f}), "
              f"l'assignation")
        print(" + CC reste viable et profitable.")
        print()
        print(" Action suggeree :")
        print("   1. Laisser le CSP expirer ITM (assignation au strike)")
        print(f"   2. Vendre CC strike ${traj_b['strike_cc']:.2f} exp "
              f"{traj_b['expiry']} (prime ${traj_b['prime_cc']:.2f})")
        print("   3. Encaisser prime + dividendes eventuels")

    elif code == "BLOQUEE":
        print(" VERDICT : POSITION BLOQUEE")
        print()
        print(" Trajectoire A : aucun candidat a credit net positif.")
        if pru_net > spot:
            print(" Trajectoire B : non viable (PRU > spot, regle prioritaire).")
        else:
            print(" Trajectoire B : non viable (CC introuvable dans la chaine).")
        print()
        print(" Justification : tu es coince entre deux mauvaises options.")
        print(" Aucun roll a credit positif n'est dispo, et l'assignation")
        print(" cristalliserait une perte non realisee au PRU.")
        print()
        print(" Action suggeree :")
        print("   - Attendre un rebond du sous-jacent pour debloquer la situation")
        print("   - Verifier si une expiration plus lointaine (>28 DTE, hors")
        print("     fenetre scan) offrirait un credit positif")
        print("   - Si fondamentaux degrades : envisager exit a perte plutot")
        print("     que de subir l'assignation ou de roller indefiniment")
        print("   - Reevaluer la these initiale sur ce titre")

    elif code == "ROLL_PAS_DE_CC":
        print(" VERDICT : ROLL RECOMMANDE")
        print(" Note : pas de strike CC viable >= max(PRU, spot) dans la chaine.")
        print("        Trajectoire B non calculee.")
        print()
        print(" Action suggeree :")
        print(f"   - Roller vers strike ${top['strike']:.2f} exp {top['expiry']} "
              f"(credit +${top['credit_net']:.2f}, +{top['dte_add']}j)")

    print("=" * 64)


# ================================================================
# MAIN
# ================================================================
def main() -> int:
    cli_inputs = parse_args_cli()
    if cli_inputs is not None:
        inputs = cli_inputs
        print("=" * 64)
        print(" ROLL ANALYZER - decision support CSP (mode CLI)")
        print("=" * 64)
    else:
        try:
            inputs = parse_inputs()
        except (KeyboardInterrupt, EOFError):
            print("\nAbandon utilisateur.")
            return 1
        except ValueError as e:
            print(f"\nErreur saisie : {e}")
            return 1

    print()
    print("-" * 64)
    print(" Position saisie")
    print("-" * 64)
    print(f"  Ticker          : {inputs['ticker']}")
    print(f"  Strike CSP      : ${inputs['strike_csp']:.2f}")
    print(f"  Expiration      : {inputs['expiry_csp'].isoformat()}")
    print(f"  Contrats        : {inputs['nb_contrats']}")
    print(f"  Premiums cumul. : ${inputs['premiums_cumul_total']:.2f} "
          f"(${inputs['premiums_cumul']:.4f}/action)")
    print(f"  Frais/leg       : ${inputs['frais_par_leg']:.2f}/contrat "
          f"(${inputs['frais_par_leg']/100:.4f}/action)")

    print()
    print("-" * 64)
    print(" Fetch marche")
    print("-" * 64)
    try:
        ticker_obj = yf.Ticker(inputs["ticker"])
        spot, last_ts = fetch_spot(ticker_obj)
        if marche_us_ouvert():
            print(f"  Spot            : ${spot:.2f} (temps reel)")
        else:
            ts_str = last_ts.strftime("%d/%m")
            print(f"  Spot            : ${spot:.2f} (cloture du {ts_str})")

        today = datetime.now(_PARIS).date()
        expiries = fetch_expirations_window(ticker_obj, today)
        if not expiries:
            print(f"  Aucune expiration dans la fenetre {DTE_MIN}-{DTE_MAX} DTE.")
        else:
            print(f"  Expirations {DTE_MIN}-{DTE_MAX} DTE ({len(expiries)}) :")
            for exp, dte in expiries:
                print(f"    - {exp} ({dte}j)")

        prix_rachat = prix_rachat_csp(
            ticker_obj, inputs["strike_csp"], inputs["expiry_csp"]
        )
        print(
            f"  Prix rachat CSP : ${prix_rachat:.2f} "
            f"(put {inputs['strike_csp']:.2f} @ {inputs['expiry_csp'].isoformat()})"
        )

        print()
        print("-" * 64)
        print(" Trajectoire A : ROLL")
        print("-" * 64)
        candidats, stats = generer_candidats_roll(
            ticker_obj, spot, inputs["strike_csp"], inputs["expiry_csp"],
            expiries, prix_rachat, today,
            frais_par_leg=inputs["frais_par_leg"],
        )
        afficher_top_rolls(candidats, stats, prix_rachat, n=3)
        if not candidats:
            print()
            print("  [INFO] Aucun roll a credit positif possible.")
            print("         La trajectoire A n'est pas viable - assignation probable.")
        else:
            top = candidats[0]
            print()
            print(f"  Top pick A : put {top['strike']:.2f} @ {top['expiry']} "
                  f"(+{top['dte_add']}j)")
            print(f"    Credit net    : ${top['credit_net']:+.2f}")
            print(f"    |Delta|       : {abs(top['delta']):.3f}")
            print(f"    IV            : {top['iv']*100:.1f}%")
            print(f"    Yield annual. : {top['yield_ann']*100:.2f}%")
            print(f"    Score         : {top['score']*100:.2f}")

        pru_net = calc_pru_net(inputs["strike_csp"], inputs["premiums_cumul"])
        traj_b = None
        if not candidats:
            print()
            print("-" * 64)
            print(" Trajectoire B : non evaluee (pas de top roll de reference).")
            print("-" * 64)
        else:
            print()
            print("-" * 64)
            print(" Trajectoire B : ASSIGNATION + COVERED CALL")
            print("-" * 64)
            traj_b = evaluer_trajectoire_b(
                ticker_obj, candidats[0], pru_net, spot, today,
                frais_par_leg=inputs["frais_par_leg"],
            )
            afficher_trajectoire_b(traj_b, inputs["strike_csp"],
                                    inputs["premiums_cumul"])

        verdict_code, ratio = calculer_verdict(candidats, traj_b, spot, pru_net)
        afficher_rapport_final(
            inputs, spot, last_ts, marche_us_ouvert(),
            candidats, traj_b, pru_net, verdict_code, ratio,
        )
    except RuntimeError as e:
        print(f"\n[ERREUR] {e}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
