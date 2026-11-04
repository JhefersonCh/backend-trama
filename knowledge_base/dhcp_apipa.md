# DHCP y direcciones APIPA

## Qué hace DHCP

DHCP entrega a los equipos una dirección IP, la máscara, el gateway y los servidores DNS durante un tiempo limitado llamado concesión. El proceso resumido es: el cliente pide una oferta por difusión, el servidor ofrece, el cliente solicita y el servidor confirma. Descrito en RFC 2131.

## Qué es APIPA

Cuando un cliente no recibe respuesta de DHCP, puede asignarse a sí mismo una dirección link-local 169.254.x.x con máscara 255.255.0.0 (RFC 3927). Esa dirección solo permite hablar con equipos del mismo segmento, nunca llegar a Internet. Ver 169.254.x.x significa que falló DHCP, no que la red esté "configurada".

## Causas habituales

- Servidor DHCP caído o sin direcciones libres en el rango.
- Cliente en una VLAN o puerto que no llega al servidor, o relay DHCP ausente.
- Enlace inestable que corta la negociación.
- Interfaz del cliente configurada de forma estática por error.

## Comprobaciones seguras

Confirmar que la interfaz esté en modo automático, verificar el estado del enlace, comparar con otro equipo del mismo segmento y, si el usuario lo autoriza, solicitar de nuevo la concesión. Revisar registros y rangos del servidor solo con permisos del responsable.

## Qué hacer si su equipo tiene 169.254.x.x

Una dirección que empieza por 169.254 significa que el equipo no obtuvo una dirección del router o del servidor DHCP. No es un problema de DNS ni de proxy, así que no sirve vaciar la caché DNS ni cambiar el proxy. Los pasos, en este orden:

1. Compruebe si otros equipos (celular, otro computador) se conectan y navegan. Si todos fallan, el problema está en el router o en el servicio, no en este equipo.
2. Reinicie el router o módem: desconéctelo de la corriente, espere 30 segundos, vuelva a conectarlo y espere unos 2 minutos a que las luces se estabilicen.
3. Desconecte y vuelva a conectar el equipo a la red: apague y encienda el Wi-Fi, olvide la red y conéctese de nuevo, o desenchufe y vuelva a enchufar el cable de red.
4. Pida una dirección nueva. Abra el símbolo del sistema (Windows+R, escriba cmd, Enter) y ejecute `ipconfig /release`, que suelta la dirección actual, y después `ipconfig /renew`, que le pide una nueva al router. Luego `ipconfig` para ver si ya no empieza por 169.254.
5. Si sigue con 169.254.x.x, es probable que el router o el servidor DHCP no esté entregando direcciones. Escale el caso a quien administra la red o al proveedor, o registre el problema para que un técnico lo revise.

## Cuándo escalar

Si varios equipos tienen 169.254.x.x a la vez, o si hace falta tocar el servidor, el relay o la VLAN, escalar a quien administra la infraestructura.

## Fuentes

RFC 2131 - https://www.rfc-editor.org/rfc/rfc2131 ; RFC 3927 - https://www.rfc-editor.org/rfc/rfc3927
