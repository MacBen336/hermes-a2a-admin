"""Backend fuer das A2A-Verwaltungsplugin (hermes-a2a-admin).

Bedient BEIDE Oberflaechen ueber denselben Namespace /api/plugins/hermes-a2a-admin/:
  - die Web-Dashboard-Haelfte (dashboard/dist/index.js)
  - die Desktop-Haelfte      (desktop/plugin.js)

Laeuft im Dashboard-Prozess (FastAPI-Mount).

Stufe 1 — lesend: /state, /card. Ist-Zustand je Identitaet; gemeldet werden nur
Gegenwart (ja/nein), Anzahl und Peer-Namen — **nie** ein Tokenwert.
Stufe 2 — schreibend: /token/generate, /token/rotate, /map/sync. Erzeugt Token je
Profil und schreibt sie in die jeweilige `.env`; jede schreibende Route verlangt
`"confirm": true`, weil die Oberflaeche vorher nachfragt.

**Schreibweg — bewusst nicht `save_env_value` selbst:** das oeffentliche
`hermes_cli.config.save_env_value()` bindet an `get_env_path()` =
`get_hermes_home()` des **laufenden** Prozesses (config.py:477-479, :2772-2796) und
veroeffentlicht den Wert zusaetzlich in `os.environ` dieses Prozesses. Fuer ein
fremdes Profil-Home ist beides falsch. Deshalb werden dieselben Bausteine mit
**explizitem Pfad** benutzt: `_read_env_lines`, `_quote_env_value`,
`_env_line_defines_key` und `_write_env_lines` (letzteres schreibt atomar per
temp-Datei + fsync + rename und erhaelt den Dateimodus). Zusaetzlich legt das
Plugin vor jedem Schreiben eine Sicherung `<env>.bak-<stamp>-a2a` an.

Quellen je Identitaet:
  <home>/config.yaml      platforms.a2a.extra.port, platforms.a2a.enabled, a2a_agents
  <home>/.env             A2A_SELF_TOKEN / A2A_PEER_TOKENS / A2A_BEARER_TOKEN / A2A_HOST / A2A_TRUSTED_PEERS
  http://127.0.0.1:<port> laufende Agent Card (Beweis, dass der Listener lebt)
  <home>/a2a_audit.jsonl  letzte Eintraege (peer = aufgeloeste Identitaet)
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter()

PLUGIN_ID = "hermes-a2a-admin"
DEFAULT_PORT = 9900
CARD_TIMEOUT = 2.0
AUDIT_TAIL = 3
TOKEN_BYTES = 32  # -> 64 Hex-Zeichen
TOKEN_RE = re.compile(r"^[0-9a-f]{32,}$")
SENTINEL_NAME = "nobody"
STAGE = 3

# AUSGEHENDER Peer-Timeout: `a2a_agents.<peer>.timeout` in der config.yaml des Anrufers.
# Der Default stammt aus Hermes selbst — plugins/platforms/a2a/tools.py:21 `_DEFAULT_TIMEOUT = 120`.
# Fehlt der Schluessel, gilt 120 und NICHT der oft danebenstehende Wunschwert. Hier nur gespiegelt,
# damit die Oberflaeche den effektiven Wert zeigen kann statt einer leeren Zeile.
DEFAULT_PEER_TIMEOUT = 120
MIN_PEER_TIMEOUT = 30
MAX_PEER_TIMEOUT = 600
PEER_TIMEOUT_STEP = 30

# --- Wurzel bestimmen (gleiche Logik wie plugins/search-fanout/dashboard/plugin_api.py) ---
HERMES_ROOT = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
if HERMES_ROOT.name == "default":
    ROOT = HERMES_ROOT.parent.parent
elif (HERMES_ROOT / "profiles").is_dir():
    ROOT = HERMES_ROOT
elif HERMES_ROOT.parent.name == "profiles":
    ROOT = HERMES_ROOT.parent.parent
else:
    ROOT = HERMES_ROOT


# ------------------------------------------------------------------ Basiszugriffe

def _identities() -> list[tuple[str, Path]]:
    """(Name, Home) je Identitaet. `default` liegt in der Wurzel (dort liegt seine .env)."""
    out: list[tuple[str, Path]] = [("default", ROOT)]
    pdir = ROOT / "profiles"
    if pdir.is_dir():
        for d in sorted(pdir.iterdir()):
            if d.is_dir() and not d.is_symlink() and d.name != "default" and (d / "config.yaml").is_file():
                out.append((d.name, d))
    return out


def _home_of(profile: str) -> Path:
    for name, home in _identities():
        if name == profile:
            return home
    raise HTTPException(status_code=404, detail=f"Unbekanntes Profil: {profile}")


def _inside(path: Path, root: Path) -> bool:
    """True, wenn `path` (aufgeloest) in `root` (aufgeloest) liegt — Symlinks eingeschlossen.

    Zweite Sicherung neben der Symlink-Pruefung der .env: selbst wenn ein Pfad durch einen
    Symlink irgendwohin zeigt, wird nur innerhalb der Hermes-Wurzel geschrieben.
    """
    try:
        p, r = path.resolve(), root.resolve()
    except OSError:
        return False
    return p == r or r in p.parents


def _read_env(home: Path) -> dict[str, str]:
    """KEY=VALUE aus <home>/.env. Werte bleiben hier — sie werden nie zurueckgegeben."""
    env: dict[str, str] = {}
    try:
        for line in (home / ".env").read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return env


def _read_config(home: Path) -> dict:
    try:
        from ruamel.yaml import YAML

        return YAML(typ="safe").load((home / "config.yaml").read_text(encoding="utf-8")) or {}
    except Exception:
        return {}


def _a2a_block(cfg: dict) -> dict:
    platforms = cfg.get("platforms") or {}
    block = platforms.get("a2a") or {}
    return block if isinstance(block, dict) else {}


def _port(cfg: dict, env: dict[str, str]) -> tuple[int, str]:
    """Port + Herkunft — config schlaegt env schlaegt Default."""
    extra = _a2a_block(cfg).get("extra") or {}
    raw = extra.get("port") if isinstance(extra, dict) else None
    if raw:
        try:
            return int(raw), "config"
        except (TypeError, ValueError):
            pass
    if env.get("A2A_PORT"):
        try:
            return int(env["A2A_PORT"]), "env"
        except ValueError:
            pass
    return DEFAULT_PORT, "default"


def _identity_state(env: dict[str, str]) -> dict:
    """Nur Gegenwart und Namen — kein Wert verlaesst diese Funktion."""
    self_token = env.get("A2A_SELF_TOKEN") or ""
    peer_raw = env.get("A2A_PEER_TOKENS") or ""
    callers = []
    for pair in peer_raw.split(","):
        if ":" in pair:
            name = pair.split(":", 1)[0].strip()
            if name:
                callers.append(name)
    trusted = [p.strip() for p in (env.get("A2A_TRUSTED_PEERS") or "").split(",") if p.strip()]
    return {
        "self_token": bool(self_token),
        "self_token_format": bool(TOKEN_RE.match(self_token)) if self_token else False,
        "callers": callers,
        "caller_count": len(callers),
        "shared_bearer": bool(env.get("A2A_BEARER_TOKEN")),
        "trusted_peers": trusted,
        "host_setting": env.get("A2A_HOST") or None,
    }


def _peers(cfg: dict) -> list[dict]:
    agents = cfg.get("a2a_agents") or {}
    out = []
    if isinstance(agents, dict):
        for name, entry in agents.items():
            entry = entry if isinstance(entry, dict) else {}
            out.append(
                {
                    "name": name,
                    "url": str(entry.get("url", "")),
                    "timeout": entry.get("timeout"),
                    "timeout_explicit": entry.get("timeout") is not None,
                    "timeout_effective": entry.get("timeout") or DEFAULT_PEER_TIMEOUT,
                    "auth": bool(entry.get("auth")),
                    "capabilities": entry.get("capabilities") or [],
                }
            )
    return out


def _card(port: int) -> dict:
    url = f"http://127.0.0.1:{port}/.well-known/agent-card.json"
    try:
        with urllib.request.urlopen(url, timeout=CARD_TIMEOUT) as resp:  # noqa: S310 (Loopback, feste Form)
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
        return {
            "ok": True,
            "status": getattr(resp, "status", 200),
            "name": data.get("name"),
            "description": (data.get("description") or "")[:200],
            "security_schemes": bool(data.get("securitySchemes")),
            "skills": len(data.get("skills") or []),
            "error": None,
        }
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "name": None, "security_schemes": False, "error": f"HTTP {exc.code}"}
    except Exception as exc:  # Timeout, refused, kein JSON
        return {"ok": False, "status": None, "name": None, "security_schemes": False, "error": type(exc).__name__}


def _audit(home: Path) -> dict:
    path = home / "a2a_audit.jsonl"
    entries = 0
    tail: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"exists": path.is_file(), "entries": 0, "tail": []}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        entries += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = rec.get("ts")
        tail.append(
            {
                "zeit": datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat() if ts else None,
                "richtung": rec.get("direction"),
                "peer": rec.get("peer"),
                "task_id": rec.get("task_id"),
            }
        )
    return {"exists": path.is_file(), "entries": entries, "tail": tail[-AUDIT_TAIL:]}


def _adapter_start(home: Path) -> str | None:
    """Zeitstempel der letzten 'serving Agent Card'-Zeile aus dem Gateway-Log des Profils.

    Der Adapter friert seinen Sicherheitskontext (Anrufer-Map) beim Start ein — dieser Zeitpunkt
    entscheidet, ob eine geaenderte Map schon wirkt. Ohne ihn ist die Oberflaeche irrefuehrend:
    Haken gesetzt, aber der laufende Adapter kennt sie nicht.
    """
    log = home / "logs" / "gateway.log"
    try:
        treffer = None
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "serving Agent Card" in line:
                treffer = line
    except OSError:
        return None
    if not treffer:
        return None
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", treffer)
    return match.group(1) if match else None


def _env_written(home: Path) -> str | None:
    try:
        return datetime.fromtimestamp((home / ".env").stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
    except OSError:
        return None


def _verdict(
    identity: dict,
    card: dict,
    port: int,
    env_zeit: str | None = None,
    adapter_zeit: str | None = None,
) -> tuple[str, str]:
    """Zustand aus drei Beobachtungen ableiten: .env-Stand, laufende Card und Startzeitpunkt."""
    if not card["ok"]:
        return "stumm", f"Kein Listener auf {port} ({card['error'] or 'ohne Antwort'})"
    if env_zeit and adapter_zeit and env_zeit > adapter_zeit:
        return (
            "neustart-noetig",
            f"Anrufer-Adressbuch geschrieben {env_zeit}, Adapter gestartet {adapter_zeit} — der "
            f"Adapter friert die Map beim Start ein: Aenderungen wirken erst nach einem Neustart",
        )
    if identity["self_token"] or identity["callers"]:
        if card["security_schemes"]:
            return "aktiv", "Identitaet gesetzt und der laufende Server verlangt sie"
        return (
            "neustart-vermutet",
            "Token steht in der .env, die laufende Card bewirbt aber keine Authentifizierung — "
            "der Adapter wurde vor der Aenderung gestartet",
        )
    return "ohne-identitaet", "Kein Token gesetzt: der Empfaenger sieht nur die Aufrufer-IP"


def _profile_state(name: str, home: Path, ctx: dict | None = None) -> dict:
    env = _read_env(home)
    cfg = _read_config(home)
    port, port_src = _port(cfg, env)
    identity = _identity_state(env)
    card = _card(port)
    env_zeit = _env_written(home)
    adapter_zeit = _adapter_start(home)
    state, reason = _verdict(identity, card, port, env_zeit, adapter_zeit)
    return {
        "profile": name,
        "rufrechte": _rufrechte(name, identity, ctx or {}),
        "env_written": env_zeit,
        "adapter_started": adapter_zeit,
        "home": str(home),
        "port": port,
        "port_source": port_src,
        "configured": bool(_a2a_block(cfg).get("enabled")),
        "identity": identity,
        "peers": _peers(cfg),
        "card": card,
        "state": state,
        "state_reason": reason,
        "audit": _audit(home),
    }


# ------------------------------------------------------------------ Schreibweg

class Confirm(BaseModel):
    confirm: bool = False


class TokenRequest(Confirm):
    profile: str


class SyncRequest(Confirm):
    profile: str | None = None


class AuthConfigRequest(Confirm):
    profile: str | None = None


def _new_token() -> str:
    return secrets.token_hex(TOKEN_BYTES)


def _validate_updates(updates: dict[str, str]) -> None:
    """Formatpruefung vor jedem Schreiben — ein falsch geformter Wert darf nie in einer .env landen."""
    for key, value in updates.items():
        if key == "A2A_SELF_TOKEN":
            if not TOKEN_RE.match(value):
                raise HTTPException(status_code=400, detail="A2A_SELF_TOKEN hat kein gueltiges Token-Format")
        elif key == "A2A_PEER_TOKENS":
            for pair in [p for p in value.split(",") if p]:
                parts = pair.split(":", 1)
                if len(parts) != 2 or not parts[0].strip() or not TOKEN_RE.match(parts[1].strip()):
                    raise HTTPException(status_code=400, detail=f"Map-Eintrag ungueltig (Name: {parts[0]!r})")
        else:
            raise HTTPException(status_code=400, detail=f"Schluessel nicht erlaubt: {key}")


def _write_env(home: Path, updates: dict[str, str]) -> dict:
    """Setzt/aktualisiert Schluessel in <home>/.env ueber die Hermes-Bausteine mit
    explizitem Pfad. Liefert nur Metadaten (Datei, Sicherung, geaenderte Schluessel)."""
    from hermes_cli.config import (  # type: ignore
        _env_line_defines_key,
        _quote_env_value,
        _read_env_lines,
        _write_env_lines,
        invalidate_env_cache,
        validate_env_var_name_for_write,
    )

    if not home.is_dir():
        raise HTTPException(status_code=400, detail=f"Profil-Home fehlt: {home}")
    _validate_updates(updates)
    for key in updates:
        validate_env_var_name_for_write(key)

    env_path = home / ".env"
    if env_path.is_symlink():
        raise HTTPException(status_code=400, detail=f"Symlink abgelehnt: {env_path}")
    if not _inside(home, ROOT) or not _inside(env_path, ROOT):
        raise HTTPException(status_code=400, detail=f"Ziel liegt ausserhalb der Hermes-Wurzel ({ROOT}) — abgelehnt")

    lines = _read_env_lines(env_path) if env_path.exists() else []
    changed: list[str] = []
    for key, value in updates.items():
        serialized = _quote_env_value(value)
        idx = next((i for i, line in enumerate(lines) if _env_line_defines_key(line, key)), None)
        if idx is not None:
            if lines[idx].strip() != f"{key}={serialized}".strip():
                lines[idx] = f"{key}={serialized}\n"
                changed.append(key)
        else:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines.append(f"{key}={serialized}\n")
            changed.append(key)

    # Nichts zu tun: keine Sicherung anlegen und — wichtig — keine bestehende anfassen.
    if not changed:
        return {"env_datei": str(env_path), "backup": None, "geaendert": [], "unveraendert": True}

    # Erst jetzt sichern, und nur einmal pro Datei und Tag: der aelteste Stand des Tages
    # ist der beste Rueckweg-Zielpunkt. Eine bestehende Sicherung wird NIE ueberschrieben
    # (sonst zerstoert ein zweiter Lauf innerhalb derselben Sekunde den ersten Stand).
    backup = env_path.with_name(env_path.name + f".bak-{time.strftime('%Y%m%d')}-a2a")
    if env_path.exists() and not backup.exists():
        backup.write_bytes(env_path.read_bytes())
        os.chmod(backup, 0o600)
    elif not env_path.exists():
        backup = None

    _write_env_lines(env_path, lines, preserve_mode=env_path.exists())
    invalidate_env_cache()
    return {
        "env_datei": str(env_path),
        "backup": str(backup) if backup else None,
        "geaendert": changed,
        "unveraendert": False,
    }


def _token_of(home: Path) -> str:
    return _read_env(home).get("A2A_SELF_TOKEN") or ""


def _target_callers() -> dict[str, list[str]]:
    """Rollengetreue Anrufer-Map: P darf von X gerufen werden, wenn X den Peer P fuehrt."""
    peers_of: dict[str, list[str]] = {}
    for name, home in _identities():
        peers_of[name] = [p["name"] for p in _peers(_read_config(home))]
    return {name: sorted(x for x, peers in peers_of.items() if name in peers) for name in peers_of}


# ------------------------------------------------------------------ Routen

def _timeout_summary(profiles: list[dict]) -> dict:
    """Verteilung der AUSGEHENDEN Peer-Timeouts ueber alle Profile — Grundlage des globalen Reglers.

    `gemeinsam` ist nur dann ein Wert, wenn wirklich jeder Peer-Eintrag denselben effektiven Wert
    hat; sonst `None` (dann zeigt die Oberflaeche die vorkommenden Werte). `explizit_gesetzt`
    zaehlt die Eintraege, die den Schluessel wirklich fuehren — die Differenz zu `peers` sind die
    stillen 120er.
    """
    effektiv: list[int] = []
    explizit = 0
    for profil in profiles:
        for peer in profil.get("peers") or []:
            if peer.get("timeout_explicit"):
                explizit += 1
            effektiv.append(int(peer.get("timeout_effective") or DEFAULT_PEER_TIMEOUT))
    return {
        "standard": DEFAULT_PEER_TIMEOUT,
        "bereich": [MIN_PEER_TIMEOUT, MAX_PEER_TIMEOUT],
        "schritt": PEER_TIMEOUT_STEP,
        "peers": len(effektiv),
        "explizit_gesetzt": explizit,
        "gemeinsam": (effektiv[0] if effektiv and len(set(effektiv)) == 1 else None),
        "werte": sorted(set(effektiv)),
    }


@router.get("/state")
def state() -> dict:
    ctx = _permission_context()
    profiles = [_profile_state(name, home, ctx) for name, home in _identities()]
    return {
        "plugin": PLUGIN_ID,
        "stage": STAGE,
        "mode": "lesend + schreibend (Token/Maps, Bestaetigung im UI)",
        "generated": datetime.now(tz=timezone.utc).isoformat(),
        "root": str(ROOT),
        "profiles": profiles,
        "timeout": _timeout_summary(profiles),
    }


@router.get("/card")
def card(port: int = DEFAULT_PORT) -> dict:
    return {"port": port, **_card(port)}


@router.post("/token/generate")
def token_generate(req: TokenRequest) -> dict:
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    home = _home_of(req.profile)
    if _token_of(home):
        return {"ok": False, "profil": req.profile, "grund": "Token ist bereits gesetzt — nichts geaendert"}
    result = _write_env(home, {"A2A_SELF_TOKEN": _new_token()})
    return {
        "ok": True,
        "profil": req.profile,
        "aktion": "token-erzeugen",
        **result,
        "hinweis": "Wirkt nach dem naechsten Neustart von Gateway bzw. Dienst.",
    }


@router.post("/token/rotate")
def token_rotate(req: TokenRequest) -> dict:
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    home = _home_of(req.profile)
    had = bool(_token_of(home))
    result = _write_env(home, {"A2A_SELF_TOKEN": _new_token()})
    return {
        "ok": True,
        "profil": req.profile,
        "aktion": "token-rotieren",
        **result,
        "achtung": (
            ["Das alte Token ist ungueltig; alle Maps, die es fuehren, muessen nachgezogen werden "
             "(maps-abgleichen)."]
            if had
            else []
        ),
        "hinweis": "Danach 'Maps abgleichen' und Neustart.",
    }


@router.post("/map/sync")
def map_sync(req: SyncRequest) -> dict:
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    targets = [req.profile] if req.profile else [n for n, _ in _identities()]
    for t in targets:
        _home_of(t)

    # 1) fehlende eigene Token erzeugen, damit jede Map vollstaendig sein kann
    erzeugt: list[str] = []
    for name, home in _identities():
        if not _token_of(home):
            _write_env(home, {"A2A_SELF_TOKEN": _new_token()})
            erzeugt.append(name)

    # 2) Token frisch einlesen (nach dem Erzeugen) und Maps schreiben
    tokens = {n: _token_of(h) for n, h in _identities()}
    callers = _target_callers()
    ergebnisse = []
    for name in targets:
        home = _home_of(name)
        cs = callers.get(name, [])
        missing = [c for c in cs if not tokens.get(c)]
        if missing:
            ergebnisse.append(
                {
                    "profil": name,
                    "geschrieben": False,
                    "anrufer": cs,
                    "grund": f"Token fehlt fuer: {', '.join(missing)}",
                }
            )
            continue
        if cs:
            payload = ",".join(f"{c}:{tokens[c]}" for c in cs)
        else:
            # Eine leere Map waere kein Deny-all, sondern wuerde den Listener auf localhost-only
            # zurueckstellen — deshalb ein Sentinel, den niemand besitzt. Vorhandenen weiterverwenden,
            # damit wiederholte Laeufe nichts aendern.
            current = _read_env(home).get("A2A_PEER_TOKENS", "")
            match = re.search(rf"{SENTINEL_NAME}:[0-9a-f]{{32,}}", current)
            payload = match.group(0) if match else f"{SENTINEL_NAME}:{_new_token()}"
        result = _write_env(home, {"A2A_PEER_TOKENS": payload})
        ergebnisse.append(
            {
                "profil": name,
                "geschrieben": not result["unveraendert"],
                "anrufer": cs,
                "sentinel": not cs,
                **result,
            }
        )

    return {
        "ok": True,
        "aktion": "maps-abgleichen",
        "token_neu_erzeugt": erzeugt,
        "ergebnisse": ergebnisse,
        "hinweis": "Wirkt nach dem naechsten Neustart von Gateway bzw. Dienst.",
    }


# ---------------------------------------------------------------- Ausweise in der config.yaml
#
# Der Absenderweg holt seinen Ausweis NICHT aus A2A_SELF_TOKEN, sondern ausschliesslich aus dem
# auth-Block des Peer-Eintrags (`plugins/platforms/a2a/tools.py:36-49`). Fehlt er, scheitert jeder
# ausgehende Ruf mit 401 — unabhaengig davon, wie vollstaendig die Maps sind.
#
# Geschrieben wird eine REFERENZ (${A2A_SELF_TOKEN}), kein Wert: die Expansion ist profil-scoped
# (`hermes_cli/config.py`), jedes Profil setzt also nur sein eigenes Token ein. In der Config steht
# damit kein Geheimnis.
#
# Textbasiert statt YAML-Roundtrip: die echten Configs fuehren Kommentare, ein Dump wuerde sie
# verlieren. Vor dem Schreiben wird der neue Text geparst und gegen den alten verglichen — es darf
# sich ausschliesslich der auth-Block unterscheiden.

AUTH_BLOCK = ["    auth:", "      type: bearer", "      token: ${A2A_SELF_TOKEN}"]
_AUTH_BLOCK_RE = re.compile(r"^    auth:\n      type: bearer\n      token: \$\{A2A_SELF_TOKEN\}\n", re.M)
_PEER_RE = re.compile(r"^  ([A-Za-z0-9_.-]+):\s*$")
_URL_RE = re.compile(r"^    url:\s*\S")


def _next_code_line(lines: list[str], i: int) -> str:
    while i < len(lines):
        if lines[i].strip():
            return lines[i]
        i += 1
    return ""


def _patch_auth_blocks(text: str) -> tuple[str, list[str], int, int]:
    """auth-Block je Peer-Eintrag ergaenzen -> (neuer_text, peers, neu, vorhanden).

    Bricht ab, statt halb zu schreiben: ohne erkannten Peer oder wenn die Blockzahl am Ende nicht
    zur Peerzahl passt, ist die Schreibweise eine andere als erwartet (dann Finger weg).
    """
    lines = text.splitlines(keepends=True)
    if not any(line.startswith("a2a_agents:") for line in lines):
        raise LookupError("kein a2a_agents-Block")
    out: list[str] = []
    peers: list[str] = []
    added = skipped = 0
    in_block = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("a2a_agents:"):
            in_block = True
        elif in_block and line[:1] not in (" ", "\n"):
            in_block = False
        if in_block:
            peer_match = _PEER_RE.match(line)
            if peer_match:
                peers.append(peer_match.group(1))
            if _URL_RE.match(line):
                out.append(line)
                i += 1
                if _next_code_line(lines, i).startswith("    auth:"):
                    skipped += 1
                    continue
                out.extend(entry + "\n" for entry in AUTH_BLOCK)
                added += 1
                continue
        out.append(line)
        i += 1
    if not peers:
        raise ValueError("a2a_agents-Block vorhanden, aber kein Peer erkannt (andere Schreibweise?)")
    new_text = "".join(out)
    if len(_AUTH_BLOCK_RE.findall(new_text)) != len(peers):
        raise ValueError("auth-Bloecke passen nicht zur Peerzahl")
    return new_text, peers, added, skipped


def _check_config_roundtrip(old_text: str, new_text: str) -> None:
    """Netz des Textpatchers: der neue Text muss parsen UND sich vom alten nur in den auth-Bloecken
    unterscheiden. Faengt jede Fehlplatzierung, bevor sie in einer echten Config landet."""
    import yaml

    alt = yaml.safe_load(old_text) or {}
    neu = yaml.safe_load(new_text) or {}
    alt_peers = alt.get("a2a_agents") or {}
    neu_peers = neu.get("a2a_agents") or {}
    if set(alt_peers) != set(neu_peers):
        raise ValueError("Peernamen haben sich geaendert")
    if not isinstance(alt_peers, dict) or not isinstance(neu_peers, dict):
        raise ValueError("a2a_agents ist keine Map")
    for name, entry in neu_peers.items():
        if isinstance(entry, dict):
            auth = entry.pop("auth", None)
            if auth is not None and not (
                isinstance(auth, dict) and auth.get("type") == "bearer" and auth.get("token") == "${A2A_SELF_TOKEN}"
            ):
                raise ValueError(f"unerwarteter auth-Block bei '{name}'")
    for name, entry in alt_peers.items():
        if isinstance(entry, dict):
            entry.pop("auth", None)
    if alt != neu:
        raise ValueError("ausserhalb der auth-Bloecke wuerde sich die Config aendern")


def _write_text(path: Path, text: str) -> None:
    """Atomar schreiben (temp + fsync + rename); Modus der Zieldatei bleibt erhalten."""
    import contextlib
    import tempfile

    mode = path.stat().st_mode & 0o7777
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".cfg-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _auth_plan() -> list[dict]:
    """Je Identitaet: Peers sowie fehlende/vorhandene auth-Bloecke — schreibt nichts."""
    plan: list[dict] = []
    for name, home in _identities():
        cfg = home / "config.yaml"
        eintrag: dict = {"profil": name, "peers": [], "auth_neu": 0, "auth_vorhanden": 0, "schreibbar": False}
        try:
            if cfg.is_symlink() or not cfg.is_file() or not _inside(cfg, ROOT):
                raise ValueError("config.yaml fehlt, ist ein Symlink oder liegt ausserhalb der Wurzel")
            _, peers, added, skipped = _patch_auth_blocks(cfg.read_text(encoding="utf-8"))
            eintrag.update({"peers": peers, "auth_neu": added, "auth_vorhanden": skipped, "schreibbar": True})
        except LookupError as exc:
            eintrag["grund"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — Vorschau darf nie werfen
            eintrag["grund"] = f"nicht lesbar: {exc}"
        plan.append(eintrag)
    return plan


@router.get("/config/auth")
def config_auth_preview() -> dict:
    """Vorschau: was wuerde an den Peer-Eintraegen ergaenzt? Schreibt nichts."""
    return {"ok": True, "aktion": "ausweise-vorschau", "ergebnisse": _auth_plan()}


@router.post("/config/auth")
def config_auth_write(req: AuthConfigRequest) -> dict:
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    targets = [req.profile] if req.profile else [n for n, _ in _identities()]
    for target in targets:
        _home_of(target)

    ergebnisse = []
    for name in targets:
        cfg = _home_of(name) / "config.yaml"
        # Alle Felder vorbelegen: ein abgelehnter Eintrag darf keinen halben Datensatz liefern —
        # die Oberflaeche liest peers/auth_neu unabhaengig vom Ergebnis.
        eintrag: dict = {
            "profil": name,
            "geschrieben": False,
            "backup": None,
            "peers": [],
            "auth_neu": 0,
            "auth_vorhanden": 0,
        }
        try:
            if cfg.is_symlink():
                raise ValueError("Symlink abgelehnt")
            if not cfg.is_file():
                raise LookupError("keine config.yaml")
            if not _inside(cfg, ROOT):
                raise ValueError("Ziel liegt ausserhalb der Hermes-Wurzel")
            alt = cfg.read_text(encoding="utf-8")
            neu, peers, added, skipped = _patch_auth_blocks(alt)
            eintrag.update({"peers": peers, "auth_neu": added, "auth_vorhanden": skipped})
            if added == 0:
                eintrag["grund"] = "schon vollstaendig"
                ergebnisse.append(eintrag)
                continue
            _check_config_roundtrip(alt, neu)
            backup = cfg.with_name(cfg.name + f".bak-{time.strftime('%Y%m%d')}-a2a")
            if not backup.exists():
                backup.write_bytes(cfg.read_bytes())
                os.chmod(backup, cfg.stat().st_mode & 0o7777)
            _write_text(cfg, neu)
            eintrag.update({"geschrieben": True, "backup": str(backup)})
        except LookupError as exc:
            eintrag["grund"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — nichts geschrieben, Grund zurueckgeben
            eintrag["grund"] = f"abgelehnt: {exc}"
        ergebnisse.append(eintrag)

    return {
        "ok": True,
        "aktion": "ausweise-eintragen",
        "ergebnisse": ergebnisse,
        "hinweis": "Ausgehende Rufe wirken sofort — die Config wird pro Aufruf gelesen, kein Neustart noetig.",
    }


# ------------------------------------------------------------------ Peer-Timeouts (globaler Regler)
#
# Der AUSGEHENDE Ruf-Timeout steht in der EIGENEN config.yaml des Anrufers, je Peer-Eintrag:
# `a2a_agents.<peer>.timeout` — gelesen in plugins/platforms/a2a/tools.py:37, Default 120.
# Die Route setzt ihn in ALLEN Profilen auf denselben Wert. Textbasiert wie der auth-Patch
# (Kommentare und Reihenfolge bleiben), mit Sicherung und einem Netz, das nur diesen einen Wert
# zulaesst.
#
# Bewusst NICHT hier: das EINGEHENDE Antwortfenster `A2A_REPLY_TIMEOUT` (env, Default 300,
# adapter.py:66-71). Andere Semantik (wie lange der Angerufene antworten darf), andere Ablage,
# andere Wirkung (Adapterstart). Ein gemeinsamer Regler waere Scheingenauigkeit.

class TimeoutRequest(Confirm):
    seconds: int


_TIMEOUT_LINE_RE = re.compile(r"^    timeout:\s*(\S+)\s*$")
_TOKEN_LINE_RE = re.compile(r"^      token:\s*\S")


def _validate_seconds(seconds: int) -> int:
    """Bereich und Schrittigkeit pruefen, bevor irgendetwas geschrieben wird."""
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise HTTPException(status_code=400, detail="Sekunden muessen eine ganze Zahl sein")
    if not MIN_PEER_TIMEOUT <= seconds <= MAX_PEER_TIMEOUT:
        raise HTTPException(status_code=400, detail=f"Ausserhalb des Bereichs {MIN_PEER_TIMEOUT}-{MAX_PEER_TIMEOUT} s")
    if seconds % PEER_TIMEOUT_STEP:
        raise HTTPException(status_code=400, detail=f"Nur Vielfache von {PEER_TIMEOUT_STEP} s")
    return seconds


def _peer_spans(lines: list[str]) -> list[tuple[str, int, int]]:
    """(Name, Start, Ende exklusiv) je Peer-Eintrag im a2a_agents-Block, in Dateireihenfolge.

    BEWUSST NICHT ueber `_peer_entry_span()`: dessen Ende ist das Blockende, nicht das Ende des
    Eintrags — die Vorwaertsschleife dort laeuft ueber alle Folge-Eintraege weiter, weil die
    ebenfalls eingerueckt sind (belegt: `span geordi 59..93` im conrad-Profil, richtig waere
    59..67). Fuer das Umbenennen eines Eintrags ist das folgenlos, fuer Einfuegen oder Loeschen
    nicht. Hier endet ein Eintrag an der naechsten Kopfzeile (oder am Blockende).
    """
    start = next((i for i, line in enumerate(lines) if line.startswith("a2a_agents:")), None)
    if start is None:
        return []
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    heads = [i for i in range(start + 1, end) if _PEER_RE.match(lines[i])]
    spans: list[tuple[str, int, int]] = []
    for nummer, kopf in enumerate(heads):
        naechster = heads[nummer + 1] if nummer + 1 < len(heads) else end
        while naechster > kopf + 1 and not lines[naechster - 1].strip():
            naechster -= 1
        spans.append((_PEER_RE.match(lines[kopf]).group(1), kopf, naechster))
    return spans


def _drop_peer_entries(text: str, namen: list[str]) -> str:
    """Entfernt Peer-Eintraege VOLLSTAENDIG — von hinten, damit die Indizes gueltig bleiben.

    BEWUSST ueber `_peer_spans` und nicht ueber `_peer_entry_span`: dessen Ende ist das
    Blockende, ein Loeschen wuerde damit den Rest des Blocks mitreissen. Das Netz in
    `/cleanup/duplicates` faengt das ab (es schreibt dann nicht), die Route waere aber
    dauerhaft funktionslos.
    """
    zeilen = text.splitlines(keepends=True)
    spannen = {name: (start, ende) for name, start, ende in _peer_spans(zeilen)}
    fehlend = [name for name in namen if name not in spannen]
    if fehlend:
        raise ValueError(f"Eintrag nicht gefunden: {', '.join(fehlend)}")
    for start, ende in sorted((spannen[name] for name in namen), reverse=True):
        del zeilen[start:ende]
    return "".join(zeilen)


def _patch_peer_timeouts(text: str, seconds: int) -> tuple[str, list[str], int, int]:
    """Setzt in jedem Peer-Eintrag `timeout: <seconds>`.

    Vorhandener Schluessel wird ersetzt, fehlender hinter den auth-Block gesetzt (sonst hinter
    `url:`). Rueckwaerts durchlaufen, damit die Zeilenindizes gueltig bleiben.
    -> (neuer_text, peers, ersetzt, ergaenzt)
    """
    lines = text.splitlines(keepends=True)
    spans = _peer_spans(lines)
    if not spans:
        raise ValueError("kein a2a_agents-Block mit Peer-Eintraegen")
    ersetzt = ergaenzt = 0
    for _name, start, end in reversed(spans):
        vorhanden = next((i for i in range(start + 1, end) if _TIMEOUT_LINE_RE.match(lines[i])), None)
        if vorhanden is not None:
            if lines[vorhanden].rstrip("\n") != f"    timeout: {seconds}":
                lines[vorhanden] = f"    timeout: {seconds}\n"
                ersetzt += 1
            continue
        anker = next(
            (i for i in range(end - 1, start, -1) if _TOKEN_LINE_RE.match(lines[i]) or _URL_RE.match(lines[i])),
            None,
        )
        if anker is None:
            anker = start
        lines.insert(anker + 1, f"    timeout: {seconds}\n")
        ergaenzt += 1
    return "".join(lines), [name for name, _, _ in spans], ersetzt, ergaenzt


def _check_timeouts_applied(old_text: str, new_text: str, seconds: int) -> None:
    """Netz: der neue Text parst und unterscheidet sich AUSSCHLIESSLICH in den timeout-Werten."""
    import yaml

    alt = yaml.safe_load(old_text) or {}
    neu = yaml.safe_load(new_text) or {}
    alt_agents = dict(alt.get("a2a_agents") or {})
    neu_agents = dict(neu.get("a2a_agents") or {})
    if set(alt_agents) != set(neu_agents):
        raise ValueError("Peernamen haben sich geaendert")
    for name, entry in neu_agents.items():
        if not isinstance(entry, dict):
            raise ValueError(f"Peer '{name}' ist keine Map")
        if entry.get("timeout") != seconds:
            raise ValueError(f"Peer '{name}' traegt nicht den Zielwert")
        entry.pop("timeout", None)
    for entry in alt_agents.values():
        if isinstance(entry, dict):
            entry.pop("timeout", None)
    if alt_agents != neu_agents:
        raise ValueError("ausserhalb der timeout-Werte wuerde sich der a2a_agents-Block aendern")
    if dict(alt, a2a_agents=alt_agents) != dict(neu, a2a_agents=neu_agents):
        raise ValueError("weitere Unterschiede ausserhalb von a2a_agents")


@router.post("/timeout/set")
def timeout_set(req: TimeoutRequest) -> dict:
    """Setzt den ausgehenden Peer-Timeout in ALLEN Profilen auf denselben Wert."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    seconds = _validate_seconds(req.seconds)

    ergebnisse = []
    for name, home in _identities():
        cfg = home / "config.yaml"
        # Vorbelegen: ein abgelehnter Eintrag darf keinen halben Datensatz liefern.
        eintrag: dict = {"profil": name, "geschrieben": False, "backup": None, "peers": [], "ersetzt": 0, "ergaenzt": 0}
        try:
            if cfg.is_symlink():
                raise ValueError("Symlink abgelehnt")
            if not cfg.is_file():
                raise LookupError("keine config.yaml")
            if not _inside(cfg, ROOT):
                raise ValueError("Ziel liegt ausserhalb der Hermes-Wurzel")
            alt = cfg.read_text(encoding="utf-8")
            neu, peers, ersetzt, ergaenzt = _patch_peer_timeouts(alt, seconds)
            eintrag.update({"peers": peers, "ersetzt": ersetzt, "ergaenzt": ergaenzt})
            if ersetzt == 0 and ergaenzt == 0:
                eintrag["grund"] = "schon auf diesem Wert"
                ergebnisse.append(eintrag)
                continue
            _check_timeouts_applied(alt, neu, seconds)
            # Zwischen Lesen und Schreiben kann ein anderer Schreiber dieselbe Datei geaendert
            # haben (z. B. `hermes config set` oder ein zweiter Plugin-Aufruf). Dann NICHT
            # ersetzen — das atomare os.replace wuerde dessen Aenderung still verwerfen.
            if cfg.read_text(encoding="utf-8") != alt:
                eintrag["grund"] = "Datei wurde waehrend des Laufs geaendert — nicht geschrieben"
                ergebnisse.append(eintrag)
                continue
            backup = cfg.with_name(cfg.name + f".bak-{time.strftime('%Y%m%d')}-a2a-timeout")
            if not backup.exists():
                backup.write_bytes(cfg.read_bytes())
                os.chmod(backup, cfg.stat().st_mode & 0o7777)
            _write_text(cfg, neu)
            eintrag.update({"geschrieben": True, "backup": str(backup)})
        except LookupError as exc:
            eintrag["grund"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — nichts geschrieben, Grund zurueckgeben
            eintrag["grund"] = f"abgelehnt: {exc}"
        ergebnisse.append(eintrag)

    # Ein Teilausfall darf nicht wie Erfolg aussehen. `probleme` zaehlt die Profile, die NICHT
    # geschrieben wurden — "schon auf diesem Wert" ist dabei kein Problem, ein abgelehntes oder
    # uebersprungenes dagegen schon. Die Oberflaechen faerben danach ("Fehler"-Praefix).
    probleme = [e for e in ergebnisse if e.get("grund") and e["grund"] != "schon auf diesem Wert"]
    return {
        "ok": not probleme,
        "probleme": len(probleme),
        "aktion": "timeout-setzen",
        "sekunden": seconds,
        "ergebnisse": ergebnisse,
        "hinweis": "Ausgehende Rufe lesen die Config pro Aufruf — kein Neustart noetig.",
    }


# ------------------------------------------------------------------ Rufrechte (Checkboxen)
#
# "Darf mich rufen" ist kein eigenes Feld, sondern ein abgeleiteter Zustand, und er hat zwei Achsen:
#   * ERLAUBNIS: der Angerufene fuehrt in A2A_PEER_TOKENS die Token seiner Anrufer. Der Empfaenger
#     vergleicht den vorgelegten Wert — wer nicht in der Map steht, bekommt 401.
#   * KOENNEN: der Anrufer braucht einen Peer-Eintrag fuer das Ziel in SEINER config.yaml, sonst
#     kennt er die Adresse nicht (tools.py:_resolve_peer).
# Ein Haken setzt beides (sonst waere er nicht ausfuehrbar). Ein entferntes Haken nimmt NUR die
# Erlaubnis weg; der Peer-Eintrag des Anrufers bleibt, weil er timeout/capabilities tragen kann.
#
# Wirksamkeit: die Map friert der Empfaenger beim Adapterstart ein -> Erlaubnis aendert sich erst
# mit einem Neustart. Der Peer-Eintrag beim Anrufer wirkt sofort.

class PermissionRequest(Confirm):
    callee: str
    caller: str
    allowed: bool


def _permission_context() -> dict:
    """Einmal fuer alle Karten: ausgehende Peer-Listen und Token-Gegenwart je Identitaet."""
    ids = _identities()
    return {
        "names": [n for n, _ in ids],
        "peers_of": {n: [p["name"] for p in _peers(_read_config(h))] for n, h in ids},
        "token_ok": {n: bool(TOKEN_RE.match(_token_of(h))) for n, h in ids},
        "block_of": {n: _has_peer_block(h) for n, h in ids},
        # Erreichbarkeit endpunkt-basiert: ob der Anrufer IRGENDEINEN Eintrag auf meinen Port hat.
        # Namensgleichheit waere zu streng — das default-Profil heisst in den Listen 'bender'.
        "own_port": {n: _port(_read_config(h), _read_env(h))[0] for n, h in ids},
        "ports_of": {n: {_port_of_url(p["url"]) for p in _peers(_read_config(h))} for n, h in ids},
    }


def _rufrechte(callee: str, identity: dict, ctx: dict) -> list[dict]:
    """Je Partnerprofil: 'darf' (steht in meiner Map) und 'kann' (fuehrt mich als Peer).

    Beide Achsen getrennt ausweisen: ein Haken ohne 'kann' waere nicht ausfuehrbar, und ein
    fehlendes 'kann' bei gesetztem 'darf' erklaert, warum ein Ruf ausbleibt.
    """
    darf = set(identity.get("callers") or [])
    peers_of = ctx.get("peers_of") or {}
    token_ok = ctx.get("token_ok") or {}
    block_of = ctx.get("block_of") or {}
    own_port = ctx.get("own_port") or {}
    ports_of = ctx.get("ports_of") or {}
    mein_port = own_port.get(callee)
    return [
        {
            "caller": name,
            "darf": name in darf,
            "kann": mein_port is not None and mein_port in (ports_of.get(name) or set()),
            "kann_name": callee in (peers_of.get(name) or []),
            "caller_token": bool(token_ok.get(name)),
            "caller_block": bool(block_of.get(name, True)),
        }
        for name in (ctx.get("names") or [])
        if name != callee
    ]


def _peer_pairs(home: Path) -> dict[str, str]:
    """Map des Angerufenen als {Name: Token}. Der Sentinel ist kein Anrufer und faellt raus."""
    out: dict[str, str] = {}
    raw = _read_env(home).get("A2A_PEER_TOKENS") or ""
    for pair in raw.split(","):
        if ":" not in pair:
            continue
        name, _, token = pair.partition(":")
        name, token = name.strip(), token.strip()
        if name and token and name != SENTINEL_NAME:
            out[name] = token
    return out


def _sentinel_payload(home: Path) -> str:
    """Eine leere Map waere kein Deny-all, sondern wuerde den Listener auf localhost-only stellen —
    deshalb der Sentinel. Vorhandenen weiterverwenden, damit wiederholte Laeufe nichts aendern."""
    match = re.search(rf"{SENTINEL_NAME}:[0-9a-f]{{32,}}", _read_env(home).get("A2A_PEER_TOKENS", ""))
    return match.group(0) if match else f"{SENTINEL_NAME}:{_new_token()}"


def _check_peer_added(old_text: str, new_text: str, name: str) -> None:
    """Netz fuer den Peer-Eintrag: der neue Text muss parsen und sich NUR um diesen Peer
    unterscheiden — mit url und auth-Referenz, sonst nichts."""
    import yaml

    alt = yaml.safe_load(old_text) or {}
    neu = yaml.safe_load(new_text) or {}
    alt_agents = alt.get("a2a_agents") or {}
    neu_agents = neu.get("a2a_agents") or {}
    if name in alt_agents:
        raise ValueError(f"'{name}' war schon vorhanden")
    entry = neu_agents.pop(name, None)
    if not isinstance(entry, dict) or not entry.get("url"):
        raise ValueError(f"neuer Eintrag '{name}' hat keine url")
    auth = entry.get("auth") or {}
    if auth.get("type") != "bearer" or auth.get("token") != "${A2A_SELF_TOKEN}":
        raise ValueError(f"neuer Eintrag '{name}' hat keinen gueltigen auth-Block")
    alt_norm = dict(alt, a2a_agents=alt_agents)
    neu_norm = dict(neu, a2a_agents=neu_agents)
    if alt_norm != neu_norm:
        raise ValueError("ausserhalb des neuen Peer-Eintrags wuerde sich die Config aendern")


def _port_of_url(url: str) -> int | None:
    """Port aus einer Peer-URL ziehen (Toleranz gegen Schreibweisen wie http://localhost:9902/x)."""
    match = re.search(r":(\d{2,5})(?:/|$)", url or "")
    return int(match.group(1)) if match else None


def _has_peer_block(home: Path) -> bool:
    try:
        return any(
            line.startswith("a2a_agents:")
            for line in (home / "config.yaml").read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return False


def _preferred_name(callee: str, callee_home: Path) -> str | None:
    """Name, unter dem der Agent sich selbst meldet (Agent Card, kleingeschrieben) — oder None,
    wenn die Karte nicht erreichbar ist. Dann wird **nicht** geraten und nichts umbenannt."""
    port, _ = _port(_read_config(callee_home), _read_env(callee_home))
    karte = _card(port)
    if not karte.get("ok"):
        return None
    name = re.sub(r"[^a-z0-9_.-]+", "", (karte.get("name") or "").strip().lower())
    return name or None


def _peer_name_for(callee: str, callee_home: Path) -> str:
    """Name fuer neue Peer-Eintraege: Kartenname, sonst der Profilname.

    Sonst entstehen zwei Namen fuer denselben Endpunkt (`bender` und `default`), genau wie am
    05-10-2026 passiert.
    """
    return _preferred_name(callee, callee_home) or callee


# ------------------------------------------------------------------ Aufraeumen: Dubletten
#
# Zwei Namen fuer denselben Endpunkt (z.B. `bender` und `default` fuer Port 9902) sind funktional
# harmlos, verwirren aber Matrix, `a2a_list` und jede Namenszuordnung. Regel dieser Route:
# **je Port ein Eintrag.** Bleibt genau einer uebrig und meldet der Agent sich unter anderem Namen,
# wird er umbenannt — aber nur, wenn die Agent Card erreichbar ist (sonst kein Raten, kein Schreiben).

class CleanupRequest(Confirm):
    profile: str | None = None


def _peer_entry_span(lines: list[str], name: str) -> tuple[int, int] | None:
    """Zeilenspanne des Peer-Eintrags `name` im a2a_agents-Block (Start inkl., Ende exkl.)."""
    start = next((i for i, line in enumerate(lines) if line.startswith("a2a_agents:")), None)
    if start is None:
        return None
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    while end > start + 1 and not lines[end - 1].strip():
        end -= 1
    i = start + 1
    while i < end:
        if lines[i].startswith(f"  {name}:"):
            j = i + 1
            while j < end and (lines[j].startswith((" ", "\t")) or not lines[j].strip()):
                j += 1
            while j > i + 1 and not lines[j - 1].strip():
                j -= 1
            return i, j
        i += 1
    return None


def _check_peers_edited(old_text: str, new_text: str, removed: list[str], renamed: list[tuple[str, str]]) -> None:
    """Netz: der neue Text muss parsen und sich NUR um die entfernten/umbenannten Eintraege
    unterscheiden."""
    import yaml

    alt = yaml.safe_load(old_text) or {}
    neu = yaml.safe_load(new_text) or {}
    vorher = dict(alt.get("a2a_agents") or {})
    nachher = dict(neu.get("a2a_agents") or {})
    for name in removed:
        vorher.pop(name, None)
    for alt_name, neu_name in renamed:
        if alt_name in vorher:
            if neu_name in vorher:
                raise ValueError(f"Zielname '{neu_name}' existiert schon")
            vorher[neu_name] = vorher.pop(alt_name)
    if vorher != nachher:
        raise ValueError("ausserhalb der Dubletten wuerde sich die Config aendern")
    if dict(alt, a2a_agents=vorher) != dict(neu, a2a_agents=nachher):
        raise ValueError("weitere Unterschiede ausserhalb von a2a_agents")


@router.post("/cleanup/duplicates")
def cleanup_duplicates(req: CleanupRequest) -> dict:
    """Je Port genau einen Peer-Eintrag, unter dem Namen des Agenten — mit Sicherung und Netz."""
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")

    ids = {n: h for n, h in _identities()}
    targets = [req.profile] if req.profile else list(ids)
    for target in targets:
        if target not in ids:
            _home_of(target)  # 404

    bevorzugt: dict[int, str | None] = {}
    for name, home in ids.items():
        port, _ = _port(_read_config(home), _read_env(home))
        bevorzugt[port] = _preferred_name(name, home)

    ergebnisse = []
    for name in targets:
        cfg_path = ids[name] / "config.yaml"
        eintrag: dict = {"profil": name, "geschrieben": False, "entfernt": [], "umbenannt": [], "backup": None}
        try:
            if cfg_path.is_symlink() or not cfg_path.is_file() or not _inside(cfg_path, ROOT):
                raise LookupError("config.yaml fehlt, ist ein Symlink oder liegt ausserhalb der Wurzel")

            je_port: dict[int, list[str]] = {}
            for peer in _peers(_read_config(ids[name])):
                port = _port_of_url(peer["url"] or "")
                if port is not None:
                    je_port.setdefault(port, []).append(peer["name"])

            entfernen: list[str] = []
            umbenennen: list[tuple[str, str]] = []
            ziel_unbekannt: list[int] = []
            for port, namen in je_port.items():
                ziel = bevorzugt.get(port)
                if ziel is None:
                    ziel_unbekannt.append(port)
                if ziel and ziel in namen:
                    behalten = ziel
                else:
                    behalten = namen[0]
                    if len(namen) == 1 and ziel:
                        umbenennen.append((behalten, ziel))
                for vorhanden in namen:
                    if vorhanden != behalten:
                        entfernen.append(vorhanden)

            if not entfernen and not umbenennen:
                eintrag["grund"] = "keine Dublette" + (
                    f" (Agent Card nicht erreichbar fuer Port {ziel_unbekannt[0]}, nichts geraten)" if ziel_unbekannt else ""
                )
                ergebnisse.append(eintrag)
                continue

            alt = cfg_path.read_text(encoding="utf-8")
            neu_text = alt
            # Loeschen ueber `_peer_spans` (Eintragsende = naechste Kopfzeile) statt ueber
            # `_peer_entry_span`: dessen Ende ist das BLOCKende und haette die Folge-Eintraege
            # mitgerissen (belegt 07-10-2026: span geordi 59..93 statt 59..67 im conrad-Profil).
            neu_text = _drop_peer_entries(neu_text, entfernen)
            for alt_name, neu_name in umbenennen:
                zeilen = neu_text.splitlines(keepends=True)
                span = _peer_entry_span(zeilen, alt_name)
                if span is None:
                    raise ValueError(f"Eintrag '{alt_name}' nicht gefunden")
                zeilen[span[0]] = f"  {neu_name}:\n"
                neu_text = "".join(zeilen)

            _check_peers_edited(alt, neu_text, entfernen, umbenennen)
            backup = cfg_path.with_name(cfg_path.name + f".bak-{time.strftime('%Y%m%d')}-a2a-dedup")
            if not backup.exists():
                backup.write_bytes(cfg_path.read_bytes())
                os.chmod(backup, cfg_path.stat().st_mode & 0o7777)
            _write_text(cfg_path, neu_text)
            eintrag.update(
                {
                    "geschrieben": True,
                    "entfernt": entfernen,
                    "umbenannt": [f"{a} -> {b}" for a, b in umbenennen],
                    "backup": str(backup),
                }
            )
        except LookupError as exc:
            eintrag["grund"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — nichts geschrieben, Grund zurueckgeben
            eintrag["grund"] = f"abgelehnt: {exc}"
        ergebnisse.append(eintrag)

    return {
        "ok": True,
        "aktion": "dubletten-entfernen",
        "ergebnisse": ergebnisse,
        "hinweis": "Je Endpunkt bleibt ein Eintrag; Namen kommen jetzt aus der Agent Card.",
    }


def _ensure_peer_entry(home: Path, callee: str, caller_name: str = "") -> dict:
    """Traegt den Peer <callee> in die a2a_agents-Map des Anrufers ein, falls er fehlt.

    Textbasiert (Kommentare bleiben), Referenz statt Wert, Sicherung, atomar. Fehlt der
    a2a_agents-Block komplett (z.B. im Home des default-Profils, das noch nie Peers hatte), wird
    er **angelegt** — aber nur, weil der Anwender genau das ausgeloest hat, mit Kommentar und
    sonst unveraendert; das Netz prueft, dass nichts anderes passiert.
    """
    label = caller_name or home.name
    cfg_path = home / "config.yaml"
    if cfg_path.is_symlink():
        raise HTTPException(status_code=400, detail=f"Symlink abgelehnt: {cfg_path}")
    if not cfg_path.is_file():
        raise HTTPException(status_code=400, detail=f"Keine config.yaml in {label}")
    if not _inside(cfg_path, ROOT):
        raise HTTPException(status_code=400, detail="Ziel liegt ausserhalb der Hermes-Wurzel")

    callee_home = _home_of(callee)
    port, _ = _port(_read_config(callee_home), _read_env(callee_home))
    vorhandene = _peers(_read_config(home))
    entry_name = _peer_name_for(callee, callee_home)
    if entry_name in [p["name"] for p in vorhandene]:
        return {"schritt": f"Peer-Eintrag '{entry_name}' war schon vorhanden", "geaendert": False, "backup": None}
    # Denselben Endpunkt schon unter anderem Namen? Ein zweiter Eintrag fuer denselben Port waere
    # eine Dublette ohne Nutzen — auch wenn sie anders heisst.
    for vorhanden in vorhandene:
        if _port_of_url(vorhanden.get("url") or "") == port:
            return {
                "schritt": f"schon erreichbar ueber '{vorhanden['name']}' (Port {port}) — kein zweiter Eintrag",
                "geaendert": False,
                "backup": None,
            }

    text = cfg_path.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    block = (
        f"  {entry_name}:\n"
        f"    url: http://127.0.0.1:{port}\n"
        f"    auth:\n      type: bearer\n      token: ${{A2A_SELF_TOKEN}}\n"
    )

    if any(line.startswith("a2a_agents:") for line in lines):
        start = next(i for i, line in enumerate(lines) if line.startswith("a2a_agents:"))
        end = start + 1
        while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
            end += 1
        while end > start + 1 and not lines[end - 1].strip():
            end -= 1
        new_text = "".join(lines[:end]) + block + "".join(lines[end:])
        angelegt = False
    else:
        prefix = text
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        new_text = prefix + "a2a_agents:  # vom A2A-Admin-Plugin angelegt (Ziel: Rufrecht)\n" + block
        angelegt = True

    _check_peer_added(text, new_text, callee)
    backup = cfg_path.with_name(cfg_path.name + f".bak-{time.strftime('%Y%m%d')}-a2a")
    if not backup.exists():
        backup.write_bytes(cfg_path.read_bytes())
        os.chmod(backup, cfg_path.stat().st_mode & 0o7777)
    _write_text(cfg_path, new_text)
    schritt = (
        f"a2a_agents-Block in {label}/config.yaml angelegt + Peer-Eintrag '{callee}'"
        if angelegt
        else f"Peer-Eintrag '{callee}' in {label}/config.yaml ergaenzt"
    )
    return {"schritt": schritt, "geaendert": True, "backup": str(backup)}


@router.post("/peers/toggle")
def permission_toggle(req: PermissionRequest) -> dict:
    if not req.confirm:
        raise HTTPException(status_code=400, detail="Bestaetigung fehlt (confirm=true)")
    if req.callee == req.caller:
        raise HTTPException(status_code=400, detail="Ein Profil kann sich nicht selbst anrufen")
    callee_home = _home_of(req.callee)
    caller_home = _home_of(req.caller)

    schritte: list[dict] = []
    achtung: list[str] = []
    paare = _peer_pairs(callee_home)

    if req.allowed:
        # Erst die Voraussetzung schreiben (Peer-Eintrag beim Anrufer): scheitert sie, darf noch
        # nichts geschehen sein — sonst bliebe ein frisches Token ohne Erlaubnis zurueck.
        schritte.append(_ensure_peer_entry(caller_home, req.callee, req.caller))
        caller_token = _token_of(caller_home)
        if not TOKEN_RE.match(caller_token):
            res = _write_env(caller_home, {"A2A_SELF_TOKEN": _new_token()})
            caller_token = _token_of(caller_home)
            schritte.append(
                {"schritt": f"eigenes Token fuer '{req.caller}' erzeugt", "geaendert": res["geaendert"], "backup": res["backup"]}
            )
        if not TOKEN_RE.match(caller_token):
            raise HTTPException(status_code=400, detail=f"'{req.caller}' hat kein gueltiges Token — Erlaubnis nicht moeglich")
        paare[req.caller] = caller_token
        achtung.append("Erlaubnis wirkt erst nach dem naechsten Neustart: der Empfaenger friert seine Anrufer-Map beim Adapterstart ein.")
    else:
        if req.caller not in paare:
            return {
                "ok": True,
                "aktion": "rufrecht-setzen",
                "ergebnis": {"callee": req.callee, "caller": req.caller, "darf": False},
                "schritte": schritte,
                "achtung": ["War schon gesperrt — nichts geaendert."],
            }
        paare.pop(req.caller)
        achtung.append("Der Peer-Eintrag im Anrufer-Profil bleibt bestehen (er gehoert dem Anrufer); der Ruf wird trotzdem abgewiesen.")
        achtung.append("Wirkt erst nach dem naechsten Neustart: der Empfaenger friert seine Anrufer-Map beim Adapterstart ein.")

    payload = ",".join(f"{name}:{token}" for name, token in sorted(paare.items())) if paare else _sentinel_payload(callee_home)
    res = _write_env(callee_home, {"A2A_PEER_TOKENS": payload})
    schritte.append(
        {
            "schritt": f"Erlaubnis-Map in {req.callee}/.env",
            "geaendert": res["geaendert"],
            "unveraendert": res["unveraendert"],
            "backup": res["backup"],
        }
    )
    return {
        "ok": True,
        "aktion": "rufrecht-setzen",
        "ergebnis": {"callee": req.callee, "caller": req.caller, "darf": req.allowed, "anrufer_jetzt": sorted(paare)},
        "schritte": schritte,
        "achtung": achtung,
    }
