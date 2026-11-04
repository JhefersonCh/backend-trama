# Fuentes de referencia del corpus RAG

Descarga: `uv run python scripts/fetch_sources.py` (UA `TRAMA-academic-rag/0.1`). Ingesta: `uv run python scripts/ingest_sources.py`.
Los `raw.*` NO se versionan (`.gitignore`); `meta.json` si. Fecha de consulta: 2026-11-03.

| id | Titulo | URL | Licencia | Uso |
|----|--------|-----|----------|-----|
| rfc1035 | RFC 1035 Domain Names | https://www.rfc-editor.org/rfc/rfc1035 | IETF Trust (copia permitida con aviso) | estandar; indexado |
| rfc2131 | RFC 2131 DHCP | https://www.rfc-editor.org/rfc/rfc2131 | IETF Trust | estandar; indexado |
| rfc9293 | RFC 9293 TCP | https://www.rfc-editor.org/rfc/rfc9293 | IETF Trust | estandar; indexado |
| rfc792 | RFC 792 ICMP | https://www.rfc-editor.org/rfc/rfc792 | IETF Trust | estandar; indexado |
| rfc8200 | RFC 8200 IPv6 | https://www.rfc-editor.org/rfc/rfc8200 | IETF Trust | estandar; indexado |
| rfc3927 | RFC 3927 IPv4 Link-Local (APIPA) | https://www.rfc-editor.org/rfc/rfc3927 | IETF Trust | estandar; indexado |
| nist_sp800_153 | NIST SP 800-153 WLAN security | https://doi.org/10.6028/NIST.SP.800-153 (PDF nvlpubs) | Dominio publico (gob. EE. UU.) | indexado |
| ms_wireless_en | Advanced troubleshooting wireless (EN) | https://learn.microsoft.com/en-us/troubleshoot/windows-client/networking/wireless-network-connectivity-issues-troubleshooting | CC BY 4.0 (repo MicrosoftDocs/SupportArticles-docs) | indexado |
| ms_wifi_es | Solucionar problemas de Wi-Fi (ES) | https://support.microsoft.com/es-es/windows/experience/connectivity-networking/fix-wi-fi-connection-issues-in-windows | Copyright Microsoft | solo uso academico local, no redistribuido |
| ubuntu_netplan / ubuntu_key_concepts / ubuntu_configuring_networks / ubuntu_about_dhcp | Ubuntu Server docs (networking) | https://ubuntu.com/server/docs/explanation/networking/ | CC BY-SA 3.0 segun Canonical (aviso no verificable en la pagina descargada) | indexado; atribuir a Canonical |
| cisco_devnet_iotod | Cisco DevNet IoT OD: Troubleshooting | https://developer.cisco.com/docs/iotod/troubleshooting-issues/ | Copyright Cisco | solo uso academico local, no redistribuido |
| cisco_tcpip, cisco_dhcp, cisco_ping_traceroute | Cisco: TCP/IP, DHCP, Ping & Traceroute | www.cisco.com/... (ver diseno) | Copyright Cisco | NO DESCARGADO: HTTP 403 (bloqueo anti-bot); alternativa web.archive.org -> 429 |

Notas: la pagina Ubuntu `/server/docs/about-netplan/` ahora vive en `/explanation/networking/about-netplan/`. El indice `how-to/networking/` es solo una lista de enlaces y se omitio.
