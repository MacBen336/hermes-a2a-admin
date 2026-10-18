/**
 * hermes-a2a-admin — Desktop-Oberflaeche (Native Hermes Desktop App), Stufe 3.
 *
 * Gegenstueck zur Web-Haelfte (../../dashboard/dist/index.js); beide sprechen
 * denselben Backend-Namespace /api/plugins/hermes-a2a-admin/ (../../dashboard/plugin_api.py).
 *
 * Ablage: <App-Home>/desktop-plugins/hermes-a2a-admin/plugin.js
 *   -> Rein lokale Datei der App. Laeuft die App auf demselben Rechner wie der
 *      Gateway (hier: die Gast-VM), ist dieses Verzeichnis ~/.hermes/desktop-plugins/.
 *
 * Schreibende Aktionen fragen vorher nach und melden nur Metadaten — **keine Geheimniswerte**:
 *   /token/generate, /token/rotate, /map/sync   (Stufe 2)
 *   /config/auth                                (Stufe 3: auth-Referenz je Peer-Eintrag)
 *
 * Plain ESM, uncompiled: nur '@hermes/plugin-sdk', 'react', 'react/jsx-runtime'
 * sind importierbar, es gibt kein JSX.
 */

import { Button, PANES_AREA, Separator } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'hermes-a2a-admin'

// ctx wird in register() gesetzt; die Datenabrufe brauchen es.
let CTX = null

const TONE = {
	aktiv: { color: 'var(--ui-accent)', label: 'aktiv' },
	'neustart-vermutet': { color: 'var(--ui-warning, var(--ui-accent))', label: 'Neustart vermutet' },
	'neustart-noetig': { color: 'var(--ui-warning, var(--ui-accent))', label: 'Neustart nötig' },
	'ohne-identitaet': { color: 'var(--ui-text-tertiary)', label: 'ohne Identität' },
	stumm: { color: 'var(--ui-text-quaternary)', label: 'stumm' }
}

function fmtTime(iso) {
	if (!iso) return '—'
	try {
		return new Date(iso).toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' })
	} catch (err) {
		return iso
	}
}

function errorText(err) {
	if (!err) return 'unbekannter Fehler'
	return err.message ? err.message : String(err)
}

/** Antwort eines Schreibaufrufs in eine Zeile uebersetzen — ohne Werte. */
function describe(res) {
	if (!res) return 'keine Antwort'

	// Stufe 3: auth-Referenzen in den Peer-Eintraegen der config.yaml
	if (res.aktion === 'ausweise-eintragen') {
		const aus = res.ergebnisse || []
		const geschrieben = aus.filter(e => e.geschrieben)
		const komplett = aus.filter(e => !e.geschrieben && e.grund === 'schon vollstaendig')
		const abgelehnt = aus.filter(e => e.grund && e.grund !== 'schon vollstaendig')
		const teile = [
			`geschrieben: ${
				geschrieben.length ? geschrieben.map(e => `${e.profil} (+${e.auth_neu} von ${(e.peers || []).length})`).join(', ') : 'nichts'
			}`
		]
		if (komplett.length) teile.push(`schon vollstaendig: ${komplett.map(e => e.profil).join(', ')}`)
		if (abgelehnt.length) teile.push(`nicht angefasst: ${abgelehnt.map(e => `${e.profil} (${e.grund})`).join('; ')}`)
		if (geschrieben.length && geschrieben[0].backup) teile.push(`Sicherung: ${geschrieben[0].backup.split('/').pop()}`)
		return `Ausweise · ${teile.join(' · ')}`
	}

	// Rufrecht umschalten: Erlauben setzt Peer-Eintrag (Anrufer) UND Map-Eintrag (Angerufener),
	// Sperren nimmt nur die Erlaubnis weg. Wirksam erst nach einem Neustart.
	if (res.aktion === 'rufrecht-setzen') {
		const e = res.ergebnis || {}
		const teile = [`${e.caller}${e.darf ? ' darf ' : ' darf nicht mehr '}${e.callee}`]
		const getan = (res.schritte || []).filter(s => s.geaendert).map(s => s.schritt)
		if (getan.length) teile.push(getan.join(' · '))
		if (e.anrufer_jetzt) teile.push(`Anrufer jetzt: ${e.anrufer_jetzt.length ? e.anrufer_jetzt.join(', ') : 'keine'}`)
		if (res.achtung && res.achtung.length) teile.push(res.achtung[0])
		return `Rufrecht · ${teile.join(' · ')}`
	}

	if (res.ergebnisse) {
		const neu = (res.token_neu_erzeugt || []).join(', ') || 'keine'
		const geschrieben = res.ergebnisse.filter(e => e.geschrieben).map(e => e.profil)
		const fehler = res.ergebnisse.filter(e => e.grund)
		let text = `Maps abgeglichen · Token neu erzeugt für: ${neu} · geschrieben: ${geschrieben.join(', ') || 'nichts (war schon korrekt)'}`
		if (fehler.length) text += ` · nicht geschrieben: ${fehler.map(e => `${e.profil} (${e.grund})`).join('; ')}`
		return text
	}
	if (res.ok === false) return `${res.profil}: ${res.grund || 'nichts geändert'}`
	const teile = [`${res.profil} · ${res.aktion}`, `geändert: ${(res.geaendert || []).join(', ') || 'nichts'}`]
	if (res.backup) teile.push(`Sicherung: ${res.backup.split('/').pop()}`)
	if (res.achtung && res.achtung.length) teile.push(`Achtung: ${res.achtung.join(' ')}`)
	return teile.join(' · ')
}

/** Effektiver ausgehender Timeout eines Peers — "(Standard)" markiert die stillen 120 s. */
function timeoutText(peerByName, name) {
	const e = peerByName[name]
	if (!e) return ''
	return e.timeout_explicit ? ` · ${e.timeout} s` : ` · ${e.timeout_effective} s (Standard)`
}

/** Antwort von /timeout/set in eine Zeile uebersetzen — Zahlen statt Namen. */
function describeTimeout(res) {
	if (!res) return 'keine Antwort'
	const aus = res.ergebnisse || []
	const geschrieben = aus.filter(e => e.geschrieben)
	const ersetzt = geschrieben.reduce((n, e) => n + (e.ersetzt || 0), 0)
	const ergaenzt = geschrieben.reduce((n, e) => n + (e.ergaenzt || 0), 0)
	const skip = aus.filter(e => !e.geschrieben)
	const teile = [
		`${res.sekunden} s`,
		`geschrieben: ${geschrieben.length ? geschrieben.map(e => e.profil).join(', ') : 'nichts'}`,
		`Felder: ${ersetzt} ersetzt, ${ergaenzt} ergänzt`
	]
	if (skip.length) teile.push(`nicht angefasst: ${skip.map(e => `${e.profil} (${e.grund})`).join('; ')}`)
	if (geschrieben.length && geschrieben[0].backup) teile.push(`Sicherung: ${geschrieben[0].backup.split('/').pop()}`)
	// Teilausfall sichtbar machen: die Oberflaeche faerbt am "Fehler"-Praefix.
	return `${res.probleme ? 'Fehler: ' : ''}Timeout · ${teile.join(' · ')}`
}

function Row({ label, value }) {
	return jsxs('div', {
		style: { display: 'flex', gap: '8px', justifyContent: 'space-between', alignItems: 'baseline' },
		children: [
			jsx('span', { style: { color: 'var(--ui-text-secondary)', flexShrink: 0 }, children: label }),
			jsx('span', {
				style: { color: 'var(--ui-text-primary, inherit)', textAlign: 'right', wordBreak: 'break-word' },
				children: value
			})
		]
	})
}

function ProfileBlock({ data, action, onToggle, allProfiles }) {
	const tone = TONE[data.state] || { color: 'var(--ui-text-quaternary)', label: data.state }
	const id = data.identity || {}
	const card = data.card || {}
	const audit = data.audit || { tail: [] }
	const callers = id.caller_count ? `${id.caller_count} (${(id.callers || []).join(', ')})` : 'niemand'
	const ohneAuth = (data.peers || []).filter(p => !p.auth)
	const rechte = data.rufrechte || []
	// Gegenrichtung: darf ICH die anderen rufen? Die Erlaubnis steht in der Map des ANDEREN,
	// wird also aus dessen Karte gelesen — dort bin ich der Anrufer.
	const auswaerts = (allProfiles || [])
		.filter(x => x.profile !== data.profile)
		.map(other => {
			const zeile = ((other.rufrechte || []).filter(z => z.caller === data.profile)[0]) || {}
			return {
				name: other.profile,
				darf: !!zeile.darf,
				kann: zeile.kann !== false,
				token: zeile.caller_token !== false,
				block: zeile.caller_block !== false
			}
		})

	// Ausgehender Timeout steht in der EIGENEN config.yaml (a2a_agents.<peer>.timeout); fehlt der
	// Schluessel, gilt 120 — der Zusatz "(Standard)" zeigt das, statt den Wert zu verschweigen.
	const peerByName = {}
	for (const pe of data.peers || []) peerByName[pe.name] = pe

	const knoepfe = []
	if (action && !id.self_token) {
		knoepfe.push(
			jsx(
				Button,
				{
					variant: 'outline',
					onClick: () =>
						action(
							'/token/generate',
							{ profile: data.profile },
							`Token für "${data.profile}" erzeugen?\n\nGeschrieben wird A2A_SELF_TOKEN in ${data.home}/.env. Vorher legt das Plugin eine Sicherung an. Wirksam nach dem nächsten Neustart von Gateway/Dienst.`
						),
					children: 'Token erzeugen'
				},
				'gen'
			)
		)
	}
	if (action && id.self_token) {
		knoepfe.push(
			jsx(
				Button,
				{
					variant: 'outline',
					onClick: () =>
						action(
							'/token/rotate',
							{ profile: data.profile },
							`Token von "${data.profile}" ersetzen?\n\nDas alte Token wird ungültig — alle Maps, die es führen, müssen danach abgeglichen werden. Sicherung wird angelegt.`
						),
					children: 'Token rotieren'
				},
				'rot'
			)
		)
	}

	return jsxs('div', {
		style: { border: '1px solid var(--ui-stroke-secondary)', borderRadius: '6px', padding: '8px', display: 'grid', gap: '3px' },
		children: [
			jsxs('div', {
				style: { display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' },
				children: [
					jsx('strong', { children: data.profile }),
					jsx('span', { style: { color: tone.color, fontSize: '0.75rem' }, children: `● ${tone.label}` })
				]
			}),
			jsx(Row, { label: 'Port', value: `${data.port} (${data.port_source})` }),
			jsx(Row, { label: 'Adressbuch geschrieben', value: data.env_written || '—' }),
			jsx(Row, { label: 'Adapter gestartet', value: data.adapter_started || '—' }),
			jsx(Row, { label: 'Agent Card', value: card.ok ? `${card.name || 'ohne Namen'} · ${card.skills} Skills` : `keine — ${card.error || 'ohne Antwort'}` }),
			jsx(Row, { label: 'Auth beworben', value: card.security_schemes ? 'ja' : 'nein' }),
			jsx(Row, { label: 'Eigenes Token', value: id.self_token ? (id.self_token_format ? 'gesetzt' : 'gesetzt (Format ungewöhnlich)') : 'nicht gesetzt' }),
			jsx(Row, { label: 'Darf mich rufen', value: callers }),
			id.shared_bearer ? jsx(Row, { label: 'Geteiltes Bearer-Token', value: 'gesetzt — Identität wäre nur die IP' }) : null,
			jsx(Row, {
				label: 'Peers',
				value: data.peers && data.peers.length ? data.peers.map(p => p.name + (p.auth ? '' : ' (ohne auth)')).join(', ') : 'keine'
			}),
			ohneAuth.length
				? jsx('div', {
						style: { fontSize: '0.75rem', color: 'var(--ui-warning, var(--ui-accent))' },
						children: `Ohne auth: ${ohneAuth.map(p => p.name).join(', ')} — ausgehende Rufe dorthin scheitern mit 401.`
				  })
				: null,
			rechte.length
				? jsxs('div', {
						style: { display: 'grid', gap: '2px', marginTop: '2px' },
						children: [
							jsx('div', {
								style: { fontSize: '0.75rem', color: 'var(--ui-text-tertiary)' },
								children: 'Darf mich rufen'
							}),
							...rechte.map(r =>
								jsxs(
									'label',
									{
										style: { display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', cursor: 'pointer' },
										title: r.kann
											? `${r.caller} kennt meine Adresse (Peer-Eintrag vorhanden)`
											: `${r.caller} führt mich nicht als Peer — beim Erlauben wird der Eintrag ergänzt`,
										children: [
											jsx('input', {
												type: 'checkbox',
												checked: !!r.darf,
												onChange: () => {
													if (onToggle) onToggle(data.profile, r, !r.darf)
												}
											}),
											jsx('span', {
												style:
													r.kann || r.caller_block === false
														? { color: 'var(--ui-warning, var(--ui-accent))' }
														: {},
												children:
													r.caller +
													(r.kann ? '' : ' (kennt mich nicht)') +
													(r.caller_block === false ? ' (kein a2a_agents-Block — wird beim Erlauben angelegt)' : '') +
													(r.caller_token ? '' : ' (ohne Token — wird beim Erlauben erzeugt)')
											})
										]
									},
									r.caller
								)
							)
						]
				  })
				: null,
			auswaerts.length
				? jsxs('div', {
						style: { display: 'grid', gap: '2px', marginTop: '2px' },
						children: [
							jsx('div', {
								style: { fontSize: '0.75rem', color: 'var(--ui-text-tertiary)' },
								children: 'Darf ICH rufen — ich bin der Anrufer'
							}),
							...auswaerts.map(r =>
								jsxs(
									'label',
									{
										style: { display: 'flex', alignItems: 'center', gap: '6px', fontSize: '0.75rem', cursor: 'pointer' },
										title: `Die Erlaubnis steht in der Karte von ${r.name} (dort bin ich der Anrufer)`,
										children: [
											jsx('input', {
												type: 'checkbox',
												checked: r.darf,
												onChange: () => {
													if (onToggle) onToggle(r.name, { caller: data.profile, darf: r.darf }, !r.darf)
												}
											}),
											jsx('span', {
												style: r.kann && r.token && r.block ? {} : { color: 'var(--ui-warning, var(--ui-accent))' },
												children:
													r.name +
													(r.kann ? '' : ' (kennt mich nicht)') +
													(r.block ? '' : ' (kein a2a_agents-Block)') +
													(r.token ? '' : ' (ohne Token)') +
													timeoutText(peerByName, r.name)
											})
										]
									},
									r.name
								)
							)
						]
				  })
				: null,
			jsx(Row, { label: 'Audit', value: String((audit.entries || 0) + ' Einträge') }),
			audit.tail && audit.tail.length
				? jsx('div', {
						style: { fontSize: '0.75rem', color: 'var(--ui-text-tertiary)', display: 'grid', gap: '1px' },
						children: audit.tail
							.slice()
							.reverse()
							.map((e, i) => jsx('div', { children: `${fmtTime(e.zeit)} · ${e.richtung || '?'} · ${e.peer || '?'}` }, String(i)))
				  })
				: null,
			jsx('div', { style: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)' }, children: data.state_reason }),
			knoepfe.length ? jsx('div', { style: { display: 'flex', gap: '8px', marginTop: '4px' }, children: knoepfe }) : null
		]
	})
}

function A2aPane() {
	const [data, setData] = useState(null)
	const [error, setError] = useState(null)
	const [msg, setMsg] = useState(null)
	// Globaler Regler fuer den AUSGEHENDEN Peer-Timeout. `null` = noch nicht angefasst, dann zeigt
	// der Regler den Serverzustand; nach dem Setzen wird wieder auf null gestellt.
	const [timeoutWert, setTimeoutWert] = useState(null)
	const [timeoutMsg, setTimeoutMsg] = useState(null)

	function load() {
		if (!CTX) return
		CTX.rest('/state')
			.then(res => {
				setData(res)
				setError(null)
			})
			.catch(err => setError(errorText(err)))
	}

	function act(path, body, frage) {
		if (!CTX) return
		if (!window.confirm(frage)) return
		setMsg('läuft ...')
		CTX.rest(path, { method: 'POST', body: { confirm: true, ...body } })
			.then(res => {
				setMsg(describe(res))
				load()
			})
			.catch(err => setMsg(`Fehler: ${errorText(err)}`))
	}

	function syncAll() {
		act(
			'/map/sync',
			{},
			'Anrufer-Maps aller Profile abgleichen?\n\nAbgeleitet aus den a2a_agents-Listen (rollengetreu). Fehlende eigene Token werden dabei erzeugt; jede geänderte .env bekommt vorher eine Sicherung. Wirksam nach dem nächsten Neustart.'
		)
	}

	/** Globaler Regler: setzt den AUSGEHENDEN Peer-Timeout in allen Profilen auf denselben Wert. */
	function applyTimeout(seconds, anzahl) {
		if (!CTX) return
		const frage =
			`Ausgehenden A2A-Timeout auf ${seconds} s setzen?\n\n` +
			`Betrifft alle Profile und alle ${anzahl} Peer-Einträge (a2a_agents.<peer>.timeout). ` +
			'Jede geänderte config.yaml bekommt vorher eine Sicherung. Ausgehende Rufe lesen die Config ' +
			'pro Aufruf — kein Neustart nötig.'
		if (!window.confirm(frage)) return
		setTimeoutMsg('läuft ...')
		CTX.rest('/timeout/set', { method: 'POST', body: { confirm: true, seconds } })
			.then(res => {
				setTimeoutMsg(describeTimeout(res))
				setTimeoutWert(null)
				load()
			})
			.catch(err => setTimeoutMsg(`Fehler: ${errorText(err)}`))
	}

	/** Stufe 3: erst Vorschau holen (schreibt nichts), dann mit den Zahlen bestätigen lassen. */
	function authAll() {
		if (!CTX) return
		setMsg('Vorschau ...')
		CTX.rest('/config/auth')
			.then(plan => {
				const offen = (plan.ergebnisse || []).filter(e => e.schreibbar && e.auth_neu > 0)
				const liste = offen.map(e => `${e.profil} (+${e.auth_neu} von ${(e.peers || []).length})`)
				if (!liste.length) {
					setMsg(describe({ aktion: 'ausweise-eintragen', ergebnisse: plan.ergebnisse }))
					return
				}
				const frage =
					`Ausweise in die Peer-Einträge schreiben?\n\nBetroffen: ${liste.join(', ')}` +
					'\n\nGeschrieben wird die Referenz ${A2A_SELF_TOKEN}, kein Tokenwert. Jede geänderte config.yaml bekommt vorher eine Sicherung. Ausgehende Rufe wirken sofort (kein Neustart nötig).'
				if (!window.confirm(frage)) return
				setMsg('läuft ...')
				CTX.rest('/config/auth', { method: 'POST', body: { confirm: true } })
					.then(res => {
						setMsg(describe(res))
						load()
					})
					.catch(err => setMsg(`Fehler: ${errorText(err)}`))
			})
			.catch(err => setMsg(`Vorschau fehlgeschlagen: ${errorText(err)}`))
	}

	/** Rufrecht umschalten (Checkbox). Erlauben schreibt an zwei Stellen, Sperren nur an einer. */
	function toggleRecht(callee, r, allowed) {
		if (!CTX) return
		const frage =
			'Rufrecht ändern?\n\n' +
			(allowed
				? `${r.caller} DARF ${callee} künftig rufen. Dazu wird (falls nötig) ein Peer-Eintrag in ${r.caller}/config.yaml ergänzt und die Erlaubnis in ${callee}/.env eingetragen.`
				: `${r.caller} darf ${callee} NICHT mehr rufen. Es wird nur die Erlaubnis aus ${callee}/.env entfernt; der Peer-Eintrag in ${r.caller}/config.yaml bleibt bestehen (er gehört dem Anrufer).`) +
			'\n\nVorher wird gesichert. Wirksam nach dem nächsten Neustart von Gateway bzw. Dienst.'
		if (!window.confirm(frage)) return
		setMsg('läuft ...')
		CTX.rest('/peers/toggle', { method: 'POST', body: { confirm: true, callee, caller: r.caller, allowed } })
			.then(res => {
				setMsg(describe(res))
				load()
			})
			.catch(err => setMsg(`Fehler: ${errorText(err)}`))
	}

	useEffect(() => {
		load()
		const timer = setInterval(load, 10000)
		return () => clearInterval(timer)
	}, [])

	const profiles = (data && data.profiles) || []
	const tSummary = (data && data.timeout) || null
	const tBereich = (tSummary && tSummary.bereich) || [30, 600]
	// Angefasst schlaegt Serverzustand: sonst zeigt der Regler, was das Backend meldet (gemeinsam
	// oder der kleinste vorkommende Wert), und nach dem Setzen wieder den frischen Zustand.
	const tWert =
		timeoutWert != null
			? timeoutWert
			: (tSummary && tSummary.gemeinsam) || (tSummary && tSummary.werte && tSummary.werte[0]) || 180

	return jsxs('div', {
		style: { padding: '8px', display: 'grid', gap: '8px', overflow: 'auto' },
		children: [
			jsxs('div', {
				style: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '8px' },
				children: [
					jsx('strong', { children: 'A2A' }),
					jsxs('div', {
						style: { display: 'flex', gap: '8px' },
						children: [
							jsx(Button, { variant: 'outline', onClick: load, children: 'Neu laden' }),
							jsx(Button, { onClick: syncAll, children: 'Maps abgleichen' }),
							jsx(Button, { variant: 'outline', onClick: authAll, children: 'Ausweise eintragen' })
						]
					})
				]
			}),
			tSummary
				? jsxs('div', {
						style: { display: 'grid', gap: '4px', fontSize: '0.75rem' },
						children: [
							jsxs('div', {
								style: { display: 'flex', alignItems: 'center', gap: '8px' },
								children: [
									jsx('span', {
										style: { color: 'var(--ui-text-secondary)', flexShrink: 0 },
										children: 'A2A-Timeout (ausgehend)'
									}),
									jsx('input', {
										type: 'range',
										min: tBereich[0],
										max: tBereich[1],
										step: tSummary.schritt,
										value: tWert,
										onChange: e => setTimeoutWert(Number(e.target.value)),
										style: { flex: '1 1 auto' }
									}),
									jsx('span', {
										style: { fontVariantNumeric: 'tabular-nums', minWidth: '52px', textAlign: 'right' },
										children: `${tWert} s`
									}),
									jsx(Button, {
										variant: 'outline',
										onClick: () => applyTimeout(tWert, tSummary.peers),
										children: 'setzen'
									})
								]
							}),
							jsx('div', {
								style: { color: 'var(--ui-text-quaternary)' },
								children:
									(tSummary.gemeinsam
										? `${tSummary.peers} Peer-Einträge · einheitlich ${tSummary.gemeinsam} s`
										: `${tSummary.peers} Peer-Einträge · uneinheitlich (${tSummary.werte.join(', ')} s)`) +
									` · ${tSummary.explizit_gesetzt} mit Feld, ${tSummary.peers - tSummary.explizit_gesetzt} stille ${tSummary.standard} s`
							}),
							timeoutMsg
								? jsx('div', {
										style: {
											color: timeoutMsg.startsWith('Fehler')
												? 'var(--ui-warning, var(--ui-accent))'
												: 'var(--ui-text-tertiary)'
										},
										children: timeoutMsg
								  })
								: null
						]
				  })
				: null,
			jsx(Separator, {}),
			error ? jsx('div', { style: { color: 'var(--ui-text-secondary)', fontSize: '0.75rem' }, children: `Fehler: ${error}` }) : null,
			msg
				? jsx('div', {
						style: {
							color: msg.startsWith('Fehler') ? 'var(--ui-warning, var(--ui-accent))' : 'var(--ui-text-tertiary)',
							fontSize: '0.75rem'
						},
						children: msg
				  })
				: null,
			!data && !error ? jsx('div', { style: { fontSize: '0.75rem', color: 'var(--ui-text-quaternary)' }, children: 'lädt ...' }) : null,
			profiles.map(p => jsx(ProfileBlock, { data: p, action: act, onToggle: toggleRecht, allProfiles: profiles }, p.profile)),
			jsx('div', { style: { fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' }, children: 'Stufe 3 · Schreiben nur nach Bestätigung · 10 s' })
		]
	})
}

export default {
	id: ID,
	name: 'A2A-Verwaltung',
	register(ctx) {
		CTX = ctx
		ctx.register({
			id: 'pane',
			area: PANES_AREA,
			title: 'a2a',
			data: { placement: 'right', width: '400px' },
			render: () => jsx(A2aPane, {})
		})
	}
}
