#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bofors-bevakaren
================
Bevakar BIK Karlskogas (Bofors IK) matcher på stats.swehockey.se och skickar
pushnotiser till ntfy-appen på iPhone vid:
  * mål (målskytt + assist, och om det var i powerplay/boxplay)
  * utvisningar (vem, hur länge, för vad) och om Bofors hamnar i PP eller BP
  * nedsläpp och slutresultat

Kräver bara Python 3 (inga extra paket).

Kommandon:
  python3 bofors.py run              Kör bevakaren (det launchd startar)
  python3 bofors.py test             Skicka en testnotis
  python3 bofors.py next             Visa nästa Bofors-match som hittas
  python3 bofors.py replay <matchid> [--send]
                                     Spela upp en färdig match (skriver ut,
                                     eller skickar med --send)
"""

import datetime as dt
import html as htmllib
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from collections import Counter

try:
    from zoneinfo import ZoneInfo
    TZ = ZoneInfo("Europe/Stockholm")
except Exception:  # äldre Python: använd datorns lokala tid
    TZ = None

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
STATE_PATH = os.path.join(HERE, "state.json")
LOG_PATH = os.path.join(HERE, "bofors.log")
BASE = "https://stats.swehockey.se"

DEFAULT_CONFIG = {
    "ntfy_server": "https://ntfy.sh",
    "ntfy_topic": "",
    # HockeyAllsvenskan 2026/27. Lägg till fler ID:n (t.ex. slutspel) vid behov.
    "league_ids": [20962],
    "team_match": "Karlskoga",       # text som finns i lagnamnet på swehockey
    "team_name": "Bofors",           # namnet som visas i notiserna
    "poll_seconds": 15,
    "notify_opponent_goals": True,
    "notify_start_and_final": True,
    "notify_periods": True,
    "notify_lineup": True,
    # Övriga matcher i serien: mål, assist och utvisningar/numerär till ett eget ntfy-ämne
    "follow_league": True,
    "ntfy_topic_league": "ha-live-stj2q222p2",
    "league_poll_seconds": 30,
    "league_penalties": False,   # utvisningar i övriga matcher
    "league_final": False,       # slutresultat i övriga matcher
}

_STATE = None


def shared_state():
    global _STATE
    if _STATE is None:
        _STATE = load_json(STATE_PATH, {})
    return _STATE

# Utvisningsorsaker: engelska (som swehockey skriver) -> svenska
PENALTY_SV = {
    "tripping": "Fällning",
    "hooking": "Hakning",
    "holding": "Fasthållning",
    "holdingthestick": "Hålla motståndarens klubba",
    "slashing": "Slashing (slag med klubban)",
    "interference": "Obstruktion",
    "interferenceonthegoalkeeper": "Obstruktion mot målvakt",
    "goalkeeperinterference": "Obstruktion mot målvakt",
    "roughing": "Ruffning",
    "boarding": "Boarding (tackling mot sargen)",
    "crosschecking": "Crosscheck",
    "highsticking": "Hög klubba",
    "histicking": "Hög klubba",
    "toomanymen": "För många spelare på isen",
    "toomanymenontheice": "För många spelare på isen",
    "delayofgame": "Fördröjning av spelet",
    "delayinggame": "Fördröjning av spelet",
    "unsportsmanlikeconduct": "Osportsligt uppträdande",
    "elbowing": "Armbåge",
    "charging": "Charging (tackling med anlopp)",
    "kneeing": "Knätackling",
    "checkingfrombehind": "Tackling bakifrån",
    "illegalchecktothehead": "Otillåten tackling mot huvudet",
    "checktotheheadorneck": "Tackling mot huvud/nacke",
    "illegalhit": "Otillåten tackling",
    "headbutting": "Skalle",
    "spearing": "Spearing (stick med klubban)",
    "buttending": "Stöt med klubbänden",
    "fighting": "Slagsmål",
    "diving": "Filmning",
    "embellishment": "Filmning",
    "abuseofofficials": "Otillåtet uppträdande mot domare",
    "misconduct": "Personligt straff",
    "misconductpenalty": "Personligt straff",
    "gamemisconduct": "Matchstraff (GM)",
    "gamemisconductpenalty": "Matchstraff (GM)",
    "matchpenalty": "Matchstraff",
    "brokenstick": "Spel med trasig klubba",
    "closinghandonpuck": "Stänga handen om pucken",
    "handlingpuck": "Stänga handen om pucken",
    "throwingstick": "Kastad klubba",
    "throwingequipment": "Kastad utrustning",
    "illegalequipment": "Otillåten utrustning",
    "clipping": "Låg tackling (clipping)",
    "kicking": "Sparkning",
    "biting": "Bitning",
    "hairpulling": "Hårdragning",
    "instigator": "Provokation (instigator)",
    "aggressor": "Aggressor",
    "benchminor": "Lagstraff",
    "leavingthebench": "Lämnade avbytarbåset",
    "leavingthepenaltybench": "Lämnade utvisningsbåset",
    "playingwithoutahelmet": "Spel utan hjälm",
    "faceoffviolation": "Fel vid tekning",
    "displacinggoal": "Flyttat målburen",
    "displacingthegoal": "Flyttat målburen",
}


# --------------------------------------------------------------------------
# Hjälpfunktioner
# --------------------------------------------------------------------------

def now():
    return dt.datetime.now(TZ) if TZ else dt.datetime.now()


def wall_sleep(seconds):
    """Sov i korta pass mot klockan, så att viloläge på Macen inte förlänger väntan."""
    target = time.time() + seconds
    while True:
        left = target - time.time()
        if left <= 0:
            return
        time.sleep(min(left, 30))


def log(msg):
    line = "%s  %s" % (now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # håll loggen liten
        if os.path.getsize(LOG_PATH) > 2_000_000:
            with open(LOG_PATH, encoding="utf-8") as f:
                rest = f.readlines()[-5000:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(rest)
    except Exception:
        pass


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(load_json(CONFIG_PATH, {}))
    return cfg


def fetch(url):
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh) BoforsBevakare/1.0",
        "Accept-Language": "sv-SE,sv;q=0.9,en;q=0.8",
    })
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8", errors="replace")


def text(fragment):
    """HTML -> ren text på en rad."""
    s = re.sub(r"<[^>]+>", " ", fragment)
    s = htmllib.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def nice_player(raw):
    """'41. Emanuelsson, Jesper' -> 'Jesper Emanuelsson (41)'"""
    raw = text(raw)
    m = re.match(r"^(\d+)\.\s*(.+)$", raw)
    num, name = (m.group(1), m.group(2)) if m else (None, raw)
    if "," in name:
        last, first = [p.strip() for p in name.split(",", 1)]
        name = ("%s %s" % (first, last)).strip()
    return "%s (%s)" % (name, num) if num else name


def ordinal_sv(n):
    n = int(n)
    return "%d:%s" % (n, "a" if n % 10 in (1, 2) and n % 100 not in (11, 12) else "e")


def period_sv(p):
    m = re.match(r"(\d)(?:st|nd|rd|th)\s+period", p, re.I)
    if m:
        return "period %s" % m.group(1)
    low = p.lower()
    if "overtime" in low:
        return "förlängning"
    if "shoot" in low or "penalty shots" in low or "winning" in low:
        return "straffläggning"
    return p


def period_no(status):
    """'2nd period' -> 2, 'Overtime' -> 4, annars None (även '1st period ended')."""
    if re.search(r"ended|intermission|break|paus|slut", status or "", re.I):
        return None
    m = re.match(r"(\d)(?:st|nd|rd|th)\s+period", status or "", re.I)
    if m:
        return int(m.group(1))
    if re.search(r"overtime|förlängning", status or "", re.I):
        return 4
    return None


def period_name(n):
    return "Förlängningen" if n >= 4 else "Period %d" % n


def mmss_to_sec(s):
    m = re.match(r"(\d+):(\d+)", s or "")
    return int(m.group(1)) * 60 + int(m.group(2)) if m else None


def penalty_sv(reason):
    if not reason:
        return ""
    key = re.sub(r"[^a-z]", "", reason.lower())
    sv = PENALTY_SV.get(key)
    if sv is None:
        # försök hitta en känd orsak i en längre text, t.ex. "Tripping - PS"
        for k, v in PENALTY_SV.items():
            if k in key:
                sv = v
                break
    if not sv or sv.lower() == reason.lower():
        return reason
    if sv.lower().startswith(reason.lower()):
        return sv
    return "%s (%s)" % (sv, reason)


# --------------------------------------------------------------------------
# Notiser via ntfy
# --------------------------------------------------------------------------

def notify(cfg, title, message, tags=None, priority=3, dry_run=False):
    log("NOTIS: %s | %s" % (title, message.replace("\n", " / ")))
    if dry_run:
        print("\n=== %s ===\n%s\n" % (title, message))
        return True
    topic = cfg.get("ntfy_topic")
    if not topic:
        log("Inget ntfy_topic i config.json – kan inte skicka.")
        return False
    payload = {"topic": topic, "title": title, "message": message,
               "priority": priority}
    if tags:
        payload["tags"] = tags
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(
                cfg["ntfy_server"].rstrip("/"), data=data,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=15).read()
            return True
        except Exception as e:
            log("Kunde inte skicka notis (försök %d): %s" % (attempt + 1, e))
            time.sleep(3)
    return False


# --------------------------------------------------------------------------
# Spelschema
# --------------------------------------------------------------------------

def norm_html(page):
    """Gör serverns rå-HTML lik det webbläsaren visar."""
    page = page.replace("\xa0", "&nbsp;")
    return re.sub(r"<td([^>]*?)\s*/>", r"<td\1></td>", page)


def cells(row):
    """Innehållet i varje <td> (tål ostängda celler)."""
    parts = re.split(r"<td\b[^>]*>", row)[1:]
    return [re.split(r"</td>|</tr>", p_, 1)[0] for p_ in parts]


def parse_schedule(page):
    page = norm_html(page)
    games = []
    cur_date = None
    for row in page.split("<tr")[1:]:
        md = re.search(r"(\d{4}-\d{2}-\d{2})", row)
        if md:
            cur_date = md.group(1)
        if "lnkTooltip" not in row:
            continue
        mdate = md or (re.match(r"(.*)", cur_date) if cur_date else None)
        mnum = re.search(r'class="lnkTooltip"\s+title="(\d+)"\s*>\s*(\d{1,2}:\d{2})', row)
        mteams = re.search(r"<td[^>]*>([^<]*?)&nbsp;-&nbsp;([^<]*?)</td>", row)
        if not (mdate and mnum and mteams):
            continue
        mid = re.search(r"Game/Events/(\d+)", row)
        games.append({
            "date": mdate.group(1),
            "time": mnum.group(2),
            "game_no": int(mnum.group(1)),
            "home": text(mteams.group(1)),
            "away": text(mteams.group(2)),
            "game_id": int(mid.group(1)) if mid else None,
        })
    # Matcher som inte spelats saknar länk. Räkna ut ID:t från matchnumret
    # (alla spelade matcher har samma skillnad mellan ID och matchnummer).
    diffs = Counter(g["game_id"] - g["game_no"] for g in games if g["game_id"])
    if diffs:
        offset, _ = diffs.most_common(1)[0]
        for g in games:
            if not g["game_id"]:
                g["game_id"] = g["game_no"] + offset
                g["predicted_id"] = True
    return games


def find_team_games(cfg):
    out = []
    for lid in cfg["league_ids"]:
        try:
            page = fetch("%s/ScheduleAndResults/Schedule/%s" % (BASE, lid))
        except Exception as e:
            log("Kunde inte hämta schemat (%s): %s" % (lid, e))
            continue
        for g in parse_schedule(page):
            if cfg["team_match"].lower() in (g["home"] + g["away"]).lower():
                g["league_id"] = lid
                out.append(g)
    return sorted(out, key=lambda g: (g["date"], g["time"]))


def game_start(g):
    d = dt.datetime.strptime("%s %s" % (g["date"], g["time"]), "%Y-%m-%d %H:%M")
    return d.replace(tzinfo=TZ) if TZ else d


# --------------------------------------------------------------------------
# Matchhändelser
# --------------------------------------------------------------------------

def parse_game(page):
    """Returnerar info om matchen och en lista med händelser i tidsordning."""
    page = norm_html(page)
    info = {"home": None, "away": None, "home_abbr": None, "away_abbr": None,
            "score": None, "periods": "", "final": False, "events": []}
    mt = re.search(r"<h2>([^<]+?)&nbsp;-&nbsp;([^<]+?)</h2>", page)
    if mt:
        info["home"], info["away"] = text(mt.group(1)), text(mt.group(2))
    mtitle = re.search(r"<title>\s*([^<]*?)\s*</title>", page, re.S)
    if mtitle:
        ma = re.match(r"(\S+)\s+-\s+(\S+)", text(mtitle.group(1)))
        if ma:
            info["home_abbr"], info["away_abbr"] = ma.group(1), ma.group(2)
    ms = re.search(r">\s*(\d+)&nbsp;-&nbsp;(\d+)\s*</div>\s*<div>\s*(\([^)]*\))?", page)
    if ms:
        info["score"] = "%s-%s" % (ms.group(1), ms.group(2))
        info["periods"] = (ms.group(3) or "").strip()
    info["final"] = bool(re.search(r"Final Score|Game Finished|Slutresultat", page))
    shots = re.findall(r"Shots</td>\s*<td[^>]*>\s*<strong>\s*(\d+)\s*</strong>\s*</td>\s*<td[^>]*>\s*\(([^)]*)\)", page)
    info["shots"] = (int(shots[0][0]), int(shots[1][0])) if len(shots) >= 2 else None
    mst = re.search(r'padding: 10px 0 3px 0; font-weight: bold;">\s*([^<]*?)\s*</div>\s*(?:<div>\s*(\d{1,3}:\d{2})\s*</div>)?', page)
    info["status"] = text(mst.group(1)) if mst else ""
    info["clock"] = mmss_to_sec(mst.group(2)) if mst and mst.group(2) else None

    period = ""
    for chunk in page.split("<tr")[1:]:
        mh = re.search(r"<h3>([^<]*(?:period|Overtime|Shootout|Penalty shots|Game Winning)[^<]*)</h3>", chunk, re.I)
        if mh:
            period = period_sv(text(mh.group(1)))
            continue
        tds = cells(chunk)
        if len(tds) < 4:
            continue
        clock = text(tds[0])
        if not re.match(r"^\d{1,3}:\d{2}$", clock):
            continue
        kind = text(tds[1])
        team = text(tds[2])
        ev = {"time": clock, "sec": mmss_to_sec(clock), "period": period,
              "team": team, "raw_kind": kind}
        mg = re.match(r"^(\d+)\s*-\s*(\d+)\s*(?:\(([^)]*)\))?", kind)
        mp = re.match(r"^([\d+]+)\s*min", kind)
        if mg:
            cell = tds[3]
            first = cell.split("<span", 1)[0]
            season = re.search(r"Total goals[^>]*>\s*\((\d+)\)", cell)
            assists = [nice_player(a) for a in
                       re.findall(r"<div[^>]*>(.*?)</div>", cell, re.S)]
            ev.update({
                "type": "goal",
                "score": "%s-%s" % (mg.group(1), mg.group(2)),
                "situation": (mg.group(3) or "").strip(),
                "scorer": nice_player(first),
                "season_goals": int(season.group(1)) if season else None,
                "assists": [a for a in assists if a],
                "pos": [int(x) for x in re.findall(r"\d+", text(tds[4]).split("Neg. Part.")[0])] if len(tds) > 4 else [],
                "neg": [int(x) for x in re.findall(r"\d+", text(tds[4]).split("Neg. Part.")[1])] if len(tds) > 4 and "Neg. Part." in text(tds[4]) else [],
            })
        elif mp:
            reason_cell = tds[4] if len(tds) > 4 else ""
            reason = text(reason_cell.split("<br", 1)[0])
            span = re.search(r"\((\d+:\d+)\s*-\s*(\d+:\d+)\)", text(reason_cell))
            parts = [int(x) for x in mp.group(1).split("+") if x]
            ev.update({
                "type": "penalty",
                "minutes": sum(parts),
                "minutes_txt": mp.group(1),
                "player": nice_player(tds[3]),
                "reason": reason,
                "start": mmss_to_sec(span.group(1)) if span else ev["sec"],
                "end": mmss_to_sec(span.group(2)) if span else None,
            })
        else:
            ev["type"] = "other"
        info["events"].append(ev)
    info["events"].sort(key=lambda e: (e["sec"] if e["sec"] is not None else 0))
    return info


LINE_SV = {"Goalies": "Mål", "Extra Players": "Reserver"}


def parse_lineup(page):
    """{lagnamn: {"goalies": [...], "lines": [(nr, backar, forwards)], "nums": {nr: kedja}}}"""
    page = norm_html(page)
    teams = {}
    for sec in page.split("<h3>")[1:]:
        mname = re.match(r"\s*([^<(]+?)\s*\((?:Blue|Red|White|Black|Home|Away|[A-Za-zåäöÅÄÖ ]+)\)", sec)
        if not mname:
            continue
        name = text(mname.group(1))
        team = {"goalies": [], "lines": [], "fwd_line": {}}
        parts = re.split(r"<strong>\s*(Goalies|\d(?:st|nd|rd|th) Line|Extra Players)\s*</strong>", sec)
        for label, body in zip(parts[1::2], parts[2::2]):
            body = body.split("Head Coach")[0]
            rows = []
            for row in body.split("<tr")[0:]:
                ps = re.findall(r'class="lineUpPlayer[^"]*">(.*?)</div>', row, re.S)
                if ps:
                    rows.append([text(x) for x in ps])
            if label == "Goalies":
                team["goalies"] = [x for r in rows for x in r]
            elif "Line" in label:
                nr = int(label[0])
                fw = [x for r in rows if len(r) == 3 for x in r]
                d = [x for r in rows if len(r) != 3 for x in r]
                if not fw and rows:
                    fw = rows[-1]
                    d = [x for r in rows[:-1] for x in r]
                team["lines"].append((nr, d, fw))
                for x in fw:
                    m = re.match(r"(\d+)\.", x)
                    if m:
                        team["fwd_line"][int(m.group(1))] = nr
        if team["lines"] or team["goalies"]:
            teams[name] = team
    return teams


def last_name(raw):
    m = re.match(r"^\d+\.\s*([^,]+)", raw.strip())
    return m.group(1).strip() if m else raw


def line_on_ice(team, nums):
    """Vilken kedja (forwards) var på isen, utifrån tröjnummer."""
    if not team or not nums:
        return None
    count = Counter(team["fwd_line"][n] for n in nums if n in team["fwd_line"])
    if not count:
        return None
    nr, c = count.most_common(1)[0]
    return nr if c >= 2 else None


def strength_affecting(ev):
    """Påverkar utvisningen numerären? (2, 2+2, 5 min gör det, 10/20 inte)"""
    return ev["minutes"] in (2, 4, 5) or ev.get("minutes_txt", "").startswith(("2", "5"))


def active_penalties(events, team, t):
    n = 0
    for e in events:
        if e["type"] != "penalty" or e["team"] != team or not strength_affecting(e):
            continue
        start = e["start"] if e["start"] is not None else e["sec"]
        length = 300 if e["minutes"] >= 5 else e["minutes"] * 60
        end = e["end"] if e["end"] is not None else start + length
        if start <= t < end:
            n += 1
    return n


def situation_text(info, ev, us_abbr, them_abbr, team_name):
    t = ev["start"] if ev.get("start") is not None else ev["sec"]
    us = active_penalties(info["events"], us_abbr, t)
    them = active_penalties(info["events"], them_abbr, t)
    us_on, them_on = 5 - min(us, 2), 5 - min(them, 2)
    if not strength_affecting(ev):
        return "Påverkar inte numerären."
    if us < them:
        return "%s i POWERPLAY (%d mot %d)" % (team_name, us_on, them_on)
    if us > them:
        return "%s i BOXPLAY (%d mot %d)" % (team_name, us_on, them_on)
    return "Lika numerär (%d mot %d)" % (us_on, them_on)


def goal_situation_text(code, scored_by_us, team_name):
    c = (code or "").upper()
    parts = []
    if "PP" in c:
        n = "5 mot 3" if "PP2" in c else "5 mot 4"
        parts.append("Powerplaymål (%s)" % n + ("" if scored_by_us else " – %s i boxplay" % team_name))
    if "SH" in c:
        parts.append("Mål i numerärt underläge" + (" – %s i boxplay!" % team_name if scored_by_us else " – %s i powerplay" % team_name))
    if "ENG" in c or "EN" == c:
        parts.append("Mål i tom bur")
    if "PS" in c:
        parts.append("Straffslag")
    if "GWS" in c or "SO" in c:
        parts.append("Straffläggning")
    if "EQ" in c and not parts:
        parts.append("Lika numerär")
    return " · ".join(parts) if parts else (code or "")


# --------------------------------------------------------------------------
# Följ en match
# --------------------------------------------------------------------------

class GameFollower:
    def __init__(self, cfg, game_id, dry_run=False):
        self.cfg = cfg
        self.game_id = str(game_id)
        self.dry_run = dry_run
        self.state_all = shared_state() if not dry_run else {}
        self.st = self.state_all.setdefault(self.game_id, {
            "goals": {}, "penalties": [], "started": False, "final": False})

    def save(self):
        if self.dry_run:
            return
        # behåll bara de 30 senaste matcherna
        keys = list(self.state_all.keys())
        for k in keys[:-200]:
            self.state_all.pop(k, None)
        save_json(STATE_PATH, self.state_all)

    lineup = None

    def our_lineup(self):
        if not self.lineup:
            return None
        for name, t in self.lineup.items():
            if self.cfg["team_match"].lower() in name.lower():
                return t
        return None

    def lineup_notice(self, header):
        t = self.our_lineup()
        if not t or not t["lines"] or self.st.get("lineup_sent"):
            return
        self.st["lineup_sent"] = True
        rows = []
        if t["goalies"]:
            rows.append("Mål: %s" % nice_player(t["goalies"][0]))
        for nr, d, fw in sorted(t["lines"]):
            row = "Kedja %d: %s" % (nr, " – ".join(last_name(x) for x in fw))
            if d:
                row += "  (backar %s)" % ", ".join(last_name(x) for x in d)
            rows.append(row)
        self.send("Kvällens kedjor – %s" % self.cfg["team_name"], "\n".join(rows) + "\n" + header, ["clipboard"], 3)

    def shots_text(self, info, we_home):
        if not info.get("shots"):
            return ""
        h, a = info["shots"]
        us, them = (h, a) if we_home else (a, h)
        return "Skott: %s %d – %d" % (self.cfg["team_name"], us, them)

    def send(self, title, msg, tags=None, priority=3):
        if getattr(self, "league", False):
            c = dict(self.cfg)
            c["ntfy_topic"] = self.cfg.get("ntfy_topic_league") or self.cfg["ntfy_topic"]
            return notify(c, title, msg, tags, priority, self.dry_run)
        return notify(self.cfg, title, msg, tags, priority, self.dry_run)

    def process_league(self, info, silent=False):
        """Övriga matcher: mål/assist, utvisningar med numerär, bortdömda mål, slutresultat."""
        if not info["home"] or not info["home_abbr"]:
            return
        names = {info["home_abbr"]: info["home"], info["away_abbr"]: info["away"]}
        other = {info["home_abbr"]: info["away_abbr"], info["away_abbr"]: info["home_abbr"]}
        header = "%s – %s" % (info["home"], info["away"])
        goals_now = [e for e in info["events"] if e["type"] == "goal"]
        cur_keys = set("%s|%s" % (e["time"], e["team"]) for e in goals_now)
        missing = self.st.setdefault("missing", {})
        if info["events"]:
            self.st["started"] = True
        for ev in info["events"]:
            team = names.get(ev["team"], ev["team"])
            if ev["type"] == "goal":
                key = "%s|%s" % (ev["time"], ev["team"])
                summary = {"scorer": ev["scorer"], "assists": ev["assists"], "time": ev["time"],
                           "situation": ev["situation"], "team": ev["team"]}
                old = self.st["goals"].get(key)
                if old is not None and all(old.get(f) == summary[f] for f in ("scorer", "assists", "situation")):
                    continue
                self.st["goals"][key] = summary
                if silent:
                    continue
                sit = goal_situation_text(ev["situation"], True, team)
                lines = ["Mål: " + ev["scorer"],
                         "Assist: " + (", ".join(ev["assists"]) if ev["assists"] else "ingen")]
                if sit:
                    lines.append(sit)
                lines.append("%s %s · %s, %s" % (header, ev["score"], ev["time"], ev["period"] or ""))
                if old is None:
                    title = "Mål %s! %s (%s)" % (team, ev["score"], header)
                else:
                    title = "Rättelse, målet till %s (%s)" % (ev["score"], header)
                self.send(title, "\n".join(lines), ["ice_hockey"], 3)
            elif ev["type"] == "penalty":
                key = "%s|%s|%s|%s" % (ev["time"], ev["team"], ev["player"], ev["minutes_txt"])
                if key in self.st["penalties"]:
                    continue
                self.st["penalties"].append(key)
                if silent or not self.cfg.get("league_penalties", False):
                    continue
                opp_abbr = other.get(ev["team"], "")
                sit = situation_text(info, ev, opp_abbr, ev["team"], names.get(opp_abbr, opp_abbr))
                lines = ["%s: %s" % (team, ev["player"] or "Lagstraff"),
                         "Straff: %s min – %s" % (ev["minutes_txt"], penalty_sv(ev["reason"]) or "orsak ej angiven"),
                         sit, "%s · %s, %s" % (header, ev["time"], ev["period"] or "")]
                self.send("Utvisning %s (%s)" % (team, header), "\n".join(lines), ["warning"], 2)
        if info["events"]:
            for k in list(self.st["goals"].keys()):
                if k in cur_keys:
                    missing.pop(k, None)
                    continue
                missing[k] = missing.get(k, 0) + 1
                if missing[k] < 3:
                    continue
                g = self.st["goals"].pop(k)
                missing.pop(k, None)
                if not silent:
                    self.send("Mål bortdömt (%s)" % header,
                              "Målet av %s vid %s räknas inte längre.\nStällning nu: %s" % (
                                  g.get("scorer", "?"), g.get("time", "?"), info["score"] or ""), ["x"], 3)
        if info["final"] and not self.st["final"]:
            self.st["final"] = True
            if not silent and info["score"] and self.cfg.get("league_final", False):
                self.send("Slut: %s %s" % (header, info["score"]), "%s %s" % (info["periods"],
                          ("\nSkott: %d – %d" % info["shots"]) if info.get("shots") else ""), ["checkered_flag"], 2)
        self.save()

    def process(self, info, silent=False):
        """Skicka notiser för nya händelser. silent=True markerar bara som sedda."""
        team_name = self.cfg["team_name"]
        match = self.cfg["team_match"].lower()
        if not info["home"] or not info["home_abbr"]:
            return
        we_home = match in info["home"].lower()
        us_abbr = info["home_abbr"] if we_home else info["away_abbr"]
        them_abbr = info["away_abbr"] if we_home else info["home_abbr"]
        opp = info["away"] if we_home else info["home"]
        header = "%s – %s" % (info["home"], info["away"])

        if not silent and self.cfg.get("notify_lineup", True):
            self.lineup_notice(header)

        if info["events"] and not self.st["started"]:
            self.st["started"] = True
            if not silent and self.cfg.get("notify_start_and_final", True):
                self.send("Nedsläpp!", "%s har börjat." % header, ["ice_hockey"], 3)

        goals_now = [e for e in info["events"] if e["type"] == "goal"]
        cur_keys = set("%s|%s" % (e["time"], e["team"]) for e in goals_now)
        # Äldre sparat format (nyckel = ställning) -> nytt format, utan notiser
        for k in [k for k in self.st["goals"] if "|" not in k]:
            v = self.st["goals"].pop(k)
            for e in goals_now:
                if e["score"] == k:
                    v["team"] = e["team"]
                    self.st["goals"]["%s|%s" % (e["time"], e["team"])] = v
        missing = self.st.setdefault("missing", {})

        for ev in info["events"]:
            if ev["type"] == "goal":
                key = "%s|%s" % (ev["time"], ev["team"])
                summary = {"scorer": ev["scorer"], "assists": ev["assists"],
                           "time": ev["time"], "situation": ev["situation"],
                           "team": ev["team"]}
                if key not in self.st["goals"]:
                    # Samma mål med ändrad tid? Flytta det i tysthet.
                    for k, v in list(self.st["goals"].items()):
                        if k not in cur_keys and v.get("team") == ev["team"] and v.get("scorer") == ev["scorer"]:
                            self.st["goals"][key] = self.st["goals"].pop(k)
                            missing.pop(k, None)
                            break
                old = self.st["goals"].get(key)
                if old is not None:
                    same = all(old.get(f) == summary[f] for f in ("scorer", "assists", "situation"))
                    if same:
                        self.st["goals"][key] = summary
                        continue
                self.st["goals"][key] = summary
                if silent:
                    continue
                ours = ev["team"] == us_abbr
                if not ours and not self.cfg.get("notify_opponent_goals", True):
                    continue
                lines = []
                scorer = ev["scorer"]
                if ev.get("season_goals"):
                    scorer += " – %s målet för säsongen" % ordinal_sv(ev["season_goals"])
                lines.append("Mål: " + scorer)
                lines.append("Assist: " + (", ".join(ev["assists"]) if ev["assists"] else "ingen (soloprestation)"))
                sit = goal_situation_text(ev["situation"], ours, team_name)
                if sit:
                    lines.append(sit)
                kedja = line_on_ice(self.our_lineup(), ev.get("pos") if ours else ev.get("neg"))
                if kedja:
                    lines.append("%s kedja %d på isen" % (team_name, kedja))
                st_txt = self.shots_text(info, we_home)
                if st_txt:
                    lines.append(st_txt)
                lines.append("%s %s · %s, %s" % (header, ev["score"], ev["time"], ev["period"] or ""))
                if old is not None:
                    title = "Rättelse, målet till %s" % ev["score"]
                    tags, prio = ["pencil2"], 3
                elif ours:
                    title = "MÅL %s! %s" % (team_name.upper(), ev["score"])
                    tags, prio = ["rotating_light", "ice_hockey"], 5
                else:
                    title = "Mål %s. %s" % (opp, ev["score"])
                    tags, prio = ["ice_hockey"], 4
                self.send(title, "\n".join(lines), tags, prio)

            elif ev["type"] == "penalty":
                key = "%s|%s|%s|%s" % (ev["time"], ev["team"], ev["player"], ev["minutes_txt"])
                if key in self.st["penalties"]:
                    continue
                self.st["penalties"].append(key)
                if silent:
                    continue
                ours = ev["team"] == us_abbr
                who = team_name if ours else opp
                lines = [
                    "%s: %s" % (who, ev["player"] or "Lagstraff"),
                    "Straff: %s min – %s" % (ev["minutes_txt"], penalty_sv(ev["reason"]) or "orsak ej angiven"),
                    situation_text(info, ev, us_abbr, them_abbr, team_name),
                    "%s · %s, %s" % (header, ev["time"], ev["period"] or ""),
                ]
                if strength_affecting(ev):
                    pp = "POWERPLAY" in lines[2]
                    bp = "BOXPLAY" in lines[2]
                    title = ("Utvisning %s – %s i powerplay" % (opp, team_name)) if pp else \
                            ("Utvisning %s – boxplay" % team_name) if bp else \
                            ("Utvisning %s" % who)
                else:
                    title = "Utvisning %s (%s min)" % (who, ev["minutes_txt"])
                tags = ["warning"] if ours else ["muscle"]
                self.send(title, "\n".join(lines), tags, 4)

        # Periodslut och periodstart
        if self.cfg.get("notify_periods", True) and not info["final"]:
            pn = period_no(info.get("status"))
            started = self.st.get("period_started", 0)
            ended = self.st.get("period_ended", 0)
            score_line = "Ställning: %s %s %s" % (header, info["score"] or "", info["periods"])
            if self.shots_text(info, we_home):
                score_line += "\n" + self.shots_text(info, we_home)

            def end_period(n):
                self.st["period_ended"] = n
                if not silent:
                    self.send("Slut på %s" % period_name(n).lower(), score_line, ["hourglass"], 3)

            if pn:
                if pn > started:
                    if started and ended < started:
                        end_period(started)
                    self.st["period_started"] = pn
                    if pn >= 2 and not silent:
                        self.send("%s har börjat" % period_name(pn), score_line, ["ice_hockey"], 3)
                limit = {1: 1200, 2: 2400, 3: 3600}.get(pn)
                if limit and info.get("clock") is not None and info["clock"] >= limit \
                        and self.st.get("period_ended", 0) < pn:
                    end_period(pn)
            elif info.get("status") and started and ended < started:
                # Status är något annat än en period (t.ex. periodpaus)
                end_period(started)
        if info["final"]:
            self.st["period_ended"] = max(self.st.get("period_started", 0), self.st.get("period_ended", 0))

        # Mål som försvunnit från swehockey = bortdömda/strukna
        if info["events"]:
            for k in list(self.st["goals"].keys()):
                if k in cur_keys:
                    missing.pop(k, None)
                    continue
                missing[k] = missing.get(k, 0) + 1
                if missing[k] < 3:   # vänta ~45 s så att det inte är en tillfällig miss
                    continue
                g = self.st["goals"].pop(k)
                missing.pop(k, None)
                if silent:
                    continue
                who = team_name if g.get("team") == us_abbr else opp
                self.send("Mål bortdömt",
                          "Målet av %s (%s) vid %s räknas inte längre.\nStällning nu: %s %s" % (
                              g.get("scorer", "?"), who, g.get("time", "?"), header, info["score"] or ""),
                          ["x"], 4)

        if info["final"] and not self.st["final"]:
            self.st["final"] = True
            if not silent and self.cfg.get("notify_start_and_final", True):
                we_score, they_score = (info["score"] or "0-0").split("-")
                if not we_home:
                    we_score, they_score = they_score, we_score
                res = "VINST" if int(we_score) > int(they_score) else "Förlust"
                self.send("Slut: %s %s" % (header, info["score"]),
                          ("%s %s %s-%s mot %s %s" % (res, team_name, we_score, they_score, opp, info["periods"]))
                          + ("\n" + self.shots_text(info, we_home) if info.get("shots") else ""),
                          ["checkered_flag"], 4)
        self.save()


def follow_game(cfg, g):
    gid = g["game_id"]
    url = "%s/Game/Events/%s" % (BASE, gid)
    log("Följer %s – %s (%s %s), match-ID %s" % (g["home"], g["away"], g["date"], g["time"], gid))
    follower = GameFollower(cfg, gid)
    if follower.st.get("final"):
        return
    # Håll Macen vaken under matchen
    caff = None
    try:
        caff = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
    except Exception:
        pass
    start = game_start(g)
    first_fetch = True
    try:
        while True:
            try:
                page = fetch(url)
                info = parse_game(page)
                polls = getattr(follower, "_polls", 0)
                follower._polls = polls + 1
                if follower.our_lineup() is None or (polls % 20 == 0):
                    try:
                        lu = parse_lineup(fetch("%s/Game/LineUps/%s" % (BASE, gid)))
                        if lu:
                            follower.lineup = lu
                            follower.lineup_notice("%s – %s, %s %s" % (g["home"], g["away"], g["date"], g["time"]))
                            follower.save()
                    except Exception as e:
                        log("Kunde inte hämta laguppställning: %s" % e)
            except Exception as e:
                log("Fel vid hämtning: %s" % e)
                info = None
            if info and info["home"]:
                # Startar bevakaren först när matchen redan är slut? Skicka inget.
                silent = first_fetch and info["final"] and not follower.st["started"]
                follower.process(info, silent=silent)
                first_fetch = False
                if info["final"]:
                    log("Matchen är slut.")
                    return
                wait = cfg["poll_seconds"] if info["events"] else 60
            else:
                wait = 60
            if now() > start + dt.timedelta(hours=6) and not g["time"].startswith("00:"):
                log("Ger upp matchen efter 6 timmar.")
                return
            if g["time"].startswith("00:") and now().date().isoformat() != g["date"]:
                return
            time.sleep(wait)
    finally:
        if caff:
            caff.terminate()


def find_all_games(cfg):
    out = []
    for lid in cfg["league_ids"]:
        try:
            page = fetch("%s/ScheduleAndResults/Schedule/%s" % (BASE, lid))
        except Exception as e:
            log("Kunde inte hämta schemat (%s): %s" % (lid, e))
            continue
        for g in parse_schedule(page):
            g["league_id"] = lid
            g["ours"] = cfg["team_match"].lower() in (g["home"] + g["away"]).lower()
            out.append(g)
    return sorted(out, key=lambda g: (g["date"], g["time"]))


def watch_window(g):
    """(från, till) då matchen ska bevakas."""
    if g["time"].startswith("00:"):
        day = dt.datetime.strptime(g["date"], "%Y-%m-%d")
        day = day.replace(tzinfo=TZ) if TZ else day
        return day + dt.timedelta(hours=11), day + dt.timedelta(hours=24)
    st = game_start(g)
    return st - dt.timedelta(minutes=45 if g["ours"] else 5), st + dt.timedelta(hours=6)


def poll_game(cfg, g, followers):
    gid = str(g["game_id"])
    f = followers.get(gid)
    if f is None:
        f = followers[gid] = GameFollower(cfg, gid)
        f.league = not g["ours"]
        f.first_fetch = True
    f.cfg = cfg
    try:
        page = fetch("%s/Game/Events/%s" % (BASE, gid))
        info = parse_game(page)
    except Exception as e:
        log("Fel vid hämtning av %s: %s" % (gid, e))
        return
    if g["ours"]:
        polls = getattr(f, "_polls", 0)
        f._polls = polls + 1
        if f.our_lineup() is None or polls % 20 == 0:
            try:
                lu = parse_lineup(fetch("%s/Game/LineUps/%s" % (BASE, gid)))
                if lu:
                    f.lineup = lu
                    f.lineup_notice("%s – %s, %s %s" % (g["home"], g["away"], g["date"], g["time"]))
                    f.save()
            except Exception as e:
                log("Kunde inte hämta laguppställning: %s" % e)
    if not info["home"]:
        return
    # Övriga matcher: vid start mitt i en match skickas bara det som händer härefter
    silent = f.first_fetch and ((info["final"] and not f.st["started"]) or (f.league and not f.st.get("started")))
    f.first_fetch = False
    if f.league:
        f.process_league(info, silent=silent)
    else:
        f.process(info, silent=silent)
    if info["final"]:
        log("Slut: %s – %s %s" % (info["home"], info["away"], info["score"]))


def run(cfg):
    log("Bevakaren startad (Bofors + övriga matcher).")
    followers = {}
    caff = None
    games, fetched_at = [], None
    last_league_poll = 0
    while True:
        cfg = load_config()
        if fetched_at is None or (now() - fetched_at).total_seconds() > 1800:
            g2 = find_all_games(cfg)
            if g2:
                games, fetched_at = g2, now()
        today = now().date().isoformat()
        state = shared_state()
        todays = [g for g in games if g["date"] == today
                  and not state.get(str(g["game_id"]), {}).get("final")
                  and (g["ours"] or cfg.get("follow_league", True))]
        t = now()
        active = [g for g in todays if watch_window(g)[0] <= t <= watch_window(g)[1]]
        if not active:
            if caff:
                caff.terminate()
                caff = None
            upcoming = [g for g in games if g["ours"] and g["date"] >= today
                        and not state.get(str(g["game_id"]), {}).get("final")]
            if upcoming:
                log("Ingen aktiv match. Nästa Bofors-match: %s %s %s – %s" % (
                    upcoming[0]["date"], upcoming[0]["time"], upcoming[0]["home"], upcoming[0]["away"]))
            starts = [watch_window(g)[0] for g in todays if watch_window(g)[0] > t]
            wait = min([(s - t).total_seconds() for s in starts] + [30 * 60])
            fetched_at = None if wait >= 30 * 60 else fetched_at
            wall_sleep(max(wait, 30))
            continue
        if caff is None:
            try:
                caff = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            except Exception:
                caff = None
        do_league = time.time() - last_league_poll >= cfg.get("league_poll_seconds", 30)
        for g in active:
            if g["ours"] or do_league:
                poll_game(cfg, g, followers)
        if do_league:
            last_league_poll = time.time()
        time.sleep(cfg["poll_seconds"])


def run_old(cfg):
    log("Bofors-bevakaren startad.")
    while True:
        cfg = load_config()
        games = find_team_games(cfg)
        today = now().date().isoformat()
        state = load_json(STATE_PATH, {})
        todays = [g for g in games if g["date"] == today
                  and not state.get(str(g["game_id"]), {}).get("final")]
        if not todays:
            upcoming = [g for g in games if g["date"] > today]
            if upcoming:
                log("Ingen match idag. Nästa: %s %s %s – %s" % (
                    upcoming[0]["date"], upcoming[0]["time"], upcoming[0]["home"], upcoming[0]["away"]))
            wall_sleep(30 * 60)
            continue
        g = todays[0]
        if g["time"].startswith("00:"):
            begin = now()  # okänd starttid – börja bevaka direkt
        else:
            begin = game_start(g) - dt.timedelta(minutes=45)
        wait = (begin - now()).total_seconds()
        if wait > 0:
            log("Match idag %s %s – %s. Väntar till %s." % (g["time"], g["home"], g["away"], begin.strftime("%H:%M")))
            wall_sleep(min(wait, 30 * 60))
            continue
        follow_game(cfg, g)
        time.sleep(60)


def main():
    cfg = load_config()
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        while True:
            try:
                run(cfg)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                log("Oväntat fel: %r – startar om om en minut." % e)
                time.sleep(60)
    elif cmd == "test":
        ok = notify(cfg, "Bofors-bevakaren fungerar!",
                    "Du får notiser här vid mål, assist, utvisningar och PP/BP i Bofors matcher.",
                    ["ice_hockey"], 4)
        print("Testnotis skickad." if ok else "Kunde inte skicka testnotisen.")
    elif cmd == "next":
        games = find_team_games(cfg)
        today = now().date().isoformat()
        for g in [g for g in games if g["date"] >= today][:5]:
            print("%s %s  %s – %s  (match-ID %s)" % (g["date"], g["time"], g["home"], g["away"], g["game_id"]))
    elif cmd == "replay":
        gid = sys.argv[2]
        send = "--send" in sys.argv
        page = fetch("%s/Game/Events/%s" % (BASE, gid))
        f = GameFollower(cfg, gid, dry_run=not send)
        f.st = {"goals": {}, "penalties": [], "started": False, "final": False}
        f.dry_run = not send
        f.save = lambda: None
        f.process(parse_game(page))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
