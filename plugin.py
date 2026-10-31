"""
<plugin key="GrdfGazpar" name="GRDF Gazpar (monespace.grdf.fr)" author="Electrocut" version="1.0.0" externallink="https://github.com/Electrocut/GrdfGazparDomoticz">
    <description>
        <h2>GRDF Gazpar</h2>
        Récupère automatiquement les consommations journalières de gaz depuis monespace.grdf.fr
        et les recopie dans un périphérique Domoticz existant de type <i>Managed Counter</i> (Gaz).
        <ul>
            <li>Valeur journalière enregistrée en Wh (énergie GRDF en kWh x 1000), comme gazpar-man.py.</li>
            <li>L'index cumulé repart du dernier index connu du périphérique : l'historique existant est conservé.</li>
            <li>GRDF publie la journée gazière J (6h → 6h) au plus tôt le lendemain, parfois avec 1 à 2 jours de retard.</li>
            <li>Les relevés journaliers nécessitent le consentement "données quotidiennes" activé sur monespace.grdf.fr.</li>
        </ul>
    </description>
    <params>
        <param field="Username" label="E-mail GRDF" width="250px" required="true" default=""/>
        <param field="Password" label="Mot de passe GRDF" width="250px" required="true" default="" password="true"/>
        <param field="Mode1" label="Numéro PCE" width="150px" required="true" default=""/>
        <param field="Mode2" label="IDX du périphérique compteur gaz" width="60px" required="true" default=""/>
        <param field="Port" label="Port HTTP Domoticz (local)" width="60px" required="true" default="8080"/>
        <param field="Mode3" label="Rattrapage max (jours)" width="60px" required="true" default="30"/>
        <param field="Mode4" label="Intervalle d'interrogation (heures)" width="60px" required="true" default="6"/>
        <param field="Mode6" label="Debug" width="75px">
            <options>
                <option label="Non" value="Normal" default="true"/>
                <option label="Oui" value="Debug"/>
            </options>
        </param>
    </params>
</plugin>
"""
import Domoticz
import json
import queue
import re
import threading
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

GRDF_START_URL = "https://monespace.grdf.fr/"
GRDF_IDENTIFY_URL = "https://connexion.grdf.fr/idp/idx/identify"
GRDF_ANSWER_URL = "https://connexion.grdf.fr/idp/idx/challenge/answer"
GRDF_CONSO_URL = "https://monespace.grdf.fr/api/e-conso/pce/consommation/informatives"
USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
OKTA_HEADERS = {"Accept": "application/json; okta-version=1.0.0", "Content-Type": "application/json"}
HTTP_TIMEOUT = 30


class GrdfError(Exception):
    pass


# --------------------------------------------------------------------------------------------
# GRDF client (runs in a worker thread: must NOT call the Domoticz module)
# --------------------------------------------------------------------------------------------
def grdf_fetch(username, password, pce, start, end, log):
    """Logs into monespace.grdf.fr (Okta IDX flow) and returns the list of daily 'releves'."""
    import requests  # imported here so that a missing module is reported cleanly

    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT

    r = s.get(GRDF_START_URL, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise GrdfError("page de connexion GRDF : HTTP {}".format(r.status_code))
    m = re.search(r'"stateToken"\s*:\s*"([^"]+)"', r.text)
    if not m:
        raise GrdfError("stateToken introuvable : la page de connexion GRDF a changé")
    state_token = m.group(1).replace("\\x2D", "-")
    log("debug", "stateToken obtenu")

    s.cookies.set("ln", username)
    r = s.post(GRDF_IDENTIFY_URL, headers=OKTA_HEADERS, timeout=HTTP_TIMEOUT,
               data=json.dumps({"identifier": username, "stateHandle": state_token}))
    if r.status_code != 200:
        raise GrdfError("identification GRDF refusée : HTTP {} {}".format(r.status_code, okta_error(r)))
    state_handle = r.json().get("stateHandle")
    if not state_handle:
        raise GrdfError("stateHandle absent de la réponse d'identification GRDF")
    log("debug", "identifiant accepté")

    r = s.post(GRDF_ANSWER_URL, headers=OKTA_HEADERS, timeout=HTTP_TIMEOUT,
               data=json.dumps({"credentials": {"passcode": password}, "stateHandle": state_handle}))
    if r.status_code != 200:
        raise GrdfError("mot de passe GRDF refusé : HTTP {} {}".format(r.status_code, okta_error(r)))
    body = r.json()
    href = (body.get("success") or {}).get("href")
    if not href:
        remediation = [v.get("name") for v in (body.get("remediation") or {}).get("value", [])]
        raise GrdfError("connexion GRDF incomplète (MFA demandé ?) : étapes attendues {}".format(remediation))
    r = s.get(href, timeout=HTTP_TIMEOUT)
    if r.status_code != 200:
        raise GrdfError("redirection après connexion GRDF : HTTP {}".format(r.status_code))
    log("debug", "connecté à monespace.grdf.fr")

    params = {"dateDebut": start.isoformat(), "dateFin": end.isoformat(), "pceList[]": pce}
    last_err = None
    for attempt in range(5):
        r = s.get(GRDF_CONSO_URL, params=params, timeout=HTTP_TIMEOUT)
        if r.status_code == 200 and "text/html" not in r.headers.get("Content-Type", ""):
            data = r.json()
            if isinstance(data, list) and not data:
                return []
            if not isinstance(data, dict) or str(pce) not in data:
                raise GrdfError("réponse GRDF inattendue : {}".format(str(data)[:200]))
            return data[str(pce)].get("releves") or []
        last_err = "HTTP {} {}".format(r.status_code, r.text[:200].replace("\n", " "))
        log("debug", "API GRDF indisponible (essai {}/5) : {}".format(attempt + 1, last_err))
        threading.Event().wait(3)
    raise GrdfError("API consommation GRDF : {}".format(last_err))


def okta_error(r):
    try:
        msgs = r.json().get("messages", {}).get("value", [])
        return "- " + "; ".join(m.get("message", "") for m in msgs) if msgs else ""
    except Exception:
        return ""


# --------------------------------------------------------------------------------------------
# Plugin
# --------------------------------------------------------------------------------------------
class BasePlugin:
    def __init__(self):
        self.messages = queue.Queue()   # (level, text) from worker thread
        self.result = queue.Queue()     # ("ok", releves) or ("error", text)
        self.worker = None
        self.next_poll = datetime.now() + timedelta(seconds=20)
        self.interval = timedelta(hours=6)
        self.max_days = 30
        self.idx = None

    # ---- Domoticz callbacks ----
    def onStart(self):
        if Parameters["Mode6"] == "Debug":
            Domoticz.Debugging(1)
        try:
            self.idx = int(Parameters["Mode2"])
            self.max_days = min(max(int(Parameters["Mode3"]), 1), 365)
            self.interval = timedelta(hours=min(max(float(Parameters["Mode4"]), 1), 24))
        except ValueError:
            Domoticz.Error("Paramètres invalides : IDX, rattrapage et intervalle doivent être numériques")
            self.idx = None
            return
        for p, label in (("Username", "e-mail GRDF"), ("Password", "mot de passe GRDF"), ("Mode1", "numéro PCE")):
            if not Parameters[p].strip():
                Domoticz.Error("Paramètre manquant : {}".format(label))
                self.idx = None
                return
        Domoticz.Heartbeat(30)
        Domoticz.Log("Périphérique cible IDX {}, interrogation toutes les {} h, rattrapage max {} jours".format(
            self.idx, self.interval.total_seconds() / 3600, self.max_days))

    def onStop(self):
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=5)

    def onHeartbeat(self):
        self.flush_messages()
        if self.idx is None:
            return
        # result of a finished fetch ?
        try:
            status, payload = self.result.get_nowait()
        except queue.Empty:
            status = None
        if status == "error":
            Domoticz.Error("Récupération GRDF échouée : {}".format(payload))
        elif status == "ok":
            self.store(payload)

        if self.worker and self.worker.is_alive():
            return
        if datetime.now() < self.next_poll:
            return
        self.next_poll = datetime.now() + self.interval
        self.poll()

    # ---- logic ----
    def flush_messages(self):
        while True:
            try:
                level, text = self.messages.get_nowait()
            except queue.Empty:
                return
            {"debug": Domoticz.Debug, "log": Domoticz.Log, "error": Domoticz.Error}[level](text)

    def state(self):
        """Last day stored and its cumulative counter (Wh). Initialised once from the device history."""
        conf = Domoticz.Configuration()
        st = conf.get("state") if isinstance(conf, dict) else None
        if st and st.get("idx") == self.idx:
            return date.fromisoformat(st["lastDate"]), int(st["lastCounter"])
        last = self.last_history_day()
        if last:
            Domoticz.Log("Initialisation depuis l'historique du périphérique : dernier jour {} , index {} Wh".format(*last))
            self.save_state(*last)
        return last if last else (None, 0)

    def save_state(self, last_day, counter):
        conf = Domoticz.Configuration()
        conf = conf if isinstance(conf, dict) else {}
        conf["state"] = {"idx": self.idx, "lastDate": last_day.isoformat(), "lastCounter": int(counter)}
        Domoticz.Configuration(conf)

    def last_history_day(self):
        """Reads the most recent day of the device history (graph API, values divided by 'Divider')."""
        for rng in ("month", "year"):
            j = domoticz_api({"type": "command", "param": "graph", "sensor": "counter", "range": rng, "idx": self.idx})
            rows = [r for r in (j or {}).get("result", []) if "c" in r and "v" in r]
            if rows:
                div = float(j.get("Divider") or 1) or 1
                r = max(rows, key=lambda x: x["d"])
                # 'c' is the counter at the start of the day, 'v' the day usage (both / divider)
                counter = round((float(r["c"]) + float(r["v"])) * div)
                return date.fromisoformat(r["d"][:10]), counter
        return None

    def poll(self):
        last_day, _ = self.state()
        yesterday = date.today() - timedelta(days=1)
        if last_day and last_day >= yesterday:
            Domoticz.Debug("Données à jour (dernier jour {}), rien à demander à GRDF".format(last_day))
            return
        start = date.today() - timedelta(days=self.max_days)
        if last_day and last_day + timedelta(days=1) > start:
            start = last_day + timedelta(days=1)
        end = date.today()
        Domoticz.Log("Interrogation GRDF du {} au {}".format(start, end))
        args = (Parameters["Username"].strip(), Parameters["Password"], Parameters["Mode1"].strip(), start, end)
        self.worker = threading.Thread(target=self.run_fetch, args=args, name="GrdfGazpar", daemon=True)
        self.worker.start()

    def run_fetch(self, *args):
        log = lambda level, text: self.messages.put((level, text))
        try:
            self.result.put(("ok", grdf_fetch(*args, log=log)))
        except Exception as e:  # report everything, never crash Domoticz
            self.result.put(("error", "{}: {}".format(type(e).__name__, e)))

    def store(self, releves):
        last_day, counter = self.state()
        days = []
        for r in sorted(releves, key=lambda x: x.get("journeeGaziere") or ""):
            try:
                d = date.fromisoformat(r["journeeGaziere"])
            except (KeyError, TypeError, ValueError):
                continue
            if last_day and d <= last_day:
                continue
            days.append((d, r))
        # do not go past trailing days that GRDF has not measured yet
        while days and days[-1][1].get("energieConsomme") is None:
            days.pop()
        if not days:
            Domoticz.Log("Pas de nouvelle donnée GRDF disponible (dernier jour enregistré : {})".format(last_day))
            return

        stored = 0
        for d, r in days:
            kwh = r.get("energieConsomme")
            if kwh is None:
                Domoticz.Error("Journée {} sans mesure GRDF ({}) : ignorée".format(d, r.get("qualificationReleve")))
                continue
            wh = round(float(kwh) * 1000)
            counter += wh
            if not self.udevice("{};{};{}".format(counter, wh, d.isoformat())):
                Domoticz.Error("Arrêt de l'import au {} (mise à jour Domoticz refusée)".format(d))
                break
            Domoticz.Debug("{} : {} kWh ({} m3, {}), index {} Wh".format(
                d, kwh, r.get("volumeBrutConsomme"), r.get("qualificationReleve"), counter))
            last_day, last_wh, last_r = d, wh, r
            stored += 1
            self.save_state(last_day, counter)

        if stored:
            # same final updates as gazpar-man.py: current value, then short log at end of gas day
            self.udevice(str(float(last_wh)))
            end = (last_r.get("dateFinReleve") or "")[:19].replace("T", " ")
            if end:
                self.udevice("{};{};{}".format(counter, last_wh, end))
            Domoticz.Log("{} journée(s) importée(s) jusqu'au {} (dernière : {} kWh)".format(
                stored, last_day, last_wh / 1000))

    def udevice(self, svalue):
        j = domoticz_api({"type": "command", "param": "udevice", "idx": self.idx, "nvalue": 0, "svalue": svalue})
        return j is not None


def domoticz_api(params):
    url = "http://127.0.0.1:{}/json.htm?{}".format(Parameters["Port"], urllib.parse.urlencode(params))
    Domoticz.Debug("API Domoticz : {}".format(url))
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            j = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        Domoticz.Error("Appel API Domoticz impossible ({}) : {}".format(e, url))
        return None
    if j.get("status") != "OK":
        Domoticz.Error("API Domoticz : statut {} pour {}".format(j.get("status"), url))
        return None
    return j


_plugin = BasePlugin()


def onStart():
    _plugin.onStart()


def onStop():
    _plugin.onStop()


def onHeartbeat():
    _plugin.onHeartbeat()
