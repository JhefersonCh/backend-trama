-- Rule set v1 (source: rules/diagnostico_red.v1.yaml), active. Idempotent.
INSERT INTO rule_sets (name, version, content_yaml, is_active)
SELECT 'diagnostico_red', 1, $yaml$version: 1
name: diagnostico_red

facts:                       # catálogo: el extractor SOLO puede producir estas keys/valores
  luz_modem:          {type: enum, values: [roja, naranja, apagada, verde, parpadea, desconocida]}
  luz_tras_reinicio:  {type: enum, values: [roja, normal, desconocida]}
  otros_equipos_navegan: {type: bool}
  reinicio_hecho:     {type: bool}
  ip_apipa:           {type: bool}        # 169.254.x.x
  navega_por_ip:      {type: bool}        # de browser_connectivity_check (fase 2)
  resuelve_nombres:   {type: bool}
  senal_wifi_pct:     {type: int}
  funciona_por_cable: {type: bool}
  clave_cambiada:     {type: bool}
  error_contrasena:   {type: bool}
  caida_reportada_zona: {type: bool}      # outage_check (fase 5) o tabla demo
  zona:               {type: string}
  modalidad:          {type: enum, values: [remoto, visita]}
  franja:             {type: enum, values: [manana, tarde, noche, cualquiera]}

hypotheses:
  H1_caida_proveedor:   {label: "Caída del proveedor o de la línea", category: connectivity, specialty: fibra}
  H2_solo_dispositivo:  {label: "Problema solo de este dispositivo", category: dhcp, specialty: redes}
  H3_dns:               {label: "Falla en la resolución de nombres (DNS)", category: dns, specialty: redes}
  H4_wifi:              {label: "Señal o canal Wi-Fi deficiente", category: wifi, specialty: wifi}
  H5_autenticacion:     {label: "Credenciales o seguridad del Wi-Fi", category: wifi, specialty: wifi}

rules:                       # encadenamiento hacia adelante: si 'when' se cumple → aplicar 'cf' a la hipótesis
  - {id: R1,  when: {otros_equipos_navegan: false},            then: {H1_caida_proveedor: +0.5, H2_solo_dispositivo: -0.8}}
  - {id: R2,  when: {otros_equipos_navegan: true},             then: {H2_solo_dispositivo: +0.6, H1_caida_proveedor: -0.8}}
  - {id: R3,  when: {luz_modem: [roja, naranja]},              then: {H1_caida_proveedor: +0.6}}
  - {id: R4,  when: {luz_tras_reinicio: roja},                 then: {H1_caida_proveedor: +0.3}}
  - {id: R5,  when: {ip_apipa: true},                          then: {H2_solo_dispositivo: +0.4}}
  - {id: R6,  when: {navega_por_ip: true, resuelve_nombres: false}, then: {H3_dns: +0.8}}
  - {id: R7,  when: {navega_por_ip: false},                    then: {H3_dns: -0.7}}
  - {id: R8,  when: {senal_wifi_pct: "<50"},                   then: {H4_wifi: +0.6}}
  - {id: R9,  when: {funciona_por_cable: true},                then: {H4_wifi: +0.5}}
  - {id: R10, when: {error_contrasena: true},                  then: {H5_autenticacion: +0.7}}
  - {id: R11, when: {clave_cambiada: false},                   then: {H5_autenticacion: -0.5}}
  - {id: R12, when: {caida_reportada_zona: true},              then: {H1_caida_proveedor: +0.7}}

actions:                     # catálogo de acciones con costo (para la persona) y ganancia por hipótesis
  ask_otros_equipos:   {kind: ask, fact: otros_equipos_navegan, cost: 1, gain: {H1_caida_proveedor: 0.8, H2_solo_dispositivo: 0.8}, autonomy: autonomo}
  ask_luz_modem:       {kind: ask, fact: luz_modem, cost: 1, gain: {H1_caida_proveedor: 0.7}, autonomy: autonomo}
  guide_reinicio:      {kind: guided_step, sets: reinicio_hecho, verify_with: ask_luz_tras_reinicio, cost: 4, gain: {H1_caida_proveedor: 0.5}, may_resolve: true, autonomy: autonomo}
  ask_luz_tras_reinicio: {kind: ask, fact: luz_tras_reinicio, cost: 1, gain: {H1_caida_proveedor: 0.6}, autonomy: autonomo}
  guide_release_renew: {kind: guided_step, requires: {ip_apipa: true}, cost: 4, gain: {H2_solo_dispositivo: 0.5}, may_resolve: true, autonomy: autonomo}
  ask_contrasena:      {kind: ask, fact: error_contrasena, cost: 1, gain: {H5_autenticacion: 0.7}, autonomy: autonomo}
  # fase 2: tool_browser_connectivity_check {kind: client_tool, cost: 0, gain: {H3_dns: 0.9, H1_caida_proveedor: 0.5}}

plans:                       # qué hacer cuando una hipótesis se ACEPTA (cf ≥ accept)
  H1_caida_proveedor: [explain: H1, escalate_ticket]       # escalar con asignación (K2)
  H2_solo_dispositivo: [guide_release_renew, verify, escalate_ticket_if_unresolved]
  H3_dns:              [guide_cambiar_dns, verify, escalate_ticket_if_unresolved]
  H4_wifi:             [guide_acercarse_o_5ghz, verify]
  H5_autenticacion:    [guide_reingresar_clave, verify]
$yaml$, NOT EXISTS (SELECT 1 FROM rule_sets WHERE name = 'diagnostico_red' AND is_active)
ON CONFLICT (name, version) DO NOTHING;
