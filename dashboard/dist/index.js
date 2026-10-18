/**
 * hermes-a2a-admin — Web-Dashboard-Oberflaeche (Stufe 3).
 *
 * Liest /api/plugins/hermes-a2a-admin/state (siehe ../plugin_api.py) und zeigt die
 * A2A-Matrix aller Identitaeten. Schreibende Aktionen laufen ueber denselben Namespace und
 * verlangen eine Bestaetigung im Browser; das Ergebnis wird als Zeile angezeigt — **ohne
 * Geheimniswerte**:
 *   /token/generate, /token/rotate, /map/sync   (Stufe 2: Token und Anrufer-Maps)
 *   /config/auth                                (Stufe 3: auth-Referenz je Peer-Eintrag)
 *
 * Reines IIFE-Bundle, kein Build: window.__HERMES_PLUGIN_SDK__ liefert React,
 * Hooks, Komponenten und fetchJSON. Keine hardcodierten Farben — nur
 * Theme-Variablen und die Klassen der Dashboard-Designsprache.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) {
    console.error("[hermes-a2a-admin] Plugin-SDK nicht gefunden");
    return;
  }

  var React = SDK.React;
  var h = React.createElement;
  var C = SDK.components || {};

  var PLUGIN_ID = "hermes-a2a-admin";
  var BASE = "/api/plugins/" + PLUGIN_ID;

  var TONE = {
    aktiv: { className: "text-emerald-500", label: "aktiv" },
    "neustart-vermutet": { className: "text-amber-500", label: "Neustart vermutet" },
    "neustart-noetig": { className: "text-amber-500", label: "Neustart noetig" },
    "ohne-identitaet": { className: "text-amber-600", label: "ohne Identitaet" },
    stumm: { className: "text-destructive", label: "stumm" }
  };

  function fmtTime(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString("de-DE", { dateStyle: "short", timeStyle: "short" });
    } catch (err) {
      return iso;
    }
  }

  // Jede schreibende Route dieses Plugins verlangt "confirm": true. Das wird HIER erzwungen und
  // nicht an den Aufrufstellen — genau dort wurde es einmal vergessen (Rufrecht-Knopf) und der
  // Aufruf scheiterte still mit 400, waehrend die Checkbox unveraendert blieb.
  function postJSON(path, body) {
    return SDK.fetchJSON(BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ confirm: true }, body || {}))
    });
  }

  function errText(err) {
    if (!err) return "unbekannter Fehler";
    return err.message ? err.message : String(err);
  }

  /** Antwort eines Schreibaufrufs in eine Zeile uebersetzen — ohne Werte. */
  function describe(res) {
    if (!res) return "keine Antwort";

    // Stufe 3: auth-Referenzen in den Peer-Eintraegen der config.yaml
    if (res.aktion === "ausweise-eintragen") {
      var aus = res.ergebnisse || [];
      var geschrieben = aus.filter(function (e) { return e.geschrieben; });
      var komplett = aus.filter(function (e) { return !e.geschrieben && e.grund === "schon vollstaendig"; });
      var abgelehnt = aus.filter(function (e) { return e.grund && e.grund !== "schon vollstaendig"; });
      var teileAuth = [
        "geschrieben: " + (geschrieben.length
          ? geschrieben.map(function (e) { return e.profil + " (+" + e.auth_neu + " von " + (e.peers || []).length + ")"; }).join(", ")
          : "nichts")
      ];
      if (komplett.length) teileAuth.push("schon vollstaendig: " + komplett.map(function (e) { return e.profil; }).join(", "));
      if (abgelehnt.length) teileAuth.push("nicht angefasst: " + abgelehnt.map(function (e) { return e.profil + " (" + e.grund + ")"; }).join("; "));
      if (geschrieben.length && geschrieben[0].backup) teileAuth.push("Sicherung: " + geschrieben[0].backup.split("/").pop());
      return "Ausweise · " + teileAuth.join(" · ");
    }

    // Rufrecht umschalten: Erlauben setzt Peer-Eintrag (Anrufer) UND Map-Eintrag (Angerufener),
    // Sperren nimmt nur die Erlaubnis weg. Wirksam erst nach einem Neustart.
    if (res.aktion === "rufrecht-setzen") {
      var e = res.ergebnis || {};
      var teileRecht = [e.caller + (e.darf ? " darf " : " darf nicht mehr ") + e.callee];
      var getan = (res.schritte || []).filter(function (s) { return s.geaendert; }).map(function (s) { return s.schritt; });
      if (getan.length) teileRecht.push(getan.join(" · "));
      if (e.anrufer_jetzt) teileRecht.push("Anrufer jetzt: " + (e.anrufer_jetzt.length ? e.anrufer_jetzt.join(", ") : "keine"));
      if (res.achtung && res.achtung.length) teileRecht.push(res.achtung[0]);
      return "Rufrecht · " + teileRecht.join(" · ");
    }

    if (res.ergebnisse) {
      var neu = (res.token_neu_erzeugt || []).join(", ") || "keine";
      var geschrieben2 = res.ergebnisse.filter(function (e) { return e.geschrieben; }).map(function (e) { return e.profil; });
      var fehler = res.ergebnisse.filter(function (e) { return e.grund; });
      var text = "Maps abgeglichen · Token neu erzeugt fuer: " + neu + " · geschrieben: " + (geschrieben2.join(", ") || "nichts (war schon korrekt)");
      if (fehler.length) {
        text += " · nicht geschrieben: " + fehler.map(function (e) { return e.profil + " (" + e.grund + ")"; }).join("; ");
      }
      return text;
    }
    if (res.ok === false) return res.profil + ": " + (res.grund || "nichts geaendert");
    var teile = [res.profil + " · " + res.aktion];
    teile.push("geaendert: " + ((res.geaendert || []).join(", ") || "nichts"));
    if (res.backup) teile.push("Sicherung: " + res.backup.split("/").pop());
    if (res.achtung && res.achtung.length) teile.push("Achtung: " + res.achtung.join(" "));
    return teile.join(" · ");
  }

  /** Effektiver ausgehender Timeout eines Peers — "(Standard)" markiert die stillen 120 s. */
  function timeoutText(peerByName, name) {
    var e = peerByName[name];
    if (!e) return "";
    return e.timeout_explicit ? " · " + e.timeout + " s" : " · " + e.timeout_effective + " s (Standard)";
  }

  /** Antwort von /timeout/set in eine Zeile uebersetzen. Bewusst NICHT ueber describe():
   *  dessen Zweig fuer "res.ergebnisse" ist auf /map/sync zugeschnitten und wuerde hier luegen. */
  function describeTimeout(res) {
    if (!res) return "keine Antwort";
    var aus = res.ergebnisse || [];
    var geschrieben = aus.filter(function (e) { return e.geschrieben; });
    var ersetzt = geschrieben.reduce(function (n, e) { return n + (e.ersetzt || 0); }, 0);
    var ergaenzt = geschrieben.reduce(function (n, e) { return n + (e.ergaenzt || 0); }, 0);
    var skip = aus.filter(function (e) { return !e.geschrieben; });
    var teile = [
      res.sekunden + " s",
      "geschrieben: " + (geschrieben.length ? geschrieben.map(function (e) { return e.profil; }).join(", ") : "nichts"),
      "Felder: " + ersetzt + " ersetzt, " + ergaenzt + " ergaenzt"
    ];
    if (skip.length) teile.push("nicht angefasst: " + skip.map(function (e) { return e.profil + " (" + e.grund + ")"; }).join("; "));
    if (geschrieben.length && geschrieben[0].backup) teile.push("Sicherung: " + geschrieben[0].backup.split("/").pop());
    // Teilausfall sichtbar machen: die Oberflaeche faerbt am "Fehler"-Praefix.
    return (res.probleme ? "Fehler: " : "") + "Timeout · " + teile.join(" · ");
  }

  function Row(props) {
    return h(
      "div",
      { className: "flex items-baseline justify-between gap-3 py-0.5" },
      h("span", { className: "text-muted-foreground" }, props.label),
      h("span", { className: "font-mono text-xs" }, props.value)
    );
  }

  function ProfileCard(props) {
    var p = props.data;
    var act = props.action;
    var onToggle = props.onToggle;
    var tone = TONE[p.state] || { className: "", label: p.state };
    var id = p.identity || {};
    var card = p.card || {};
    var audit = p.audit || { tail: [] };
    var ohneAuth = (p.peers || []).filter(function (x) { return !x.auth; });
    var rechte = p.rufrechte || [];
    // Gegenrichtung: darf ICH die anderen rufen? Diese Erlaubnis steht in der Map des ANDEREN,
    // wird also aus dessen Karte gelesen — dort bin ich der Anrufer. Damit ist in jeder Karte
    // beides sichtbar und setzbar: "darf mich rufen" und "darf ich rufen".
    var auswaerts = (props.allProfiles || [])
      .filter(function (x) { return x.profile !== p.profile; })
      .map(function (other) {
        var zeile = ((other.rufrechte || []).filter(function (z) { return z.caller === p.profile; })[0]) || {};
        return {
          name: other.profile,
          darf: !!zeile.darf,
          kann: zeile.kann !== false,
          token: zeile.caller_token !== false,
          block: zeile.caller_block !== false
        };
      });

    // Ausgehender Timeout steht in der EIGENEN config.yaml (a2a_agents.<peer>.timeout); fehlt der
    // Schluessel, gilt 120 — "(Standard)" zeigt das, statt den Wert zu verschweigen.
    var peerByName = {};
    (p.peers || []).forEach(function (pe) { peerByName[pe.name] = pe; });

    return h(
      "div",
      { className: "rounded-md border border-border p-3 space-y-1" },
      h(
        "div",
        { className: "flex items-center justify-between gap-2" },
        h("div", { className: "font-medium" }, p.profile),
        h("div", { className: "flex items-center gap-2 text-xs " + tone.className }, "● " + tone.label)
      ),
      h(Row, { label: "Port", value: String(p.port) + " (" + p.port_source + ")" }),
      h(Row, { label: "Adressbuch geschrieben", value: p.env_written || "—" }),
      h(Row, { label: "Adapter gestartet", value: p.adapter_started || "—" }),
      h(Row, {
        label: "Agent Card",
        value: card.ok ? (card.name || "ohne Namen") + " · " + card.skills + " Skills" : "keine — " + (card.error || "ohne Antwort")
      }),
      h(Row, { label: "Authentifizierung beworben", value: card.security_schemes ? "ja" : "nein" }),
      h(Row, {
        label: "Eigenes Token",
        value: id.self_token ? (id.self_token_format ? "gesetzt" : "gesetzt (Format ungewoehnlich)") : "nicht gesetzt"
      }),
      h(Row, {
        label: "Darf mich rufen",
        value: id.caller_count ? id.caller_count + " (" + id.callers.join(", ") + ")" : "niemand"
      }),
      id.shared_bearer
        ? h(Row, { label: "Geteiltes Bearer-Token", value: "gesetzt — Identitaet waere nur die IP" })
        : null,
      id.trusted_peers && id.trusted_peers.length
        ? h(Row, { label: "Trusted Peers", value: id.trusted_peers.join(", ") })
        : null,
      h(Row, {
        label: "Peers in der Config",
        value: p.peers && p.peers.length ? p.peers.map(function (x) { return x.name + (x.auth ? "" : " (ohne auth)"); }).join(", ") : "keine"
      }),
      ohneAuth.length
        ? h(
            "div",
            { className: "text-xs text-amber-600" },
            "Ohne auth: " + ohneAuth.map(function (x) { return x.name; }).join(", ") + " — ausgehende Rufe dorthin scheitern mit 401."
          )
        : null,
      rechte.length
        ? h(
            "div",
            { className: "pt-1 space-y-0.5" },
            h("div", { className: "text-xs text-muted-foreground" }, "Darf mich rufen"),
            rechte.map(function (r) {
              return h(
                "label",
                {
                  key: r.caller,
                  className: "flex items-center gap-2 text-xs cursor-pointer",
                  title: r.kann
                    ? r.caller + " kennt meine Adresse (Peer-Eintrag vorhanden)"
                    : r.caller + " fuehrt mich nicht als Peer — beim Erlauben wird der Eintrag ergaenzt"
                },
                h("input", {
                  type: "checkbox",
                  checked: !!r.darf,
                  onChange: function () {
                    if (onToggle) onToggle(p.profile, r, !r.darf);
                  }
                }),
                h(
                  "span",
                  { className: r.kann || r.caller_block === false ? "text-amber-600" : "" },
                  r.caller +
                    (r.kann ? "" : " (kennt mich nicht)") +
                    (r.caller_block === false ? " (kein a2a_agents-Block — wird beim Erlauben angelegt)" : "") +
                    (r.caller_token ? "" : " (ohne Token — wird beim Erlauben erzeugt)")
                )
              );
            })
          )
        : null,
      auswaerts.length
        ? h(
            "div",
            { className: "pt-1 space-y-0.5" },
            h("div", { className: "text-xs text-muted-foreground" }, "Darf ICH rufen — ich bin der Anrufer"),
            auswaerts.map(function (r) {
              return h(
                "label",
                {
                  key: r.name,
                  className: "flex items-center gap-2 text-xs cursor-pointer",
                  title: "Die Erlaubnis steht in der Karte von " + r.name + " (dort bin ich der Anrufer)"
                },
                h("input", {
                  type: "checkbox",
                  checked: r.darf,
                  onChange: function () {
                    if (onToggle) onToggle(r.name, { caller: p.profile, darf: r.darf }, !r.darf);
                  }
                }),
                h(
                  "span",
                  { className: r.kann && r.token && r.block ? "" : "text-amber-600" },
                  r.name +
                    (r.kann ? "" : " (kennt mich nicht)") +
                    (r.block ? "" : " (kein a2a_agents-Block)") +
                    (r.token ? "" : " (ohne Token)") +
                    timeoutText(peerByName, r.name)
                )
              );
            })
          )
        : null,
      h(Row, { label: "Audit-Eintraege", value: String((audit && audit.entries) || 0) }),
      audit && audit.tail && audit.tail.length
        ? h(
            "div",
            { className: "text-xs text-muted-foreground pt-1 space-y-0.5" },
            audit.tail.slice().reverse().map(function (e, i) {
              return h("div", { key: i }, fmtTime(e.zeit) + " · " + (e.richtung || "?") + " · " + (e.peer || "?"));
            })
          )
        : null,
      h("div", { className: "text-xs text-muted-foreground pt-1" }, p.state_reason),
      act
        ? h(
            "div",
            { className: "flex flex-wrap gap-2 pt-2" },
            !id.self_token && C.Button
              ? h(
                  C.Button,
                  {
                    variant: "outline",
                    onClick: function () {
                      act(
                        "/token/generate",
                        { profile: p.profile },
                        "Token fuer '" + p.profile + "' erzeugen?\n\nGeschrieben wird A2A_SELF_TOKEN in " + p.home + "/.env. " +
                          "Vorher legt das Plugin eine Sicherung an. Wirksam wird es nach dem naechsten Neustart von Gateway/Dienst."
                      );
                    }
                  },
                  "Token erzeugen"
                )
              : null,
            id.self_token && C.Button
              ? h(
                  C.Button,
                  {
                    variant: "outline",
                    onClick: function () {
                      act(
                        "/token/rotate",
                        { profile: p.profile },
                        "Token von '" + p.profile + "' ersetzen?\n\nDas alte Token wird ungueltig — alle Maps, die es fuehren, " +
                          "muessen danach abgeglichen werden. Sicherung wird angelegt."
                      );
                    }
                  },
                  "Token rotieren"
                )
              : null
          )
        : null
    );
  }

  function A2aPage() {
    var stateHook = React.useState({ loading: true, data: null, error: null });
    var st = stateHook[0];
    var setSt = stateHook[1];
    var msgHook = React.useState(null);
    var msg = msgHook[0];
    var setMsg = msgHook[1];

    function load() {
      SDK.fetchJSON(BASE + "/state")
        .then(function (data) {
          setSt({ loading: false, data: data, error: null });
        })
        .catch(function (err) {
          setSt({ loading: false, data: null, error: errText(err) });
        });
    }

    function act(path, body, frage) {
      if (!window.confirm(frage)) return;
      setMsg("laeuft ...");
      postJSON(path, body)
        .then(function (res) {
          setMsg(describe(res));
          load();
        })
        .catch(function (err) {
          setMsg("Fehler: " + errText(err));
        });
    }

    function syncAll() {
      act(
        "/map/sync",
        {},
        "Anrufer-Maps aller Profile abgleichen?\n\nAbgeleitet aus den a2a_agents-Listen (rollengetreu). Fehlende eigene Token " +
          "werden dabei erzeugt; jede geaenderte .env bekommt vorher eine Sicherung. Wirksam nach dem naechsten Neustart."
      );
    }

    /** Globaler Regler: setzt den AUSGEHENDEN Peer-Timeout in allen Profilen auf denselben Wert. */
    function applyTimeout(seconds, anzahl) {
      var frage =
        "Ausgehenden A2A-Timeout auf " + seconds + " s setzen?\n\n" +
        "Betrifft alle Profile und alle " + anzahl + " Peer-Eintraege (a2a_agents.<peer>.timeout). " +
        "Jede geaenderte config.yaml bekommt vorher eine Sicherung. Ausgehende Rufe lesen die Config " +
        "pro Aufruf — kein Neustart noetig.";
      if (!window.confirm(frage)) return;
      setMsgTimeout("laeuft ...");
      postJSON("/timeout/set", { seconds: seconds })
        .then(function (res) {
          setMsgTimeout(describeTimeout(res));
          setValTimeout(null);
          load();
        })
        .catch(function (err) {
          setMsgTimeout("Fehler: " + errText(err));
        });
    }

    /** Stufe 3: erst Vorschau holen (schreibt nichts), dann mit den Zahlen bestaetigen lassen. */
    function authAll() {
      setMsg("Vorschau ...");
      SDK.fetchJSON(BASE + "/config/auth")
        .then(function (plan) {
          var offen = (plan.ergebnisse || []).filter(function (e) { return e.schreibbar && e.auth_neu > 0; });
          var liste = offen.map(function (e) { return e.profil + " (+" + e.auth_neu + " von " + (e.peers || []).length + ")"; });
          if (!liste.length) {
            setMsg(describe({ aktion: "ausweise-eintragen", ergebnisse: plan.ergebnisse }));
            return;
          }
          var frage =
            "Ausweise in die Peer-Eintraege schreiben?\n\nBetroffen: " + liste.join(", ") +
            "\n\nGeschrieben wird die Referenz ${A2A_SELF_TOKEN}, kein Tokenwert. Jede geaenderte config.yaml bekommt vorher " +
            "eine Sicherung. Ausgehende Rufe wirken sofort (kein Neustart noetig).";
          if (!window.confirm(frage)) return;
          setMsg("laeuft ...");
          postJSON("/config/auth", {})
            .then(function (res) {
              setMsg(describe(res));
              load();
            })
            .catch(function (err) {
              setMsg("Fehler: " + errText(err));
            });
        })
        .catch(function (err) {
          setMsg("Vorschau fehlgeschlagen: " + errText(err));
        });
    }

    /** Rufrecht umschalten (Checkbox). Erlauben schreibt an zwei Stellen, Sperren nur an einer. */
    function toggleRecht(callee, r, allowed) {
      var frage =
        "Rufrecht aendern?\n\n" +
        (allowed
          ? r.caller + " DARF " + callee + " kuenftig rufen. Dazu wird (falls noetig) ein Peer-Eintrag in " +
            r.caller + "/config.yaml ergaenzt und die Erlaubnis in " + callee + "/.env eingetragen."
          : r.caller + " darf " + callee + " NICHT mehr rufen. Es wird nur die Erlaubnis aus " + callee +
            "/.env entfernt; der Peer-Eintrag in " + r.caller + "/config.yaml bleibt bestehen (er gehoert dem Anrufer).") +
        "\n\nVorher wird gesichert. Wirksam nach dem naechsten Neustart von Gateway bzw. Dienst.";
      if (!window.confirm(frage)) return;
      setMsg("laeuft ...");
      postJSON("/peers/toggle", { callee: callee, caller: r.caller, allowed: allowed })
        .then(function (res) {
          setMsg(describe(res));
          load();
        })
        .catch(function (err) {
          setMsg("Fehler: " + errText(err));
        });
    }

    React.useEffect(function () {
      load();
      var timer = setInterval(load, 10000);
      return function () {
        clearInterval(timer);
      };
    }, []);

    var profiles = (st.data && st.data.profiles) || [];
    // Globaler Regler fuer den AUSGEHENDEN Peer-Timeout. null = noch nicht angefasst -> der Regler
    // zeigt den Serverzustand; nach dem Setzen wird wieder auf null gestellt.
    var tWertHook = React.useState(null);
    var valTimeout = tWertHook[0];
    var setValTimeout = tWertHook[1];
    var tMsgHook = React.useState(null);
    var msgTimeout = tMsgHook[0];
    var setMsgTimeout = tMsgHook[1];
    var tSummary = (st.data && st.data.timeout) || null;
    var tBereich = (tSummary && tSummary.bereich) || [30, 600];
    // Angefasst schlaegt Serverzustand: sonst zeigt der Regler, was das Backend meldet (gemeinsam
    // oder der kleinste vorkommende Wert).
    var tWert = valTimeout != null
      ? valTimeout
      : (tSummary && tSummary.gemeinsam) || (tSummary && tSummary.werte && tSummary.werte[0]) || 180;

    return h(
      "div",
      { className: "p-4 space-y-3 overflow-auto" },
      h(
        "div",
        { className: "flex flex-wrap items-center gap-3" },
        h("div", { className: "text-sm font-medium" }, "A2A-Identitaet und Peers"),
        h("div", { className: "text-xs text-muted-foreground" }, "Stufe 3 · Schreiben nur nach Bestaetigung · Aktualisierung alle 10 s"),
        C.Button ? h(C.Button, { variant: "outline", onClick: load }, "Neu laden") : null,
        C.Button ? h(C.Button, { onClick: syncAll }, "Maps abgleichen") : null,
        C.Button ? h(C.Button, { variant: "outline", onClick: authAll }, "Ausweise eintragen") : null
      ),
      tSummary
        ? h(
            "div",
            { className: "space-y-1 text-xs" },
            h(
              "div",
              { className: "flex items-center gap-2" },
              h("span", { className: "text-muted-foreground shrink-0" }, "A2A-Timeout (ausgehend)"),
              h("input", {
                type: "range",
                min: tBereich[0],
                max: tBereich[1],
                step: tSummary.schritt,
                value: tWert,
                className: "flex-1",
                onChange: function (e) {
                  setValTimeout(Number(e.target.value));
                }
              }),
              h("span", { className: "font-mono tabular-nums w-14 text-right" }, tWert + " s"),
              C.Button
                ? h(
                    C.Button,
                    {
                      variant: "outline",
                      onClick: function () {
                        applyTimeout(tWert, tSummary.peers);
                      }
                    },
                    "setzen"
                  )
                : null
            ),
            h(
              "div",
              { className: "text-muted-foreground" },
              (tSummary.gemeinsam
                ? tSummary.peers + " Peer-Eintraege · einheitlich " + tSummary.gemeinsam + " s"
                : tSummary.peers + " Peer-Eintraege · uneinheitlich (" + tSummary.werte.join(", ") + " s)") +
                " · " +
                tSummary.explizit_gesetzt +
                " mit Feld, " +
                (tSummary.peers - tSummary.explizit_gesetzt) +
                " stille " +
                tSummary.standard +
                " s"
            ),
            msgTimeout
              ? h(
                  "div",
                  { className: msgTimeout.indexOf("Fehler") === 0 ? "text-destructive" : "text-muted-foreground" },
                  msgTimeout
                )
              : null
          )
        : null,
      st.error ? h("div", { className: "text-xs text-destructive" }, "Fehler: " + st.error) : null,
      msg
        ? h(
            "div",
            { className: "text-xs " + (msg.indexOf("Fehler") === 0 ? "text-destructive" : "text-muted-foreground") },
            msg
          )
        : null,
      st.loading && !st.data ? h("div", { className: "text-xs text-muted-foreground" }, "laedt ...") : null,
      h(
        "div",
        { className: "grid gap-3", style: { gridTemplateColumns: "repeat(auto-fill, minmax(320px, 1fr))" } },
        profiles.map(function (p) {
          return h(ProfileCard, { key: p.profile, data: p, action: act, onToggle: toggleRecht, allProfiles: profiles });
        })
      ),
      st.data && st.data.root
        ? h("div", { className: "text-xs text-muted-foreground" }, "Wurzel: " + st.data.root)
        : null
    );
  }

  window.__HERMES_PLUGINS__.register(PLUGIN_ID, A2aPage);
})();
